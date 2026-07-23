from __future__ import annotations

import json
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vibe.models import ContractError, ProviderStatus
from vibe.providers.base import (
    ProviderFailureKind,
    ProviderHandle,
    ProviderIdentityError,
    ProviderRequest,
    classify_provider_failure,
    parse_exit_receipt,
)
from vibe.providers import codex_cli as codex_cli_module
from vibe.providers.codex_cli import (
    CodexCLIAdapter,
    build_child_env,
    parse_launch_receipt,
    process_start_identity,
)
from vibe.state_store import canonical_json_bytes


FAKE_CODEX = r"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("codex-test 1.0")
    raise SystemExit(0)

if sys.argv[1:] == ["exec", "--help"]:
    print("--ephemeral --ignore-user-config --ignore-rules --strict-config")
    print("--sandbox --output-schema --output-last-message -c --disable")
    raise SystemExit(0)

args = sys.argv[1:]
prompt = sys.stdin.buffer.read()
mode = "success"
for line in prompt.decode("utf-8").splitlines():
    if line.startswith("MODE:"):
        mode = line.split(":", 1)[1]
result = Path(args[args.index("--output-last-message") + 1])
observation = {
    "argv": args,
    "env": dict(os.environ),
    "prompt": prompt.decode("utf-8"),
}
Path("fake-observation.json").write_text(
    json.dumps(observation, sort_keys=True),
    encoding="utf-8",
)
if mode == "auth-error":
    print("authentication required", file=sys.stderr)
    raise SystemExit(1)
if mode == "spawn-descendant-and-hang":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    Path("descendant.pid").write_text(str(child.pid), encoding="utf-8")
    result.write_text('{"partial":true}\n', encoding="utf-8")
    time.sleep(60)
if mode == "hang":
    time.sleep(60)
result.write_text('{"ok":true}\n', encoding="utf-8")
print('{"event":"done"}')
"""


class CodexCLIAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.codex = self.root / "codex"
        self.codex.write_text(FAKE_CODEX, encoding="utf-8")
        self.codex.chmod(0o755)
        self.adapter = CodexCLIAdapter(codex_bin=self.codex)

    def request(
        self,
        mode: str = "success",
        *,
        timeout_seconds: int = 10,
        prompt: bytes | None = None,
    ) -> ProviderRequest:
        attempt = f"ATTEMPT-{mode.upper().replace('_', '-')}"
        prompt_path = self.root / f"{attempt}.md"
        schema_path = self.root / f"{attempt}.schema.json"
        prompt_path.write_bytes(
            prompt if prompt is not None else f"MODE:{mode}\n".encode()
        )
        schema_path.write_text(
            '{"type":"object"}\n',
            encoding="utf-8",
        )
        execution = self.adapter.execution_identity()
        request = ProviderRequest(
            attempt_token=attempt,
            role="planner",
            request_path=str(self.root / f"{attempt}.request.json"),
            prompt_path=str(prompt_path),
            schema_path=str(schema_path),
            cwd=str(self.root),
            sandbox="read-only",
            launch_path=str(self.root / f"{attempt}.launch.json"),
            stdout_path=str(self.root / f"{attempt}.stdout.log"),
            stderr_path=str(self.root / f"{attempt}.stderr.log"),
            exit_path=str(self.root / f"{attempt}.exit.json"),
            result_path=str(self.root / f"{attempt}.result.json"),
            timeout_seconds=timeout_seconds,
            codex_version=execution.codex_version,
            execution_policy_sha256=execution.policy_sha256,
        )
        Path(request.request_path).write_bytes(
            canonical_json_bytes(request.as_dict())
        )
        return request

    @staticmethod
    def partial_result_path(request: ProviderRequest) -> Path:
        result = Path(request.result_path)
        return result.with_name(
            f".{result.name}.{request.attempt_token}.tmp"
        )

    def test_real_wrapper_passes_prompt_and_atomically_publishes_result(
        self,
    ) -> None:
        request = self.request(
            prompt=b"MODE:success\nreturn structured output\n"
        )
        handle = self.adapter.start(request)
        self.assertEqual(
            self.adapter.wait(handle, 10),
            ProviderStatus.SUCCEEDED,
        )
        self.assertEqual(
            self.adapter.result(handle).body,
            b'{"ok":true}\n',
        )
        self.assertEqual(
            self.adapter.completion(handle).exit_code,
            0,
        )
        self.assertFalse(self.partial_result_path(request).exists())
        observation = json.loads(
            (self.root / "fake-observation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("return structured output", observation["prompt"])

    def test_nonzero_exit_has_classifiable_completion_without_result(
        self,
    ) -> None:
        handle = self.adapter.start(self.request(mode="auth-error"))
        self.assertEqual(
            self.adapter.wait(handle, 10),
            ProviderStatus.FAILED,
        )
        completion = self.adapter.completion(handle)
        self.assertEqual(
            classify_provider_failure(
                completion.exit_code,
                completion.stderr_body.decode(
                    "utf-8",
                    "replace",
                ),
            ).kind,
            ProviderFailureKind.AUTH,
        )
        with self.assertRaises(ContractError):
            self.adapter.result(handle)

    def test_timeout_kills_descendant_group_and_publishes_no_partial_result(
        self,
    ) -> None:
        request = self.request(
            mode="spawn-descendant-and-hang",
            timeout_seconds=1,
        )
        handle = self.adapter.start(request)
        self.assertEqual(
            self.adapter.wait(handle, 10),
            ProviderStatus.FAILED,
        )
        completion = self.adapter.completion(handle)
        self.assertTrue(completion.timed_out)
        self.assertEqual(completion.exit_code, 124)
        self.assertFalse(Path(request.result_path).exists())
        self.assertFalse(self.partial_result_path(request).exists())
        descendant = int(
            (self.root / "descendant.pid").read_text(encoding="utf-8")
        )
        deadline = time.monotonic() + 5
        while _pid_exists(descendant) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_pid_exists(descendant))

    def test_repeated_stop_is_idempotent_and_both_groups_end(self) -> None:
        request = self.request(mode="hang", timeout_seconds=30)
        handle = self.adapter.start(request)
        first = self.adapter.stop(handle, 2)
        second = self.adapter.stop(handle, 2)
        self.assertTrue(first.stopped)
        self.assertEqual(second, first)
        self.assertFalse(_pid_exists(handle.pid))
        self.assertFalse(_pid_exists(handle.child_pid))

    def test_request_file_mismatch_is_rejected_without_rewrite(self) -> None:
        request = self.request()
        path = Path(request.request_path)
        path.write_bytes(b"{}\n")
        original = path.read_bytes()
        with self.assertRaisesRegex(
            ContractError,
            "request bytes",
        ):
            self.adapter.start(request)
        self.assertEqual(path.read_bytes(), original)

    def test_adapter_failure_after_launch_closes_activation_and_cleans_groups(
        self,
    ) -> None:
        request = self.request()
        with mock.patch(
            "vibe.providers.codex_cli.os.write",
            side_effect=BrokenPipeError("injected activation failure"),
        ):
            with self.assertRaisesRegex(
                BrokenPipeError,
                "activation",
            ):
                self.adapter.start(request)
        launch = parse_launch_receipt(
            Path(request.launch_path).read_bytes(),
            request,
        )
        deadline = time.monotonic() + 5
        while (
            _pid_exists(launch.pid)
            or _pid_exists(launch.child_pid)
        ) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_pid_exists(launch.pid))
        self.assertFalse(_pid_exists(launch.child_pid))
        self.assertFalse(Path(request.result_path).exists())

    def test_symlinked_output_is_rejected_without_touching_target(
        self,
    ) -> None:
        request = self.request()
        external = self.root / "external.json"
        external.write_text("sentinel\n", encoding="utf-8")
        Path(request.result_path).symlink_to(external)
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            self.adapter.start(request)
        self.assertEqual(
            external.read_text(encoding="utf-8"),
            "sentinel\n",
        )

    def test_descriptor_rooted_output_open_rejects_symlink_parent(
        self,
    ) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            codex_cli_module._open_output_exclusive(
                linked / "stdout.log"
            )
        self.assertFalse((outside / "stdout.log").exists())

    def test_identity_mismatch_never_signals_a_process_group(self) -> None:
        request = self.request()
        handle = ProviderHandle(
            adapter="codex-cli",
            attempt_token=request.attempt_token,
            pid=os.getpid(),
            process_start_identity="wrong",
            process_group=os.getpgrp(),
            child_pid=os.getpid(),
            child_process_start_identity="wrong",
            child_process_group=os.getpgrp(),
            codex_version=request.codex_version,
            execution_policy_sha256=request.execution_policy_sha256,
            launch_path=request.launch_path,
            stdout_path=request.stdout_path,
            stderr_path=request.stderr_path,
            exit_path=request.exit_path,
            result_path=request.result_path,
        )
        with mock.patch("vibe.providers.codex_cli.os.killpg") as kill:
            with self.assertRaises(ProviderIdentityError):
                self.adapter.stop(handle, 0.01)
        kill.assert_not_called()

    def test_hostile_parent_environment_is_not_exposed_to_codex(
        self,
    ) -> None:
        request = self.request()
        with mock.patch.dict(
            os.environ,
            {
                "VIBE_SECRET_SENTINEL": "must-not-leak",
                "CODEX_HOME": "/hostile/config",
            },
        ):
            handle = self.adapter.start(request)
            self.assertEqual(
                self.adapter.wait(handle, 10),
                ProviderStatus.SUCCEEDED,
            )
        observation = json.loads(
            (self.root / "fake-observation.json").read_text(
                encoding="utf-8"
            )
        )
        constructed = build_child_env(
            home=self.root / "home",
            codex_home=self.root / "codex-home",
            tmpdir=self.root / "tmp",
        )
        self.assertEqual(
            set(constructed),
            {"HOME", "CODEX_HOME", "PATH", "LANG", "LC_ALL", "TMPDIR"},
        )
        self.assertTrue(
            set(constructed).issubset(observation["env"])
        )
        self.assertNotIn(
            "must-not-leak",
            json.dumps(observation),
        )
        argv = observation["argv"]
        for flag in (
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--output-schema",
            "--output-last-message",
        ):
            self.assertIn(flag, argv)
        self.assertIn("mcp", argv)
        self.assertIn("web_search", " ".join(argv))

    def test_completed_receipt_supports_new_adapter_recovery(self) -> None:
        request = self.request()
        handle = self.adapter.start(request)
        self.assertEqual(
            self.adapter.wait(handle, 10),
            ProviderStatus.SUCCEEDED,
        )
        recovered = CodexCLIAdapter(codex_bin=self.codex)
        self.assertEqual(
            recovered.poll(handle),
            ProviderStatus.SUCCEEDED,
        )
        self.assertEqual(
            recovered.result(handle).body,
            b'{"ok":true}\n',
        )

    def test_launch_and_exit_receipts_reject_identity_or_cause_drift(
        self,
    ) -> None:
        request = self.request()
        launch = {
            "adapter": "codex-cli",
            "attempt_token": request.attempt_token,
            "pid": 11,
            "process_start_identity": "test:11",
            "process_group": 11,
            "child_pid": 12,
            "child_process_start_identity": "test:12",
            "child_process_group": 12,
            "codex_version": request.codex_version,
            "execution_policy_sha256": (
                request.execution_policy_sha256
            ),
        }
        handle = parse_launch_receipt(
            canonical_json_bytes(launch),
            request,
        )
        broken = dict(launch)
        broken["attempt_token"] = "ATTEMPT-WRONG"
        with self.assertRaises(ProviderIdentityError):
            parse_launch_receipt(
                canonical_json_bytes(broken),
                request,
            )

        base_exit = {
            **launch,
            "exit_code": 0,
            "timed_out": False,
            "result_published": True,
            "stop_requested": False,
            "stop_forced": False,
        }
        invalid = (
            {"exit_code": 1, "result_published": True},
            {"exit_code": 0, "timed_out": True},
            {
                "exit_code": 143,
                "stop_requested": True,
                "result_published": True,
            },
            {
                "exit_code": 137,
                "stop_requested": False,
                "stop_forced": True,
                "result_published": False,
            },
            {"exit_code": 0, "result_published": False},
        )
        for patch in invalid:
            value = {**base_exit, **patch}
            with self.subTest(patch=patch), self.assertRaises(
                ContractError
            ):
                parse_exit_receipt(
                    canonical_json_bytes(value),
                    handle,
                )

    def test_process_start_identity_is_stable_for_current_process(
        self,
    ) -> None:
        first = process_start_identity(os.getpid())
        self.assertEqual(first, process_start_identity(os.getpid()))
        self.assertTrue(
            first.startswith(("linux:", "darwin:"))
        )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    return True


if __name__ == "__main__":
    unittest.main()
