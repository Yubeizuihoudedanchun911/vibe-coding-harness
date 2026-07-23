from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.support.fake_provider import ProviderScript, ScenarioProvider
from tests.test_controller_fake_provider import (
    _edit_worker,
    _pass_evaluation,
    _single_task_plan,
    _worker_result,
)
from vibe.config import frozen_config_bytes, load_run_config
from vibe.controller import Controller, ControllerDependencies
from vibe.integrator import Integrator
from vibe.migration.schema3 import (
    discover_requirements,
    migrate_schema3,
    validate_schema3_requirement,
)
from vibe.models import ContractError, StateConflictError
from vibe.prompt_registry import PromptRegistry
from vibe.runners.evaluator import EvaluatorRunner
from vibe.runners.planner import PlannerRunner
from vibe.runners.worker import WorkerRunner
from vibe.scheduler import Scheduler
from vibe.state_store import StateStore
from vibe.verification import VerificationGate
from vibe.worktrees import GitReadOnlyAudit, WorktreeManager


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "schema3"
MIGRATION_FAULT_POINTS = (
    "after_reservation",
    "after_staged_requirement",
    "after_prepared_manifest",
    "after_backup_install",
    "after_imported_run",
    "after_index_claim",
    "after_completed_manifest",
    "after_run_finalization",
)


def _git(target: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(target), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class Schema3MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.target = Path(self.temporary.name) / "repository"
        self.target.mkdir()
        _git(self.target, "init", "-b", "main")
        _git(self.target, "config", "user.name", "Migration Tests")
        _git(
            self.target,
            "config",
            "user.email",
            "migration@example.invalid",
        )
        (self.target / "README.md").write_text(
            "schema 4 baseline\n",
            encoding="utf-8",
        )
        _git(self.target, "add", "README.md")
        _git(self.target, "commit", "-m", "baseline")
        self.head = _git(self.target, "rev-parse", "HEAD")
        fixture = json.loads(
            (FIXTURES / "states.json").read_text(encoding="utf-8")
        )
        self.templates = fixture["states"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def requirement(
        self,
        requirement_id: str,
        status: str,
    ) -> Path:
        root = (
            self.target
            / ".vibe-coding"
            / "requirements"
            / requirement_id
        )
        (root / "rounds" / "001").mkdir(parents=True)
        state = copy.deepcopy(self.templates[status])
        state["requirement_id"] = requirement_id
        if state["accepted_revision"] == "BASE_SHA":
            state["accepted_revision"] = self.head
        interruption = (
            b'{"reason":"historical stop","schema_version":2}\n'
        )
        (root / "state.json").write_text(
            json.dumps(
                state,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "goal.md").write_text(
            state["goal"] + "\n",
            encoding="utf-8",
        )
        (root / "plan.md").write_text(
            "# Plan\n\n- AC-001: preserve history\n",
            encoding="utf-8",
        )
        (root / "rounds" / "001" / "evaluation.json").write_text(
            '{"schema_version":2,"verdict":"FAIL"}\n',
            encoding="utf-8",
        )
        (root / "rounds" / "001" / "interruption.json").write_bytes(
            interruption
        )
        (root / "rounds" / "001" / "review.md").write_text(
            "# Historical review\n",
            encoding="utf-8",
        )
        return root

    @staticmethod
    def tree_signature(root: Path) -> list[tuple[str, int, int, bytes]]:
        result: list[tuple[str, int, int, bytes]] = []
        for path in sorted(root.rglob("*")):
            metadata = path.lstat()
            relative = path.relative_to(root).as_posix()
            kind = stat.S_IFMT(metadata.st_mode)
            mode = stat.S_IMODE(metadata.st_mode)
            body = path.read_bytes() if stat.S_ISREG(metadata.st_mode) else b""
            result.append((relative, kind, mode, body))
        return result

    def run_directories(self) -> list[str]:
        root = self.target / ".vibe-coding" / "runs"
        return sorted(item.name for item in root.iterdir()) if root.exists() else []

    def backup_directories(self) -> list[str]:
        root = self.target / ".vibe-coding" / "schema3-backups"
        return sorted(item.name for item in root.iterdir()) if root.exists() else []

    def runtime_controller(
        self,
        run_id: str,
        *,
        fault_point: str | None = None,
    ) -> Controller:
        store = StateStore.for_run(self.target, run_id)
        config = load_run_config(
            self.target,
            {"allow_project_commands": True},
        )
        remaining_fault = [fault_point]

        def fault_hook(point: str) -> None:
            if remaining_fault[0] == point:
                remaining_fault[0] = None
                raise RuntimeError(point)

        provider = ScenarioProvider(
            [
                ProviderScript("planner", _single_task_plan()),
                ProviderScript(
                    "worker",
                    _worker_result,
                    on_start=_edit_worker,
                ),
                ProviderScript("evaluator", _pass_evaluation()),
            ]
        )
        worktrees = WorktreeManager(self.target)
        registry = PromptRegistry.default()
        common = {
            "registry": registry,
            "provider": provider,
            "target_root": self.target,
            "run_root": store.root,
            "expected_base": store.load()["repository"]["base_sha"],
            "config": config,
            "config_sha256": (
                "sha256:"
                + __import__("hashlib").sha256(
                    frozen_config_bytes(config)
                ).hexdigest()
            ),
        }
        verification = VerificationGate(
            config,
            store,
            worktrees=worktrees,
        )
        return Controller(
            self.target,
            config,
            ControllerDependencies(
                store_factory=lambda target, requested: (
                    store
                    if target.resolve() == self.target
                    and requested == run_id
                    else StateStore.for_run(target, requested)
                ),
                worktrees=worktrees,
                scheduler=Scheduler(),
                planner=PlannerRunner(
                    **common,
                    read_only_audit=GitReadOnlyAudit(worktrees),
                ),
                worker=WorkerRunner(**common),
                evaluator=EvaluatorRunner(
                    **common,
                    read_only_audit=GitReadOnlyAudit(worktrees),
                ),
                verification=verification,
                integrator=Integrator(
                    worktrees=worktrees,
                    store=store,
                    verification=verification,
                    config=config,
                    fault_hook=fault_hook,
                ),
                clock=Controller.utc_now,
                sleep=lambda seconds: time.sleep(min(seconds, 0.01)),
                fault_hook=fault_hook,
            ),
        )

    def test_four_status_mapping_and_exact_backup(self) -> None:
        for index, status in enumerate(
            ("ACCEPTED", "DEGRADED", "ACTIVE", "BLOCKED"),
            start=1,
        ):
            self.requirement(f"REQ-{index:03d}", status)

        results = migrate_schema3(
            self.target,
            requirement_id=None,
            migrate_all=True,
            base=self.head,
        )

        self.assertEqual(
            [item.requirement_id for item in results],
            ["REQ-001", "REQ-002", "REQ-003", "REQ-004"],
        )
        self.assertEqual(
            [item.mapped_status for item in results],
            [
                "IMPORTED_READ_ONLY",
                "IMPORTED_READ_ONLY",
                "PAUSED",
                "PAUSED",
            ],
        )
        for result in results:
            source = (
                self.target
                / ".vibe-coding"
                / "requirements"
                / result.requirement_id
            )
            backup = (
                self.target
                / ".vibe-coding"
                / "schema3-backups"
                / result.migration_id
                / result.requirement_id
            )
            self.assertEqual(
                self.tree_signature(source),
                self.tree_signature(backup),
            )
            state = StateStore.for_run(
                self.target,
                result.run_id,
            ).load()
            self.assertEqual(state["status"], result.mapped_status)
            self.assertEqual(state["plan_version"], 0)
            self.assertEqual(state["tasks"], {})
            self.assertIsNotNone(state["legacy_import"])
            if result.mapped_status == "PAUSED":
                self.assertEqual(
                    state["last_error"]["code"],
                    "SCHEMA3_REPLAN_REQUIRED",
                )
            else:
                self.assertIsNone(state["last_error"])

    def test_identical_replay_is_idempotent_and_conflicts_on_drift(self) -> None:
        source = self.requirement("REQ-001", "ACCEPTED")
        first = migrate_schema3(
            self.target,
            "REQ-001",
            False,
            self.head,
        )[0]
        second = migrate_schema3(
            self.target,
            "REQ-001",
            False,
            self.head,
        )[0]
        self.assertEqual(first, second)
        self.assertEqual(len(self.run_directories()), 1)

        (self.target / "other.txt").write_text("other\n", encoding="utf-8")
        _git(self.target, "add", "other.txt")
        _git(self.target, "commit", "-m", "other")
        other = _git(self.target, "rev-parse", "HEAD")
        with self.assertRaisesRegex(StateConflictError, "different base"):
            migrate_schema3(self.target, "REQ-001", False, other)

        (source / "goal.md").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(StateConflictError, "source changed"):
            migrate_schema3(self.target, "REQ-001", False, self.head)

    def test_batch_validation_failure_has_no_run_backup_or_claim(self) -> None:
        self.requirement("REQ-001", "ACCEPTED")
        broken = self.requirement("REQ-002", "BLOCKED")
        (broken / "state.json").write_text("{broken", encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "Schema 3 validation"):
            migrate_schema3(
                self.target,
                requirement_id=None,
                migrate_all=True,
                base=self.head,
            )

        self.assertEqual(self.run_directories(), [])
        self.assertEqual(self.backup_directories(), [])
        self.assertFalse(
            (self.target / ".vibe-coding" / "migrations").exists()
        )

    def test_strict_inputs_reject_symlink_duplicate_key_and_versions(self) -> None:
        mutations = ("symlink", "duplicate", "state-version", "record-version")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                requirement = self.requirement("REQ-001", "ACTIVE")
                if mutation == "symlink":
                    review = requirement / "rounds" / "001" / "review.md"
                    review.unlink()
                    review.symlink_to(self.target / "README.md")
                elif mutation == "duplicate":
                    (requirement / "state.json").write_text(
                        '{"schema_version":3,"schema_version":3}\n',
                        encoding="utf-8",
                    )
                elif mutation == "state-version":
                    state = json.loads(
                        (requirement / "state.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    state["schema_version"] = 2
                    (requirement / "state.json").write_text(
                        json.dumps(state),
                        encoding="utf-8",
                    )
                else:
                    (
                        requirement
                        / "rounds"
                        / "001"
                        / "evaluation.json"
                    ).write_text(
                        '{"schema_version":1}\n',
                        encoding="utf-8",
                    )
                with self.assertRaises(ContractError):
                    validate_schema3_requirement(
                        self.target,
                        "REQ-001",
                    )
                control = self.target / ".vibe-coding"
                requirements = control / "requirements"
                for child in list(requirements.iterdir()):
                    if child.name == "REQ-001":
                        for path in sorted(
                            child.rglob("*"),
                            reverse=True,
                        ):
                            if path.is_symlink() or path.is_file():
                                path.unlink()
                            else:
                                path.rmdir()
                        child.rmdir()

    def test_requirement_discovery_is_numeric(self) -> None:
        self.requirement("REQ-1000", "ACTIVE")
        self.requirement("REQ-999", "ACTIVE")
        self.assertEqual(
            discover_requirements(self.target),
            ("REQ-999", "REQ-1000"),
        )

    def test_dirty_product_bytes_are_not_used_as_the_base(self) -> None:
        self.requirement("REQ-001", "ACTIVE")
        (self.target / "dirty.txt").write_text(
            "uncommitted\n",
            encoding="utf-8",
        )
        result = migrate_schema3(
            self.target,
            "REQ-001",
            False,
            self.head,
        )[0]
        state = StateStore.for_run(self.target, result.run_id).load()
        self.assertEqual(state["repository"]["base_sha"], self.head)
        self.assertEqual(
            _git(
                self.target,
                "rev-parse",
                state["repository"]["integration_ref"],
            ),
            self.head,
        )

    def test_resume_replan_requires_clean_finalized_import(self) -> None:
        (self.target / "vibe.json").write_text(
            json.dumps(
                {
                    "verification": {
                        "command_catalog": [
                            {
                                "id": "unit",
                                "purpose": "Migration test gate",
                                "argv": [
                                    "python3",
                                    "-c",
                                    "print('ok')",
                                ],
                            }
                        ],
                        "required_command_ids": ["unit"],
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        _git(self.target, "add", "vibe.json")
        _git(self.target, "commit", "-m", "configure verification")
        self.head = _git(self.target, "rev-parse", "HEAD")
        self.requirement("REQ-001", "ACTIVE")
        entry = migrate_schema3(
            self.target,
            "REQ-001",
            False,
            self.head,
            allow_project_commands=True,
        )[0]
        controller = self.runtime_controller(entry.run_id)
        with self.assertRaisesRegex(
            StateConflictError,
            "explicit --replan",
        ):
            controller.resume(entry.run_id)

        (self.target / "dirty.txt").write_text(
            "uncommitted\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ContractError,
            "clean product baseline",
        ):
            controller.resume(entry.run_id, replan=True)
        (self.target / "dirty.txt").unlink()

        controller = self.runtime_controller(
            entry.run_id,
            fault_point="after_dispatch_intent_before_provider_start",
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "after_dispatch_intent_before_provider_start",
        ):
            controller.resume(entry.run_id, replan=True)
        replanning = StateStore.for_run(
            self.target,
            entry.run_id,
        ).load()
        self.assertEqual(replanning["status"], "PLANNING")
        self.assertEqual(replanning["plan_version"], 0)
        self.assertTrue(replanning["pending_dispatches"])
        self.assertIsNone(replanning["last_error"])

    def test_selector_is_explicit(self) -> None:
        self.requirement("REQ-001", "ACTIVE")
        for requirement_id, migrate_all in (
            (None, False),
            ("REQ-001", True),
        ):
            with self.subTest(
                requirement_id=requirement_id,
                migrate_all=migrate_all,
            ):
                with self.assertRaises(ContractError):
                    migrate_schema3(
                        self.target,
                        requirement_id,
                        migrate_all,
                        self.head,
                    )

    def test_each_crash_window_retries_to_one_exact_mapping(self) -> None:
        for index, point in enumerate(MIGRATION_FAULT_POINTS):
            if index:
                self.temporary.cleanup()
                self.setUp()
            with self.subTest(point=point):
                self.requirement("REQ-001", "ACCEPTED")
                self.requirement("REQ-002", "ACTIVE")
                remaining = [point]

                def fault_hook(actual: str) -> None:
                    if remaining[0] == actual:
                        remaining[0] = ""
                        raise RuntimeError(actual)

                with self.assertRaisesRegex(RuntimeError, point):
                    migrate_schema3(
                        self.target,
                        None,
                        True,
                        self.head,
                        fault_hook=fault_hook,
                    )
                results = migrate_schema3(
                    self.target,
                    None,
                    True,
                    self.head,
                )
                self.assertEqual(len(results), 2)
                self.assertEqual(len(self.run_directories()), 2)
                self.assertEqual(len(self.backup_directories()), 1)
                claims = (
                    self.target
                    / ".vibe-coding"
                    / "migrations"
                    / "index"
                )
                self.assertEqual(
                    sorted(path.name for path in claims.glob("*.json")),
                    ["REQ-001.json", "REQ-002.json"],
                )
                for entry in results:
                    self.assertEqual(
                        StateStore.for_run(
                            self.target,
                            entry.run_id,
                        ).load()["status"],
                        entry.mapped_status,
                    )

    def test_native_creation_skips_prepared_migration_reservation(self) -> None:
        self.requirement("REQ-001", "ACTIVE")

        def fault_hook(point: str) -> None:
            if point == "after_reservation":
                raise RuntimeError(point)

        with self.assertRaisesRegex(RuntimeError, "after_reservation"):
            migrate_schema3(
                self.target,
                "REQ-001",
                False,
                self.head,
                fault_hook=fault_hook,
            )
        config = load_run_config(self.target, {})
        worktrees = WorktreeManager(self.target)
        native = Controller(
            self.target,
            config,
            ControllerDependencies(
                store_factory=StateStore.for_run,
                worktrees=worktrees,
                scheduler=Scheduler(),
                planner=None,
                worker=None,
                evaluator=None,
                verification=None,
                integrator=None,
                clock=Controller.utc_now,
                sleep=time.sleep,
                fault_hook=lambda point: None,
            ),
        ).create_run("Native run", config)
        imported = migrate_schema3(
            self.target,
            "REQ-001",
            False,
            self.head,
        )[0]
        self.assertNotEqual(native, imported.run_id)


if __name__ == "__main__":
    unittest.main()
