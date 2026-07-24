from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

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
from vibe.scheduler import path_matches_scope
from vibe.state_store import canonical_json_bytes


CASES = (
    (
        "frontend",
        "implementation",
        "Add an accessible loading state",
        "web/app.js",
    ),
    (
        "backend",
        "testing",
        "Add a regression test for stable API errors",
        "server/api.py",
    ),
    (
        "client",
        "implementation",
        "Recover pending work after an offline restart",
        "client/app.py",
    ),
    (
        "data",
        "code-quality",
        "Review and make the schema migration idempotent",
        "data/schema.sql",
    ),
    (
        "infrastructure",
        "documentation",
        "Document the health check and rollback configuration",
        "infra/service.yaml",
    ),
    (
        "general",
        "general",
        "Clarify the cross-cutting release behavior",
        "README.md",
    ),
)


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


@unittest.skipUnless(
    os.environ.get("VIBE_CODEX_CLI_PROMPT_EVAL") == "1",
    "set VIBE_CODEX_CLI_PROMPT_EVAL=1 to run prompt profile evals",
)
class CodexCLIPromptProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.adapter = CodexCLIAdapter()

    @staticmethod
    def git(repository: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def create_runners(
        self,
        repository: Path,
    ) -> tuple[PlannerRunner, WorkerRunner]:
        run_root = (
            Path(self.temporary.name) / f"{repository.name}-run"
        ).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        config = FrozenRunConfig(
            contract_profile="domain-v2",
            provider_name="codex-cli",
            max_workers=1,
            task_attempts=1,
            provider_retries=1,
            evidence_rounds=1,
            repair_rounds=1,
            max_plan_tasks=1,
            command_catalog=(
                CommandSpec(
                    id="syntax",
                    purpose="Run repository syntax checks",
                    argv=(
                        "python3",
                        "-m",
                        "compileall",
                        "-q",
                        ".",
                    ),
                    cwd=".",
                    timeout_seconds=120,
                    env_allowlist=(),
                ),
            ),
            required_command_ids=("syntax",),
            command_authorization=CommandAuthorization(
                mode="EXPLICIT_PROJECT_FILE",
                source_path="vibe.json",
                source_sha256="sha256:" + "f" * 64,
            ),
        )
        config_body = frozen_config_bytes(config)
        common = {
            "registry": PromptRegistry.default(),
            "provider": self.adapter,
            "target_root": repository.parent,
            "run_root": run_root,
            "expected_base": self.git(
                repository,
                "rev-parse",
                "HEAD",
            ),
            "config": config,
            "config_sha256": (
                "sha256:" + hashlib.sha256(config_body).hexdigest()
            ),
        }
        planner = PlannerRunner(
            **common,
            read_only_audit=_NoopAudit(),
        )
        return planner, WorkerRunner(**common)

    def create_repository(self, relative_path: str) -> Path:
        repository = (
            Path(self.temporary.name)
            / relative_path.replace("/", "-")
        ).resolve()
        repository.mkdir(parents=True)
        self.git(repository, "init", "-b", "main")
        self.git(repository, "config", "user.name", "Prompt Eval")
        self.git(
            repository,
            "config",
            "user.email",
            "prompt-eval@example.invalid",
        )
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("baseline\n", encoding="utf-8")
        self.git(repository, "add", relative_path)
        self.git(repository, "commit", "-m", "baseline")
        return repository

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
        self.assertEqual(status, ProviderStatus.SUCCEEDED)
        return self.adapter.result(handle).body

    def test_domain_prompt_profiles(self) -> None:
        for (
            expected_domain,
            expected_work_kind,
            goal,
            relative_path,
        ) in CASES:
            repository = self.create_repository(relative_path)
            planner, worker = self.create_runners(repository)
            with self.subTest(
                domain=expected_domain,
                work_kind=expected_work_kind,
            ):
                plan_invocation = planner.prepare(
                    run_id="RUN-20260724-001",
                    operation_id=f"PLAN-{expected_domain.upper()}",
                    attempt_no=1,
                    attempt_created_at="2026-07-24T00:00:00+00:00",
                    attempt_token=(
                        f"ATTEMPT-PLAN-{expected_domain.upper()}"
                    ),
                    worktree=repository,
                    context={
                        "goal": goal,
                        "repository": {"files": [relative_path]},
                    },
                    artifact_prefix=f"eval/{expected_domain}/planner",
                    timeout_seconds=180,
                )
                plan_body = self.execute_invocation(plan_invocation)
                plan = planner.parse_result(plan_body)
                self.assertEqual(len(plan.tasks), 1)
                task = plan.tasks[0]
                self.assertEqual(task.domain, expected_domain)
                self.assertEqual(task.work_kind, expected_work_kind)

                worker_invocation = worker.prepare(
                    run_id="RUN-20260724-001",
                    task=task,
                    operation_id=f"WORK-{expected_domain.upper()}",
                    attempt_no=1,
                    attempt_created_at="2026-07-24T00:00:00+00:00",
                    attempt_token=(
                        f"ATTEMPT-WORK-{expected_domain.upper()}"
                    ),
                    worktree=repository,
                    task_base_sha=self.git(
                        repository,
                        "rev-parse",
                        "HEAD",
                    ),
                    previous_failure=None,
                    artifact_prefix=f"eval/{expected_domain}/worker",
                    timeout_seconds=300,
                )
                result_body = self.execute_invocation(
                    worker_invocation
                )
                result = worker.parse_result(
                    worker_invocation,
                    result_body,
                )
                actual_paths = tuple(
                    filter(
                        None,
                        self.git(
                            repository,
                            "diff",
                            "--name-only",
                            "HEAD",
                        ).splitlines(),
                    )
                )
                self.assertTrue(
                    all(
                        any(
                            path_matches_scope(path, scope)
                            for scope in task.path_scope
                        )
                        for path in actual_paths
                    )
                )
                self.assertEqual(result.status, "COMPLETED")
                self.assertEqual(
                    set(result.changed_paths),
                    set(actual_paths),
                )
                self.assertTrue(
                    {
                        check.command_id
                        for check in result.checks
                    }.issubset(task.acceptance_checks)
                )


if __name__ == "__main__":
    unittest.main()
