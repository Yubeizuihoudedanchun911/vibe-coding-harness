from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from tests.support.fake_provider import ScriptedProvider
from tests.support.git_repo import GitRepositoryFixture
from tests.test_models import minimal_state
from vibe.models import (
    CommandAuthorization,
    CommandSpec,
    ContractError,
    FrozenRunConfig,
    ProviderStatus,
    StateConflictError,
    TaskContract,
)
from vibe.prompt_registry import PromptRegistry
from vibe.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
    ProviderHandle,
    ProviderRequest,
)
from vibe.runners import (
    DispatchLedger,
    bind_matching_handle,
    read_regular_bytes,
    role_attempt_prefix,
)
from vibe.runners.evaluator import EvaluatorRunner
from vibe.runners.planner import PlannerRunner
from vibe.runners.worker import WorkerRunner
from vibe.state_store import (
    StateStore,
    artifact_ref,
    canonical_json_bytes,
)
from vibe.worktrees import GitReadOnlyAudit, WorktreeManager


class _Audit:
    def __init__(self) -> None:
        self.events: list[str] = []

    def capture(self, worktree: Path) -> dict[str, object]:
        self.events.append("capture")
        return {"path": str(worktree), "revision": 1}

    def assert_unchanged(
        self,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        self.events.append("assert")
        if before != after:
            raise ContractError("read-only worktree changed")


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name).resolve()
        self.worktree = self.target / "worktree"
        self.worktree.mkdir()
        self.run_id = "RUN-20260723-001"
        self.store = StateStore.for_run(self.target, self.run_id)
        self.provider = ScriptedProvider()
        self.ledger = DispatchLedger(self.store, self.provider)
        self.registry = PromptRegistry.default()
        self.config = FrozenRunConfig(
            provider_name="codex-cli",
            max_workers=4,
            task_attempts=3,
            provider_retries=3,
            evidence_rounds=3,
            repair_rounds=3,
            max_plan_tasks=128,
            command_catalog=(
                CommandSpec(
                    id="unit",
                    purpose="Run unit tests",
                    argv=("python3", "-m", "unittest"),
                ),
            ),
            required_command_ids=("unit",),
            command_authorization=CommandAuthorization(
                mode="EXPLICIT_PROJECT_FILE",
                source_path="vibe.json",
                source_sha256="sha256:" + "f" * 64,
            ),
        )
        self.task = TaskContract(
            id="TASK-001",
            objective="Implement one bounded behavior",
            worker_type="implementation",
            covers=("AC-001",),
            depends_on=(),
            path_scope=("src/vibe/example.py",),
            exclusive_resources=("example",),
            acceptance_checks=("unit",),
            max_attempts=3,
        )
        self.audit = _Audit()
        common = {
            "registry": self.registry,
            "provider": self.provider,
            "target_root": self.target,
            "run_root": self.store.root,
            "expected_base": "a" * 40,
            "config": self.config,
            "config_sha256": "sha256:" + "e" * 64,
        }
        self.planner = PlannerRunner(
            **common,
            read_only_audit=self.audit,
        )
        self.worker = WorkerRunner(**common)
        self.evaluator = EvaluatorRunner(
            **common,
            read_only_audit=self.audit,
        )
        self._create_initial_state()

    def _create_initial_state(self) -> None:
        config = b'{"frozen":true}\n'
        intent = b'{"run_id":"RUN-20260723-001"}\n'
        task_body = b'{"id":"TASK-001"}\n'
        state = minimal_state()
        state["config"] = artifact_ref(
            "config.json",
            config,
        ).as_dict()
        state["creation"]["intent"] = artifact_ref(
            "creation.intent.json",
            intent,
        ).as_dict()
        task_ref = artifact_ref(
            "tasks/TASK-001/task.json",
            task_body,
        )
        state["tasks"] = {
            "TASK-001": {
                "status": "PENDING",
                "task": task_ref.as_dict(),
                "attempt_no": 0,
                "failure_count": 0,
                "max_attempts": 3,
                "active_attempt": None,
                "attempts": [],
                "result": None,
                "verification": None,
                "source_commits": [],
                "integrated_commits": [],
                "last_error": None,
            }
        }
        state["artifact_index"] = [
            state["creation"]["intent"],
            state["config"],
            task_ref.as_dict(),
        ]
        with self.store.lock():
            self.store.create(
                state,
                {
                    "config.json": config,
                    "creation.intent.json": intent,
                    "tasks/TASK-001/task.json": task_body,
                },
            )

    def planner_invocation(
        self,
        token: str = "ATTEMPT-PLANNER-1",
    ):
        return self.planner.prepare(
            run_id=self.run_id,
            operation_id="PLAN-INITIAL-uuid",
            attempt_no=1,
            attempt_created_at="2026-07-23T10:00:00.123456+00:00",
            attempt_token=token,
            worktree=self.worktree,
            context={"goal": "Create a finite plan"},
            artifact_prefix=role_attempt_prefix(
                "planner",
                "PLAN-INITIAL-uuid",
                1,
            ),
        )

    def _prime_planner(self, invocation) -> None:
        with self.store.lock():
            revision = self.store.load()["revision"]

            def mutate(state, refs) -> None:
                del refs
                runtime = state["role_runtime"]["planner"]
                runtime["operation_id"] = invocation.operation_id
                runtime["attempt_no"] = invocation.attempt_no
                runtime["active_attempt_token"] = (
                    invocation.attempt_token
                )

            self.store.transact(revision, {}, mutate)

    def worker_invocation(
        self,
        token: str = "ATTEMPT-WORKER-1",
        retry_no: int = 0,
    ):
        invocation = self.worker.prepare(
            run_id=self.run_id,
            task=self.task,
            operation_id="WORK-TASK-001-A1",
            attempt_no=1,
            attempt_created_at="2026-07-23T10:00:00.123456+00:00",
            attempt_token=token,
            worktree=self.worktree,
            task_base_sha="a" * 40,
            previous_failure=None,
            artifact_prefix="tasks/TASK-001/attempts/001",
            provider_retry_no=retry_no,
        )
        return invocation

    def _prime_worker(self, invocation) -> None:
        preflight_path = (
            f"{invocation.artifact_prefix}/preflight.json"
        )
        with self.store.lock():
            revision = self.store.load()["revision"]

            def mutate(state, refs) -> None:
                task = state["tasks"]["TASK-001"]
                task["status"] = "RUNNING"
                task["attempt_no"] = 1
                task["active_attempt"] = {
                    "attempt_token": invocation.attempt_token,
                    "status": "STARTING",
                    "created_at": invocation.attempt_created_at,
                    "task_base_sha": invocation.expected_base,
                    "branch": invocation.branch,
                    "worktree": invocation.worktree,
                    "preflight": refs[preflight_path].as_dict(),
                    "provider_handle": None,
                    "result_path": (
                        "tasks/TASK-001/attempts/001/result.json"
                    ),
                }

            self.store.transact(
                revision,
                {preflight_path: invocation.preflight_body},
                mutate,
            )

    def test_role_attempt_prefix_is_canonical_and_path_safe(self) -> None:
        self.assertEqual(
            role_attempt_prefix(
                "planner",
                "PLAN-INITIAL-uuid",
                2,
            ),
            (
                "roles/planner/operations/PLAN-INITIAL-uuid/"
                "attempts/002"
            ),
        )
        for operation_id in (
            "",
            ".",
            "..",
            "a/b",
            r"a\b",
            "two words",
            "lookalike／slash",
            "a" * 129,
        ):
            with self.subTest(operation_id=operation_id), self.assertRaises(
                ContractError
            ):
                role_attempt_prefix("planner", operation_id, 1)

    def test_dispatch_intent_is_committed_before_provider_start(
        self,
    ) -> None:
        invocation = self.planner_invocation()
        self._prime_planner(invocation)
        seen: list[bool] = []

        def start_after_intent(
            request: ProviderRequest,
        ) -> ProviderHandle:
            state = self.store.load()
            seen.append(
                request.attempt_token
                in state["pending_dispatches"]
            )
            return self.provider.start(request)

        with self.store.lock():
            handle = self.ledger.dispatch(
                invocation,
                start_after_intent,
            )
        self.assertEqual(seen, [True])
        pending = self.store.load()["pending_dispatches"][
            handle.attempt_token
        ]
        self.assertEqual(
            pending["provider_handle"]["attempt_token"],
            handle.attempt_token,
        )
        self.assertNotIn(
            "launch_path",
            pending["provider_handle"],
        )
        self.assertFalse(Path(pending["launch_path"]).is_absolute())

    def test_worker_handle_binding_atomically_starts_exact_active_attempt(
        self,
    ) -> None:
        invocation = self.worker_invocation()
        self._prime_worker(invocation)
        with self.store.lock():
            handle = self.ledger.dispatch(
                invocation,
                self.provider.start,
            )

        state = self.store.load()
        pending = state["pending_dispatches"][handle.attempt_token]
        active = state["tasks"]["TASK-001"]["active_attempt"]
        self.assertEqual(
            active["attempt_token"],
            handle.attempt_token,
        )
        self.assertEqual(active["status"], "RUNNING")
        self.assertEqual(
            active["provider_handle"],
            pending["provider_handle"],
        )
        self.assertEqual(active["preflight"], pending["preflight"])

        stale = dataclasses.replace(
            handle,
            attempt_token="ATTEMPT-STALE",
        )
        with self.assertRaisesRegex(
            StateConflictError,
            "superseded",
        ):
            bind_matching_handle(
                state,
                handle.attempt_token,
                stale,
                artifact_ref("launch.json", b"{}\n"),
            )

    def test_completion_is_bound_before_planner_parses_result(
        self,
    ) -> None:
        invocation = self.planner_invocation()
        self._prime_planner(invocation)
        with self.store.lock():
            handle = self.ledger.dispatch(
                invocation,
                self.provider.start,
            )
        body = {
            "schema_version": 1,
            "plan_version": 1,
            "summary": "One finite task",
            "acceptance_criteria": [
                {"id": "AC-001", "description": "Behavior works"}
            ],
            "global_verification": ["unit"],
            "tasks": [
                {
                    "id": "TASK-001",
                    "objective": "Implement behavior",
                    "worker_type": "implementation",
                    "covers": ["AC-001"],
                    "depends_on": [],
                    "path_scope": ["src/vibe/example.py"],
                    "exclusive_resources": ["example"],
                    "acceptance_checks": ["unit"],
                    "max_attempts": 3,
                }
            ],
        }
        Path(handle.result_path).write_bytes(
            canonical_json_bytes(body)
        )
        self.provider.complete(handle.attempt_token)
        with self.store.lock():
            committed = self.ledger.bind_completion(
                invocation,
                handle,
            )
        pending = committed["pending_dispatches"][
            handle.attempt_token
        ]
        self.assertIsNotNone(pending["exit"])
        self.assertIsNotNone(pending["result"])
        parsed = self.planner.parse_result(
            self.provider.result(handle).body
        )
        self.assertEqual(parsed.tasks[0].id, "TASK-001")

    def test_read_only_runner_audits_before_start_and_before_result(
        self,
    ) -> None:
        invocation = self.planner_invocation()
        self._prime_planner(invocation)
        with self.store.lock():
            handle = self.planner.start(
                invocation,
                self.ledger,
            )
        Path(handle.result_path).write_bytes(
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "plan_version": 1,
                    "summary": "One task",
                    "acceptance_criteria": [
                        {
                            "id": "AC-001",
                            "description": "works",
                        }
                    ],
                    "global_verification": ["unit"],
                    "tasks": [
                        {
                            "id": "TASK-001",
                            "objective": "Implement",
                            "worker_type": "implementation",
                            "covers": ["AC-001"],
                            "depends_on": [],
                            "path_scope": ["src/vibe/example.py"],
                            "exclusive_resources": ["example"],
                            "acceptance_checks": ["unit"],
                            "max_attempts": 3,
                        }
                    ],
                }
            )
        )
        self.provider.complete(handle.attempt_token)
        with self.store.lock():
            parsed = self.planner.result(
                invocation,
                handle,
                self.ledger,
            )
        self.assertEqual(parsed.plan_version, 1)
        self.assertEqual(
            self.audit.events,
            ["capture", "capture", "assert"],
        )

    def test_provider_artifact_reader_rejects_escape_and_symlinks(
        self,
    ) -> None:
        outside = self.target / "outside"
        outside.mkdir()
        secret = outside / "secret.json"
        secret.write_text("secret\n", encoding="utf-8")
        linked_parent = self.store.root / "linked"
        linked_parent.symlink_to(
            outside,
            target_is_directory=True,
        )
        linked_leaf = self.store.root / "linked-leaf.json"
        linked_leaf.symlink_to(secret)
        for path in (
            linked_parent / "secret.json",
            linked_leaf,
            outside / "secret.json",
        ):
            with self.subTest(path=path), self.assertRaises(
                ContractError
            ):
                read_regular_bytes(
                    path,
                    self.store.root,
                    max_bytes=1024,
                )

    def test_transient_worker_retry_rotates_both_handle_views_through_null(
        self,
    ) -> None:
        first = self.worker_invocation("ATTEMPT-WORKER-P1")
        self._prime_worker(first)
        with self.store.lock():
            first_handle = self.ledger.dispatch(
                first,
                self.provider.start,
            )
        self.provider.fail(
            first_handle.attempt_token,
            ProviderFailure(
                ProviderFailureKind.TRANSIENT,
                "rate limit exceeded",
            ),
        )
        with self.store.lock():
            self.ledger.bind_completion(first, first_handle)
            semantic_result_path = self.store.load()["tasks"][
                "TASK-001"
            ]["active_attempt"]["result_path"]
            second = self.worker_invocation(
                "ATTEMPT-WORKER-P2",
                retry_no=1,
            )
            observed: list[tuple[object, object, object]] = []

            def start_after_rotation(
                request: ProviderRequest,
            ) -> ProviderHandle:
                state = self.store.load()
                active = state["tasks"]["TASK-001"][
                    "active_attempt"
                ]
                pending = state["pending_dispatches"][
                    request.attempt_token
                ]
                observed.append(
                    (
                        active["attempt_token"],
                        active["provider_handle"],
                        pending["provider_handle"],
                    )
                )
                return self.provider.start(request)

            second_handle = self.ledger.retry_transient(
                first,
                first_handle,
                second,
                start_after_rotation,
            )

        self.assertEqual(
            observed,
            [("ATTEMPT-WORKER-P2", None, None)],
        )
        state = self.store.load()
        active = state["tasks"]["TASK-001"]["active_attempt"]
        pending = state["pending_dispatches"][
            "ATTEMPT-WORKER-P2"
        ]
        self.assertNotIn(
            "ATTEMPT-WORKER-P1",
            state["pending_dispatches"],
        )
        self.assertEqual(active["status"], "RUNNING")
        self.assertEqual(
            active["provider_handle"],
            second_handle.as_state_dict(),
        )
        self.assertEqual(
            active["provider_handle"],
            pending["provider_handle"],
        )
        self.assertEqual(
            active["result_path"],
            semantic_result_path,
        )
        self.assertNotEqual(
            active["result_path"],
            pending["result_path"],
        )

    def test_stale_attempt_token_is_not_allowed_to_write_artifacts(
        self,
    ) -> None:
        invocation = self.planner_invocation(
            token="ATTEMPT-CURRENT"
        )
        self._prime_planner(invocation)
        with self.store.lock():
            self.ledger.dispatch(invocation, self.provider.start)
        with self.store.lock():
            accepted = self.ledger.accept_current_result(
                attempt_token="ATTEMPT-STALE",
                artifacts={"stale/attempt.json": b"{}\n"},
                accept=lambda current, pending, refs: self.fail(
                    "stale close callback must not run"
                ),
            )
        self.assertFalse(accepted)
        self.assertIn(
            "ATTEMPT-CURRENT",
            self.store.load()["pending_dispatches"],
        )
        self.assertFalse(
            (self.store.root / "stale/attempt.json").exists()
        )

    def test_worker_result_identity_must_match_assignment(self) -> None:
        invocation = self.worker.prepare(
            run_id=self.run_id,
            task=self.task,
            operation_id="WORK-TASK-001-A2",
            attempt_no=2,
            attempt_created_at="2026-07-23T10:00:00.123456+00:00",
            attempt_token="ATTEMPT-WORKER-2",
            worktree=self.worktree,
            task_base_sha="a" * 40,
            previous_failure=None,
            artifact_prefix="tasks/TASK-001/attempts/002",
        )
        body = self.valid_worker_result(invocation)
        body["task_id"] = "TASK-999"
        with self.assertRaisesRegex(ContractError, "task identity"):
            self.worker.parse_result(
                invocation,
                canonical_json_bytes(body),
            )

    def valid_worker_result(self, invocation) -> dict[str, object]:
        return {
            "schema_version": 1,
            "task_id": invocation.task_id,
            "attempt_no": invocation.attempt_no,
            "attempt_token": invocation.attempt_token,
            "status": "COMPLETED",
            "task_base_sha": invocation.expected_base,
            "changed_paths": ["src/vibe/example.py"],
            "checks": [
                {
                    "command_id": "unit",
                    "exit_code": 0,
                    "summary": "passed",
                }
            ],
            "residual_risks": [],
            "blocker": None,
        }

    def test_evaluator_preserves_four_distinct_verdicts(self) -> None:
        for verdict in (
            "PASS",
            "NEEDS_REPAIR",
            "UNVERIFIED",
            "BLOCKED",
        ):
            body = self.valid_evaluation_result(verdict)
            parsed = self.evaluator.parse_result(
                canonical_json_bytes(body)
            )
            self.assertEqual(parsed.verdict.value, verdict)

    @staticmethod
    def valid_evaluation_result(
        verdict: str,
    ) -> dict[str, object]:
        criterion_verdict = {
            "PASS": "PASS",
            "NEEDS_REPAIR": "FAIL",
            "UNVERIFIED": "UNVERIFIED",
            "BLOCKED": "BLOCKED",
        }[verdict]
        findings = (
            []
            if verdict in {"PASS", "UNVERIFIED", "BLOCKED"}
            else [
                {
                    "criterion_id": "AC-001",
                    "severity": "HIGH",
                    "evidence": "test failed",
                    "affected_paths": ["src/vibe/example.py"],
                    "repair_hint": "fix behavior",
                }
            ]
        )
        return {
            "schema_version": 1,
            "verdict": verdict,
            "criteria": [
                {
                    "id": "AC-001",
                    "verdict": criterion_verdict,
                    "evidence_ids": (
                        ["verification:unit"]
                        if verdict == "PASS"
                        else []
                    ),
                }
            ],
            "findings": findings,
            "evidence_requests": (
                ["unit"] if verdict == "UNVERIFIED" else []
            ),
            "residual_risks": [],
        }


class GitReadOnlyRunnerAuditTests(
    GitRepositoryFixture,
    unittest.TestCase,
):
    def setUp(self) -> None:
        self.setUpGitRepository()
        self.audit = GitReadOnlyAudit(WorktreeManager(self.target))

    def test_planner_source_change_invalidates_otherwise_valid_plan(self) -> None:
        before = self.audit.capture(self.target)
        (self.target / "README.md").write_text(
            "changed\n",
            encoding="utf-8",
        )
        after = self.audit.capture(self.target)
        with self.assertRaisesRegex(
            ContractError,
            "read-only role changed",
        ):
            self.audit.assert_unchanged(before, after)

    def test_planner_ref_change_invalidates_otherwise_valid_plan(self) -> None:
        before = self.audit.capture(self.target)
        self.git("update-ref", "refs/heads/unauthorized", "HEAD")
        after = self.audit.capture(self.target)
        with self.assertRaisesRegex(
            ContractError,
            "read-only role changed",
        ):
            self.audit.assert_unchanged(before, after)


if __name__ == "__main__":
    unittest.main()
