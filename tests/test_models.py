from __future__ import annotations

import copy
import unittest

from vibe.models import (
    AttemptStatus,
    ContractError,
    EvaluationVerdict,
    RunStatus,
    TaskStatus,
    goal_gate_satisfied,
    transition_run,
    validate_run_state,
)


def minimal_state() -> dict[str, object]:
    return {
        "schema_version": 4,
        "run_id": "RUN-20260723-001",
        "revision": 0,
        "goal": "Build the external controller",
        "repository": {
            "identity": "sha256:" + "1" * 64,
            "base_ref": "refs/heads/main",
            "base_sha": "a" * 40,
            "integration_ref": "refs/heads/vibe/run-RUN-20260723-001",
            "integration_head": "a" * 40,
        },
        "status": "CREATED",
        "resume_status": None,
        "plan_version": 0,
        "repair_round": 0,
        "max_repair_rounds": 3,
        "max_workers": 4,
        "controller": None,
        "creation": {
            "intent": {
                "path": "creation.intent.json",
                "sha256": "sha256:" + "6" * 64,
            },
            "receipt": None,
        },
        "config": {
            "path": "config.json",
            "sha256": "sha256:" + "2" * 64,
        },
        "artifact_index": [
            {
                "path": "creation.intent.json",
                "sha256": "sha256:" + "6" * 64,
            },
            {
                "path": "config.json",
                "sha256": "sha256:" + "2" * 64,
            },
        ],
        "plans": [],
        "role_attempts": {"planner": [], "evaluator": []},
        "role_runtime": {
            role: {
                "operation_id": None,
                "attempt_no": 0,
                "failure_count": 0,
                "max_attempts": 3,
                "active_attempt_token": None,
                "last_error": None,
            }
            for role in ("planner", "evaluator")
        },
        "evaluations": [],
        "verifications": [],
        "legacy_import": None,
        "tasks": {},
        "pending_dispatches": {},
        "pending_source_commit": None,
        "pending_integration": None,
        "pending_evaluation": None,
        "latest_evaluation": None,
        "global_verification": None,
        "stop_receipts": [],
        "last_error": None,
        "created_at": "2026-07-23T10:00:00+08:00",
        "updated_at": "2026-07-23T10:00:00+08:00",
    }


class ModelContractTests(unittest.TestCase):
    def test_enums_keep_run_task_attempt_and_verdict_semantics_separate(
        self,
    ) -> None:
        self.assertEqual(RunStatus.PAUSED.value, "PAUSED")
        self.assertEqual(
            TaskStatus.READY_TO_INTEGRATE.value,
            "READY_TO_INTEGRATE",
        )
        self.assertEqual(AttemptStatus.ABANDONED.value, "ABANDONED")
        self.assertEqual(EvaluationVerdict.UNVERIFIED.value, "UNVERIFIED")

    def test_validate_run_state_accepts_the_minimum_schema_four_shape(
        self,
    ) -> None:
        state = minimal_state()
        self.assertEqual(validate_run_state(state), state)

    def test_validate_run_state_rejects_boolean_revision_and_unknown_fields(
        self,
    ) -> None:
        state = minimal_state()
        state["revision"] = True
        with self.assertRaisesRegex(ContractError, "revision"):
            validate_run_state(state)

        state = minimal_state()
        state["unexpected"] = "value"
        with self.assertRaisesRegex(ContractError, "unknown run state fields"):
            validate_run_state(state)

    def test_validate_run_state_rejects_nested_unknowns_and_escaping_artifacts(
        self,
    ) -> None:
        state = minimal_state()
        state["controller"] = {
            "pid": 123,
            "process_start_identity": "linux:1234",
            "process_group": 123,
            "controller_token": "CONTROLLER-1",
            "unexpected": True,
        }
        with self.assertRaisesRegex(ContractError, "controller fields"):
            validate_run_state(state)

        state = minimal_state()
        state["config"]["path"] = "../../outside.json"
        with self.assertRaisesRegex(ContractError, "artifact path"):
            validate_run_state(state)

    def test_pause_records_previous_activity_and_resume_restores_it(
        self,
    ) -> None:
        state = transition_run(minimal_state(), RunStatus.PLANNING)
        paused = transition_run(state, RunStatus.PAUSED)
        self.assertEqual(paused["status"], "PAUSED")
        self.assertEqual(paused["resume_status"], "PLANNING")

        restored = transition_run(paused, RunStatus.PLANNING)
        self.assertEqual(restored["status"], "PLANNING")
        self.assertIsNone(restored["resume_status"])

    def test_only_migration_owned_pauses_may_omit_resume_status(self) -> None:
        for code in ("SCHEMA3_REPLAN_REQUIRED", "MIGRATION_INSTALLING"):
            with self.subTest(code=code):
                state = minimal_state()
                state["status"] = "PAUSED"
                state["last_error"] = {
                    "code": code,
                    "message": "migration-owned pause",
                    "retryable": code == "SCHEMA3_REPLAN_REQUIRED",
                }
                self.assertEqual(validate_run_state(state), state)

        invalid = minimal_state()
        invalid["status"] = "PAUSED"
        invalid["last_error"] = {
            "code": "ARBITRARY_PAUSE",
            "message": "must have a resume target",
            "retryable": True,
        }
        with self.assertRaisesRegex(ContractError, "resume_status"):
            validate_run_state(invalid)

    def test_invalid_transition_does_not_mutate_input(self) -> None:
        state = minimal_state()
        with self.assertRaisesRegex(ContractError, "CREATED -> SUCCEEDED"):
            transition_run(state, RunStatus.SUCCEEDED)
        self.assertEqual(state["status"], "CREATED")

    def test_goal_gate_requires_bound_pass_and_no_pending_work(self) -> None:
        state = minimal_state()
        state["status"] = "EVALUATING"
        state["plan_version"] = 1
        plan_ref = {
            "path": "plan/plan-v001.json",
            "sha256": "sha256:" + "7" * 64,
        }
        state["plans"] = [plan_ref]
        attempt_ref = {
            "path": "tasks/TASK-001/attempts/001/attempt.json",
            "sha256": "sha256:" + "d" * 64,
        }
        state["tasks"] = {
            "TASK-001": {
                "status": "COMPLETED",
                "task": {
                    "path": "tasks/TASK-001/task.json",
                    "sha256": "sha256:" + "3" * 64,
                },
                "attempt_no": 1,
                "failure_count": 0,
                "max_attempts": 3,
                "active_attempt": None,
                "attempts": [attempt_ref],
                "result": {
                    "path": "tasks/TASK-001/attempts/001/result.json",
                    "sha256": "sha256:" + "8" * 64,
                },
                "verification": {
                    "path": (
                        "verification/tasks/TASK-001-a1/"
                        "VERIFY-00000000-0000-4000-8000-000000000001/"
                        "manifest.json"
                    ),
                    "sha256": "sha256:" + "9" * 64,
                },
                "source_commits": ["b" * 40],
                "integrated_commits": ["c" * 40],
                "last_error": None,
            }
        }
        state["latest_evaluation"] = {
            "evaluation": {
                "path": "evaluations/001/evaluation.json",
                "sha256": "sha256:" + "4" * 64,
            },
            "verdict": "PASS",
            "evaluation_round": 1,
            "evidence_round": 0,
            "integration_head": "a" * 40,
        }
        state["global_verification"] = {
            "verification": {
                "path": (
                    "verification/global/"
                    "VERIFY-00000000-0000-4000-8000-000000000002/"
                    "manifest.json"
                ),
                "sha256": "sha256:" + "5" * 64,
            },
            "integration_head": "a" * 40,
            "passed": True,
        }
        state["evaluations"] = [state["latest_evaluation"]["evaluation"]]
        state["verifications"] = [
            state["tasks"]["TASK-001"]["verification"],
            state["global_verification"]["verification"],
        ]
        state["artifact_index"].extend(
            [
                plan_ref,
                state["tasks"]["TASK-001"]["task"],
                attempt_ref,
                state["tasks"]["TASK-001"]["result"],
                state["tasks"]["TASK-001"]["verification"],
                state["latest_evaluation"]["evaluation"],
                state["global_verification"]["verification"],
            ]
        )
        envelope = {
            "verdict": "PASS",
            "integration_head": "a" * 40,
            "evidence_catalog": {
                "verification:global": {
                    "integration_head": "a" * 40,
                    "verification": state["global_verification"][
                        "verification"
                    ],
                    "criterion_ids": ["AC-001"],
                }
            },
            "criteria": [
                {
                    "id": "AC-001",
                    "verdict": "PASS",
                    "evidence_ids": ["verification:global"],
                }
            ],
            "findings": [],
        }

        self.assertTrue(goal_gate_satisfied(state, envelope, "a" * 40))

        unknown = copy.deepcopy(envelope)
        unknown["criteria"][0]["evidence_ids"] = ["unknown"]
        self.assertFalse(goal_gate_satisfied(state, unknown, "a" * 40))

        stale = copy.deepcopy(envelope)
        stale["evidence_catalog"]["verification:global"][
            "integration_head"
        ] = "b" * 40
        self.assertFalse(goal_gate_satisfied(state, stale, "a" * 40))

        state["pending_dispatches"] = {"ATTEMPT-stale": {}}
        self.assertFalse(goal_gate_satisfied(state, envelope, "a" * 40))
        state["pending_dispatches"] = {}
        state["verifications"].remove(
            state["global_verification"]["verification"]
        )
        self.assertFalse(goal_gate_satisfied(state, envelope, "a" * 40))


if __name__ == "__main__":
    unittest.main()
