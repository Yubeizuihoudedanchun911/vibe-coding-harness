from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "SKILL.md"


class SkillStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = SKILL_PATH.read_text(encoding="utf-8")

    def test_description_is_a_trigger_not_a_workflow_summary(self) -> None:
        frontmatter = self.skill.split("---", 2)[1]
        lines = [line for line in frontmatter.splitlines() if line.strip()]
        self.assertEqual(lines[0], "name: vibe-coding-harness")
        self.assertTrue(lines[1].startswith("description: Use when "))
        self.assertEqual(len(lines), 2)
        self.assertLess(len(lines[1]), 500)

    def test_skill_body_stays_bounded(self) -> None:
        body = self.skill.split("---", 2)[2]
        words = re.findall(r"\b[\w'-]+\b", body)
        self.assertLessEqual(len(words), 900)

    def test_root_only_orchestrates_and_tracks(self) -> None:
        self.assertIn("Root never writes business code", self.skill)
        self.assertIn("Goal Gate", self.skill)
        self.assertIn("state.json", self.skill)

    def test_skill_launches_distinct_role_agents(self) -> None:
        self.assertIn("spawn_agent", self.skill)
        self.assertIn("followup_task", self.skill)
        self.assertIn("wait_agent", self.skill)
        self.assertIn("Planner runs once per requirement", self.skill)
        self.assertIn("Reuse the requirement's Generator", self.skill)
        self.assertIn("Reuse the requirement's Evaluator", self.skill)
        self.assertIn("roles run strictly serially", self.skill)

    def test_requirement_artifacts_replace_global_progress(self) -> None:
        self.assertIn("requirements/REQ-NNN", self.skill)
        self.assertIn("rounds/NNN/implementation.md", self.skill)
        self.assertIn("rounds/NNN/review.md", self.skill)
        self.assertNotIn("progress.md", self.skill)

    def test_evaluation_state_transitions_preserve_live_schema_semantics(self) -> None:
        self.assertIn("sets `latest_verdict=null`", self.skill)
        self.assertIn("current `review.md` does not exist yet", self.skill)
        self.assertIn("sets `phase=BUILDING` and `latest_verdict=FAIL`", self.skill)

    def test_evaluator_uses_machine_readable_review_headings(self) -> None:
        self.assertIn("exact single-line heading `## Overall verdict`", self.skill)
        self.assertIn("only plain-text verdict: `PASS`, `FAIL`, or `UNVERIFIED`", self.skill)
        self.assertIn("exact `## Evidence` section with substantive evidence", self.skill)
        self.assertIn("Do not decorate these machine-readable headings", self.skill)

    def test_bundle_keeps_one_runtime_script_and_no_fixed_role_configs(self) -> None:
        scripts = sorted(path.name for path in (ROOT / "scripts").glob("*.py"))
        bundled_content = [
            path
            for directory in (ROOT / "assets", ROOT / "references")
            if directory.exists()
            for path in directory.rglob("*")
            if path.is_file() and ".weaver" not in path.parts and path.name != ".DS_Store"
        ]
        self.assertEqual(scripts, ["harness.py"])
        self.assertEqual(bundled_content, [])


if __name__ == "__main__":
    unittest.main()
