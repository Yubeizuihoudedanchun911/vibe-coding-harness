from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

from tests.support.git_repo import GitRepositoryFixture
from vibe.config import effective_command_ids
from vibe.models import (
    CommandAuthorization,
    CommandSpec,
    ContractError,
    FrozenRunConfig,
)
from vibe.state_store import StateStore
from vibe.verification import (
    VerificationEnvironmentError,
    VerificationGate,
)


class RecordingProcessFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.Popen(*args, **kwargs)


class VerificationGateTests(GitRepositoryFixture, unittest.TestCase):
    def setUp(self) -> None:
        self.setUpGitRepository()
        self.head = self.git("rev-parse", "HEAD")
        self.store = StateStore.for_run(self.target, "RUN-20260723-001")
        self.process_factory = RecordingProcessFactory()
        self.configure(
            (
                CommandSpec(
                    "unit",
                    "Run a unit fixture",
                    (sys.executable, "-c", "print('verified')"),
                    ".",
                    30,
                    (),
                ),
            ),
            (),
        )

    def configure(
        self,
        catalog: tuple[CommandSpec, ...],
        required: tuple[str, ...],
    ) -> None:
        self.config = FrozenRunConfig(
            provider_name="codex-cli",
            max_workers=2,
            task_attempts=3,
            provider_retries=3,
            evidence_rounds=3,
            repair_rounds=3,
            max_plan_tasks=128,
            command_catalog=catalog,
            required_command_ids=required,
            command_authorization=CommandAuthorization(
                "EXPLICIT_PROJECT_FILE",
                "vibe.json",
                "sha256:" + "f" * 64,
            ),
        )
        self.gate = VerificationGate(
            self.config,
            self.store,
            process_factory=self.process_factory,
        )

    def run_gate(
        self,
        command_ids: tuple[str, ...],
        prefix: str | None = None,
    ):
        operation = prefix or f"verification/global/VERIFY-{uuid.uuid4()}"
        with self.store.lock():
            return self.gate.run(
                self.head,
                self.target,
                command_ids,
                operation,
            )

    def read_json(self, path: str) -> dict[str, object]:
        return json.loads((self.store.root / path).read_text(encoding="utf-8"))

    def test_runs_argv_without_shell_and_binds_exact_commit(self) -> None:
        result = self.run_gate(
            ("unit",),
            (
                "verification/tasks/TASK-001-a1/"
                "VERIFY-00000000-0000-4000-8000-000000000001"
            ),
        )
        manifest = self.read_json(result.manifest_ref.path)
        command = manifest["commands"][0]
        self.assertTrue(result.passed)
        self.assertEqual(manifest["commit_sha"], self.head)
        self.assertEqual(command["exit_code"], 0)
        self.assertEqual(command["env_names"], [])
        self.assertIn(
            b"verified",
            (self.store.root / command["stdout"]["path"]).read_bytes(),
        )
        self.assertFalse(self.process_factory.calls[0][1]["shell"])

    def test_configured_missing_command_is_environment_error(self) -> None:
        self.configure(
            (
                CommandSpec(
                    "missing",
                    "Missing executable",
                    ("definitely-missing-vibe-command",),
                    ".",
                    30,
                    (),
                ),
            ),
            (),
        )
        with self.assertRaises(VerificationEnvironmentError):
            self.run_gate(("missing",))
        self.assertEqual(self.process_factory.calls, [])

    def test_invalid_configured_cwd_is_rejected_before_process_start(self) -> None:
        self.configure(
            (
                CommandSpec(
                    "escape",
                    "Escaping cwd",
                    (sys.executable, "-c", "print(1)"),
                    "../",
                    30,
                    (),
                ),
            ),
            (),
        )
        with self.assertRaises(VerificationEnvironmentError):
            self.run_gate(("escape",))
        self.assertEqual(self.process_factory.calls, [])

    def test_timeout_is_failed_gate_with_immutable_evidence(self) -> None:
        self.configure(
            (
                CommandSpec(
                    "timeout",
                    "Timeout fixture",
                    (
                        sys.executable,
                        "-c",
                        "import time; time.sleep(60)",
                    ),
                    ".",
                    1,
                    (),
                ),
            ),
            (),
        )
        result = self.run_gate(("timeout",))
        self.assertFalse(result.passed)
        self.assertTrue(result.commands[0].timed_out)
        self.assertIsNone(result.commands[0].exit_code)

    def test_timeout_terminates_complete_descendant_process_group(self) -> None:
        pid_file = self.store.root / "diagnostics/verification-child.pid"
        pid_file.parent.mkdir(parents=True)
        script = (
            "import pathlib,subprocess,sys,time;"
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import time;time.sleep(60)']);"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='utf-8');"
            "time.sleep(60)"
        )
        self.configure(
            (
                CommandSpec(
                    "descendant",
                    "Descendant cleanup",
                    (sys.executable, "-c", script, str(pid_file)),
                    ".",
                    1,
                    (),
                ),
            ),
            (),
        )
        result = self.run_gate(("descendant",))
        self.assertTrue(result.commands[0].timed_out)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("verification descendant process survived timeout")

    def test_product_change_during_command_invalidates_evidence(self) -> None:
        self.configure(
            (
                CommandSpec(
                    "mutating",
                    "Mutation fixture",
                    (
                        sys.executable,
                        "-c",
                        "open('changed.txt','w').write('x')",
                    ),
                    ".",
                    30,
                    (),
                ),
            ),
            (),
        )
        with self.assertRaisesRegex(ContractError, "verification changed"):
            self.run_gate(("mutating",))

    def test_ref_change_during_command_invalidates_evidence(self) -> None:
        resolved_git = shutil.which("git")
        self.assertIsNotNone(resolved_git)
        self.configure(
            (
                CommandSpec(
                    "move-ref",
                    "Protected ref mutation",
                    (
                        str(resolved_git),
                        "update-ref",
                        "refs/heads/verification-evil",
                        "HEAD",
                    ),
                    ".",
                    30,
                    (),
                ),
            ),
            (),
        )
        with self.assertRaisesRegex(ContractError, "verification changed"):
            self.run_gate(("move-ref",))
        self.git("update-ref", "-d", "refs/heads/verification-evil")

    def test_orphaned_artifacts_do_not_conflict_with_fresh_retry(self) -> None:
        first_prefix = f"verification/global/VERIFY-{uuid.uuid4()}"
        self.configure(
            (
                CommandSpec(
                    "orphan",
                    "Leave an orphaned command artifact",
                    (
                        sys.executable,
                        "-c",
                        "open('changed.txt','w').write('x')",
                    ),
                ),
            ),
            (),
        )
        with self.assertRaisesRegex(ContractError, "verification changed"):
            self.run_gate(("orphan",), first_prefix)
        (self.target / "changed.txt").unlink()
        self.configure(
            (
                CommandSpec(
                    "unit",
                    "Fresh retry",
                    (sys.executable, "-c", "print('retry')"),
                ),
            ),
            (),
        )
        second_prefix = f"verification/global/VERIFY-{uuid.uuid4()}"
        result = self.run_gate(("unit",), second_prefix)
        self.assertTrue(result.passed)
        self.assertTrue((self.store.root / first_prefix).is_dir())
        self.assertTrue((self.store.root / second_prefix).is_dir())

    def test_unknown_or_duplicate_agent_ids_spawn_nothing(self) -> None:
        for command_ids in (("python-c-delete",), ("unit", "unit")):
            with self.subTest(command_ids=command_ids), self.assertRaises(
                ContractError
            ):
                self.run_gate(command_ids)
        self.assertEqual(self.process_factory.calls, [])

    def test_task_ids_append_to_required_ids(self) -> None:
        required = CommandSpec(
            "required",
            "Required",
            (sys.executable, "-c", "print('required')"),
        )
        task = CommandSpec(
            "task",
            "Task",
            (sys.executable, "-c", "print('task')"),
        )
        self.configure((required, task), ("required",))
        command_ids = effective_command_ids(self.config, ("task",))
        result = self.run_gate(
            command_ids,
            f"verification/tasks/TASK-001-a1/VERIFY-{uuid.uuid4()}",
        )
        self.assertEqual(
            tuple(command.command_id for command in result.commands),
            ("required", "task"),
        )
        self.assertTrue(result.passed)

    def test_noncanonical_or_reused_operation_prefix_is_rejected(self) -> None:
        for prefix in (
            "verification/global/not-a-verification",
            f"verification/other/VERIFY-{uuid.uuid4()}",
        ):
            with self.subTest(prefix=prefix), self.assertRaises(ContractError):
                self.run_gate(("unit",), prefix)
        self.assertEqual(self.process_factory.calls, [])


if __name__ == "__main__":
    unittest.main()
