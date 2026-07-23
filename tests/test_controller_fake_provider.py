from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from tests.support.controller_scenario import (
    ControllerScenario,
    ScenarioSpec,
)
from tests.support.fake_provider import ProviderScript
from tests.test_controller import controller_config
from vibe.models import FrozenRunConfig
from vibe.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)


def _body(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _plan() -> bytes:
    return _body(
        {
            "schema_version": 1,
            "plan_version": 1,
            "summary": "Two independent tasks",
            "acceptance_criteria": [
                {"id": "AC-001", "description": "Both files exist"}
            ],
            "global_verification": ["unit"],
            "tasks": [
                {
                    "id": "TASK-001",
                    "objective": "Create a.txt",
                    "worker_type": "implementation",
                    "covers": ["AC-001"],
                    "depends_on": [],
                    "path_scope": ["src/a.txt"],
                    "exclusive_resources": [],
                    "acceptance_checks": [],
                    "max_attempts": 2,
                },
                {
                    "id": "TASK-002",
                    "objective": "Create b.txt",
                    "worker_type": "testing",
                    "covers": ["AC-001"],
                    "depends_on": [],
                    "path_scope": ["src/b.txt"],
                    "exclusive_resources": [],
                    "acceptance_checks": [],
                    "max_attempts": 2,
                },
            ],
        }
    )


def _single_task_plan(
    *,
    plan_version: int = 1,
    task_id: str = "TASK-001",
    path: str = "src/a.txt",
    exclusive_resources: tuple[str, ...] = (),
) -> bytes:
    return _body(
        {
            "schema_version": 1,
            "plan_version": plan_version,
            "summary": f"Plan {task_id}",
            "acceptance_criteria": [
                {
                    "id": "AC-001",
                    "description": "All requested files exist",
                }
            ],
            "global_verification": ["unit"],
            "tasks": [
                {
                    "id": task_id,
                    "objective": f"Create {path}",
                    "worker_type": "implementation",
                    "covers": ["AC-001"],
                    "depends_on": [],
                    "path_scope": [path],
                    "exclusive_resources": list(
                        exclusive_resources
                    ),
                    "acceptance_checks": [],
                    "max_attempts": 2,
                }
            ],
        }
    )


def _conflicting_plan() -> bytes:
    value = json.loads(_plan())
    for task in value["tasks"]:
        task["exclusive_resources"] = ["shared-generator"]
    return _body(value)


def _worker_result(request) -> bytes:
    match = re.search(
        r"/tasks/(TASK-\d{3})/attempts/(\d{3})/",
        request.result_path,
    )
    if match is None:
        raise AssertionError(request.result_path)
    task_id = match.group(1)
    attempt_no = int(match.group(2))
    base = subprocess.run(
        ["git", "-C", request.cwd, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    changed = "src/a.txt" if task_id == "TASK-001" else "src/b.txt"
    return _body(
        {
            "schema_version": 1,
            "task_id": task_id,
            "attempt_no": attempt_no,
            "attempt_token": request.attempt_token,
            "status": "COMPLETED",
            "task_base_sha": base,
            "changed_paths": [changed],
            "checks": [],
            "residual_risks": [],
            "blocker": None,
        }
    )


def _edit_worker(request) -> None:
    task_id = "TASK-001" if "TASK-001" in request.result_path else "TASK-002"
    relative = "src/a.txt" if task_id == "TASK-001" else "src/b.txt"
    path = Path(request.cwd) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(task_id + "\n", encoding="utf-8")


def _pass_evaluation(
    evidence_id: str = "global:unit",
) -> bytes:
    return _body(
        {
            "schema_version": 1,
            "verdict": "PASS",
            "criteria": [
                {
                    "id": "AC-001",
                    "verdict": "PASS",
                    "evidence_ids": [evidence_id],
                }
            ],
            "findings": [],
            "evidence_requests": [],
            "residual_risks": [],
        }
    )


def _evaluation(
    verdict: str,
    *,
    evidence_requests: tuple[str, ...] = (),
) -> bytes:
    criterion_verdict = {
        "NEEDS_REPAIR": "FAIL",
        "UNVERIFIED": "UNVERIFIED",
        "BLOCKED": "BLOCKED",
    }[verdict]
    findings = (
        [
            {
                "criterion_id": "AC-001",
                "severity": "HIGH",
                "evidence": "The requested output is incomplete",
                "affected_paths": ["src/b.txt"],
                "repair_hint": "Create the missing output",
            }
        ]
        if verdict in {"NEEDS_REPAIR", "BLOCKED"}
        else []
    )
    return _body(
        {
            "schema_version": 1,
            "verdict": verdict,
            "criteria": [
                {
                    "id": "AC-001",
                    "verdict": criterion_verdict,
                    "evidence_ids": [],
                }
            ],
            "findings": findings,
            "evidence_requests": list(evidence_requests),
            "residual_risks": [],
        }
    )


class ControllerFakeProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def scenario(self, release: threading.Event) -> ControllerScenario:
        scripts = (
            ProviderScript("planner", _plan()),
            ProviderScript(
                "worker",
                _worker_result,
                on_start=_edit_worker,
                release=release,
            ),
            ProviderScript(
                "worker",
                _worker_result,
                on_start=_edit_worker,
                release=release,
            ),
            ProviderScript("evaluator", _pass_evaluation()),
        )
        scenario = ControllerScenario.build(
            Path(self.temporary.name),
            ScenarioSpec(
                goal="Create two independent files",
                config=controller_config(),
                scripts=scripts,
            ),
        )
        self.addCleanup(scenario.close)
        return scenario

    def build_scenario(
        self,
        scripts: tuple[ProviderScript, ...],
        *,
        config: FrozenRunConfig | None = None,
    ) -> ControllerScenario:
        scenario = ControllerScenario.build(
            Path(self.temporary.name),
            ScenarioSpec(
                goal="Create the requested files",
                config=config or controller_config(),
                scripts=scripts,
            ),
        )
        self.addCleanup(scenario.close)
        return scenario

    def test_two_independent_workers_overlap_then_integrate_serially(self) -> None:
        release = threading.Event()
        scenario = self.scenario(release)
        thread = scenario.start_controller()
        scenario.wait_until(
            lambda: scenario.provider.maximum_active == 2
        )
        self.assertEqual(scenario.integration_count, 0)
        release.set()
        state = scenario.join_controller(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(state["status"], "SUCCEEDED")
        self.assertEqual(
            scenario.maximum_simultaneous_integrations,
            1,
        )
        self.assertEqual(
            scenario.git_changed_paths(
                state["repository"]["integration_head"]
            ),
            {"src/a.txt", "src/b.txt"},
        )

    def test_conflicting_resources_never_run_together(self) -> None:
        release = threading.Event()
        scenario = self.build_scenario(
            (
                ProviderScript("planner", _conflicting_plan()),
                ProviderScript(
                    "worker",
                    _worker_result,
                    on_start=_edit_worker,
                    release=release,
                ),
                ProviderScript(
                    "worker",
                    _worker_result,
                    on_start=_edit_worker,
                ),
                ProviderScript(
                    "evaluator",
                    _pass_evaluation(),
                ),
            )
        )
        scenario.start_controller()
        scenario.wait_until(
            lambda: len(
                [
                    request
                    for request in scenario.provider.requests
                    if request.role == "worker"
                ]
            )
            == 1
            and scenario.store.load()["tasks"]["TASK-002"][
                "status"
            ]
            == "READY"
        )
        self.assertEqual(scenario.provider.maximum_active, 1)
        release.set()
        state = scenario.join_controller(timeout=30)
        self.assertEqual(state["status"], "SUCCEEDED")
        self.assertEqual(scenario.provider.maximum_active, 1)

    def test_worker_failure_uses_a_fresh_attempt_identity(self) -> None:
        scenario = self.build_scenario(
            (
                ProviderScript(
                    "planner",
                    _single_task_plan(),
                ),
                ProviderScript(
                    "worker",
                    b"{}\n",
                    failure=ProviderFailure(
                        ProviderFailureKind.PROCESS,
                        "simulated worker failure",
                    ),
                ),
                ProviderScript(
                    "worker",
                    _worker_result,
                    on_start=_edit_worker,
                ),
                ProviderScript(
                    "evaluator",
                    _pass_evaluation(),
                ),
            )
        )
        state = scenario.controller.execute(scenario.run_id)
        worker_requests = [
            request
            for request in scenario.provider.requests
            if request.role == "worker"
        ]
        self.assertEqual(state["status"], "SUCCEEDED")
        self.assertEqual(len(worker_requests), 2)
        self.assertNotEqual(
            worker_requests[0].attempt_token,
            worker_requests[1].attempt_token,
        )
        task = state["tasks"]["TASK-001"]
        self.assertEqual(task["attempt_no"], 2)
        self.assertEqual(task["failure_count"], 1)
        self.assertEqual(len(task["attempts"]), 2)
        failed_result = Path(worker_requests[0].result_path)
        self.assertFalse(failed_result.exists())

    def test_needs_repair_appends_a_new_plan_and_succeeds(self) -> None:
        scenario = self.build_scenario(
            (
                ProviderScript(
                    "planner",
                    _single_task_plan(),
                ),
                ProviderScript(
                    "worker",
                    _worker_result,
                    on_start=_edit_worker,
                ),
                ProviderScript(
                    "evaluator",
                    _evaluation("NEEDS_REPAIR"),
                ),
                ProviderScript(
                    "planner",
                    _single_task_plan(
                        plan_version=2,
                        task_id="TASK-002",
                        path="src/b.txt",
                    ),
                ),
                ProviderScript(
                    "worker",
                    _worker_result,
                    on_start=_edit_worker,
                ),
                ProviderScript("evaluator", _pass_evaluation()),
            )
        )
        state = scenario.controller.execute(scenario.run_id)
        self.assertEqual(state["status"], "SUCCEEDED")
        self.assertEqual(state["plan_version"], 2)
        self.assertEqual(state["repair_round"], 1)
        self.assertEqual(len(state["plans"]), 2)
        self.assertTrue(
            (
                scenario.store.root / "plan/plan-v001.json"
            ).is_file()
        )
        self.assertTrue(
            (
                scenario.store.root / "plan/repair-v002.json"
            ).is_file()
        )

    def test_unverified_reevaluates_the_same_commit(self) -> None:
        scenario = self.build_scenario(
            (
                ProviderScript(
                    "planner",
                    _single_task_plan(),
                ),
                ProviderScript(
                    "worker",
                    _worker_result,
                    on_start=_edit_worker,
                ),
                ProviderScript(
                    "evaluator",
                    _evaluation(
                        "UNVERIFIED",
                        evidence_requests=("unit",),
                    ),
                ),
                ProviderScript(
                    "evaluator",
                    _pass_evaluation("supplemental:unit"),
                ),
            )
        )
        state = scenario.controller.execute(scenario.run_id)
        envelopes = [
            json.loads(
                (
                    scenario.store.root / item["path"]
                ).read_bytes()
            )
            for item in state["evaluations"]
        ]
        self.assertEqual(state["status"], "SUCCEEDED")
        self.assertEqual(state["repair_round"], 0)
        self.assertEqual(
            [item["evidence_round"] for item in envelopes],
            [0, 1],
        )
        self.assertEqual(
            {
                item["integration_head"]
                for item in envelopes
            },
            {state["repository"]["integration_head"]},
        )
        self.assertTrue(
            any(
                item["path"].startswith(
                    "verification/supplemental/"
                )
                for item in state["verifications"]
            )
        )
        self.assertIn(
            "supplemental:unit",
            envelopes[-1]["evidence_catalog"],
        )

    def test_repeated_unverified_pauses_at_the_evidence_bound(
        self,
    ) -> None:
        scenario = self.build_scenario(
            (
                ProviderScript(
                    "planner",
                    _single_task_plan(),
                ),
                ProviderScript(
                    "worker",
                    _worker_result,
                    on_start=_edit_worker,
                ),
                ProviderScript(
                    "evaluator",
                    _evaluation("UNVERIFIED"),
                ),
                ProviderScript(
                    "evaluator",
                    _evaluation("UNVERIFIED"),
                ),
                ProviderScript(
                    "evaluator",
                    _evaluation("UNVERIFIED"),
                ),
            )
        )
        state = scenario.controller.execute(scenario.run_id)
        self.assertEqual(state["status"], "PAUSED")
        self.assertEqual(
            state["last_error"]["code"],
            "EVIDENCE_EXHAUSTED",
        )
        self.assertEqual(state["repair_round"], 0)
        self.assertEqual(len(state["evaluations"]), 3)

    def test_blocked_evaluation_pauses_without_repair(self) -> None:
        scenario = self.build_scenario(
            (
                ProviderScript(
                    "planner",
                    _single_task_plan(),
                ),
                ProviderScript(
                    "worker",
                    _worker_result,
                    on_start=_edit_worker,
                ),
                ProviderScript(
                    "evaluator",
                    _evaluation("BLOCKED"),
                ),
            )
        )
        state = scenario.controller.execute(scenario.run_id)
        self.assertEqual(state["status"], "PAUSED")
        self.assertEqual(
            state["last_error"]["code"],
            "EVALUATOR_BLOCKED",
        )
        self.assertEqual(state["repair_round"], 0)


if __name__ == "__main__":
    unittest.main()
