from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from vibe.cli import build_parser, main
from vibe.models import ContractError


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name)

    def call_main(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(arguments))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_run_requires_exactly_one_goal_source(self) -> None:
        code, _, _ = self.call_main(
            "run",
            "--target",
            str(self.target),
        )
        self.assertEqual(code, 2)
        goal_file = self.target / "goal.md"
        goal_file.write_text("goal\n", encoding="utf-8")
        code, _, _ = self.call_main(
            "run",
            "--target",
            str(self.target),
            "--goal",
            "goal",
            "--goal-file",
            str(goal_file),
        )
        self.assertEqual(code, 2)

    def test_foreground_states_map_to_stable_exit_codes(self) -> None:
        expected = {
            "SUCCEEDED": 0,
            "PAUSED": 3,
            "FAILED": 4,
            "STOPPED": 130,
        }
        for status, exit_code in expected.items():
            controller = mock.Mock()
            controller.create_run.return_value = (
                "RUN-20260723-001"
            )
            controller.execute.return_value = {
                "run_id": "RUN-20260723-001",
                "status": status,
            }
            with self.subTest(status=status), mock.patch(
                "vibe.cli.resolve_target",
                return_value=self.target,
            ), mock.patch(
                "vibe.cli.load_run_config",
                return_value=mock.sentinel.config,
            ), mock.patch(
                "vibe.cli.controller_for_target",
                return_value=controller,
            ):
                code, output, _ = self.call_main(
                    "run",
                    "--target",
                    str(self.target),
                    "--goal",
                    "goal",
                    "--json",
                )
            self.assertEqual(code, exit_code)
            self.assertEqual(
                json.loads(output)["status"],
                status,
            )

    def test_status_json_is_one_stable_projection(self) -> None:
        state = {
            "run_id": "RUN-20260723-001",
            "status": "EXECUTING",
            "revision": 7,
            "plan_version": 1,
            "repair_round": 0,
            "tasks": {
                "TASK-001": {"status": "COMPLETED"},
                "TASK-002": {"status": "RUNNING"},
            },
            "last_error": None,
        }
        with mock.patch(
            "vibe.cli.resolve_target",
            return_value=self.target,
        ), mock.patch(
            "vibe.cli.load_status",
            return_value=state,
        ):
            code, output, _ = self.call_main(
                "status",
                "--target",
                str(self.target),
                "RUN-20260723-001",
                "--json",
            )
        value = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(value["status"], "EXECUTING")
        self.assertEqual(value["tasks"]["completed"], 1)
        self.assertEqual(value["tasks"]["running"], 1)

    def test_expected_error_has_no_traceback(self) -> None:
        with mock.patch(
            "vibe.cli.resolve_target",
            return_value=self.target,
        ), mock.patch(
            "vibe.cli.load_status",
            side_effect=ContractError("invalid state"),
        ):
            code, _, error = self.call_main(
                "status",
                "--target",
                str(self.target),
                "RUN-20260723-001",
            )
        self.assertEqual(code, 2)
        self.assertIn("invalid state", error)
        self.assertNotIn("Traceback", error)

    def test_parser_exposes_all_commands_and_help(self) -> None:
        parser = build_parser()
        for command in (
            "run",
            "resume",
            "status",
            "stop",
            "logs",
            "migrate",
        ):
            with self.subTest(command=command):
                with self.assertRaises(SystemExit) as stopped:
                    parser.parse_args([command, "--help"])
                self.assertEqual(stopped.exception.code, 0)

    def test_json_usage_error_is_machine_stable(self) -> None:
        code, output, _ = self.call_main(
            "status",
            "--target",
            str(self.target),
            "NOT-A-RUN",
            "--json",
        )
        value = json.loads(output)
        self.assertEqual(code, 2)
        self.assertFalse(value["ok"])
        self.assertEqual(value["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
