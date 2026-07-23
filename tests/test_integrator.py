from __future__ import annotations

import dataclasses
import json
import sys
import unittest

from tests.support.fault_injector import FaultInjector, InjectedCrash
from tests.support.git_repo import GitRepositoryFixture
from tests.test_models import minimal_state
from vibe.config import frozen_config_bytes
from vibe.integrator import (
    IntegrationRecovery,
    IntegrationRejected,
    Integrator,
)
from vibe.models import (
    CommandAuthorization,
    CommandSpec,
    FrozenRunConfig,
    TaskContract,
)
from vibe.state_store import (
    StateStore,
    artifact_ref,
    canonical_json_bytes,
)
from vibe.verification import VerificationGate
from vibe.worktrees import SourceCommitMetadata, WorktreeManager


class IntegrationFixture(GitRepositoryFixture):
    def setUpIntegration(
        self,
        *,
        crash_at: str | None = None,
        verification_exit: int = 0,
        contract_scope: tuple[str, ...] = ("README.md",),
        conflicting_head: bool = False,
        merge_source: bool = False,
    ) -> None:
        self.setUpGitRepository()
        self.manager = WorktreeManager(self.target)
        self.baseline = self.manager.assert_clean_baseline()
        self.integration_ref = self.manager.create_run_ref(
            "RUN-20260723-001",
            self.baseline.base_sha,
        )
        source_contract = TaskContract(
            id="TASK-001",
            objective="Edit README",
            worker_type="implementation",
            covers=("AC-001",),
            depends_on=(),
            path_scope=("README.md",),
            exclusive_resources=(),
            acceptance_checks=(),
            max_attempts=3,
        )
        self.worker = self.manager.create_task_worktree(
            "RUN-20260723-001",
            "TASK-001",
            1,
            self.baseline.base_sha,
        )
        self.preflight = self.manager.capture_worker_preflight(
            self.worker,
            "TASK-001",
            "2026-07-23T10:00:00+00:00",
            self.manager.snapshot_protected_git(self.worker.branch),
        )
        (self.worker.path / "README.md").write_text(
            "worker change\n",
            encoding="utf-8",
        )
        self.prepared = self.manager.prepare_source_commit(
            source_contract,
            self.worker,
            self.preflight,
            SourceCommitMetadata(
                "RUN-20260723-001",
                "TASK-001",
                1,
                "2026-07-23T10:00:00+00:00",
            ),
        )
        self.manager.apply_source_commit_cas(self.worker, self.prepared)
        if merge_source:
            tree = self.git(
                "rev-parse",
                f"{self.prepared.candidate_commit}^{{tree}}",
            )
            other = self.git(
                "commit-tree",
                f"{self.baseline.base_sha}^{{tree}}",
                "-p",
                self.baseline.base_sha,
                "-m",
                "other parent",
            )
            merge = self.git(
                "commit-tree",
                tree,
                "-p",
                self.baseline.base_sha,
                "-p",
                other,
                "-m",
                "merge source",
            )
            self.git(
                "update-ref",
                self.worker.branch,
                merge,
                self.prepared.candidate_commit,
            )
            audit = dataclasses.replace(
                self.prepared.source_audit,
                source_head=merge,
                source_commits=(merge,),
            )
            source_body = json.loads(
                self.prepared.source_audit_body
            )
            source_body["candidate_commit"] = merge
            source_body["source_commits"] = [merge]
            self.prepared = dataclasses.replace(
                self.prepared,
                candidate_commit=merge,
                source_audit=audit,
                source_audit_body=canonical_json_bytes(source_body),
            )
        self.contract = TaskContract(
            id=source_contract.id,
            objective=source_contract.objective,
            worker_type=source_contract.worker_type,
            covers=source_contract.covers,
            depends_on=source_contract.depends_on,
            path_scope=contract_scope,
            exclusive_resources=source_contract.exclusive_resources,
            acceptance_checks=source_contract.acceptance_checks,
            max_attempts=source_contract.max_attempts,
        )
        integration_head = self.baseline.base_sha
        if conflicting_head:
            (self.target / "README.md").write_text(
                "integration change\n",
                encoding="utf-8",
            )
            self.git("add", "README.md")
            self.git("commit", "-m", "integration change")
            integration_head = self.git("rev-parse", "HEAD")
            self.git(
                "update-ref",
                self.integration_ref,
                integration_head,
                self.baseline.base_sha,
            )
        self.config = FrozenRunConfig(
            provider_name="codex-cli",
            max_workers=2,
            task_attempts=3,
            provider_retries=3,
            evidence_rounds=3,
            repair_rounds=3,
            max_plan_tasks=128,
            command_catalog=(
                CommandSpec(
                    "unit",
                    "Candidate verification",
                    (
                        sys.executable,
                        "-c",
                        f"raise SystemExit({verification_exit})",
                    ),
                ),
            ),
            required_command_ids=("unit",),
            command_authorization=CommandAuthorization(
                "EXPLICIT_PROJECT_FILE",
                "vibe.json",
                "sha256:" + "f" * 64,
            ),
        )
        self.store = StateStore.for_run(
            self.target,
            "RUN-20260723-001",
        )
        self._create_state(integration_head)
        self.fault = FaultInjector(crash_at)
        self.gate = VerificationGate(
            self.config,
            self.store,
            worktrees=self.manager,
        )
        self.integrator = Integrator(
            worktrees=self.manager,
            store=self.store,
            verification=self.gate,
            config=self.config,
            fault_hook=self.fault,
        )

    def _create_state(self, integration_head: str) -> None:
        creation_body = canonical_json_bytes(
            {"run_id": "RUN-20260723-001"}
        )
        config_body = frozen_config_bytes(self.config)
        task_body = canonical_json_bytes(
            {
                "id": self.contract.id,
                "path_scope": list(self.contract.path_scope),
            }
        )
        preflight_body = canonical_json_bytes(self.preflight.as_dict())
        result_body = canonical_json_bytes(
            {
                "schema_version": 1,
                "task_id": "TASK-001",
                "status": "COMPLETED",
            }
        )
        source_body = self.prepared.source_audit_body
        bodies = {
            "creation.intent.json": creation_body,
            "config.json": config_body,
            "tasks/TASK-001/task.json": task_body,
            "tasks/TASK-001/attempts/001/preflight.json": preflight_body,
            "tasks/TASK-001/attempts/001/result.json": result_body,
            "tasks/TASK-001/attempts/001/source-audit.json": source_body,
        }
        refs = {
            path: artifact_ref(path, body)
            for path, body in bodies.items()
        }
        state = minimal_state()
        state["repository"] = {
            "identity": self.manager.identity,
            "base_ref": self.baseline.base_ref,
            "base_sha": self.baseline.base_sha,
            "integration_ref": self.integration_ref,
            "integration_head": integration_head,
        }
        state["status"] = "EXECUTING"
        state["creation"]["intent"] = refs[
            "creation.intent.json"
        ].as_dict()
        state["config"] = refs["config.json"].as_dict()
        state["artifact_index"] = [
            reference.as_dict()
            for reference in refs.values()
        ]
        state["tasks"] = {
            "TASK-001": {
                "task": refs["tasks/TASK-001/task.json"].as_dict(),
                "status": "READY_TO_INTEGRATE",
                "attempt_no": 1,
                "failure_count": 0,
                "max_attempts": 3,
                "active_attempt": {
                    "attempt_token": "ATTEMPT-WORKER-1",
                    "status": "VERIFYING",
                    "created_at": "2026-07-23T10:00:00+00:00",
                    "task_base_sha": self.baseline.base_sha,
                    "branch": self.worker.branch,
                    "worktree": str(
                        self.worker.path.relative_to(self.manager.target)
                    ),
                    "preflight": refs[
                        "tasks/TASK-001/attempts/001/preflight.json"
                    ].as_dict(),
                    "provider_handle": None,
                    "result_path": (
                        "tasks/TASK-001/attempts/001/result.json"
                    ),
                },
                "attempts": [],
                "result": refs[
                    "tasks/TASK-001/attempts/001/result.json"
                ].as_dict(),
                "verification": None,
                "source_commits": [
                    self.prepared.candidate_commit
                ],
                "integrated_commits": [],
                "last_error": None,
            }
        }
        with self.store.lock():
            self.store.create(state, bodies)

    def integration_head(self) -> str:
        return self.git("rev-parse", self.integration_ref)


class IntegratorTests(IntegrationFixture, unittest.TestCase):
    def test_success_advances_ref_and_completes_task(self) -> None:
        self.setUpIntegration()
        original_branch = self.git("symbolic-ref", "--short", "HEAD")
        original_index = self.git("write-tree")

        with self.store.lock():
            result = self.integrator.integrate(
                self.contract,
                self.prepared.source_audit,
            )

        state = self.store.load()
        self.assertEqual(self.integration_head(), result.candidate_head)
        self.assertEqual(
            state["repository"]["integration_head"],
            result.candidate_head,
        )
        self.assertEqual(state["tasks"]["TASK-001"]["status"], "COMPLETED")
        self.assertIsNone(state["pending_integration"])
        self.assertEqual(
            self.git("symbolic-ref", "--short", "HEAD"),
            original_branch,
        )
        self.assertEqual(self.git("write-tree"), original_index)

    def test_failed_gate_and_scope_escape_leave_ref_unchanged(self) -> None:
        for scenario in ("gate", "scope"):
            with self.subTest(scenario=scenario):
                self.setUpIntegration(
                    verification_exit=1 if scenario == "gate" else 0,
                    contract_scope=(
                        ("README.md",)
                        if scenario == "gate"
                        else ("src/",)
                    ),
                )
                before = self.integration_head()
                with self.store.lock(), self.assertRaises(IntegrationRejected):
                    self.integrator.integrate(
                        self.contract,
                        self.prepared.source_audit,
                    )
                self.assertEqual(self.integration_head(), before)
                self.assertIsNone(
                    self.store.load()["pending_integration"]
                )

    def test_conflict_leaves_run_ref_unchanged(self) -> None:
        self.setUpIntegration(conflicting_head=True)
        before = self.integration_head()
        with self.store.lock(), self.assertRaisesRegex(
            IntegrationRejected,
            "conflicted",
        ):
            self.integrator.integrate(
                self.contract,
                self.prepared.source_audit,
            )
        self.assertEqual(self.integration_head(), before)

    def test_merge_source_commit_is_rejected_before_candidate(self) -> None:
        self.setUpIntegration(merge_source=True)
        before = self.integration_head()
        with self.store.lock(), self.assertRaisesRegex(
            IntegrationRejected,
            "non-merge",
        ):
            self.integrator.integrate(
                self.contract,
                self.prepared.source_audit,
            )
        self.assertEqual(self.integration_head(), before)

    def test_crash_after_pending_before_cas_retries_exact_candidate(self) -> None:
        self.setUpIntegration(
            crash_at="after_pending_integration_before_update_ref"
        )
        with self.store.lock(), self.assertRaises(InjectedCrash):
            self.integrator.integrate(
                self.contract,
                self.prepared.source_audit,
            )
        pending = self.store.load()["pending_integration"]
        self.assertEqual(self.integration_head(), pending["expected_head"])

        with self.store.lock():
            outcome = self.integrator.recover()
        self.assertEqual(outcome, IntegrationRecovery.RETRY_CAS)
        self.assertEqual(self.integration_head(), pending["candidate_head"])
        self.assertIsNone(self.store.load()["pending_integration"])

    def test_crash_after_cas_completes_state_without_reapplying(self) -> None:
        self.setUpIntegration(
            crash_at="after_update_ref_before_state_completion"
        )
        with self.store.lock(), self.assertRaises(InjectedCrash):
            self.integrator.integrate(
                self.contract,
                self.prepared.source_audit,
            )
        pending = self.store.load()["pending_integration"]
        self.assertEqual(self.integration_head(), pending["candidate_head"])

        with self.store.lock():
            outcome = self.integrator.recover()
        self.assertEqual(outcome, IntegrationRecovery.COMPLETE_STATE)
        self.assertEqual(
            self.store.load()["tasks"]["TASK-001"]["status"],
            "COMPLETED",
        )

    def test_external_ref_move_pauses_without_force_update(self) -> None:
        self.setUpIntegration(
            crash_at="after_pending_integration_before_update_ref"
        )
        with self.store.lock(), self.assertRaises(InjectedCrash):
            self.integrator.integrate(
                self.contract,
                self.prepared.source_audit,
            )
        pending = self.store.load()["pending_integration"]
        tree = self.git("rev-parse", f"{pending['expected_head']}^{{tree}}")
        external = self.git(
            "commit-tree",
            tree,
            "-p",
            pending["expected_head"],
            "-m",
            "external",
        )
        self.git(
            "update-ref",
            self.integration_ref,
            external,
            pending["expected_head"],
        )

        with self.store.lock():
            outcome = self.integrator.recover()
        state = self.store.load()
        self.assertEqual(outcome, IntegrationRecovery.PAUSE)
        self.assertEqual(state["status"], "PAUSED")
        self.assertEqual(state["last_error"]["code"], "INTEGRATION_REF_MOVED")
        self.assertEqual(self.integration_head(), external)

    def test_fault_hook_names_and_order_are_canonical(self) -> None:
        self.setUpIntegration()
        with self.store.lock():
            self.integrator.integrate(
                self.contract,
                self.prepared.source_audit,
            )
        self.assertEqual(
            self.fault.seen,
            [
                "after_candidate_verification_before_pending_integration",
                "after_pending_integration_before_update_ref",
                "after_update_ref_before_state_completion",
            ],
        )


if __name__ == "__main__":
    unittest.main()
