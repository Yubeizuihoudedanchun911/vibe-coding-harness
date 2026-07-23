from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support.git_repo import GitRepositoryFixture
from vibe.worktrees import repository_snapshot


class RepositorySnapshotTests(GitRepositoryFixture, unittest.TestCase):
    def setUp(self) -> None:
        self.setUpGitRepository()

    def snapshot(self) -> dict[str, str]:
        return repository_snapshot(self.target).as_dict()

    def test_snapshot_is_stable_and_excludes_control_state(self) -> None:
        before = self.snapshot()
        control = self.target / ".vibe-coding"
        control.mkdir()
        (control / "state.json").write_text("{}\n", encoding="utf-8")
        self.assertEqual(before, self.snapshot())
        self.assertRegex(before["workspace_fingerprint"], r"^sha256:[0-9a-f]{64}$")

    def test_snapshot_detects_tracked_unstaged_content(self) -> None:
        before = self.snapshot()
        (self.target / "README.md").write_text("changed\n", encoding="utf-8")
        self.assertNotEqual(
            before["workspace_fingerprint"],
            self.snapshot()["workspace_fingerprint"],
        )

    def test_snapshot_detects_staged_content(self) -> None:
        before = self.snapshot()
        (self.target / "README.md").write_text("staged\n", encoding="utf-8")
        self.git("add", "README.md")
        self.assertNotEqual(
            before["workspace_fingerprint"],
            self.snapshot()["workspace_fingerprint"],
        )

    def test_snapshot_detects_nonignored_untracked_content_and_bytes(self) -> None:
        before = self.snapshot()
        payload = self.target / "new.bin"
        payload.write_bytes(b"\x00one")
        first = self.snapshot()
        payload.write_bytes(b"\x00two")
        second = self.snapshot()
        self.assertNotEqual(before["workspace_fingerprint"], first["workspace_fingerprint"])
        self.assertNotEqual(first["workspace_fingerprint"], second["workspace_fingerprint"])

    def test_snapshot_excludes_ignored_content(self) -> None:
        (self.target / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore cache")
        before = self.snapshot()
        (self.target / "cache.ignored").write_text("ignored\n", encoding="utf-8")
        self.assertEqual(before, self.snapshot())

    def test_snapshot_disables_textconv_helpers_and_hashes_exact_bytes(self) -> None:
        marker = self.target.parent / f"{self.target.name}-textconv-called"
        converter = self.target.parent / f"{self.target.name}-textconv"
        converter.write_text(
            (
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('called', encoding='utf-8')\n"
                "print('constant')\n"
            ),
            encoding="utf-8",
        )
        converter.chmod(0o755)
        self.addCleanup(converter.unlink, missing_ok=True)
        self.addCleanup(marker.unlink, missing_ok=True)
        (self.target / ".gitattributes").write_text(
            "payload.dat diff=constant\n",
            encoding="utf-8",
        )
        payload = self.target / "payload.dat"
        payload.write_bytes(b"original")
        self.git("config", "diff.constant.textconv", str(converter))
        self.git("add", ".gitattributes", "payload.dat")
        self.git("commit", "-m", "add textconv")
        payload.write_bytes(b"changed1")
        first = self.snapshot()
        payload.write_bytes(b"changed2")
        second = self.snapshot()
        self.assertNotEqual(first["workspace_fingerprint"], second["workspace_fingerprint"])
        self.assertFalse(marker.exists())

    def test_snapshot_hashes_raw_tracked_bytes_before_clean_filters(self) -> None:
        converter = self.target.parent / f"{self.target.name}-clean"
        converter.write_text(
            (
                f"#!{sys.executable}\n"
                "import sys\n"
                "sys.stdin.buffer.read()\n"
                "sys.stdout.buffer.write(b'constant canonical content\\n')\n"
            ),
            encoding="utf-8",
        )
        converter.chmod(0o755)
        self.addCleanup(converter.unlink, missing_ok=True)
        (self.target / ".gitattributes").write_text(
            "payload.dat filter=constant\n",
            encoding="utf-8",
        )
        payload = self.target / "payload.dat"
        payload.write_bytes(b"original")
        self.git("config", "filter.constant.clean", str(converter))
        self.git("config", "filter.constant.required", "true")
        self.git("add", ".gitattributes", "payload.dat")
        self.git("commit", "-m", "add filter")
        payload.write_bytes(b"first!!!")
        first = self.snapshot()
        payload.write_bytes(b"second!!")
        second = self.snapshot()
        self.assertNotEqual(first["workspace_fingerprint"], second["workspace_fingerprint"])

    def test_snapshot_hashes_assume_unchanged_tracked_bytes(self) -> None:
        self.git("update-index", "--assume-unchanged", "README.md")
        (self.target / "README.md").write_text("first\n", encoding="utf-8")
        first = self.snapshot()
        (self.target / "README.md").write_text("other\n", encoding="utf-8")
        second = self.snapshot()
        self.assertNotEqual(first["workspace_fingerprint"], second["workspace_fingerprint"])

    def test_snapshot_hashes_dirty_submodule_content_recursively(self) -> None:
        source_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(source_tmp.cleanup)
        source = Path(source_tmp.name)
        subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
        (source / "nested.txt").write_text("original\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "nested.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=Vibe Tests",
                "-c",
                "user.email=vibe-tests@example.invalid",
                "commit",
                "-qm",
                "submodule base",
            ],
            check=True,
        )
        self.git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(source),
            "vendor/sub",
        )
        self.git("add", ".gitmodules", "vendor/sub")
        self.git("commit", "-m", "add submodule")
        nested = self.target / "vendor/sub/nested.txt"
        nested.write_text("changed1\n", encoding="utf-8")
        first = self.snapshot()
        nested.write_text("changed2\n", encoding="utf-8")
        second = self.snapshot()
        self.assertNotEqual(first["workspace_fingerprint"], second["workspace_fingerprint"])


if __name__ == "__main__":
    unittest.main()
