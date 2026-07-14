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

    def test_pass_records_the_verified_head_before_acceptance(self) -> None:
        step = next(
            line for line in self.skill.splitlines() if line.startswith("- `PASS`:")
        )
        fragments = (
            "Evaluator returns",
            "writes the current `review.md`",
            "sets `latest_verdict=PASS`",
            "`last_good_revision` to the canonical full commit OID",
            "must equal current `HEAD`",
            "keeps `phase=EVALUATING`",
            "confirms Goal and evidence",
            "sets `status=ACCEPTED`",
            "runs `check --final`",
        )
        for fragment in fragments:
            self.assertIn(fragment, step)
        positions = [step.index(fragment) for fragment in fragments]
        self.assertEqual(positions, sorted(positions))

    def test_unverified_preserves_round_and_updates_one_machine_view(self) -> None:
        step = next(
            line for line in self.skill.splitlines() if line.startswith("- `UNVERIFIED`:")
        )
        self.assertIn("sets `latest_verdict=UNVERIFIED`", step)
        self.assertIn("keeps the same `active_round`", step)
        self.assertIn("keeps `phase=EVALUATING`", step)
        self.assertIn("`followup_task`", step)
        self.assertIn(
            "Keep exactly one `## Overall verdict` machine section", self.skill
        )
        self.assertIn(
            "at most one `## Evidence` machine section", self.skill
        )
        self.assertIn("one `## Attempts` section", self.skill)
        self.assertIn("`### Attempt N`", self.skill)
        self.assertIn(
            "update the single verdict and evidence sections", self.skill
        )
        self.assertNotIn(
            "append the next evaluation attempt to the same review file", self.skill
        )

    def test_resume_reuses_only_addressable_handles_in_the_current_tree(self) -> None:
        self.assertIn(
            "Use `list_agents` on the current Root agent tree", self.skill
        )
        self.assertIn(
            "handle is in that tree and `followup_task` can address it", self.skill
        )
        self.assertIn(
            "absent from the tree, unaddressable, or rejected by an explicit "
            "`followup_task` failure is unusable",
            self.skill,
        )
        self.assertNotIn("agent_id", self.skill)

    def test_resume_replaces_the_role_for_the_current_phase(self) -> None:
        self.assertIn(
            "`PLANNING`: spawn a replacement Planner only if `plan.md` is absent",
            self.skill,
        )
        self.assertIn("`BUILDING`: spawn a replacement Generator", self.skill)
        self.assertIn("`EVALUATING`: spawn a replacement Evaluator", self.skill)
        self.assertIn(
            "Replay every artifact that exists: `state.json`, `plan.md`, the current "
            "round artifact, the previous `review.md` for repairs, Git status and "
            "revision, and repository instructions",
            self.skill,
        )
        self.assertIn("then use `wait_agent`", self.skill)
        self.assertIn(
            "Recovery replacement is an interruption exception", self.skill
        )
        self.assertIn(
            "not normal-round role recreation or a Planner rerun", self.skill
        )

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
