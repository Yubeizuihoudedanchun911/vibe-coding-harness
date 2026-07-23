from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from vibe.models import ProviderStatus
from vibe.providers.base import ProviderRequest
from vibe.providers.codex_cli import CodexCLIAdapter
from vibe.state_store import canonical_json_bytes


@unittest.skipUnless(
    os.environ.get("VIBE_CODEX_CLI_SMOKE") == "1",
    "set VIBE_CODEX_CLI_SMOKE=1 to run the authenticated Codex smoke test",
)
class CodexCLISmokeTests(unittest.TestCase):
    def test_read_only_json_result(self) -> None:
        self.assertIsNotNone(shutil.which("codex"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            prompt = root / "prompt.md"
            schema = root / "schema.json"
            prompt.write_text(
                'Return {"ok": true} and do not edit files.\n',
                encoding="utf-8",
            )
            schema.write_text(
                '{"type":"object","additionalProperties":false,'
                '"required":["ok"],'
                '"properties":{"ok":{"const":true}}}\n',
                encoding="utf-8",
            )
            adapter = CodexCLIAdapter()
            execution = adapter.execution_identity()
            request = ProviderRequest(
                attempt_token="ATTEMPT-SMOKE",
                role="planner",
                request_path=str(root / "request.json"),
                prompt_path=str(prompt),
                schema_path=str(schema),
                cwd=str(root),
                sandbox="read-only",
                launch_path=str(root / "launch.json"),
                stdout_path=str(root / "stdout.log"),
                stderr_path=str(root / "stderr.log"),
                exit_path=str(root / "exit.json"),
                result_path=str(root / "result.json"),
                timeout_seconds=120,
                codex_version=execution.codex_version,
                execution_policy_sha256=execution.policy_sha256,
            )
            Path(request.request_path).write_bytes(
                canonical_json_bytes(request.as_dict())
            )
            handle = adapter.start(request)
            status = adapter.wait(handle, timeout_seconds=120)
            self.assertEqual(status, ProviderStatus.SUCCEEDED)
            self.assertIn(b'"ok"', adapter.result(handle).body)


if __name__ == "__main__":
    unittest.main()
