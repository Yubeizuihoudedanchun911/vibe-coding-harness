from __future__ import annotations

import unittest

from vibe.models import (
    AcceptanceCriterion,
    CommandAuthorization,
    CommandSpec,
    ContractError,
    FrozenRunConfig,
    PlanDocument,
    TaskContract,
)
from vibe.scheduler import (
    Scheduler,
    effective_global_verification,
    normalize_scope,
    path_matches_scope,
)


class PlanValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = FrozenRunConfig(
            provider_name="codex-cli",
            max_workers=2,
            task_attempts=3,
            provider_retries=3,
            evidence_rounds=3,
            repair_rounds=3,
            max_plan_tasks=128,
            command_catalog=(
                CommandSpec("required", "Required gate", ("python3", "-V")),
                CommandSpec("unit", "Unit gate", ("python3", "-V")),
                CommandSpec("repair", "Repair gate", ("python3", "-V")),
            ),
            required_command_ids=("required",),
            command_authorization=CommandAuthorization(
                "EXPLICIT_PROJECT_FILE",
                "vibe.json",
                "sha256:" + "f" * 64,
            ),
        )
        self.scheduler = Scheduler()

    @staticmethod
    def task(
        task_id: str,
        *,
        objective: str = "Implement bounded behavior",
        worker_type: str = "implementation",
        covers: tuple[str, ...] = ("AC-001", "AC-002"),
        depends_on: tuple[str, ...] = (),
        path_scope: tuple[str, ...] = ("src/",),
        exclusive_resources: tuple[str, ...] = (),
        acceptance_checks: tuple[str, ...] = ("unit",),
        max_attempts: int = 3,
    ) -> TaskContract:
        return TaskContract(
            task_id,
            objective,
            worker_type,
            covers,
            depends_on,
            path_scope,
            exclusive_resources,
            acceptance_checks,
            max_attempts,
        )

    @staticmethod
    def plan(
        *,
        tasks: tuple[TaskContract, ...],
        plan_version: int = 1,
        ac_ids: tuple[str, ...] = ("AC-001", "AC-002"),
        global_verification: tuple[str, ...] = ("unit",),
    ) -> PlanDocument:
        return PlanDocument(
            schema_version=1,
            plan_version=plan_version,
            summary=f"Plan {plan_version}",
            acceptance_criteria=tuple(
                AcceptanceCriterion(ac_id, f"Criterion {ac_id}")
                for ac_id in ac_ids
            ),
            global_verification=global_verification,
            tasks=tasks,
        )

    def test_valid_plan_has_stable_topological_order_and_full_coverage(self) -> None:
        plan = self.plan(
            tasks=(
                self.task(
                    "TASK-002",
                    depends_on=("TASK-001",),
                    covers=("AC-002",),
                    path_scope=("tests/",),
                ),
                self.task("TASK-001", covers=("AC-001",)),
            )
        )
        self.scheduler.validate_plan(plan, self.config, ())
        self.assertEqual(
            self.scheduler.topological_order(plan),
            ("TASK-001", "TASK-002"),
        )

    def test_cycle_missing_dependency_duplicate_id_and_uncovered_ac_are_rejected(
        self,
    ) -> None:
        invalid = (
            self.plan(
                tasks=(
                    self.task(
                        "TASK-001",
                        depends_on=("TASK-001",),
                    ),
                )
            ),
            self.plan(
                tasks=(
                    self.task(
                        "TASK-001",
                        depends_on=("TASK-999",),
                    ),
                )
            ),
            self.plan(
                tasks=(self.task("TASK-001"), self.task("TASK-001"))
            ),
            self.plan(
                tasks=(self.task("TASK-001", covers=("AC-001",)),),
                ac_ids=("AC-001", "AC-002"),
            ),
        )
        for plan in invalid:
            with self.subTest(plan=plan), self.assertRaises(ContractError):
                self.scheduler.validate_plan(plan, self.config, ())

    def test_scope_worker_type_attempt_and_resource_limits_fail_closed(self) -> None:
        invalid_tasks = (
            self.task("TASK-001", path_scope=("../escape",)),
            self.task("TASK-001", path_scope=("/absolute",)),
            self.task("TASK-001", path_scope=(".vibe-coding/state.json",)),
            self.task("TASK-001", path_scope=(".git/config",)),
            self.task("TASK-001", worker_type="security"),
            self.task("TASK-001", max_attempts=4),
            self.task("TASK-001", exclusive_resources=("bad resource",)),
        )
        for task in invalid_tasks:
            with self.subTest(task=task), self.assertRaises(ContractError):
                self.scheduler.validate_plan(
                    self.plan(tasks=(task,)),
                    self.config,
                    (),
                )

    def test_repair_increments_version_and_cannot_rewrite_prior_tasks(self) -> None:
        original = self.plan(
            tasks=(self.task("TASK-001"),),
            plan_version=1,
        )
        valid_repair = self.plan(
            tasks=(
                self.task(
                    "TASK-002",
                    depends_on=("TASK-001",),
                    acceptance_checks=("repair",),
                ),
            ),
            plan_version=2,
            global_verification=(),
        )
        self.scheduler.validate_plan(valid_repair, self.config, (original,))
        self.assertEqual(
            effective_global_verification(
                self.config,
                (original, valid_repair),
            ),
            ("required", "unit"),
        )

        rewritten = self.plan(
            tasks=(
                self.task(
                    "TASK-001",
                    objective="rewrite history",
                ),
            ),
            plan_version=2,
        )
        with self.assertRaisesRegex(ContractError, "completed or prior task"):
            self.scheduler.validate_plan(rewritten, self.config, (original,))

    def test_repair_cannot_change_acceptance_contract_or_skip_version(self) -> None:
        original = self.plan(tasks=(self.task("TASK-001"),))
        changed = PlanDocument(
            schema_version=1,
            plan_version=2,
            summary="changed",
            acceptance_criteria=(
                AcceptanceCriterion("AC-001", "rewritten"),
                AcceptanceCriterion("AC-002", "Criterion AC-002"),
            ),
            global_verification=(),
            tasks=(self.task("TASK-002"),),
        )
        with self.assertRaisesRegex(ContractError, "acceptance criteria"):
            self.scheduler.validate_plan(changed, self.config, (original,))
        skipped = self.plan(tasks=(self.task("TASK-002"),), plan_version=3)
        with self.assertRaisesRegex(ContractError, "plan_version"):
            self.scheduler.validate_plan(skipped, self.config, (original,))

    def test_global_task_limit_counts_prior_versions(self) -> None:
        prior = self.plan(
            tasks=tuple(
                self.task(f"TASK-{index:03d}")
                for index in range(1, 101)
            )
        )
        candidate = self.plan(
            tasks=tuple(
                self.task(f"TASK-{index:03d}")
                for index in range(101, 130)
            ),
            plan_version=2,
        )
        with self.assertRaisesRegex(ContractError, "task limit"):
            self.scheduler.validate_plan(candidate, self.config, (prior,))

    def test_unknown_duplicate_or_malicious_command_ids_are_rejected(self) -> None:
        for checks in (
            ("unit", "unit"),
            ("python-c-delete",),
            ("curl",),
        ):
            task = self.task("TASK-001", acceptance_checks=checks)
            with self.subTest(checks=checks), self.assertRaises(ContractError):
                self.scheduler.validate_plan(
                    self.plan(tasks=(task,)),
                    self.config,
                    (),
                )

    def test_scope_normalization_and_matching_are_exact(self) -> None:
        self.assertEqual(normalize_scope("."), ".")
        self.assertEqual(normalize_scope("src/api/"), "src/api/")
        self.assertTrue(path_matches_scope("src/api/app.py", "src/api/"))
        self.assertFalse(path_matches_scope("src/apis.py", "src/api/"))
        self.assertTrue(path_matches_scope("README.md", "README.md"))


if __name__ == "__main__":
    unittest.main()
