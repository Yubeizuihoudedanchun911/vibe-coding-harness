from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness.py"


class HarnessCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.target = Path(self.temporary_directory.name)
        self._git("init", "-q")
        (self.target / "README.md").write_text("# Fixture\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git(
            "-c",
            "user.name=Harness Test",
            "-c",
            "user.email=harness@example.com",
            "commit",
            "-qm",
            "initial fixture",
        )
        self.revision = self._git("rev-parse", "HEAD").stdout.strip()

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.target), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    def _run_harness(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HARNESS), *arguments, "--target", str(self.target)],
            check=False,
            capture_output=True,
            text=True,
        )

    @property
    def requirements_root(self) -> Path:
        return self.target / ".vibe-coding" / "requirements"

    def _requirement_root(self, requirement_id: str = "REQ-001") -> Path:
        return self.requirements_root / requirement_id

    def _state_path(self, requirement_id: str = "REQ-001") -> Path:
        return self._requirement_root(requirement_id) / "state.json"

    def _init(self, goal: str = "Add --version") -> subprocess.CompletedProcess[str]:
        return self._run_harness("init", "--goal", goal)

    def _load_state(self, requirement_id: str = "REQ-001") -> dict[str, object]:
        return json.loads(self._state_path(requirement_id).read_text(encoding="utf-8"))

    def _write_state(self, requirement_id: str, state: dict[str, object]) -> None:
        self._state_path(requirement_id).write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_init_creates_only_requirement_state(self) -> None:
        result = self._init("Add --version")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["requirement_id"], "REQ-001")
        created = sorted(
            path.relative_to(self.target).as_posix()
            for path in (self.target / ".vibe-coding").rglob("*")
            if path.is_file()
        )
        self.assertEqual(
            created,
            [".vibe-coding/requirements/REQ-001/state.json"],
        )
        state = self._load_state()
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["status"], "ACTIVE")
        self.assertEqual(state["phase"], "PLANNING")
        self.assertEqual(state["active_round"], 1)
        self.assertIsNone(state["latest_verdict"])

    def test_init_allocates_monotonic_requirement_ids(self) -> None:
        self.assertEqual(self._init("First goal").returncode, 0)
        self.assertEqual(self._init("Second goal").returncode, 0)

        self.assertEqual(self._load_state("REQ-001")["goal"], "First goal")
        self.assertEqual(self._load_state("REQ-002")["goal"], "Second goal")

    def test_resume_requires_an_id_when_multiple_requirements_are_nonterminal(
        self,
    ) -> None:
        self.assertEqual(self._init("First goal").returncode, 0)
        self.assertEqual(self._init("Second goal").returncode, 0)

        ambiguous = self._run_harness("init", "--resume")
        selected = self._run_harness(
            "init", "--resume", "--requirement", "REQ-002"
        )

        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertIn("multiple nonterminal requirements", ambiguous.stderr)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(json.loads(selected.stdout)["requirement_id"], "REQ-002")

    def test_resume_auto_selects_the_only_nonterminal_requirement(self) -> None:
        self.assertEqual(self._init("First goal").returncode, 0)
        first = self._load_state("REQ-001")
        first["status"] = "ACCEPTED"
        self._write_state("REQ-001", first)
        self.assertEqual(self._init("Second goal").returncode, 0)

        result = self._run_harness("init", "--resume")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["requirement_id"], "REQ-002")

    def test_init_refuses_to_write_while_another_init_holds_the_lock(self) -> None:
        control_root = self.target / ".vibe-coding"
        control_root.mkdir()
        (control_root / ".init.lock").write_text("held\n", encoding="utf-8")

        result = self._init("Concurrent goal")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another init is running", result.stderr)
        self.assertFalse(self.requirements_root.exists())

    def test_resume_rejects_a_different_goal_without_changing_state(self) -> None:
        self.assertEqual(self._init("Original goal").returncode, 0)
        original_state = self._state_path().read_text(encoding="utf-8")

        mismatch = self._run_harness("init", "--resume", "--goal", "Other goal")
        matching = self._run_harness("init", "--resume", "--goal", "Original goal")

        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("does not match", mismatch.stderr)
        self.assertEqual(matching.returncode, 0, matching.stderr)
        self.assertEqual(self._state_path().read_text(encoding="utf-8"), original_state)


if __name__ == "__main__":
    unittest.main()
