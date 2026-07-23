from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vibe.models import (
    AcceptanceCriterion,
    ArtifactRef,
    PlanDocument,
    TaskContract,
)
from vibe.scheduler import (
    Scheduler,
    bind_attempt_preflight,
    close_attempt,
    new_task_state,
    resources_overlap,
    scopes_overlap,
    start_attempt,
)
from vibe.worktrees import TaskWorktree


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = Scheduler()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    @staticmethod
    def task(
        task_id: str,
        *,
        depends_on: tuple[str, ...] = (),
        path_scope: tuple[str, ...] = ("src/",),
        exclusive_resources: tuple[str, ...] = (),
    ) -> TaskContract:
        return TaskContract(
            id=task_id,
            objective=f"Implement {task_id}",
            worker_type="implementation",
            covers=("AC-001",),
            depends_on=depends_on,
            path_scope=path_scope,
            exclusive_resources=exclusive_resources,
            acceptance_checks=(),
            max_attempts=3,
        )

    @staticmethod
    def plan(*tasks: TaskContract) -> PlanDocument:
        return PlanDocument(
            schema_version=1,
            plan_version=1,
            summary="Parallel tasks",
            acceptance_criteria=(
                AcceptanceCriterion("AC-001", "Behavior works"),
            ),
            global_verification=(),
            tasks=tuple(tasks),
        )

    @staticmethod
    def state(*tasks: TaskContract) -> dict[str, object]:
        return {
            "status": "EXECUTING",
            "max_workers": 2,
            "tasks": {
                task.id: new_task_state(task)
                for task in tasks
            },
        }

    def test_independent_tasks_fill_worker_slots_in_topological_order(self) -> None:
        tasks = (
            self.task("TASK-001", path_scope=("src/a/",)),
            self.task("TASK-002", path_scope=("src/b/",)),
        )
        state = self.state(*tasks)
        plan = self.plan(*tasks)

        self.assertEqual(
            self.scheduler.promote_ready(state, plan),
            ["TASK-001", "TASK-002"],
        )
        self.assertEqual(
            self.scheduler.dispatchable(state, plan),
            ["TASK-001", "TASK-002"],
        )

    def test_dependency_must_be_integrated_before_ready(self) -> None:
        first = self.task("TASK-001", path_scope=("src/a/",))
        second = self.task(
            "TASK-002",
            depends_on=("TASK-001",),
            path_scope=("src/b/",),
        )
        state = self.state(first, second)
        plan = self.plan(first, second)

        self.scheduler.promote_ready(state, plan)
        self.assertEqual(self.scheduler.dispatchable(state, plan), ["TASK-001"])
        state["tasks"]["TASK-001"]["status"] = "COMPLETED"
        self.scheduler.promote_ready(state, plan)
        self.assertEqual(self.scheduler.dispatchable(state, plan), ["TASK-002"])

    def test_file_directory_and_whole_repo_scopes_overlap_conservatively(self) -> None:
        cases = (
            (("src/a.py",), ("src/a.py",)),
            (("src/",), ("src/",)),
            (("src",), ("src/",)),
            (("src/",), ("src/a.py",)),
            (("src/",), ("src/api/",)),
            ((".",), ("docs/",)),
        )
        for left, right in cases:
            with self.subTest(left=left, right=right):
                self.assertTrue(scopes_overlap(left, right))
        self.assertFalse(scopes_overlap(("src/",), ("tests/",)))

    def test_exclusive_resource_and_active_scope_force_serial_dispatch(self) -> None:
        first = self.task(
            "TASK-001",
            path_scope=("src/a/",),
            exclusive_resources=("port:8000",),
        )
        second = self.task(
            "TASK-002",
            path_scope=("src/b/",),
            exclusive_resources=("port:8000",),
        )
        state = self.state(first, second)
        plan = self.plan(first, second)
        self.scheduler.promote_ready(state, plan)
        state["tasks"]["TASK-001"]["status"] = "RUNNING"

        self.assertEqual(self.scheduler.dispatchable(state, plan), [])
        self.assertTrue(
            resources_overlap(
                first.exclusive_resources,
                second.exclusive_resources,
            )
        )

    def test_selected_candidates_are_compared_with_each_other(self) -> None:
        first = self.task("TASK-001", path_scope=("src/",))
        second = self.task("TASK-002", path_scope=("src/api/",))
        state = self.state(first, second)
        plan = self.plan(first, second)
        self.scheduler.promote_ready(state, plan)
        self.assertEqual(self.scheduler.dispatchable(state, plan), ["TASK-001"])

    def test_non_executing_run_or_exhausted_slots_dispatches_nothing(self) -> None:
        task = self.task("TASK-001")
        state = self.state(task)
        plan = self.plan(task)
        self.scheduler.promote_ready(state, plan)
        state["status"] = "PAUSED"
        self.assertEqual(self.scheduler.dispatchable(state, plan), [])
        state["status"] = "EXECUTING"
        state["max_workers"] = 0
        self.assertEqual(self.scheduler.dispatchable(state, plan), [])

    def test_attempt_failure_retries_same_task_without_repair_round(self) -> None:
        task = new_task_state(self.task("TASK-001"))
        worktree = TaskWorktree(
            path=Path(self.temporary.name)
            / ".vibe-coding/worktrees/RUN-20260723-001/TASK-001-a1",
            branch="refs/heads/vibe/RUN-20260723-001/TASK-001-a1",
            base_sha="a" * 40,
        )
        start_attempt(task, "a" * 40, worktree, "ATTEMPT-1")
        self.assertEqual(task["active_attempt"]["status"], "STARTING")
        self.assertIsNone(task["active_attempt"]["preflight"])
        bind_attempt_preflight(
            task,
            "ATTEMPT-1",
            ArtifactRef(
                "tasks/TASK-001/attempts/001/preflight.json",
                "sha256:" + "b" * 64,
            ),
        )
        close_attempt(
            task,
            "FAILED",
            {"code": "TEST_FAILED", "message": "failed", "retryable": True},
            retryable=True,
        )
        self.assertEqual(task["status"], "READY")
        self.assertEqual(task["attempt_no"], 1)
        self.assertEqual(task["failure_count"], 1)
        self.assertIsNone(task["active_attempt"])
        self.assertNotIn("repair_round", task)

    def test_attempt_allocation_is_two_phase_and_tokens_cannot_be_reused(self) -> None:
        task = new_task_state(self.task("TASK-001"))
        worktree = TaskWorktree(
            path=Path(self.temporary.name)
            / ".vibe-coding/worktrees/RUN-20260723-001/TASK-001-a1",
            branch="refs/heads/vibe/RUN-20260723-001/TASK-001-a1",
            base_sha="a" * 40,
        )
        start_attempt(task, "a" * 40, worktree, "ATTEMPT-1")
        with self.assertRaises(ValueError):
            start_attempt(task, "a" * 40, worktree, "ATTEMPT-2")
        preflight = ArtifactRef(
            "tasks/TASK-001/attempts/001/preflight.json",
            "sha256:" + "c" * 64,
        )
        bind_attempt_preflight(task, "ATTEMPT-1", preflight)
        bind_attempt_preflight(task, "ATTEMPT-1", preflight)
        with self.assertRaises(ValueError):
            bind_attempt_preflight(
                task,
                "ATTEMPT-OTHER",
                preflight,
            )


if __name__ == "__main__":
    unittest.main()
