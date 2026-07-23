from __future__ import annotations

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
from tests.test_controller_fake_provider import (
    _edit_worker,
    _pass_evaluation,
    _single_task_plan,
    _worker_result,
)


DISPATCH_CRASH_POINTS = (
    "after_dispatch_intent_before_provider_start",
    "after_provider_start_before_handle_binding",
)
SOURCE_AND_INTEGRATION_CRASH_POINTS = (
    "after_worker_edit_before_controller_commit",
    "after_controller_commit_before_verification_binding",
    "after_candidate_verification_before_pending_integration",
    "after_pending_integration_before_update_ref",
    "after_update_ref_before_state_completion",
)


class ControllerRecoveryTests(unittest.TestCase):
    def build(
        self,
        root: Path,
        *,
        fault_point: str | None = None,
        release: threading.Event | None = None,
        resume_worker: bool = False,
    ) -> ControllerScenario:
        scripts = [
            ProviderScript("planner", _single_task_plan()),
            ProviderScript(
                "worker",
                _worker_result,
                on_start=_edit_worker,
                release=release,
            ),
        ]
        if resume_worker:
            scripts.append(
                ProviderScript(
                    "worker",
                    _worker_result,
                    on_start=_edit_worker,
                )
            )
        scripts.append(
            ProviderScript("evaluator", _pass_evaluation())
        )
        return ControllerScenario.build(
            root,
            ScenarioSpec(
                goal="Create a recoverable file",
                config=controller_config(),
                scripts=tuple(scripts),
                fault_points=(
                    frozenset({fault_point})
                    if fault_point is not None
                    else frozenset()
                ),
            ),
        )

    def test_dispatch_crash_windows_resume_without_duplicate_start(
        self,
    ) -> None:
        for point in DISPATCH_CRASH_POINTS:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                scenario = self.build(
                    Path(tmp),
                    fault_point=point,
                )
                try:
                    with self.assertRaisesRegex(RuntimeError, point):
                        scenario.controller.execute(scenario.run_id)
                    state = scenario.new_controller().resume(
                        scenario.run_id
                    )
                    self.assertEqual(state["status"], "SUCCEEDED")
                    planner_starts = [
                        request
                        for request in scenario.provider.requests
                        if request.role == "planner"
                    ]
                    self.assertEqual(len(planner_starts), 1)
                finally:
                    scenario.close()

    def test_source_and_integration_crash_windows_resume_exactly_once(
        self,
    ) -> None:
        for point in SOURCE_AND_INTEGRATION_CRASH_POINTS:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                scenario = self.build(
                    Path(tmp),
                    fault_point=point,
                )
                try:
                    with self.assertRaisesRegex(RuntimeError, point):
                        scenario.controller.execute(scenario.run_id)
                    state = scenario.new_controller().resume(
                        scenario.run_id
                    )
                    self.assertEqual(state["status"], "SUCCEEDED")
                    base = state["repository"]["base_sha"]
                    head = state["repository"]["integration_head"]
                    count = subprocess.run(
                        [
                            "git",
                            "-C",
                            str(scenario.target),
                            "rev-list",
                            "--count",
                            f"{base}..{head}",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()
                    self.assertEqual(count, "1")
                finally:
                    scenario.close()

    def test_live_stop_is_consumed_and_resume_uses_fresh_attempt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = threading.Event()
            scenario = self.build(
                Path(tmp),
                release=release,
                resume_worker=True,
            )
            try:
                scenario.start_controller()
                scenario.wait_until(
                    lambda: any(
                        request.role == "worker"
                        for request in scenario.provider.requests
                    )
                )
                request = scenario.controller.request_stop(
                    scenario.run_id
                )
                scenario._thread.join(timeout=20)
                self.assertFalse(scenario._thread.is_alive())
                if not scenario.thread_errors.empty():
                    raise scenario.thread_errors.get()
                stopped = scenario.store.load()
                self.assertEqual(stopped["status"], "STOPPED")
                self.assertEqual(
                    stopped["resume_status"],
                    "EXECUTING",
                )
                self.assertEqual(
                    stopped["stop_receipts"][-1]["nonce"],
                    request.nonce,
                )
                self.assertEqual(
                    stopped["tasks"]["TASK-001"][
                        "failure_count"
                    ],
                    0,
                )
                release.set()
                terminal = scenario.new_controller().resume(
                    scenario.run_id
                )
                task = terminal["tasks"]["TASK-001"]
                self.assertEqual(terminal["status"], "SUCCEEDED")
                self.assertEqual(task["attempt_no"], 2)
                self.assertEqual(task["failure_count"], 0)
            finally:
                release.set()
                scenario.close()


if __name__ == "__main__":
    unittest.main()
