from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness.py"
HARNESS_SPEC = importlib.util.spec_from_file_location("harness_under_test", HARNESS)
if HARNESS_SPEC is None or HARNESS_SPEC.loader is None:
    raise RuntimeError(f"cannot load harness module: {HARNESS}")
HARNESS_MODULE = importlib.util.module_from_spec(HARNESS_SPEC)
sys.modules[HARNESS_SPEC.name] = HARNESS_MODULE
HARNESS_SPEC.loader.exec_module(HARNESS_MODULE)


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

    def _round_root(
        self, requirement_id: str = "REQ-001", round_number: int = 1
    ) -> Path:
        return self._requirement_root(requirement_id) / "rounds" / f"{round_number:03d}"

    def _write_artifact(self, relative_path: str, body: str) -> None:
        path = self.target / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def _prepare_accepted_review(self, review_body: str) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            review_body,
        )
        state = self._load_state()
        state.update(
            {
                "status": "ACCEPTED",
                "phase": "EVALUATING",
                "latest_verdict": "PASS",
                "next_action": "Delivery complete.",
                "last_good_revision": self.revision,
            }
        )
        self._write_state("REQ-001", state)

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

    def test_building_requires_a_nonempty_plan(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        state = self._load_state()
        state["phase"] = "BUILDING"
        state["next_action"] = "Dispatch Generator."
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BUILDING requires non-empty plan.md", result.stdout)

    def test_evaluating_requires_current_implementation(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md",
            "# Plan\n\n## Acceptance\n\n- Version is printed.\n",
        )
        state = self._load_state()
        state["phase"] = "EVALUATING"
        state["next_action"] = "Dispatch Evaluator."
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EVALUATING requires current implementation.md", result.stdout)

    def test_failed_review_advances_to_a_build_round_with_previous_evidence(
        self,
    ) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n\n- Commit: abc\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nFAIL\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "BUILDING",
                "active_round": 2,
                "latest_verdict": "FAIL",
                "next_action": "Fix the failed criterion.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_second_round_rejects_a_null_latest_verdict(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nFAIL\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "BUILDING",
                "active_round": 2,
                "latest_verdict": None,
                "next_action": "Dispatch Generator.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "BUILDING after round 1 requires latest_verdict FAIL",
            result.stdout,
        )

    def test_second_round_can_wait_for_review_with_a_null_verdict(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nFAIL\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/002/implementation.md",
            "# Implementation\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "EVALUATING",
                "active_round": 2,
                "latest_verdict": None,
                "next_action": "Dispatch Evaluator.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_evaluating_null_rejects_an_existing_current_review(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\nDraft review.\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "EVALUATING",
                "latest_verdict": None,
                "next_action": "Dispatch Evaluator.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "EVALUATING with null latest_verdict requires current review.md to be absent",
            result.stdout,
        )

    def test_next_round_rejects_a_previous_pass_review(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nPASS\n\n## Evidence\n\nVerified.\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "BUILDING",
                "active_round": 2,
                "latest_verdict": "FAIL",
                "next_action": "Fix the failed criterion.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "history round 001 Overall verdict must be FAIL", result.stdout
        )

    def test_unverified_cannot_advance_without_a_previous_fail(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nUNVERIFIED\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/002/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/002/review.md",
            "# Review\n\n## Overall verdict\n\nUNVERIFIED\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "EVALUATING",
                "active_round": 2,
                "latest_verdict": "UNVERIFIED",
                "next_action": "Collect runtime evidence.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "history round 001 Overall verdict must be FAIL", result.stdout
        )

    def test_round_two_pass_requires_first_round_implementation(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nFAIL\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/002/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/002/review.md",
            "# Review\n\n## Overall verdict\n\nPASS\n\n## Evidence\n\nVerified.\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "EVALUATING",
                "active_round": 2,
                "latest_verdict": "PASS",
                "next_action": "Run Goal Gate.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "history round 001 requires non-empty implementation.md",
            result.stdout,
        )

    def test_round_two_unverified_requires_first_round_implementation(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nFAIL\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/002/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/002/review.md",
            "# Review\n\n## Overall verdict\n\nUNVERIFIED\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "EVALUATING",
                "active_round": 2,
                "latest_verdict": "UNVERIFIED",
                "next_action": "Collect runtime evidence.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "history round 001 requires non-empty implementation.md",
            result.stdout,
        )

    def test_round_three_requires_every_earlier_round(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/002/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/002/review.md",
            "# Review\n\n## Overall verdict\n\nFAIL\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "BUILDING",
                "active_round": 3,
                "latest_verdict": "FAIL",
                "next_action": "Fix the failed criterion.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "history round 001 requires non-empty implementation.md",
            result.stdout,
        )
        self.assertIn(
            "history round 001 requires non-empty review.md", result.stdout
        )

    def test_round_three_rejects_a_non_fail_earlier_review(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nPASS\n\n## Evidence\n\nVerified.\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/002/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/002/review.md",
            "# Review\n\n## Overall verdict\n\nFAIL\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "BUILDING",
                "active_round": 3,
                "latest_verdict": "FAIL",
                "next_action": "Fix the failed criterion.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "history round 001 Overall verdict must be FAIL", result.stdout
        )

    def test_round_three_rejects_an_earlier_artifact_symlink(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nFAIL\n",
        )
        (self._round_root() / "implementation.md").symlink_to(
            "missing-implementation.md"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/002/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/002/review.md",
            "# Review\n\n## Overall verdict\n\nFAIL\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "BUILDING",
                "active_round": 3,
                "latest_verdict": "FAIL",
                "next_action": "Fix the failed criterion.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("implementation.md must not be a symbolic link", result.stdout)

    def test_each_historical_artifact_is_read_once_per_check(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        historical_paths: list[Path] = []
        for round_number in (1, 2):
            implementation = self._round_root(
                round_number=round_number
            ) / "implementation.md"
            review = self._round_root(round_number=round_number) / "review.md"
            self._write_artifact(
                implementation.relative_to(self.target).as_posix(),
                "# Implementation\n",
            )
            self._write_artifact(
                review.relative_to(self.target).as_posix(),
                "# Review\n\n## Overall verdict\n\nFAIL\n",
            )
            historical_paths.extend([implementation, review])
        state = self._load_state()
        state.update(
            {
                "phase": "BUILDING",
                "active_round": 3,
                "latest_verdict": "FAIL",
                "next_action": "Fix the failed criterion.",
            }
        )
        self._write_state("REQ-001", state)
        reads = dict.fromkeys(historical_paths, 0)
        original_read_text = Path.read_text

        def counting_read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path in reads:
                reads[path] += 1
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", counting_read_text):
            result, valid = HARNESS_MODULE.check(self.target, "REQ-001")

        self.assertTrue(valid, result)
        self.assertEqual(set(reads.values()), {1})

    def test_unverified_keeps_the_same_evaluation_round(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nUNVERIFIED\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "EVALUATING",
                "active_round": 1,
                "latest_verdict": "UNVERIFIED",
                "next_action": "Collect runtime evidence.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_final_check_requires_pass_review_and_current_head(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nPASS\n\n## Evidence\n\nCLI output verified.\n",
        )
        state = self._load_state()
        state.update(
            {
                "status": "ACCEPTED",
                "phase": "EVALUATING",
                "latest_verdict": "PASS",
                "next_action": "Delivery complete.",
                "last_good_revision": self.revision,
            }
        )
        self._write_state("REQ-001", state)

        valid = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )
        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)

        (self.target / "AFTER.md").write_text("later\n", encoding="utf-8")
        self._git("add", "AFTER.md")
        self._git(
            "-c",
            "user.name=Harness Test",
            "-c",
            "user.email=harness@example.com",
            "commit",
            "-qm",
            "advance fixture",
        )
        historical = self._run_harness("check", "--requirement", "REQ-001")
        stale_final = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )

        self.assertEqual(historical.returncode, 0, historical.stdout)
        self.assertNotEqual(stale_final.returncode, 0)
        self.assertIn("current HEAD", stale_final.stdout)

    def test_final_check_rejects_pass_without_evidence(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nPASS\n\n## Evidence\n\n",
        )
        state = self._load_state()
        state.update(
            {
                "status": "ACCEPTED",
                "phase": "EVALUATING",
                "latest_verdict": "PASS",
                "next_action": "Delivery complete.",
                "last_good_revision": self.revision,
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PASS review requires evidence", result.stdout)

    def test_pass_in_evidence_cannot_override_overall_fail(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nFAIL\n\n## Evidence\n\nPASS\n",
        )
        state = self._load_state()
        state.update(
            {
                "status": "ACCEPTED",
                "phase": "EVALUATING",
                "latest_verdict": "PASS",
                "next_action": "Delivery complete.",
                "last_good_revision": self.revision,
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "current review Overall verdict must equal latest_verdict PASS",
            result.stdout,
        )

    def test_empty_evidence_is_not_filled_by_following_risks_section(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nPASS\n\n## Evidence\n\n"
            "## Risks\n\nKnown limitation.\n",
        )
        state = self._load_state()
        state.update(
            {
                "status": "ACCEPTED",
                "phase": "EVALUATING",
                "latest_verdict": "PASS",
                "next_action": "Delivery complete.",
                "last_good_revision": self.revision,
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PASS review requires evidence", result.stdout)

    def test_state_verdict_must_match_current_review_overall_verdict(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nPASS\n\n## Evidence\n\nVerified.\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "EVALUATING",
                "latest_verdict": "UNVERIFIED",
                "next_action": "Collect runtime evidence.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "current review Overall verdict must equal latest_verdict UNVERIFIED",
            result.stdout,
        )

    def test_overall_verdict_requires_one_substantive_value(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nPASS\nFAIL\n\n"
            "## Evidence\n\nVerified.\n",
        )
        state = self._load_state()
        state.update(
            {
                "status": "ACCEPTED",
                "phase": "EVALUATING",
                "latest_verdict": "PASS",
                "next_action": "Delivery complete.",
                "last_good_revision": self.revision,
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "current review Overall verdict must equal latest_verdict PASS",
            result.stdout,
        )

    def test_review_headings_inside_a_fence_do_not_count(self) -> None:
        self._prepare_accepted_review(
            "# Review\n\n```markdown\n## Overall verdict\nPASS\n"
            "## Evidence\nFake evidence.\n```\n"
        )

        result = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "current review Overall verdict must equal latest_verdict PASS",
            result.stdout,
        )

    def test_review_headings_inside_an_html_comment_do_not_count(self) -> None:
        self._prepare_accepted_review(
            "# Review\n\n<!--\n## Overall verdict\nPASS\n"
            "## Evidence\nFake evidence.\n-->\n"
        )

        result = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "current review Overall verdict must equal latest_verdict PASS",
            result.stdout,
        )

    def test_empty_evidence_fence_is_not_substantive(self) -> None:
        self._prepare_accepted_review(
            "# Review\n\n## Overall verdict\n\nPASS\n\n"
            "## Evidence\n\n```text\n```\n"
        )

        result = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PASS review requires evidence", result.stdout)

    def test_evidence_html_comment_is_not_substantive(self) -> None:
        self._prepare_accepted_review(
            "# Review\n\n## Overall verdict\n\nPASS\n\n"
            "## Evidence\n\n<!-- placeholder -->\n"
        )

        result = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PASS review requires evidence", result.stdout)

    def test_fenced_command_output_is_substantive_evidence(self) -> None:
        self._prepare_accepted_review(
            "# Review\n\n## Overall verdict\n\nPASS\n\n"
            "## Evidence\n\n```text\n$ pytest -q\n1 passed\n```\n"
        )

        result = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_final_check_rejects_a_fresh_active_requirement(self) -> None:
        self.assertEqual(self._init().returncode, 0)

        result = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("final check requires status ACCEPTED", result.stdout)

    def test_final_check_rejects_an_active_pass_requirement(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nPASS\n\n## Evidence\n\nVerified.\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "EVALUATING",
                "latest_verdict": "PASS",
                "next_action": "Run Goal Gate.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("final check requires status ACCEPTED", result.stdout)

    def test_final_check_rejects_a_degraded_requirement(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        state = self._load_state()
        state.update(
            {
                "status": "DEGRADED",
                "degradation_acceptance": "Accepted by the user.",
                "next_action": "Delivery complete with known gaps.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness(
            "check", "--requirement", "REQ-001", "--final"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("final check requires status ACCEPTED", result.stdout)

    def test_terminal_requirement_requires_a_nonempty_plan(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        state = self._load_state()
        state.update(
            {
                "status": "DEGRADED",
                "degradation_acceptance": "Accepted by the user.",
                "next_action": "Delivery complete with known gaps.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "terminal requirement requires non-empty plan.md", result.stdout
        )

    def test_degraded_requires_user_acceptance(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        state = self._load_state()
        state.update(
            {
                "status": "DEGRADED",
                "next_action": "Ask the user to accept degradation.",
            }
        )
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEGRADED requires degradation_acceptance", result.stdout)

    def test_resume_rejects_an_invalid_last_good_revision(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        state = self._load_state()
        state["last_good_revision"] = "not-a-commit"
        self._write_state("REQ-001", state)

        result = self._run_harness(
            "init", "--resume", "--requirement", "REQ-001"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("last_good_revision does not resolve", result.stderr)

    def test_check_rejects_head_as_last_good_revision(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        state = self._load_state()
        state["last_good_revision"] = "HEAD"
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "last_good_revision must be a canonical full commit OID",
            result.stdout,
        )

    def test_check_rejects_a_branch_as_last_good_revision(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        branch = self._git("symbolic-ref", "--short", "HEAD").stdout.strip()
        state = self._load_state()
        state["last_good_revision"] = branch
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "last_good_revision must be a canonical full commit OID",
            result.stdout,
        )

    def test_check_rejects_a_short_sha_as_last_good_revision(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        state = self._load_state()
        state["last_good_revision"] = self.revision[:12]
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "last_good_revision must be a canonical full commit OID",
            result.stdout,
        )

    def test_check_returns_the_same_state_snapshot_that_it_validates(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        validated_state = self._load_state()
        changed_state = {**validated_state, "goal": "Changed after validation"}

        with mock.patch.object(
            HARNESS_MODULE,
            "_load_state",
            side_effect=[validated_state, changed_state],
        ) as load_state:
            result, valid = HARNESS_MODULE.check(self.target, "REQ-001")

        self.assertTrue(valid, result)
        self.assertEqual(load_state.call_count, 1)
        self.assertEqual(result["goal"], validated_state["goal"])

    def test_resume_returns_the_same_state_snapshot_that_it_validates(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        validated_state = self._load_state()
        changed_state = {**validated_state, "goal": "Changed after validation"}

        with mock.patch.object(
            HARNESS_MODULE,
            "_load_state",
            side_effect=[validated_state, changed_state],
        ) as load_state:
            result = HARNESS_MODULE.init(self.target, None, True, "REQ-001")

        self.assertEqual(load_state.call_count, 1)
        self.assertEqual(result["goal"], validated_state["goal"])

    def test_current_review_is_read_once_per_check(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nPASS\n\n## Evidence\n\nVerified.\n",
        )
        state = self._load_state()
        state.update(
            {
                "status": "ACCEPTED",
                "phase": "EVALUATING",
                "latest_verdict": "PASS",
                "next_action": "Delivery complete.",
                "last_good_revision": self.revision,
            }
        )
        self._write_state("REQ-001", state)
        review_path = self._round_root() / "review.md"
        original_read_text = Path.read_text
        review_reads = 0

        def counting_read_text(path: Path, *args: object, **kwargs: object) -> str:
            nonlocal review_reads
            if path == review_path:
                review_reads += 1
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", counting_read_text):
            result, valid = HARNESS_MODULE.check(self.target, "REQ-001")

        self.assertTrue(valid, result)
        self.assertEqual(review_reads, 1)

    def test_previous_review_is_read_once_per_check(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/plan.md", "# Plan\n"
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
            "# Implementation\n",
        )
        self._write_artifact(
            ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
            "# Review\n\n## Overall verdict\n\nFAIL\n",
        )
        state = self._load_state()
        state.update(
            {
                "phase": "BUILDING",
                "active_round": 2,
                "latest_verdict": "FAIL",
                "next_action": "Fix the failed criterion.",
            }
        )
        self._write_state("REQ-001", state)
        review_path = self._round_root() / "review.md"
        original_read_text = Path.read_text
        review_reads = 0

        def counting_read_text(path: Path, *args: object, **kwargs: object) -> str:
            nonlocal review_reads
            if path == review_path:
                review_reads += 1
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", counting_read_text):
            result, valid = HARNESS_MODULE.check(self.target, "REQ-001")

        self.assertTrue(valid, result)
        self.assertEqual(review_reads, 1)

    def test_init_rejects_legacy_global_state(self) -> None:
        control_root = self.target / ".vibe-coding"
        control_root.mkdir()
        (control_root / "state.json").write_text("{}\n", encoding="utf-8")

        result = self._init()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("legacy global harness state", result.stderr)

    def test_init_rejects_a_requirement_directory_symlink(self) -> None:
        requirements = self.target / ".vibe-coding" / "requirements"
        requirements.mkdir(parents=True)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (requirements / "REQ-001").symlink_to(outside.name)

        result = self._init()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be a symbolic link", result.stderr)

    def test_check_rejects_a_dangling_plan_symlink(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        (self._requirement_root() / "plan.md").symlink_to("missing-plan.md")
        state = self._load_state()
        state["phase"] = "BUILDING"
        state["next_action"] = "Dispatch Generator."
        self._write_state("REQ-001", state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("plan.md must not be a symbolic link", result.stdout)


if __name__ == "__main__":
    unittest.main()
