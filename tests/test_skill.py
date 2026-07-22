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
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        cls.changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        cls.agent_metadata = (ROOT / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )

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
        self.assertLessEqual(len(words), 1000)

    def test_root_only_orchestrates_and_roles_are_serial(self) -> None:
        self.assertIn("Root never writes business code", self.skill)
        self.assertIn("Generator is the only business-code writer", self.skill)
        self.assertIn("roles run serially", self.skill)
        self.assertIn("spawn_agent", self.skill)
        self.assertIn("followup_task", self.skill)
        self.assertIn("wait_agent", self.skill)
        self.assertIn("Planner runs once", self.skill)
        self.assertIn("Reuse the requirement's Generator", self.skill)
        self.assertIn("Reuse it with `followup_task`", self.skill)

    def test_role_prompts_require_autonomous_verified_progress(self) -> None:
        required_contracts = (
            "ordered independently verifiable work units",
            "success signal",
            "canonical verifier",
            "optional fast check",
            "broader regression/public-path check",
            "actionable failure output",
            "Loop: choose the smallest unfinished step",
            "fastest deterministic check",
            "Stop only when every `AC-NNN` has implementation and verification",
            "partial improvement, focused `PASS`, or unrelated failure alone do not stop",
            "regression/public-path checks",
            "large-log path, digest, actionable lines",
            "Verify checks exercise each `AC-NNN`, inspect output, and cover regressions",
            "Tests mirroring assumptions or skipping the public path are insufficient",
            "`UNVERIFIED` unless evidence distinguishes correct from plausible incorrect behavior",
            "Reference, never create, SHA-256-bound logs",
        )
        for contract in required_contracts:
            self.assertIn(contract, self.skill)
        self.assertNotIn("until perfect", self.skill)

    def test_skill_uses_explicit_schema_v3_evaluation_commands(self) -> None:
        for command in (
            "snapshot",
            "begin-evaluation",
            "record-review",
            "restart-evaluation",
            "accept",
            "check --final",
        ):
            self.assertIn(f"`{command}`", self.skill)
        self.assertIn("schema 3", self.skill)
        self.assertNotIn("last_good_revision", self.skill)
        self.assertNotIn("sets `latest_verdict", self.skill)
        self.assertNotIn("sets `status=ACCEPTED`", self.skill)

    def test_skill_requires_stable_acceptance_ids_and_evaluation_json(self) -> None:
        self.assertIn("`## Acceptance criteria`", self.skill)
        self.assertIn("`AC-NNN`", self.skill)
        self.assertIn("`## Evaluation record`", self.skill)
        self.assertIn("workspace_fingerprint", self.skill)
        for field in (
            "requirement_id",
            "round",
            "goal_sha256",
            "plan_sha256",
            "implementation_sha256",
        ):
            self.assertIn(f'"{field}"', self.skill)
        self.assertIn("Schema 2", self.skill)
        self.assertIn('"verdict": "PASS"', self.skill)
        self.assertIn('"kind": "command"', self.skill)
        self.assertIn('"observations"', self.skill)
        self.assertIn('"kind": "metric"', self.skill)
        self.assertIn("typed `exact`, `metric`", self.skill)
        self.assertIn("`artifact` observations", self.skill)
        self.assertNotIn('"result":', self.skill)
        self.assertNotIn('"overall_verdict"', self.skill)
        self.assertNotIn('"type": "command"', self.skill)
        self.assertNotIn("`## Overall verdict`", self.skill)
        self.assertNotIn("`## Evidence`", self.skill)

    def test_read_only_roles_are_audited_not_claimed_as_sandboxed(self) -> None:
        self.assertIn("instruction-level read-only", self.skill)
        self.assertIn("`snapshot` before and after", self.skill)
        self.assertIn("record `BLOCKED`", self.skill)
        self.assertIn("do not attribute the writer without evidence", self.skill)
        self.assertNotIn("independent read-only Evaluator", self.skill)

    def test_evaluation_transaction_uses_runtime_as_the_only_writer(self) -> None:
        begin_position = self.skill.index("`begin-evaluation`")
        record_position = self.skill.index("`record-review`")
        accept_position = self.skill.index("`accept`")
        final_position = self.skill.index("`check --final`")
        self.assertLess(begin_position, record_position)
        self.assertLess(record_position, accept_position)
        self.assertLess(accept_position, final_position)
        self.assertIn("never hand-edit transaction fields", self.skill)
        self.assertIn("outside `TARGET_ROOT`", self.skill)

    def test_crash_recovery_reconciles_a_complete_review(self) -> None:
        self.assertIn("`pending_evaluation`", self.skill)
        self.assertIn("rerun `begin-evaluation`", self.skill)
        self.assertIn(
            "`init --resume` intentionally does not reconcile this marker",
            self.skill,
        )
        self.assertIn("runtime-prepared pending review", self.skill)
        self.assertIn("`init --resume` reconciles", self.skill)
        self.assertIn("archives prior exact review bytes", self.skill)
        self.assertIn("digest-bound in history", self.skill)

    def test_goal_gate_rejects_every_kind_of_workspace_drift(self) -> None:
        self.assertIn("raw tracked, staged, unstaged", self.skill)
        self.assertIn("Only structured `PASS` may run `accept`", self.skill)
        self.assertIn("transaction-input or review drift", self.skill)
        self.assertIn("`check --final` rechecks all hashes and receipts", self.skill)
        self.assertIn("run `restart-evaluation`", self.skill)
        self.assertIn("--reason", self.skill)

    def test_user_visible_pass_requires_direct_real_path_evidence(self) -> None:
        self.assertIn("evaluated revision's public entrypoint", self.skill)
        self.assertIn("unit-only or mocked evidence is `UNVERIFIED`", self.skill)
        self.assertIn("Replace weak `PASS` through `record-review`", self.skill)
        self.assertIn("pressure cannot supply evidence", self.skill)

    def test_blocked_and_degraded_states_cannot_mutate_evaluation_truth(self) -> None:
        self.assertIn(
            "edit only ordinary orchestration fields: `status`, `next_action`",
            self.skill,
        )
        self.assertIn("and `residual_risks`", self.skill)
        self.assertIn(
            "leave `evaluation`, `accepted_revision`, `latest_verdict`, and review bytes unchanged",
            self.skill,
        )
        self.assertIn("`DEGRADED` is not `ACCEPTED`", self.skill)
        self.assertIn("never runs `accept` or `check --final`", self.skill)

    def test_requirement_artifacts_and_recovery_handles_remain_durable(self) -> None:
        self.assertIn("requirements/REQ-NNN", self.skill)
        self.assertIn("rounds/NNN/implementation.md", self.skill)
        self.assertIn("evaluation-inputs/", self.skill)
        self.assertIn("attempts/", self.skill)
        self.assertIn("review.md", self.skill)
        self.assertIn("interruption.json", self.skill)
        self.assertNotIn("progress.md", self.skill)
        self.assertIn("Use `list_agents` on the current Root agent tree", self.skill)
        self.assertIn(
            "handle is in that tree and `followup_task` can address it",
            self.skill,
        )
        self.assertNotIn("agent_id", self.skill)
        self.assertIn("`BUILDING`: spawn a replacement Generator", self.skill)
        self.assertIn("`EVALUATING`: spawn a replacement Evaluator", self.skill)

    def test_public_docs_describe_the_breaking_schema_v3_workflow(self) -> None:
        for document in (self.readme, self.readme_zh):
            self.assertIn("schema 3", document.lower())
            self.assertIn("python3 scripts/harness.py snapshot", document)
            self.assertIn("begin-evaluation", document)
            self.assertIn("record-review", document)
            self.assertIn("restart-evaluation", document)
            self.assertIn("accept", document)
            self.assertIn("--final", document)
            self.assertIn("outside", document.lower())
            self.assertIn("evaluation-inputs/", document)
            self.assertIn("attempts/", document)
            self.assertIn("prepared", document.lower())
            self.assertIn("pending_evaluation", document)
            self.assertIn("init --resume", document)
            self.assertIn("999", document)
        self.assertNotIn("\npython scripts/harness.py", self.readme)
        self.assertNotIn("\npython scripts/harness.py", self.readme_zh)

    def test_changelog_and_agent_metadata_publish_the_new_contract(self) -> None:
        self.assertIn("schema 3 evaluation transactions", self.changelog)
        self.assertIn("schema 2 state is intentionally unsupported", self.changelog)
        self.assertIn("complete workspace fingerprint", self.changelog)
        self.assertIn("restart-evaluation", self.changelog)
        self.assertIn("exact transaction inputs", self.changelog)
        self.assertIn("state-first prepared markers", self.changelog)
        self.assertIn("evaluation-input", self.changelog)
        self.assertIn("failed evaluations", self.changelog)
        self.assertIn(
            'short_description: "Run durable, snapshot-bound coding goals"',
            self.agent_metadata,
        )
        self.assertIn("schema 3 evaluation records", self.agent_metadata)
        self.assertIn("exact transaction inputs", self.agent_metadata)
        self.assertIn("hash-bound attempts and receipts", self.agent_metadata)
        self.assertIn("exact evidenced repository snapshot", self.agent_metadata)

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
