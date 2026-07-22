from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "SKILL.md"
HARNESS_PATH = ROOT / "scripts" / "harness.py"


def _load_harness_runtime() -> object:
    name = "_vibe_coding_harness_skill_contract_runtime"
    spec = importlib.util.spec_from_file_location(name, HARNESS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load harness runtime")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HARNESS_RUNTIME = _load_harness_runtime()


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

    def _section(self, heading: str) -> str:
        marker = f"## {heading}\n"
        start = self.skill.index(marker) + len(marker)
        match = re.search(r"^## ", self.skill[start:], flags=re.MULTILINE)
        end = len(self.skill) if match is None else start + match.start()
        return self.skill[start:end]

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
        planner = self._section("Planner: once per requirement")
        generator = self._section("Generator: build rounds")
        evaluator = self._section("Evaluator and review")

        planner_contracts = (
            "smallest ordered independently verifiable work units",
            "ordered independently verifiable work units",
            "explicit dependencies and required execution order",
            "success signal",
            "canonical verifier",
            "optional fast check",
            "broader regression/public-path check",
            "actionable failure output",
            "stable observable behavior or a Goal-required repository invariant",
            "ordinary implementation details are not acceptance criteria",
        )
        generator_contracts = (
            "Loop: choose the smallest unfinished step",
            "highest-signal",
            "fastest deterministic check",
            "relevant to the current step",
            "Stop only when every `AC-NNN` has implementation and verification",
            "prevents further safe progress",
            "Finish all independent safe work first",
            "partial improvement, focused `PASS`, or unrelated failure alone do not stop",
            "regression/public-path checks",
            "large-log path, digest, actionable lines",
            "compact summary and SHA-256-bound Artifact",
            "round objective, changed paths, commands/results, unverified items, residual risks, and next verification target",
            "Autonomy does not expand authority",
            "publishing, destructive operations, new permissions, or decisions outside the Goal",
        )
        evaluator_contracts = (
            "Verify checks exercise each `AC-NNN`, inspect output, and cover regressions",
            "Tests mirroring assumptions or skipping the public path are insufficient",
            "`UNVERIFIED` unless evidence distinguishes correct from plausible incorrect behavior",
            "Reference, never create, SHA-256-bound logs",
            "raw evidence",
            "Missing required raw evidence is `UNVERIFIED`",
            "replace every transaction and evidence placeholder",
        )
        for contract in planner_contracts:
            self.assertIn(contract, planner)
        for contract in generator_contracts:
            self.assertIn(contract, generator)
        for contract in evaluator_contracts:
            self.assertIn(contract, evaluator)

        self.assertNotIn("implementation details may be acceptance criteria", planner)
        self.assertNotIn("any concrete external blocker", generator)
        self.assertNotIn("autonomy expands authority", generator)
        self.assertNotIn("until perfect", planner + generator + evaluator)

    def test_evaluator_schema_2_contract_is_exact_and_runtime_valid(self) -> None:
        evaluator = self._section("Evaluator and review")
        match = re.search(r"```json\n(.*?)\n```", evaluator, flags=re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None
        record = json.loads(match.group(1))

        self.assertEqual(
            set(record),
            {
                "schema_version",
                "requirement_id",
                "round",
                "revision",
                "workspace_fingerprint",
                "goal_sha256",
                "plan_sha256",
                "implementation_sha256",
                "verdict",
                "criteria",
                "evidence",
                "residual_risks",
            },
        )
        self.assertEqual(
            [item["verdict"] for item in record["criteria"]],
            ["PASS", "FAIL", "UNVERIFIED"],
        )
        for criterion in record["criteria"]:
            self.assertEqual(set(criterion), {"id", "verdict", "evidence_ids"})
        self.assertEqual(
            set(record["evidence"][0]),
            {"id", "kind", "command", "exit_code", "summary", "observations"},
        )
        self.assertEqual(
            set(record["evidence"][1]),
            {"id", "kind", "subject", "summary", "observations"},
        )
        self.assertEqual(
            [set(item) for item in record["evidence"][0]["observations"]],
            [
                {"kind", "name", "value"},
                {"kind", "name", "value", "unit"},
            ],
        )
        self.assertEqual(
            set(record["evidence"][1]["observations"][0]),
            {"kind", "path", "sha256"},
        )
        self.assertIn(
            "Top verdict is derived from criteria: `FAIL` over `UNVERIFIED` over `PASS`",
            evaluator,
        )

        criterion_ids = ["AC-001", "AC-002", "AC-003"]
        snapshot = HARNESS_RUNTIME.snapshot(ROOT)
        expected_evaluation = {
            "requirement_id": "REQ-001",
            "round": 1,
            "revision": snapshot["revision"],
            "workspace_fingerprint": snapshot["workspace_fingerprint"],
            "goal_sha256": "sha256:" + "1" * 64,
            "plan_sha256": "sha256:" + "2" * 64,
            "implementation_sha256": "sha256:" + "3" * 64,
            "acceptance_criteria": criterion_ids,
        }
        for key in (
            "requirement_id",
            "round",
            "revision",
            "workspace_fingerprint",
            "goal_sha256",
            "plan_sha256",
            "implementation_sha256",
        ):
            record[key] = expected_evaluation[key]

        record["evidence"][0].update(
            {
                "command": "python3 -m unittest tests.test_skill",
                "exit_code": 0,
                "summary": "Skill contract tests passed.",
            }
        )
        record["evidence"][0]["observations"][0].update(
            {"name": "selected_role", "value": "Evaluator"}
        )
        record["evidence"][0]["observations"][1].update(
            {"name": "failed_tests", "value": 0, "unit": "tests"}
        )
        record["evidence"][1].update(
            {
                "subject": "tests/test_skill.py",
                "summary": "The executable contract is covered by a focused test.",
            }
        )
        artifact_path = ROOT / "README.md"
        record["evidence"][1]["observations"][0].update(
            {
                "path": "README.md",
                "sha256": "sha256:"
                + hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            }
        )
        body = (
            "# Review\n\n## Evaluation record\n\n```json\n"
            + json.dumps(record, indent=2)
            + "\n```\n"
        )
        accepted = HARNESS_RUNTIME._validated_review(
            body,
            criterion_ids,
            ROOT,
            expected_evaluation,
        )
        self.assertEqual(accepted["verdict"], "FAIL")

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

    def test_lifecycle_shell_blocks_are_self_contained(self) -> None:
        blocks = re.findall(r"```bash\n(.*?)\n```", self.skill, flags=re.DOTALL)
        self.assertEqual(len(blocks), 4)
        for block in blocks:
            variable_call = 'python3 "$HARNESS"' in block
            direct_call = 'python3 "$SKILL_ROOT/scripts/harness.py"' in block
            self.assertTrue(variable_call or direct_call, block)
            if variable_call:
                assignment = 'HARNESS="$SKILL_ROOT/scripts/harness.py"'
                self.assertIn(assignment, block)
                self.assertLess(
                    block.index(assignment), block.index('python3 "$HARNESS"')
                )

    def test_skill_requires_stable_acceptance_ids_and_evaluation_json(self) -> None:
        planner = self._section("Planner: once per requirement")
        evaluator = self._section("Evaluator and review")
        self.assertIn("`## Acceptance criteria`", planner)
        self.assertIn("`AC-NNN`", planner)
        self.assertIn("`## Evaluation record`", evaluator)
        self.assertIn("workspace_fingerprint", evaluator)
        for field in (
            "requirement_id",
            "round",
            "goal_sha256",
            "plan_sha256",
            "implementation_sha256",
        ):
            self.assertIn(f'"{field}"', evaluator)
        self.assertIn("Schema 2", evaluator)
        self.assertIn('"verdict": "PASS"', evaluator)
        self.assertIn('"verdict": "FAIL"', evaluator)
        self.assertIn('"verdict": "UNVERIFIED"', evaluator)
        self.assertIn('"kind": "command"', evaluator)
        self.assertIn('"kind": "inspection"', evaluator)
        self.assertIn('"observations"', evaluator)
        self.assertIn('"kind": "exact"', evaluator)
        self.assertIn('"kind": "metric"', evaluator)
        self.assertIn('"kind": "artifact"', evaluator)
        self.assertNotIn('"result":', evaluator)
        self.assertNotIn('"overall_verdict"', evaluator)
        self.assertNotIn('"type": "command"', evaluator)
        self.assertNotIn("`## Overall verdict`", evaluator)
        self.assertNotIn("`## Evidence`", evaluator)

    def test_read_only_roles_are_audited_not_claimed_as_sandboxed(self) -> None:
        audit = self._section("Snapshot and role audit")
        self.assertIn("Instruction-level read-only", audit)
        self.assertIn("Root runs `snapshot` before and after each role", audit)
        self.assertIn("record `BLOCKED`", audit)
        self.assertIn("do not attribute the writer without evidence", audit)
        self.assertNotIn("before and after roles", audit)
        self.assertNotIn("independent read-only Evaluator", audit)

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
        self.assertIn("duplicate progress logs", self._section("File maintenance"))
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
