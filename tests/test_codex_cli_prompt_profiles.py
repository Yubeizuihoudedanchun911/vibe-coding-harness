from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from vibe.config import frozen_config_bytes
from vibe.models import (
    CommandAuthorization,
    CommandSpec,
    FrozenRunConfig,
    ProviderStatus,
)
from vibe.prompt_registry import PromptRegistry
from vibe.providers.codex_cli import CodexCLIAdapter
from vibe.runners import RoleInvocation, provider_request_for
from vibe.runners.planner import PlannerRunner
from vibe.runners.worker import WorkerRunner
from vibe.state_store import (
    StateStore,
    canonical_json_bytes,
)
from vibe.verification import VerificationGate, VerificationResult


@dataclass(frozen=True)
class PromptEvalCase:
    name: str
    domain: str
    work_kind: str
    goal: str
    relative_path: str
    broken_content: str
    fixed_content: str
    verification_source: str

    @property
    def command_id(self) -> str:
        return "oracle"

    @property
    def maximum_score(self) -> int:
        return 4

    def command(self) -> CommandSpec:
        return CommandSpec(
            id=self.command_id,
            purpose=f"Run the deterministic {self.name} semantic oracle",
            argv=(
                sys.executable,
                "-c",
                self.verification_source,
            ),
            cwd=".",
            timeout_seconds=30,
            env_allowlist=(),
        )


PROMPT_EVAL_CASES = (
    PromptEvalCase(
        name="frontend-loading",
        domain="frontend",
        work_kind="implementation",
        goal=(
            "Make renderLoading return an accessible disabled loading "
            "button with aria-busy=true and the visible label Saving…."
        ),
        relative_path="web/app.js",
        broken_content=(
            "function renderLoading() {\n"
            "  return '<button>Save</button>';\n"
            "}\n"
        ),
        fixed_content=(
            "function renderLoading() {\n"
            "  return '<button aria-busy=\"true\" "
            "disabled>Saving…</button>';\n"
            "}\n"
        ),
        verification_source=(
            "from pathlib import Path\n"
            "text = Path('web/app.js').read_text(encoding='utf-8')\n"
            "assert 'aria-busy=\"true\"' in text\n"
            "assert 'disabled' in text\n"
            "assert 'Saving…' in text\n"
            "assert '<button>Save</button>' not in text\n"
        ),
    ),
    PromptEvalCase(
        name="backend-regression",
        domain="backend",
        work_kind="testing",
        goal=(
            "Correct the executable regression test so empty input keeps "
            "the stable API error INVALID_INPUT."
        ),
        relative_path="server/test_api.py",
        broken_content=(
            "import unittest\n\n"
            "def stable_error(value):\n"
            "    return 'INVALID_INPUT' if not value else 'OK'\n\n"
            "class StableErrorTests(unittest.TestCase):\n"
            "    def test_empty_input(self):\n"
            "        self.assertEqual(stable_error(''), 'ERROR')\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        ),
        fixed_content=(
            "import unittest\n\n"
            "def stable_error(value):\n"
            "    return 'INVALID_INPUT' if not value else 'OK'\n\n"
            "class StableErrorTests(unittest.TestCase):\n"
            "    def test_empty_input(self):\n"
            "        self.assertEqual(\n"
            "            stable_error(''), 'INVALID_INPUT'\n"
            "        )\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        ),
        verification_source=(
            "from pathlib import Path\n"
            "import subprocess, sys\n"
            "path = Path('server/test_api.py')\n"
            "text = path.read_text(encoding='utf-8')\n"
            "assert 'unittest.TestCase' in text\n"
            "assert \"stable_error(''), 'INVALID_INPUT'\" in text\n"
            "result = subprocess.run([sys.executable, str(path)], "
            "capture_output=True, text=True)\n"
            "assert result.returncode == 0, result.stderr\n"
            "assert \"stable_error(''), 'ERROR'\" not in text\n"
        ),
    ),
    PromptEvalCase(
        name="client-offline-resume",
        domain="client",
        work_kind="implementation",
        goal=(
            "Implement resume_pending so pending work survives an offline "
            "restart and remains stable online and when empty."
        ),
        relative_path="client/app.py",
        broken_content=(
            "def resume_pending(online, pending):\n"
            "    return []\n"
        ),
        fixed_content=(
            "def resume_pending(online, pending):\n"
            "    del online\n"
            "    return list(pending)\n"
        ),
        verification_source=(
            "import runpy\n"
            "ns = runpy.run_path('client/app.py')\n"
            "resume = ns['resume_pending']\n"
            "assert resume(False, ['A']) == ['A']\n"
            "assert resume(True, ['A']) == ['A']\n"
            "assert resume(False, []) == []\n"
            "assert resume(True, []) == []\n"
        ),
    ),
    PromptEvalCase(
        name="data-idempotent-migration",
        domain="data",
        work_kind="code-quality",
        goal=(
            "Make the accounts risk-column migration idempotent without "
            "adding destructive statements."
        ),
        relative_path="data/schema.sql",
        broken_content=(
            "ALTER TABLE accounts ADD COLUMN risk TEXT;\n"
        ),
        fixed_content=(
            "ALTER TABLE accounts "
            "ADD COLUMN IF NOT EXISTS risk TEXT;\n"
        ),
        verification_source=(
            "from pathlib import Path\n"
            "text = Path('data/schema.sql').read_text("
            "encoding='utf-8').strip()\n"
            "upper = text.upper()\n"
            "assert 'ADD COLUMN IF NOT EXISTS RISK TEXT' in upper\n"
            "assert upper.count('ALTER TABLE ACCOUNTS') == 1\n"
            "assert 'DROP ' not in upper\n"
            "assert text.endswith(';')\n"
        ),
    ),
    PromptEvalCase(
        name="infrastructure-runbook",
        domain="infrastructure",
        work_kind="documentation",
        goal=(
            "Document the exact /healthz health check and the "
            "deploy --revision previous rollback command."
        ),
        relative_path="infra/service.yaml",
        broken_content=(
            "health_check: TODO\n"
            "rollback: TODO\n"
        ),
        fixed_content=(
            "health_check: /healthz\n"
            "rollback: deploy --revision previous\n"
        ),
        verification_source=(
            "from pathlib import Path\n"
            "lines = Path('infra/service.yaml').read_text("
            "encoding='utf-8').splitlines()\n"
            "assert 'health_check: /healthz' in lines\n"
            "assert 'rollback: deploy --revision previous' in lines\n"
            "assert all('TODO' not in line for line in lines)\n"
            "assert len(lines) == 2\n"
        ),
    ),
    PromptEvalCase(
        name="general-release-contract",
        domain="general",
        work_kind="general",
        goal=(
            "Replace the release TODO with the cross-cutting contract that "
            "failed checks block release and successful checks permit it."
        ),
        relative_path="README.md",
        broken_content=(
            "# Release contract\n\n"
            "Release behavior: TODO\n"
        ),
        fixed_content=(
            "# Release contract\n\n"
            "Failed checks block release.\n"
            "Successful checks permit release.\n"
        ),
        verification_source=(
            "from pathlib import Path\n"
            "text = Path('README.md').read_text("
            "encoding='utf-8').lower()\n"
            "assert '# release contract' in text\n"
            "assert 'failed checks block release' in text\n"
            "assert 'successful checks permit release' in text\n"
            "assert 'todo' not in text\n"
        ),
    ),
)


@dataclass(frozen=True)
class OfflineOracleEvidence:
    broken_score: int
    broken_verification_passed: bool
    fixed_score: int
    fixed_verification_passed: bool
    artifact_digests_verified: bool


@dataclass(frozen=True)
class ProfileOutcome:
    profile: str
    semantic_score: int
    maximum_score: int
    verification_passed: bool


class _NoopAudit:
    def capture(self, worktree: Path) -> dict[str, object]:
        return {"worktree": str(worktree)}

    def assert_unchanged(
        self,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        if before != after:
            raise AssertionError("read-only prompt eval changed repository")


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _project_config_body(case: PromptEvalCase) -> bytes:
    return canonical_json_bytes(
        {
            "verification": {
                "command_catalog": [case.command().as_dict()],
                "required_command_ids": [case.command_id],
            }
        }
    )


def _config_for_case(
    case: PromptEvalCase,
    profile: str,
) -> FrozenRunConfig:
    source = _project_config_body(case)
    return FrozenRunConfig(
        contract_profile=profile,
        provider_name="codex-cli",
        max_workers=1,
        task_attempts=1,
        provider_retries=1,
        evidence_rounds=1,
        repair_rounds=1,
        max_plan_tasks=1,
        command_catalog=(case.command(),),
        required_command_ids=(case.command_id,),
        command_authorization=CommandAuthorization(
            mode="EXPLICIT_PROJECT_FILE",
            source_path="vibe.json",
            source_sha256=(
                "sha256:" + hashlib.sha256(source).hexdigest()
            ),
        ),
    )


def _create_case_repository(
    root: Path,
    case: PromptEvalCase,
    label: str,
) -> Path:
    repository = (root / f"{case.name}-{label}").resolve()
    repository.mkdir(parents=True)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Prompt Eval")
    _git(
        repository,
        "config",
        "user.email",
        "prompt-eval@example.invalid",
    )
    path = repository / case.relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(case.broken_content, encoding="utf-8")
    (repository / "vibe.json").write_bytes(
        _project_config_body(case)
    )
    (repository / ".gitignore").write_text(
        ".vibe-coding/\n__pycache__/\n*.py[cod]\n*$py.class\n",
        encoding="utf-8",
    )
    _git(
        repository,
        "add",
        ".gitignore",
        "vibe.json",
        case.relative_path,
    )
    _git(repository, "commit", "-m", "broken baseline")
    return repository


def _frontend_checks(repository: Path) -> tuple[bool, ...]:
    text = (repository / "web/app.js").read_text(encoding="utf-8")
    return (
        'aria-busy="true"' in text,
        "disabled" in text,
        "Saving…" in text,
        "<button>Save</button>" not in text,
    )


def _backend_checks(repository: Path) -> tuple[bool, ...]:
    path = repository / "server/test_api.py"
    text = path.read_text(encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(path)],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    return (
        "unittest.TestCase" in text,
        "stable_error(''), 'INVALID_INPUT'" in text,
        result.returncode == 0,
        "stable_error(''), 'ERROR'" not in text,
    )


def _client_checks(repository: Path) -> tuple[bool, ...]:
    namespace = runpy.run_path(str(repository / "client/app.py"))
    resume = namespace["resume_pending"]
    return (
        resume(False, ["A"]) == ["A"],
        resume(True, ["A"]) == ["A"],
        resume(False, []) == [],
        resume(True, []) == [],
    )


def _data_checks(repository: Path) -> tuple[bool, ...]:
    text = (
        repository / "data/schema.sql"
    ).read_text(encoding="utf-8").strip()
    upper = text.upper()
    return (
        "ADD COLUMN IF NOT EXISTS RISK TEXT" in upper,
        upper.count("ALTER TABLE ACCOUNTS") == 1,
        "DROP " not in upper,
        text.endswith(";"),
    )


def _infrastructure_checks(repository: Path) -> tuple[bool, ...]:
    lines = (
        repository / "infra/service.yaml"
    ).read_text(encoding="utf-8").splitlines()
    return (
        "health_check: /healthz" in lines,
        "rollback: deploy --revision previous" in lines,
        all("TODO" not in line for line in lines),
        len(lines) == 2,
    )


def _general_checks(repository: Path) -> tuple[bool, ...]:
    text = (
        repository / "README.md"
    ).read_text(encoding="utf-8").lower()
    return (
        "# release contract" in text,
        "failed checks block release" in text,
        "successful checks permit release" in text,
        "todo" not in text,
    )


_SEMANTIC_ORACLES = {
    "frontend-loading": _frontend_checks,
    "backend-regression": _backend_checks,
    "client-offline-resume": _client_checks,
    "data-idempotent-migration": _data_checks,
    "infrastructure-runbook": _infrastructure_checks,
    "general-release-contract": _general_checks,
}


def _semantic_score(
    case: PromptEvalCase,
    repository: Path,
) -> int:
    checks = _SEMANTIC_ORACLES[case.name](repository)
    if len(checks) != case.maximum_score:
        raise AssertionError("semantic oracle score contract changed")
    return sum(checks)


def _run_verification(
    case: PromptEvalCase,
    repository: Path,
    config: FrozenRunConfig,
    store: StateStore,
) -> VerificationResult:
    gate = VerificationGate(config, store)
    operation_id = f"VERIFY-{uuid.uuid4()}"
    with store.lock():
        return gate.run(
            _git(repository, "rev-parse", "HEAD"),
            repository,
            (case.command_id,),
            (
                "verification/tasks/TASK-001-a1/"
                f"{operation_id}"
            ),
        )


def _artifact_digest_matches(
    store: StateStore,
    path: str,
    expected_sha256: str,
) -> bool:
    body = (store.root / path).read_bytes()
    return expected_sha256 == (
        "sha256:" + hashlib.sha256(body).hexdigest()
    )


def _verification_evidence_complete(
    case: PromptEvalCase,
    store: StateStore,
    result: VerificationResult,
    *,
    expected_passed: bool,
) -> bool:
    if result.passed is not expected_passed:
        return False
    if len(result.commands) != 1:
        return False
    command = result.commands[0]
    if (
        command.command_id != case.command_id
        or command.timed_out
        or (command.exit_code == 0) is not expected_passed
    ):
        return False
    references = (
        result.manifest_ref,
        command.stdout,
        command.stderr,
    )
    if not all(
        _artifact_digest_matches(
            store,
            reference.path,
            reference.sha256,
        )
        for reference in references
    ):
        return False
    manifest = json.loads(
        (store.root / result.manifest_ref.path).read_bytes()
    )
    return bool(
        manifest["commit_sha"] == result.commit_sha
        and manifest["passed"] is expected_passed
        and len(manifest["commands"]) == 1
        and manifest["commands"][0]["command_id"]
        == case.command_id
    )


def exercise_offline_oracle(
    case: PromptEvalCase,
) -> OfflineOracleEvidence:
    with tempfile.TemporaryDirectory() as temporary:
        repository = _create_case_repository(
            Path(temporary),
            case,
            "offline",
        )
        config = _config_for_case(case, "domain-v2")
        store = StateStore.for_run(
            repository,
            "RUN-20260724-001",
        )
        broken_score = _semantic_score(case, repository)
        broken = _run_verification(
            case,
            repository,
            config,
            store,
        )
        broken_evidence = _verification_evidence_complete(
            case,
            store,
            broken,
            expected_passed=False,
        )

        (repository / case.relative_path).write_text(
            case.fixed_content,
            encoding="utf-8",
        )
        _git(repository, "add", case.relative_path)
        _git(repository, "commit", "-m", "known-good fixture")
        fixed_score = _semantic_score(case, repository)
        fixed = _run_verification(
            case,
            repository,
            config,
            store,
        )
        fixed_evidence = _verification_evidence_complete(
            case,
            store,
            fixed,
            expected_passed=True,
        )
        return OfflineOracleEvidence(
            broken_score=broken_score,
            broken_verification_passed=broken.passed,
            fixed_score=fixed_score,
            fixed_verification_passed=fixed.passed,
            artifact_digests_verified=(
                broken_evidence and fixed_evidence
            ),
        )


class PromptEvalOracleFixtureTests(unittest.TestCase):
    def test_six_case_oracles_reject_broken_and_accept_fixed_fixtures(
        self,
    ) -> None:
        self.assertEqual(len(PROMPT_EVAL_CASES), 6)
        self.assertEqual(
            len({case.verification_source for case in PROMPT_EVAL_CASES}),
            6,
        )
        for case in PROMPT_EVAL_CASES:
            with self.subTest(case=case.name):
                evidence = exercise_offline_oracle(case)
                self.assertLess(
                    evidence.broken_score,
                    case.maximum_score,
                )
                self.assertFalse(evidence.broken_verification_passed)
                self.assertEqual(
                    evidence.fixed_score,
                    case.maximum_score,
                )
                self.assertTrue(evidence.fixed_verification_passed)
                self.assertTrue(evidence.artifact_digests_verified)


@unittest.skipUnless(
    os.environ.get("VIBE_CODEX_CLI_PROMPT_EVAL") == "1",
    "set VIBE_CODEX_CLI_PROMPT_EVAL=1 to run prompt profile evals",
)
class CodexCLIPromptProfileTests(unittest.TestCase):
    """Opt-in semantic comparison; ProviderResult has no usage/cost fields."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.adapter = CodexCLIAdapter()

    def create_runners(
        self,
        repository: Path,
        case: PromptEvalCase,
        profile: str,
    ) -> tuple[
        PlannerRunner,
        WorkerRunner,
        FrozenRunConfig,
        StateStore,
    ]:
        config = _config_for_case(case, profile)
        store = StateStore.for_run(
            repository,
            "RUN-20260724-001",
        )
        config_body = frozen_config_bytes(config)
        common = {
            "registry": PromptRegistry.default(),
            "provider": self.adapter,
            "target_root": repository.parent,
            "run_root": store.root,
            "expected_base": _git(
                repository,
                "rev-parse",
                "HEAD",
            ),
            "config": config,
            "config_sha256": (
                "sha256:"
                + hashlib.sha256(config_body).hexdigest()
            ),
        }
        planner = PlannerRunner(
            **common,
            read_only_audit=_NoopAudit(),
        )
        return planner, WorkerRunner(**common), config, store

    def execute_invocation(self, invocation: RoleInvocation) -> bytes:
        run_root = Path(invocation.run_root)
        prompt_relative = f"{invocation.artifact_prefix}/prompt.md"
        schema_relative = (
            f"{invocation.artifact_prefix}/output.schema.json"
        )
        request_relative = (
            f"{invocation.provider_prefix}/request.json"
        )
        prompt_path = run_root / prompt_relative
        schema_path = run_root / schema_relative
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        schema_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_bytes(invocation.prompt_body)
        schema_path.write_bytes(invocation.schema_body)
        request = provider_request_for(
            invocation,
            prompt_relative,
            schema_relative,
            request_relative,
        )
        Path(request.request_path).parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        Path(request.request_path).write_bytes(
            canonical_json_bytes(request.as_dict())
        )
        handle = self.adapter.start(request)
        status = self.adapter.wait(
            handle,
            timeout_seconds=invocation.timeout_seconds,
        )
        if status is not ProviderStatus.SUCCEEDED:
            stderr = Path(handle.stderr_path).read_text(
                encoding="utf-8",
                errors="replace",
            )
            self.fail(
                f"Provider status={status.value}; stderr={stderr}"
            )
        return self.adapter.result(handle).body

    def run_profile(
        self,
        case: PromptEvalCase,
        profile: str,
    ) -> ProfileOutcome:
        repository = _create_case_repository(
            Path(self.temporary.name),
            case,
            profile,
        )
        planner, worker, config, store = self.create_runners(
            repository,
            case,
            profile,
        )
        broken_score = _semantic_score(case, repository)
        self.assertLess(broken_score, case.maximum_score)
        broken_verification = _run_verification(
            case,
            repository,
            config,
            store,
        )
        self.assertTrue(
            _verification_evidence_complete(
                case,
                store,
                broken_verification,
                expected_passed=False,
            )
        )

        routing = (
            {"worker_type": case.work_kind}
            if profile == "legacy-v1"
            else {
                "work_kind": case.work_kind,
                "domain": case.domain,
            }
        )
        plan_invocation = planner.prepare(
            run_id="RUN-20260724-001",
            operation_id=(
                f"PLAN-{case.domain.upper()}-"
                f"{profile.upper()}"
            ),
            attempt_no=1,
            attempt_created_at="2026-07-24T00:00:00+00:00",
            attempt_token=(
                f"ATTEMPT-PLAN-{case.domain.upper()}-"
                f"{profile.upper()}"
            ),
            worktree=repository,
            context={
                "goal": (
                    f"{case.goal} Plan exactly one task limited to "
                    f"{case.relative_path} and use only the "
                    f"{case.command_id} acceptance check."
                ),
                "repository": {
                    "files": [
                        case.relative_path,
                        "vibe.json",
                    ],
                    "required_path_scope": [
                        case.relative_path
                    ],
                    "required_routing": routing,
                    "required_acceptance_checks": [
                        case.command_id
                    ],
                },
            },
            artifact_prefix=(
                f"eval/{case.name}/{profile}/planner"
            ),
            timeout_seconds=180,
        )
        plan_body = self.execute_invocation(plan_invocation)
        plan = planner.parse_result(plan_body)
        self.assertEqual(len(plan.tasks), 1)
        task = plan.tasks[0]
        self.assertEqual(task.work_kind, case.work_kind)
        self.assertEqual(
            task.domain,
            "general" if profile == "legacy-v1" else case.domain,
        )
        self.assertEqual(
            task.path_scope,
            (str(PurePosixPath(case.relative_path)),),
        )
        self.assertEqual(
            task.acceptance_checks,
            (case.command_id,),
        )

        worker_invocation = worker.prepare(
            run_id="RUN-20260724-001",
            task=task,
            operation_id=(
                f"WORK-{case.domain.upper()}-"
                f"{profile.upper()}"
            ),
            attempt_no=1,
            attempt_created_at="2026-07-24T00:00:00+00:00",
            attempt_token=(
                f"ATTEMPT-WORK-{case.domain.upper()}-"
                f"{profile.upper()}"
            ),
            worktree=repository,
            task_base_sha=_git(
                repository,
                "rev-parse",
                "HEAD",
            ),
            previous_failure=None,
            artifact_prefix=(
                f"eval/{case.name}/{profile}/worker"
            ),
            timeout_seconds=300,
        )
        result_body = self.execute_invocation(worker_invocation)
        result = worker.parse_result(
            worker_invocation,
            result_body,
        )
        tracked_paths = _git(
            repository,
            "diff",
            "--name-only",
            "HEAD",
        ).splitlines()
        untracked_paths = _git(
            repository,
            "ls-files",
            "--others",
            "--exclude-standard",
        ).splitlines()
        actual_paths = tuple(
            sorted(
                {
                    str(PurePosixPath(path))
                    for path in (*tracked_paths, *untracked_paths)
                    if path
                }
            )
        )
        expected_paths = (
            str(PurePosixPath(case.relative_path)),
        )
        self.assertEqual(actual_paths, expected_paths)
        self.assertEqual(result.status, "COMPLETED")
        self.assertIsNone(result.blocker)
        self.assertEqual(
            tuple(sorted(result.changed_paths)),
            expected_paths,
        )
        self.assertEqual(
            tuple(check.command_id for check in result.checks),
            (case.command_id,),
        )
        self.assertTrue(
            all(check.exit_code == 0 for check in result.checks)
        )

        semantic_score = _semantic_score(case, repository)
        self.assertEqual(
            semantic_score,
            case.maximum_score,
        )
        _git(repository, "add", case.relative_path)
        _git(
            repository,
            "commit",
            "-m",
            f"{profile} candidate",
        )
        verification = _run_verification(
            case,
            repository,
            config,
            store,
        )
        self.assertTrue(
            _verification_evidence_complete(
                case,
                store,
                verification,
                expected_passed=True,
            )
        )
        return ProfileOutcome(
            profile=profile,
            semantic_score=semantic_score,
            maximum_score=case.maximum_score,
            verification_passed=verification.passed,
        )

    def test_domain_v2_quality_is_not_below_legacy_v1(self) -> None:
        for case in PROMPT_EVAL_CASES:
            with self.subTest(case=case.name):
                legacy = self.run_profile(case, "legacy-v1")
                domain = self.run_profile(case, "domain-v2")
                self.assertTrue(legacy.verification_passed)
                self.assertTrue(domain.verification_passed)
                self.assertEqual(
                    legacy.semantic_score,
                    legacy.maximum_score,
                )
                self.assertEqual(
                    domain.semantic_score,
                    domain.maximum_score,
                )
                self.assertGreaterEqual(
                    domain.semantic_score,
                    legacy.semantic_score,
                )


if __name__ == "__main__":
    unittest.main()
