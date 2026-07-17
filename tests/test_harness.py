from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness.py"


def _load_harness_runtime() -> object:
    name = "_vibe_coding_harness_runtime"
    spec = importlib.util.spec_from_file_location(name, HARNESS)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load harness runtime")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HARNESS_RUNTIME = _load_harness_runtime()


class HarnessCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.target = Path(self.temporary_directory.name)
        self.review_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.review_directory.cleanup)
        self.review_root = Path(self.review_directory.name)

        self._git("init", "-q")
        (self.target / "README.md").write_text("# Fixture\n", encoding="utf-8")
        (self.target / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
        self._git("add", "README.md", ".gitignore")
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

    def _round_root(
        self, requirement_id: str = "REQ-001", round_number: int = 1
    ) -> Path:
        return self._requirement_root(requirement_id) / "rounds" / f"{round_number:03d}"

    def _review_path(
        self, requirement_id: str = "REQ-001", round_number: int = 1
    ) -> Path:
        return self._round_root(requirement_id, round_number) / "review.md"

    def _interruption_path(
        self, requirement_id: str = "REQ-001", round_number: int = 1
    ) -> Path:
        return self._round_root(requirement_id, round_number) / "interruption.json"

    def _attempt_path(
        self,
        sequence: int = 1,
        requirement_id: str = "REQ-001",
        round_number: int = 1,
    ) -> Path:
        return (
            self._round_root(requirement_id, round_number)
            / "attempts"
            / f"{sequence:03d}.md"
        )

    def _init(self, goal: str = "Add --version") -> subprocess.CompletedProcess[str]:
        return self._run_harness("init", "--goal", goal)

    def _load_state(self, requirement_id: str = "REQ-001") -> dict[str, object]:
        return json.loads(self._state_path(requirement_id).read_text(encoding="utf-8"))

    def _write_state(
        self, state: dict[str, object], requirement_id: str = "REQ-001"
    ) -> None:
        self._state_path(requirement_id).write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _stage_pending_review(self, source: Path) -> None:
        state = self._load_state()
        body = source.read_text(encoding="utf-8")
        evaluation = state["evaluation"]
        assert isinstance(evaluation, dict)
        state["pending_review"] = {
            "round": state["active_round"],
            "body": body,
            "sha256": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "prior_review_sha256": evaluation["review_sha256"],
        }
        self._write_state(state)

    def _stage_pending_interruption(
        self,
        interruption: dict[str, object],
    ) -> None:
        state = self._load_state()
        state["pending_interruption"] = interruption
        self._write_state(state)

    def _write_artifact(self, relative_path: str, body: str) -> Path:
        path = self.target / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def _plan_body(self, criteria: list[tuple[str, str]] | None = None) -> str:
        values = criteria or [
            ("AC-001", "The command reports a version."),
            ("AC-002", "The full test suite passes."),
        ]
        lines = ["# Plan", "", "## Acceptance criteria", ""]
        lines.extend(f"- {criterion_id}: {description}" for criterion_id, description in values)
        return "\n".join(lines) + "\n"

    def _write_plan(
        self,
        body: str | None = None,
        requirement_id: str = "REQ-001",
    ) -> None:
        self._write_artifact(
            f".vibe-coding/requirements/{requirement_id}/plan.md",
            body if body is not None else self._plan_body(),
        )

    def _write_implementation(
        self,
        round_number: int = 1,
        requirement_id: str = "REQ-001",
    ) -> None:
        self._write_artifact(
            (
                f".vibe-coding/requirements/{requirement_id}/rounds/"
                f"{round_number:03d}/implementation.md"
            ),
            "# Implementation\n\nImplemented and tested.\n",
        )

    def _enter_building(self, requirement_id: str = "REQ-001") -> None:
        state = self._load_state(requirement_id)
        state["phase"] = "BUILDING"
        state["next_action"] = "Dispatch Generator."
        self._write_state(state, requirement_id)

    def _snapshot(self) -> dict[str, str]:
        result = self._run_harness("snapshot")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def _begin_evaluation(
        self, requirement_id: str = "REQ-001"
    ) -> subprocess.CompletedProcess[str]:
        return self._run_harness(
            "begin-evaluation",
            "--requirement",
            requirement_id,
        )

    def _prepare_evaluation(self) -> dict[str, str]:
        self.assertEqual(self._init().returncode, 0)
        self._write_plan()
        self._enter_building()
        self._write_implementation()
        result = self._begin_evaluation()
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["evaluation"]

    def _record(
        self,
        review_source: Path,
        requirement_id: str = "REQ-001",
    ) -> subprocess.CompletedProcess[str]:
        return self._run_harness(
            "record-review",
            "--requirement",
            requirement_id,
            "--review-source",
            str(review_source),
        )

    def _restart(
        self,
        reason: str,
        requirement_id: str = "REQ-001",
    ) -> subprocess.CompletedProcess[str]:
        return self._run_harness(
            "restart-evaluation",
            "--requirement",
            requirement_id,
            "--reason",
            reason,
        )

    def _record_payload(
        self,
        snapshot: dict[str, str],
        verdict: str = "PASS",
        *,
        revision: str | None = None,
        workspace_fingerprint: str | None = None,
        criteria: list[dict[str, object]] | None = None,
        evidence: list[dict[str, object]] | None = None,
        residual_risks: list[str] | None = None,
    ) -> dict[str, object]:
        if criteria is None:
            criterion_verdicts = {
                "PASS": ("PASS", "PASS"),
                "FAIL": ("FAIL", "PASS"),
                "UNVERIFIED": ("UNVERIFIED", "PASS"),
            }[verdict]
            criteria = [
                {
                    "id": "AC-001",
                    "verdict": criterion_verdicts[0],
                    "evidence_ids": ["EV-001"],
                },
                {
                    "id": "AC-002",
                    "verdict": criterion_verdicts[1],
                    "evidence_ids": ["EV-002"],
                },
            ]
        if evidence is None:
            evidence = [
                {
                    "id": "EV-001",
                    "kind": "command",
                    "command": "python -m product --version",
                    "exit_code": 0,
                    "summary": "The version command returned the product version.",
                    "observations": [
                        {
                            "kind": "exact",
                            "name": "stdout",
                            "value": "product 3.0.0",
                        }
                    ],
                },
                {
                    "id": "EV-002",
                    "kind": "inspection",
                    "subject": "full test report",
                    "summary": "The full report records passing tests and no failures.",
                    "observations": [
                        {
                            "kind": "metric",
                            "name": "passed_tests",
                            "value": 42,
                            "unit": "tests",
                        },
                        {
                            "kind": "metric",
                            "name": "failed_tests",
                            "value": 0,
                            "unit": "tests",
                        },
                    ],
                },
            ]
        return {
            "schema_version": 2,
            "requirement_id": snapshot["requirement_id"],
            "round": snapshot["round"],
            "revision": revision or snapshot["revision"],
            "workspace_fingerprint": (
                workspace_fingerprint or snapshot["workspace_fingerprint"]
            ),
            "goal_sha256": snapshot["goal_sha256"],
            "plan_sha256": snapshot["plan_sha256"],
            "implementation_sha256": snapshot["implementation_sha256"],
            "verdict": verdict,
            "criteria": criteria,
            "evidence": evidence,
            "residual_risks": residual_risks or [],
        }

    def _review_body(self, record: dict[str, object]) -> str:
        return (
            "# Review\n\n"
            "## Evaluation record\n\n"
            "```json\n"
            + json.dumps(record, ensure_ascii=False, indent=2)
            + "\n```\n"
        )

    def _review_source(
        self,
        snapshot: dict[str, str],
        verdict: str = "PASS",
        *,
        name: str = "review.md",
        **overrides: object,
    ) -> Path:
        record = self._record_payload(snapshot, verdict, **overrides)
        path = self.review_root / name
        path.write_text(self._review_body(record), encoding="utf-8")
        return path

    def _prepare_pass(self) -> tuple[dict[str, str], Path]:
        snapshot = self._prepare_evaluation()
        source = self._review_source(snapshot)
        result = self._record(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        return snapshot, source

    def test_init_creates_schema_v3_state_without_legacy_revision(self) -> None:
        result = self._init()

        self.assertEqual(result.returncode, 0, result.stderr)
        state = self._load_state()
        self.assertEqual(state["schema_version"], 3)
        self.assertEqual(state["accepted_revision"], "")
        self.assertIsNone(state["evaluation"])
        self.assertIsNone(state["pending_evaluation"])
        self.assertIsNone(state["pending_review"])
        self.assertIsNone(state["pending_interruption"])
        self.assertNotIn("last_good_revision", state)
        self.assertEqual(state["phase"], "PLANNING")

    def test_schema_v2_state_is_rejected_without_migration(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        state = self._load_state()
        state["schema_version"] = 2
        state["last_good_revision"] = self.revision
        self._write_state(state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema_version must be 3", result.stdout)
        self.assertIn("last_good_revision is not supported", result.stdout)

    def test_snapshot_is_stable_and_excludes_harness_state(self) -> None:
        before = self._snapshot()
        self.assertEqual(self._init().returncode, 0)
        self._state_path().write_text(
            self._state_path().read_text(encoding="utf-8") + " \n",
            encoding="utf-8",
        )

        after = self._snapshot()

        self.assertEqual(before, after)
        self.assertEqual(before["revision"], self.revision)
        self.assertRegex(before["workspace_fingerprint"], r"^sha256:[0-9a-f]{64}$")

    def test_snapshot_detects_tracked_unstaged_content(self) -> None:
        before = self._snapshot()
        (self.target / "README.md").write_text("# Changed\n", encoding="utf-8")

        self.assertNotEqual(
            before["workspace_fingerprint"],
            self._snapshot()["workspace_fingerprint"],
        )

    def test_snapshot_detects_staged_content(self) -> None:
        before = self._snapshot()
        (self.target / "README.md").write_text("# Staged\n", encoding="utf-8")
        self._git("add", "README.md")

        self.assertNotEqual(
            before["workspace_fingerprint"],
            self._snapshot()["workspace_fingerprint"],
        )

    def test_snapshot_detects_nonignored_untracked_content_and_bytes(self) -> None:
        before = self._snapshot()
        path = self.target / "new.bin"
        path.write_bytes(b"\x00one")
        first = self._snapshot()
        path.write_bytes(b"\x00two")
        second = self._snapshot()

        self.assertNotEqual(before["workspace_fingerprint"], first["workspace_fingerprint"])
        self.assertNotEqual(first["workspace_fingerprint"], second["workspace_fingerprint"])

    def test_snapshot_excludes_ignored_content(self) -> None:
        before = self._snapshot()
        (self.target / "cache.ignored").write_text("ignored\n", encoding="utf-8")

        self.assertEqual(before, self._snapshot())

    def test_snapshot_disables_textconv_helpers_and_hashes_exact_bytes(self) -> None:
        marker = self.review_root / "textconv-called"
        converter = self.review_root / "constant-textconv"
        converter.write_text(
            (
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('called', encoding='utf-8')\n"
                "print('constant output')\n"
            ),
            encoding="utf-8",
        )
        converter.chmod(0o755)
        (self.target / ".gitattributes").write_text(
            "payload.dat diff=constant\n",
            encoding="utf-8",
        )
        payload = self.target / "payload.dat"
        payload.write_bytes(b"original")
        self._git("config", "diff.constant.textconv", str(converter))
        self._git("add", ".gitattributes", "payload.dat")
        self._git(
            "-c",
            "user.name=Harness Test",
            "-c",
            "user.email=harness@example.com",
            "commit",
            "-qm",
            "add textconv fixture",
        )

        payload.write_bytes(b"changed1")
        first = self._snapshot()
        marker.unlink(missing_ok=True)
        payload.write_bytes(b"changed2")
        second = self._snapshot()

        self.assertNotEqual(
            first["workspace_fingerprint"],
            second["workspace_fingerprint"],
        )
        self.assertFalse(marker.exists(), "snapshot must not execute textconv")

    def test_snapshot_hashes_raw_tracked_bytes_before_clean_filters(self) -> None:
        converter = self.review_root / "constant-clean-filter"
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
        (self.target / ".gitattributes").write_text(
            "payload.dat filter=constant\n",
            encoding="utf-8",
        )
        payload = self.target / "payload.dat"
        payload.write_bytes(b"original")
        self._git("config", "filter.constant.clean", str(converter))
        self._git("config", "filter.constant.required", "true")
        self._git("add", ".gitattributes", "payload.dat")
        self._git(
            "-c",
            "user.name=Harness Test",
            "-c",
            "user.email=harness@example.com",
            "commit",
            "-qm",
            "add clean-filter fixture",
        )

        payload.write_bytes(b"first!!!")
        first = self._snapshot()
        payload.write_bytes(b"second!!")
        second = self._snapshot()

        self.assertNotEqual(
            first["workspace_fingerprint"],
            second["workspace_fingerprint"],
        )

    def test_snapshot_hashes_assume_unchanged_tracked_bytes(self) -> None:
        self._git("update-index", "--assume-unchanged", "README.md")
        (self.target / "README.md").write_text("# First!\n", encoding="utf-8")
        first = self._snapshot()
        (self.target / "README.md").write_text("# Second\n", encoding="utf-8")
        second = self._snapshot()

        self.assertNotEqual(
            first["workspace_fingerprint"],
            second["workspace_fingerprint"],
        )

    def test_snapshot_hashes_dirty_submodule_content_recursively(self) -> None:
        source = self.review_root / "submodule-source"
        source.mkdir()

        def sub_git(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", str(source), *arguments],
                check=True,
                capture_output=True,
                text=True,
            )

        sub_git("init", "-q")
        nested_source = source / "nested.txt"
        nested_source.write_text("original\n", encoding="utf-8")
        sub_git("add", "nested.txt")
        sub_git(
            "-c",
            "user.name=Harness Test",
            "-c",
            "user.email=harness@example.com",
            "commit",
            "-qm",
            "initial submodule fixture",
        )
        self._git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(source),
            "vendor/sub",
        )
        self._git("add", ".gitmodules", "vendor/sub")
        self._git(
            "-c",
            "user.name=Harness Test",
            "-c",
            "user.email=harness@example.com",
            "commit",
            "-qm",
            "add submodule fixture",
        )
        nested = self.target / "vendor" / "sub" / "nested.txt"

        nested.write_text("changed1\n", encoding="utf-8")
        first = self._snapshot()
        nested.write_text("changed2\n", encoding="utf-8")
        second = self._snapshot()

        self.assertNotEqual(
            first["workspace_fingerprint"],
            second["workspace_fingerprint"],
        )

    def test_begin_evaluation_captures_exact_snapshot(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_plan()
        self._enter_building()
        self._write_implementation()
        expected = self._snapshot()

        result = self._begin_evaluation()

        self.assertEqual(result.returncode, 0, result.stderr)
        state = self._load_state()
        self.assertEqual(state["phase"], "EVALUATING")
        self.assertIsNone(state["latest_verdict"])
        evaluation = json.loads(result.stdout)["evaluation"]
        self.assertEqual(state["evaluation"], evaluation)
        self.assertEqual(evaluation["revision"], expected["revision"])
        self.assertEqual(
            evaluation["workspace_fingerprint"],
            expected["workspace_fingerprint"],
        )
        self.assertEqual(evaluation["requirement_id"], "REQ-001")
        self.assertEqual(evaluation["round"], 1)
        self.assertEqual(evaluation["goal"], "Add --version")
        self.assertEqual(evaluation["acceptance_criteria"], ["AC-001", "AC-002"])
        self.assertEqual(evaluation["review_sha256"], "")

    def test_begin_reprepares_a_pending_transaction_after_input_drift(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_plan()
        self._enter_building()
        self._write_implementation()
        real_write_state = getattr(HARNESS_RUNTIME, "_write_state")

        def crash_before_final_state(
            path: Path,
            state: dict[str, object],
        ) -> None:
            if (
                state.get("phase") == "EVALUATING"
                and state.get("pending_evaluation") is None
            ):
                raise OSError("simulated final state write interruption")
            real_write_state(path, state)

        with mock.patch.object(
            HARNESS_RUNTIME,
            "_write_state",
            side_effect=crash_before_final_state,
        ):
            with self.assertRaisesRegex(
                OSError,
                "simulated final state write interruption",
            ):
                getattr(HARNESS_RUNTIME, "begin_evaluation")(
                    self.target,
                    "REQ-001",
                )

        prepared = self._load_state()
        self.assertEqual(prepared["phase"], "BUILDING")
        self.assertIsInstance(prepared["pending_evaluation"], dict)
        resume = self._run_harness(
            "init",
            "--resume",
            "--requirement",
            "REQ-001",
        )
        self.assertNotEqual(resume.returncode, 0)
        self.assertIn(
            "pending evaluation transaction requires `begin-evaluation`",
            resume.stderr,
        )
        prepared["goal"] = "Add --version with a changed user-visible goal"
        self._write_state(prepared)
        ordinary_check = self._run_harness(
            "check",
            "--requirement",
            "REQ-001",
        )
        self.assertNotEqual(ordinary_check.returncode, 0)
        self.assertIn(
            "pending evaluation goal must match state.goal",
            ordinary_check.stdout,
        )
        implementation = self._round_root() / "implementation.md"
        implementation.write_text(
            "# Implementation\n\nChanged after the interrupted begin.\n",
            encoding="utf-8",
        )

        retried = self._begin_evaluation()

        self.assertEqual(retried.returncode, 0, retried.stderr)
        state = self._load_state()
        evaluation = state["evaluation"]
        self.assertIsInstance(evaluation, dict)
        assert isinstance(evaluation, dict)
        expected_digest = "sha256:" + hashlib.sha256(
            implementation.read_bytes()
        ).hexdigest()
        self.assertEqual(
            evaluation["implementation_sha256"],
            expected_digest,
        )
        self.assertEqual(
            evaluation["goal"],
            "Add --version with a changed user-visible goal",
        )
        self.assertEqual(
            evaluation["goal_sha256"],
            "sha256:"
            + hashlib.sha256(
                b"Add --version with a changed user-visible goal"
            ).hexdigest(),
        )
        self.assertIsNone(state["pending_evaluation"])
        self.assertEqual(
            (
                self._round_root()
                / "evaluation-inputs"
                / "implementation.md"
            ).read_bytes(),
            implementation.read_bytes(),
        )

    def test_mutating_evaluation_commands_require_an_explicit_requirement(self) -> None:
        results = [
            self._run_harness("begin-evaluation"),
            self._run_harness(
                "record-review",
                "--review-source",
                str(self.review_root / "review.md"),
            ),
            self._run_harness(
                "restart-evaluation",
                "--reason",
                "workspace drift",
            ),
            self._run_harness("accept"),
        ]

        for result in results:
            self.assertEqual(result.returncode, 2)
            self.assertIn("--requirement", result.stderr)

    def test_begin_evaluation_requires_plan_and_implementation(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._enter_building()

        without_plan = self._begin_evaluation()
        self._write_plan()
        without_implementation = self._begin_evaluation()

        self.assertNotEqual(without_plan.returncode, 0)
        self.assertIn("plan.md", without_plan.stderr)
        self.assertNotEqual(without_implementation.returncode, 0)
        self.assertIn("implementation.md", without_implementation.stderr)

    def test_begin_evaluation_rejects_planning_phase(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_plan()
        self._write_implementation()

        result = self._begin_evaluation()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BUILDING", result.stderr)

    def test_begin_evaluation_requires_exact_acceptance_criterion_ids(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._enter_building()
        self._write_plan("# Plan\n\n## Acceptance criteria\n\n- works\n")
        self._write_implementation()

        result = self._begin_evaluation()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AC-NNN", result.stderr)

    def test_begin_evaluation_rejects_duplicate_acceptance_criterion_ids(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._enter_building()
        self._write_plan(
            self._plan_body(
                [
                    ("AC-001", "First."),
                    ("AC-001", "Duplicate."),
                ]
            )
        )
        self._write_implementation()

        result = self._begin_evaluation()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate acceptance criterion", result.stderr)

    def test_record_review_commits_pass_and_review_digest(self) -> None:
        snapshot = self._prepare_evaluation()
        source = self._review_source(snapshot)
        body = source.read_text(encoding="utf-8")

        result = self._record(source)

        self.assertEqual(result.returncode, 0, result.stderr)
        state = self._load_state()
        self.assertEqual(state["latest_verdict"], "PASS")
        self.assertEqual(state["phase"], "EVALUATING")
        self.assertEqual(
            state["evaluation"]["review_sha256"],
            "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(self._review_path().read_text(encoding="utf-8"), body)

    def test_record_review_preserves_and_hashes_exact_utf8_bytes(self) -> None:
        snapshot = self._prepare_evaluation()
        source = self.review_root / "crlf-review.md"
        source_bytes = self._review_body(
            self._record_payload(snapshot)
        ).replace("\n", "\r\n").encode("utf-8")
        source.write_bytes(source_bytes)

        result = self._record(source)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._review_path().read_bytes(), source_bytes)
        self.assertEqual(
            self._load_state()["evaluation"]["review_sha256"],
            "sha256:" + hashlib.sha256(source_bytes).hexdigest(),
        )

    def test_record_review_rejects_source_inside_target_repository(self) -> None:
        snapshot = self._prepare_evaluation()
        source = self._write_artifact(
            "candidate-review.md",
            self._review_body(self._record_payload(snapshot)),
        )

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the target repository", result.stderr)
        self.assertFalse(self._review_path().exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_record_review_rejects_a_symlink_source(self) -> None:
        snapshot = self._prepare_evaluation()
        real_source = self._review_source(snapshot, name="real-review.md")
        source = self.review_root / "linked-review.md"
        source.symlink_to(real_source)

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symbolic link", result.stderr)

    def test_record_review_rejects_tracked_workspace_drift(self) -> None:
        snapshot = self._prepare_evaluation()
        source = self._review_source(snapshot)
        (self.target / "README.md").write_text("# Drift\n", encoding="utf-8")

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workspace snapshot changed", result.stderr)
        self.assertIsNone(self._load_state()["latest_verdict"])

    def test_record_review_rejects_untracked_workspace_drift(self) -> None:
        snapshot = self._prepare_evaluation()
        source = self._review_source(snapshot)
        (self.target / "unexpected.txt").write_text("drift\n", encoding="utf-8")

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workspace snapshot changed", result.stderr)

    def test_record_review_rejects_revision_or_fingerprint_mismatch(self) -> None:
        snapshot = self._prepare_evaluation()
        bad_revision = self._review_source(
            snapshot,
            revision="0" * 40,
            name="bad-revision.md",
        )
        bad_fingerprint = self._review_source(
            snapshot,
            workspace_fingerprint="sha256:" + "0" * 64,
            name="bad-fingerprint.md",
        )

        revision_result = self._record(bad_revision)
        fingerprint_result = self._record(bad_fingerprint)

        self.assertNotEqual(revision_result.returncode, 0)
        self.assertIn("revision must match", revision_result.stderr)
        self.assertNotEqual(fingerprint_result.returncode, 0)
        self.assertIn("workspace_fingerprint must match", fingerprint_result.stderr)

    def test_record_review_rejects_weak_or_malformed_command_evidence(self) -> None:
        snapshot = self._prepare_evaluation()
        evidence = [
            {
                "id": "EV-001",
                "kind": "command",
                "command": "pytest -q",
                "exit_code": True,
                "summary": "",
                "observations": [],
            },
            {
                "id": "EV-002",
                "kind": "inspection",
                "subject": "tests",
                "summary": "The report contains a count.",
                "observations": [
                    {
                        "kind": "metric",
                        "name": "passed_tests",
                        "value": "42",
                        "unit": "tests",
                    }
                ],
            },
        ]
        source = self._review_source(snapshot, evidence=evidence)

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exit_code must be an integer", result.stderr)
        self.assertIn("summary must be a non-empty string", result.stderr)
        self.assertIn("observations must be a non-empty list", result.stderr)
        self.assertIn("metric value must be a finite number", result.stderr)

    def test_record_review_rejects_untyped_or_empty_observations(self) -> None:
        snapshot = self._prepare_evaluation()
        scenarios = {
            "legacy-result": [
                {
                    "id": "EV-001",
                    "kind": "command",
                    "command": "pytest -q",
                    "exit_code": 0,
                    "result": "tests passed",
                },
                {
                    "id": "EV-002",
                    "kind": "inspection",
                    "subject": "report.html",
                    "result": "rendered report contains 42 passing cases",
                },
            ],
            "empty-observations": [
                {
                    "id": "EV-001",
                    "kind": "command",
                    "command": "pytest -q",
                    "exit_code": 0,
                    "summary": "The entire test suite passed successfully.",
                    "observations": [],
                },
                {
                    "id": "EV-002",
                    "kind": "inspection",
                    "subject": "full test report",
                    "summary": "The report was inspected.",
                    "observations": [
                        {
                            "kind": "metric",
                            "name": "passed_tests",
                            "value": 42,
                            "unit": "tests",
                        }
                    ],
                },
            ],
            "unknown-observation": [
                {
                    "id": "EV-001",
                    "kind": "command",
                    "command": "python -m product --version",
                    "exit_code": 0,
                    "summary": "The command returned a version.",
                    "observations": [
                        {
                            "kind": "guess",
                            "name": "version",
                            "value": "3.0.0",
                        }
                    ],
                },
                {
                    "id": "EV-002",
                    "kind": "inspection",
                    "subject": "full test report",
                    "summary": "The report contains exact counts.",
                    "observations": [
                        {
                            "kind": "metric",
                            "name": "passed_tests",
                            "value": 42,
                            "unit": "tests",
                        }
                    ],
                },
            ],
        }

        for name, evidence in scenarios.items():
            with self.subTest(name=name):
                source = self._review_source(
                    snapshot,
                    evidence=evidence,
                    name=f"{name}.md",
                )
                result = self._record(source)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("observation", result.stderr)

    def test_record_review_accepts_exact_metric_and_artifact_observations(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_plan()
        self._enter_building()
        self._write_implementation()
        artifact = self._write_artifact(
            "my reports/report.html",
            "<p>42 passing cases</p>\n",
        )
        begin = self._begin_evaluation()
        self.assertEqual(begin.returncode, 0, begin.stderr)
        snapshot = json.loads(begin.stdout)["evaluation"]
        artifact_digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        evidence = [
            {
                "id": "EV-001",
                "kind": "command",
                "command": "curl -i https://example.test/health",
                "exit_code": 0,
                "summary": "The endpoint returned the expected protocol status and media type.",
                "observations": [
                    {
                        "kind": "exact",
                        "name": "status_line",
                        "value": "HTTP/1.1 200 OK",
                    },
                    {
                        "kind": "exact",
                        "name": "content_type",
                        "value": "application/json",
                    },
                ],
            },
            {
                "id": "EV-002",
                "kind": "inspection",
                "subject": "rendered test report",
                "summary": "The saved report contains 42 passing cases.",
                "observations": [
                    {
                        "kind": "artifact",
                        "path": "my reports/report.html",
                        "sha256": artifact_digest,
                    },
                    {
                        "kind": "metric",
                        "name": "passing_cases",
                        "value": 42,
                        "unit": "cases",
                    },
                ],
            },
        ]
        source = self._review_source(snapshot, evidence=evidence)

        result = self._record(source)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._load_state()["latest_verdict"], "PASS")

    def test_record_review_rejects_artifact_observation_hash_mismatch(self) -> None:
        snapshot = self._prepare_evaluation()
        record = self._record_payload(snapshot)
        record["evidence"][1]["observations"] = [
            {
                "kind": "artifact",
                "path": "README.md",
                "sha256": "sha256:" + "0" * 64,
            }
        ]
        source = self.review_root / "bad-artifact-digest.md"
        source.write_text(self._review_body(record), encoding="utf-8")

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sha256 does not match target bytes", result.stderr)

    def test_historical_artifact_observation_does_not_bind_current_bytes(
        self,
    ) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_plan()
        self._enter_building()
        self._write_implementation()
        artifact = self._write_artifact("report.ignored", "round one\n")
        begin = self._begin_evaluation()
        self.assertEqual(begin.returncode, 0, begin.stderr)
        snapshot = json.loads(begin.stdout)["evaluation"]
        evidence = self._record_payload(snapshot)["evidence"]
        evidence[1]["observations"] = [
            {
                "kind": "artifact",
                "path": "report.ignored",
                "sha256": (
                    "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
                ),
            }
        ]
        failed = self._review_source(
            snapshot,
            verdict="FAIL",
            evidence=evidence,
            name="failed-artifact-review.md",
        )

        recorded = self._record(failed)
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        artifact.write_text("round two\n", encoding="utf-8")
        self._write_implementation(round_number=2)

        next_evaluation = self._begin_evaluation()

        self.assertEqual(next_evaluation.returncode, 0, next_evaluation.stderr)

    def test_accept_rejects_and_restart_preserves_ignored_artifact_drift(
        self,
    ) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_plan()
        self._enter_building()
        self._write_implementation()
        artifact = self._write_artifact("report.ignored", "evaluated\n")
        begin = self._begin_evaluation()
        self.assertEqual(begin.returncode, 0, begin.stderr)
        snapshot = json.loads(begin.stdout)["evaluation"]
        evidence = self._record_payload(snapshot)["evidence"]
        evidence[1]["observations"] = [
            {
                "kind": "artifact",
                "path": "report.ignored",
                "sha256": (
                    "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
                ),
            }
        ]
        source = self._review_source(snapshot, evidence=evidence)
        recorded = self._record(source)
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        evaluating_state = self._state_path().read_bytes()
        artifact.write_text("changed after review\n", encoding="utf-8")

        result = self._run_harness("accept", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sha256 does not match target bytes", result.stderr)
        self.assertEqual(self._load_state()["status"], "ACTIVE")

        reason = "Ignored evidence artifact changed after review."
        restarted = self._restart(reason)

        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        interruption_bytes = self._interruption_path().read_bytes()
        interruption = json.loads(
            interruption_bytes.decode("utf-8")
        )
        self.assertEqual(interruption["schema_version"], 2)
        self.assertEqual(
            {
                key: interruption["observed"][key]
                for key in ("revision", "workspace_fingerprint")
            },
            {
                "revision": snapshot["revision"],
                "workspace_fingerprint": snapshot["workspace_fingerprint"],
            },
        )
        self.assertEqual(
            interruption["artifact_drift"],
            [
                {
                    "path": "report.ignored",
                    "expected_sha256": (
                        "sha256:"
                        + hashlib.sha256(b"evaluated\n").hexdigest()
                    ),
                    "observed": (
                        "sha256:"
                        + hashlib.sha256(b"changed after review\n").hexdigest()
                    ),
                }
            ],
        )
        self._state_path().write_bytes(evaluating_state)
        self._stage_pending_interruption(interruption)
        artifact.write_text("changed again after interruption\n", encoding="utf-8")

        resumed = self._restart(reason)

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self._interruption_path().read_bytes(), interruption_bytes)
        self.assertEqual(self._load_state()["active_round"], 2)

    def test_record_review_accepts_arbitrarily_large_integer_metric(self) -> None:
        snapshot = self._prepare_evaluation()
        record = self._record_payload(snapshot)
        record["evidence"][1]["observations"][0]["value"] = 10**400
        source = self.review_root / "large-metric.md"
        source.write_text(self._review_body(record), encoding="utf-8")

        result = self._record(source)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_record_review_rejects_over_limit_integer_without_traceback(
        self,
    ) -> None:
        snapshot = self._prepare_evaluation()
        body = self._review_body(self._record_payload(snapshot)).replace(
            '"value": 42',
            '"value": ' + "9" * 5000,
            1,
        )
        source = self.review_root / "over-limit-integer.md"
        source.write_text(body, encoding="utf-8")

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertIn("invalid JSON in review.md Evaluation record", result.stderr)

    def test_record_review_rejects_git_control_artifact_paths(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_plan()
        self._enter_building()
        self._write_implementation()
        nested_control = self._write_artifact(
            "nested/.git/config",
            "[core]\n",
        )
        upper_control = self.target / ".GIT" / "config"
        if not upper_control.is_file():
            upper_control.parent.mkdir()
            upper_control.write_text("[core]\n", encoding="utf-8")
        begin = self._begin_evaluation()
        self.assertEqual(begin.returncode, 0, begin.stderr)
        snapshot = json.loads(begin.stdout)["evaluation"]

        for name, artifact in (
            ("nested", nested_control),
            ("casefolded", upper_control),
        ):
            with self.subTest(name=name):
                record = self._record_payload(snapshot)
                record["evidence"][1]["observations"] = [
                    {
                        "kind": "artifact",
                        "path": artifact.relative_to(self.target).as_posix(),
                        "sha256": (
                            "sha256:"
                            + hashlib.sha256(artifact.read_bytes()).hexdigest()
                        ),
                    }
                ]
                source = self.review_root / f"{name}-control-path.md"
                source.write_text(self._review_body(record), encoding="utf-8")

                result = self._record(source)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "canonical repository-relative product path",
                    result.stderr,
                )

    def test_record_review_rejects_nul_artifact_path_without_traceback(
        self,
    ) -> None:
        snapshot = self._prepare_evaluation()
        record = self._record_payload(snapshot)
        record["evidence"][1]["observations"] = [
            {
                "kind": "artifact",
                "path": "bad\u0000path",
                "sha256": "sha256:" + "0" * 64,
            }
        ]
        source = self.review_root / "nul-artifact-path.md"
        source.write_text(self._review_body(record), encoding="utf-8")

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertIn(
            "canonical repository-relative product path",
            result.stderr,
        )

    def test_record_review_rejects_unknown_evidence_references(self) -> None:
        snapshot = self._prepare_evaluation()
        criteria = [
            {"id": "AC-001", "verdict": "PASS", "evidence_ids": ["EV-404"]},
            {"id": "AC-002", "verdict": "PASS", "evidence_ids": ["EV-002"]},
        ]
        source = self._review_source(snapshot, criteria=criteria)

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown evidence id EV-404", result.stderr)

    def test_record_review_rejects_missing_or_extra_criteria(self) -> None:
        snapshot = self._prepare_evaluation()
        criteria = [
            {"id": "AC-001", "verdict": "PASS", "evidence_ids": ["EV-001"]},
            {"id": "AC-003", "verdict": "PASS", "evidence_ids": ["EV-002"]},
        ]
        source = self._review_source(snapshot, criteria=criteria)

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("criteria ids must exactly match plan.md", result.stderr)

    def test_record_review_derives_verdict_from_criteria(self) -> None:
        snapshot = self._prepare_evaluation()
        criteria = [
            {"id": "AC-001", "verdict": "FAIL", "evidence_ids": ["EV-001"]},
            {"id": "AC-002", "verdict": "PASS", "evidence_ids": ["EV-002"]},
        ]
        source = self._review_source(snapshot, verdict="PASS", criteria=criteria)

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verdict must be FAIL", result.stderr)

    def test_unverified_review_can_be_replaced_by_pass_on_same_snapshot(self) -> None:
        snapshot = self._prepare_evaluation()
        unverified = self._review_source(
            snapshot,
            "UNVERIFIED",
            name="unverified.md",
        )
        passed = self._review_source(snapshot, "PASS", name="pass.md")

        first = self._record(unverified)
        second = self._record(passed)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self._load_state()["latest_verdict"], "PASS")
        self.assertEqual(
            self._review_path().read_text(encoding="utf-8"),
            passed.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            self._attempt_path().read_text(encoding="utf-8"),
            unverified.read_text(encoding="utf-8"),
        )
        self.assertEqual(len(self._load_state()["review_attempts"]), 1)

    def test_fail_review_advances_to_next_building_round(self) -> None:
        snapshot = self._prepare_evaluation()
        failed = self._review_source(snapshot, "FAIL")

        result = self._record(failed)

        self.assertEqual(result.returncode, 0, result.stderr)
        state = self._load_state()
        self.assertEqual(state["latest_verdict"], "FAIL")
        self.assertEqual(state["phase"], "BUILDING")
        self.assertEqual(state["active_round"], 2)
        self.assertIsNone(state["evaluation"])
        self.assertTrue(self._review_path(round_number=1).is_file())

    def test_restart_evaluation_after_drift_starts_a_new_round(self) -> None:
        self._prepare_evaluation()
        prior_evaluation = self._load_state()["evaluation"]
        (self.target / "README.md").write_text(
            "# Drift during evaluation\n",
            encoding="utf-8",
        )
        observed = self._snapshot()

        result = self._restart("Product workspace changed during evaluation.")

        self.assertEqual(result.returncode, 0, result.stderr)
        state = self._load_state()
        self.assertEqual(state["status"], "ACTIVE")
        self.assertEqual(state["phase"], "BUILDING")
        self.assertEqual(state["active_round"], 2)
        self.assertIsNone(state["latest_verdict"])
        self.assertIsNone(state["evaluation"])
        interruption = json.loads(
            self._interruption_path().read_text(encoding="utf-8")
        )
        self.assertEqual(interruption["evaluation"], prior_evaluation)
        self.assertEqual(
            {
                key: interruption["observed"][key]
                for key in ("revision", "workspace_fingerprint")
            },
            observed,
        )
        self.assertIsNone(interruption["prior_verdict"])
        self.assertFalse(self._review_path().exists())

        self._write_implementation(round_number=2)
        begin = self._begin_evaluation()

        self.assertEqual(begin.returncode, 0, begin.stderr)
        self.assertEqual(
            json.loads(begin.stdout)["evaluation"]["workspace_fingerprint"],
            observed["workspace_fingerprint"],
        )

    def test_restart_after_pass_drift_preserves_review_and_can_accept(self) -> None:
        self._prepare_pass()
        first_review = self._review_path().read_bytes()
        (self.target / "README.md").write_text(
            "# Drift after PASS\n",
            encoding="utf-8",
        )
        rejected = self._run_harness("accept", "--requirement", "REQ-001")
        self.assertNotEqual(rejected.returncode, 0)

        restarted = self._restart("PASS snapshot no longer matches the workspace.")

        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(self._review_path().read_bytes(), first_review)
        interruption = json.loads(
            self._interruption_path().read_text(encoding="utf-8")
        )
        self.assertEqual(interruption["prior_verdict"], "PASS")
        self._write_implementation(round_number=2)
        begin = self._begin_evaluation()
        self.assertEqual(begin.returncode, 0, begin.stderr)
        snapshot = json.loads(begin.stdout)["evaluation"]
        source = self._review_source(snapshot, name="second-pass.md")
        self.assertEqual(self._record(source).returncode, 0)

        accepted = self._run_harness("accept", "--requirement", "REQ-001")

        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(self._load_state()["status"], "ACCEPTED")

    def test_restart_accepts_blocked_pass_with_appended_drift_risk(self) -> None:
        self._prepare_pass()
        (self.target / "README.md").write_text(
            "# Drift after PASS\n",
            encoding="utf-8",
        )
        state = self._load_state()
        state["status"] = "BLOCKED"
        state["next_action"] = "Preserve drift and restart evaluation."
        state["residual_risks"].append("Workspace changed after PASS.")
        self._write_state(state)

        blocked = self._run_harness("check", "--requirement", "REQ-001")
        restarted = self._restart("Workspace changed after PASS.")

        self.assertEqual(blocked.returncode, 0, blocked.stdout)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(self._load_state()["status"], "ACTIVE")
        self.assertEqual(self._load_state()["phase"], "BUILDING")

    def test_restart_requires_drift_and_a_nonempty_reason(self) -> None:
        self._prepare_evaluation()
        before = self._state_path().read_bytes()

        unchanged = self._restart("No actual drift.")
        empty_reason = self._restart("")

        self.assertNotEqual(unchanged.returncode, 0)
        self.assertIn("evaluation input or evidence artifact drift", unchanged.stderr)
        self.assertNotEqual(empty_reason.returncode, 0)
        self.assertIn("non-empty", empty_reason.stderr)
        self.assertEqual(self._state_path().read_bytes(), before)
        self.assertFalse(self._interruption_path().exists())

    def test_restart_recovers_an_interruption_written_before_state(self) -> None:
        self._prepare_evaluation()
        evaluating_state = self._state_path().read_bytes()
        (self.target / "README.md").write_text(
            "# Drift before restart\n",
            encoding="utf-8",
        )
        reason = "Simulate a crash after interruption persistence."
        first = self._restart(reason)
        self.assertEqual(first.returncode, 0, first.stderr)
        interruption = self._interruption_path().read_bytes()
        self._state_path().write_bytes(evaluating_state)
        self._stage_pending_interruption(
            json.loads(interruption.decode("utf-8"))
        )

        resumed = self._restart(reason)

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self._interruption_path().read_bytes(), interruption)
        self.assertEqual(self._load_state()["active_round"], 2)

    def test_restart_commits_pending_interruption_after_drift_reverts(self) -> None:
        self._prepare_evaluation()
        evaluating_state = self._state_path().read_bytes()
        readme = self.target / "README.md"
        evaluated_readme = readme.read_bytes()
        readme.write_text("# Transient drift before restart\n", encoding="utf-8")
        reason = "Persist transient drift before a simulated crash."
        first = self._restart(reason)
        self.assertEqual(first.returncode, 0, first.stderr)
        interruption = self._interruption_path().read_bytes()
        self._state_path().write_bytes(evaluating_state)
        self._stage_pending_interruption(
            json.loads(interruption.decode("utf-8"))
        )
        readme.write_bytes(evaluated_readme)

        resumed = self._restart(reason)

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self._interruption_path().read_bytes(), interruption)
        self.assertEqual(self._load_state()["active_round"], 2)

    def test_restart_is_idempotent_after_state_commit(self) -> None:
        self._prepare_evaluation()
        (self.target / "README.md").write_text(
            "# Drift before restart\n",
            encoding="utf-8",
        )
        reason = "Retry the completed restart."
        first = self._restart(reason)
        before = self._state_path().read_bytes()

        second = self._restart(reason)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self._state_path().read_bytes(), before)

    def test_check_rejects_interruption_without_prior_evaluation(self) -> None:
        self._prepare_evaluation()
        (self.target / "README.md").write_text(
            "# Drift before restart\n",
            encoding="utf-8",
        )
        self.assertEqual(
            self._restart("Preserve the prior evaluation.").returncode,
            0,
        )
        interruption = json.loads(
            self._interruption_path().read_text(encoding="utf-8")
        )
        interruption["evaluation"] = None
        self._interruption_path().write_text(
            json.dumps(interruption, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("interruption evaluation must be an object", result.stdout)

    def test_check_rejects_interruption_schema_one_without_compatibility(
        self,
    ) -> None:
        self._prepare_evaluation()
        (self.target / "README.md").write_text(
            "# Drift before restart\n",
            encoding="utf-8",
        )
        self.assertEqual(
            self._restart("Preserve the schema 2 interruption.").returncode,
            0,
        )
        interruption = json.loads(
            self._interruption_path().read_text(encoding="utf-8")
        )
        interruption["schema_version"] = 1
        self._interruption_path().write_text(
            json.dumps(interruption, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema_version must be 2", result.stdout)

    def test_check_rejects_artifact_drift_not_bound_to_review(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_plan()
        self._enter_building()
        self._write_implementation()
        artifact = self._write_artifact("report.ignored", "evaluated\n")
        begin = self._begin_evaluation()
        self.assertEqual(begin.returncode, 0, begin.stderr)
        snapshot = json.loads(begin.stdout)["evaluation"]
        evidence = self._record_payload(snapshot)["evidence"]
        evidence[1]["observations"] = [
            {
                "kind": "artifact",
                "path": "report.ignored",
                "sha256": (
                    "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
                ),
            }
        ]
        source = self._review_source(snapshot, evidence=evidence)
        self.assertEqual(self._record(source).returncode, 0)
        artifact.write_text("changed\n", encoding="utf-8")
        self.assertEqual(self._restart("Artifact changed.").returncode, 0)
        interruption = json.loads(
            self._interruption_path().read_text(encoding="utf-8")
        )
        interruption["artifact_drift"] = [
            {
                "path": "README.md",
                "expected_sha256": (
                    "sha256:"
                    + hashlib.sha256(
                        (self.target / "README.md").read_bytes()
                    ).hexdigest()
                ),
                "observed": "sha256:" + "0" * 64,
            }
        ]
        self._interruption_path().write_text(
            json.dumps(interruption, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "artifact_drift must reference artifact observations from review",
            result.stdout,
        )

    def test_restart_rejects_artifact_drift_without_prior_review(self) -> None:
        self._prepare_evaluation()
        state = self._load_state()
        interruption = {
            "schema_version": 2,
            "reason": "Forged artifact-only drift.",
            "prior_verdict": None,
            "evaluation": state["evaluation"],
            "observed": {
                "revision": state["evaluation"]["revision"],
                "workspace_fingerprint": state["evaluation"][
                    "workspace_fingerprint"
                ],
                "goal_sha256": state["evaluation"]["goal_sha256"],
                "plan_sha256": state["evaluation"]["plan_sha256"],
                "implementation_sha256": state["evaluation"][
                    "implementation_sha256"
                ],
            },
            "artifact_drift": [
                {
                    "path": "README.md",
                    "expected_sha256": "sha256:" + "0" * 64,
                    "observed": "missing",
                }
            ],
        }
        destination = self._interruption_path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(interruption, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = self._restart("Forged artifact-only drift.")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "null prior_verdict requires empty artifact_drift",
            result.stderr,
        )
        self.assertEqual(self._load_state()["active_round"], 1)

    def test_restart_rejects_forged_bound_drift_when_artifact_is_unchanged(
        self,
    ) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_plan()
        self._enter_building()
        self._write_implementation()
        artifact = self._write_artifact("report.ignored", "evaluated\n")
        begin = self._begin_evaluation()
        self.assertEqual(begin.returncode, 0, begin.stderr)
        snapshot = json.loads(begin.stdout)["evaluation"]
        artifact_digest = (
            "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        )
        evidence = self._record_payload(snapshot)["evidence"]
        evidence[1]["observations"] = [
            {
                "kind": "artifact",
                "path": "report.ignored",
                "sha256": artifact_digest,
            }
        ]
        source = self._review_source(snapshot, evidence=evidence)
        self.assertEqual(self._record(source).returncode, 0)
        state = self._load_state()
        interruption = {
            "schema_version": 2,
            "reason": "Forged bound drift.",
            "prior_verdict": "PASS",
            "evaluation": state["evaluation"],
            "observed": {
                "revision": state["evaluation"]["revision"],
                "workspace_fingerprint": state["evaluation"][
                    "workspace_fingerprint"
                ],
                "goal_sha256": state["evaluation"]["goal_sha256"],
                "plan_sha256": state["evaluation"]["plan_sha256"],
                "implementation_sha256": state["evaluation"][
                    "implementation_sha256"
                ],
            },
            "artifact_drift": [
                {
                    "path": "report.ignored",
                    "expected_sha256": artifact_digest,
                    "observed": "missing",
                }
            ],
        }
        self._interruption_path().write_text(
            json.dumps(interruption, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = self._restart("Forged bound drift.")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "lacks a matching pending_interruption transaction",
            result.stderr,
        )
        self.assertEqual(self._load_state()["active_round"], 1)

    def test_resume_recovers_first_review_written_before_state_update(self) -> None:
        snapshot = self._prepare_evaluation()
        source = self._review_source(snapshot)
        destination = self._review_path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._stage_pending_review(source)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

        result = self._run_harness(
            "init",
            "--resume",
            "--requirement",
            "REQ-001",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._load_state()["latest_verdict"], "PASS")
        self.assertTrue(self._load_state()["evaluation"]["review_sha256"])

    def test_resume_commits_pending_review_before_restarting_artifact_drift(
        self,
    ) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_plan()
        self._enter_building()
        self._write_implementation()
        artifact = self._write_artifact("report.ignored", "evaluated\n")
        begin = self._begin_evaluation()
        self.assertEqual(begin.returncode, 0, begin.stderr)
        snapshot = json.loads(begin.stdout)["evaluation"]
        evidence = self._record_payload(snapshot)["evidence"]
        evidence[1]["observations"] = [
            {
                "kind": "artifact",
                "path": "report.ignored",
                "sha256": (
                    "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
                ),
            }
        ]
        source = self._review_source(snapshot, evidence=evidence)
        destination = self._review_path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._stage_pending_review(source)
        destination.write_bytes(source.read_bytes())
        artifact.write_text("changed after review write\n", encoding="utf-8")

        resumed = self._run_harness(
            "init",
            "--resume",
            "--requirement",
            "REQ-001",
        )

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        state = self._load_state()
        self.assertEqual(state["latest_verdict"], "PASS")
        self.assertTrue(state["evaluation"]["review_sha256"])

        restarted = self._restart("Artifact changed after review persistence.")

        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(self._load_state()["phase"], "BUILDING")
        interruption = json.loads(
            self._interruption_path().read_text(encoding="utf-8")
        )
        self.assertEqual(
            interruption["artifact_drift"][0]["path"],
            "report.ignored",
        )

    def test_resume_recovers_replacement_review_written_before_state_update(self) -> None:
        snapshot = self._prepare_evaluation()
        unverified = self._review_source(
            snapshot,
            "UNVERIFIED",
            name="unverified.md",
        )
        self.assertEqual(self._record(unverified).returncode, 0)
        old_hash = self._load_state()["evaluation"]["review_sha256"]
        old_review = self._review_path().read_bytes()
        passed = self._review_source(snapshot, "PASS", name="pass.md")
        self._stage_pending_review(passed)
        attempt = self._attempt_path()
        attempt.parent.mkdir(parents=True, exist_ok=True)
        attempt.write_bytes(old_review)
        self._review_path().write_text(passed.read_text(encoding="utf-8"), encoding="utf-8")

        result = self._run_harness(
            "init",
            "--resume",
            "--requirement",
            "REQ-001",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        state = self._load_state()
        self.assertEqual(state["latest_verdict"], "PASS")
        self.assertNotEqual(state["evaluation"]["review_sha256"], old_hash)

    def test_resume_rejects_an_invalid_pending_review_without_mutating_state(self) -> None:
        self._prepare_evaluation()
        source = self.review_root / "invalid-pending.md"
        source.write_text("# Review\n\nnot structured\n", encoding="utf-8")
        self._stage_pending_review(source)
        before = self._state_path().read_bytes()
        path = self._review_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.read_bytes())

        result = self._run_harness(
            "init",
            "--resume",
            "--requirement",
            "REQ-001",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Evaluation record", result.stderr)
        self.assertEqual(self._state_path().read_bytes(), before)

    def test_accept_records_the_exact_evaluated_revision(self) -> None:
        snapshot, _ = self._prepare_pass()

        result = self._run_harness("accept", "--requirement", "REQ-001")

        self.assertEqual(result.returncode, 0, result.stderr)
        state = self._load_state()
        self.assertEqual(state["status"], "ACCEPTED")
        self.assertEqual(state["accepted_revision"], snapshot["revision"])
        self.assertEqual(state["evaluation"]["revision"], snapshot["revision"])

    def test_accept_rejects_tracked_drift_without_mutating_state(self) -> None:
        self._prepare_pass()
        before = self._state_path().read_bytes()
        (self.target / "README.md").write_text("# Changed after review\n", encoding="utf-8")

        result = self._run_harness("accept", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workspace snapshot changed", result.stderr)
        self.assertEqual(self._state_path().read_bytes(), before)

    def test_accept_rejects_untracked_drift_without_mutating_state(self) -> None:
        self._prepare_pass()
        before = self._state_path().read_bytes()
        (self.target / "untracked.txt").write_text("drift\n", encoding="utf-8")

        result = self._run_harness("accept", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workspace snapshot changed", result.stderr)
        self.assertEqual(self._state_path().read_bytes(), before)

    def test_accept_rejects_review_mutation_without_mutating_state(self) -> None:
        self._prepare_pass()
        before = self._state_path().read_bytes()
        self._review_path().write_text(
            self._review_path().read_text(encoding="utf-8") + "\nchanged\n",
            encoding="utf-8",
        )

        result = self._run_harness("accept", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("review digest changed", result.stderr)
        self.assertEqual(self._state_path().read_bytes(), before)

    def test_accept_rejects_line_ending_only_review_mutation(self) -> None:
        self._prepare_pass()
        before = self._state_path().read_bytes()
        original = self._review_path().read_bytes()
        self._review_path().write_bytes(original.replace(b"\n", b"\r\n"))

        result = self._run_harness("accept", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("review digest changed", result.stderr)
        self.assertEqual(self._state_path().read_bytes(), before)

    def test_accept_rejects_a_new_head_revision_without_mutating_state(self) -> None:
        self._prepare_pass()
        before = self._state_path().read_bytes()
        (self.target / "README.md").write_text("# New revision\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git(
            "-c",
            "user.name=Harness Test",
            "-c",
            "user.email=harness@example.com",
            "commit",
            "-qm",
            "post-review revision",
        )

        result = self._run_harness("accept", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workspace snapshot changed", result.stderr)
        self.assertEqual(self._state_path().read_bytes(), before)

    def test_accept_requires_pass(self) -> None:
        snapshot = self._prepare_evaluation()
        source = self._review_source(snapshot, "UNVERIFIED")
        self.assertEqual(self._record(source).returncode, 0)

        result = self._run_harness("accept", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PASS", result.stderr)

    def test_accept_is_idempotent_when_snapshot_and_review_are_unchanged(self) -> None:
        self._prepare_pass()
        first = self._run_harness("accept", "--requirement", "REQ-001")
        before = self._state_path().read_bytes()

        second = self._run_harness("accept", "--requirement", "REQ-001")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self._state_path().read_bytes(), before)

    def test_final_check_rejects_workspace_or_review_drift(self) -> None:
        self._prepare_pass()
        self.assertEqual(
            self._run_harness("accept", "--requirement", "REQ-001").returncode,
            0,
        )

        clean = self._run_harness("check", "--requirement", "REQ-001", "--final")
        (self.target / "README.md").write_text("# Post-accept drift\n", encoding="utf-8")
        dirty = self._run_harness("check", "--requirement", "REQ-001", "--final")

        self.assertEqual(clean.returncode, 0, clean.stdout)
        self.assertNotEqual(dirty.returncode, 0)
        self.assertIn("workspace snapshot changed", dirty.stdout)

    def test_structured_fail_history_allows_a_later_pass_and_accept(self) -> None:
        first_snapshot = self._prepare_evaluation()
        failed = self._review_source(first_snapshot, "FAIL", name="failed.md")
        self.assertEqual(self._record(failed).returncode, 0)
        self._write_implementation(round_number=2)

        begin = self._begin_evaluation()
        self.assertEqual(begin.returncode, 0, begin.stderr)
        second_snapshot = json.loads(begin.stdout)["evaluation"]
        passed = self._review_source(second_snapshot, "PASS", name="passed.md")
        self.assertEqual(self._record(passed).returncode, 0)

        accepted = self._run_harness("accept", "--requirement", "REQ-001")

        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(self._load_state()["status"], "ACCEPTED")

    def test_resume_requires_id_when_multiple_requirements_are_nonterminal(self) -> None:
        self.assertEqual(self._init("First goal").returncode, 0)
        self.assertEqual(self._init("Second goal").returncode, 0)

        ambiguous = self._run_harness("init", "--resume")
        selected = self._run_harness(
            "init",
            "--resume",
            "--requirement",
            "REQ-002",
        )

        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertIn("multiple nonterminal requirements", ambiguous.stderr)
        self.assertEqual(selected.returncode, 0, selected.stderr)

    def test_check_reports_invalid_utf8_without_traceback(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._state_path().write_bytes(b"\xff")

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertIn("cannot read", result.stdout)

    def test_check_rejects_unhashable_state_values_without_traceback(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        state = self._load_state()
        state["latest_verdict"] = {"unexpected": "object"}
        self._write_state(state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertIn("latest_verdict must be", result.stdout)

    def test_check_rejects_duplicate_state_keys_without_traceback(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        body = self._state_path().read_text(encoding="utf-8")
        self._state_path().write_text(
            body.replace(
                '"status": "ACTIVE",',
                '"status": "ACTIVE",\n  "status": "BLOCKED",',
                1,
            ),
            encoding="utf-8",
        )

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertIn("duplicate JSON key: status", result.stdout)

    def test_check_reports_surrogate_state_without_traceback(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        body = self._state_path().read_text(encoding="utf-8").replace(
            "Add --version",
            "\\ud800",
            1,
        )
        self._state_path().write_text(body, encoding="utf-8")

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertIn("goal must be a non-empty string", result.stdout)
        self.assertIn("\\ud800", result.stdout)

    def test_record_review_rejects_duplicate_keys_without_traceback(self) -> None:
        snapshot = self._prepare_evaluation()
        body = self._review_body(self._record_payload(snapshot)).replace(
            '"verdict": "PASS",',
            '"verdict": "PASS",\n  "verdict": "FAIL",',
            1,
        )
        source = self.review_root / "duplicate-verdict.md"
        source.write_text(body, encoding="utf-8")

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertIn("duplicate JSON key: verdict", result.stderr)

    def test_record_review_rejects_surrogate_risk_before_persistence(
        self,
    ) -> None:
        snapshot = self._prepare_evaluation()
        record = self._record_payload(
            snapshot,
            residual_risks=["placeholder"],
        )
        body = self._review_body(record).replace(
            "placeholder",
            "\\ud800",
            1,
        )
        source = self.review_root / "surrogate-risk.md"
        source.write_text(body, encoding="utf-8")

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertIn(
            "residual_risks must be a list of non-empty strings",
            result.stderr,
        )
        self.assertFalse(self._review_path().exists())
        self.assertIsNone(self._load_state()["latest_verdict"])

    def test_record_review_rejects_boolean_schema_version(self) -> None:
        snapshot = self._prepare_evaluation()
        record = self._record_payload(snapshot)
        record["schema_version"] = True
        source = self.review_root / "boolean-schema.md"
        source.write_text(self._review_body(record), encoding="utf-8")

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema_version must be 2", result.stderr)

    def test_record_review_rejects_schema_version_one_without_compatibility(self) -> None:
        snapshot = self._prepare_evaluation()
        record = self._record_payload(snapshot)
        record["schema_version"] = 1
        source = self.review_root / "schema-one.md"
        source.write_text(self._review_body(record), encoding="utf-8")

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema_version must be 2", result.stderr)

    def test_record_review_rejects_unhashable_verdict_without_traceback(self) -> None:
        snapshot = self._prepare_evaluation()
        record = self._record_payload(snapshot)
        record["verdict"] = {"unexpected": "object"}
        source = self.review_root / "invalid-verdict.md"
        source.write_text(self._review_body(record), encoding="utf-8")

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertIn("verdict must be", result.stderr)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_init_rejects_a_symlink_control_directory(self) -> None:
        external = self.review_root / "control"
        external.mkdir()
        (self.target / ".vibe-coding").symlink_to(external, target_is_directory=True)

        result = self._init()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symbolic link", result.stderr)

    def test_init_rejects_legacy_global_state(self) -> None:
        control = self.target / ".vibe-coding"
        control.mkdir()
        (control / "state.json").write_text("{}\n", encoding="utf-8")

        result = self._init()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("legacy global harness state", result.stderr)

    def test_historical_fail_review_is_bound_to_its_transaction(self) -> None:
        snapshot = self._prepare_evaluation()
        failed = self._review_source(snapshot, "FAIL")
        self.assertEqual(self._record(failed).returncode, 0)
        review = self._review_path()
        review.write_text(
            review.read_text(encoding="utf-8").replace(
                "product 3.0.0",
                "product 999.0.0",
            ),
            encoding="utf-8",
        )

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "history round 001 review digest must equal failed evaluation",
            result.stdout,
        )

    def test_record_review_rejects_plan_semantic_drift(self) -> None:
        snapshot = self._prepare_evaluation()
        source = self._review_source(snapshot)
        self._write_plan(
            self._plan_body(
                [
                    ("AC-001", "The command permanently deletes user data."),
                    ("AC-002", "The full test suite passes."),
                ]
            )
        )

        result = self._record(source)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "evaluation inputs changed since begin-evaluation",
            result.stderr,
        )
        self.assertFalse(self._review_path().exists())

    def test_final_check_rejects_plan_or_implementation_drift(self) -> None:
        self._prepare_pass()
        accepted = self._run_harness("accept", "--requirement", "REQ-001")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        plan = self._requirement_root() / "plan.md"
        implementation = self._round_root() / "implementation.md"
        original_plan = plan.read_bytes()
        original_implementation = implementation.read_bytes()

        plan.write_text(
            self._plan_body(
                [
                    ("AC-001", "The command permanently deletes user data."),
                    ("AC-002", "The full test suite passes."),
                ]
            ),
            encoding="utf-8",
        )
        plan_result = self._run_harness(
            "check",
            "--final",
            "--requirement",
            "REQ-001",
        )
        plan.write_bytes(original_plan)
        implementation.write_text(
            "# Implementation\n\nUnrelated replacement handoff.\n",
            encoding="utf-8",
        )
        implementation_result = self._run_harness(
            "check",
            "--final",
            "--requirement",
            "REQ-001",
        )
        implementation.write_bytes(original_implementation)
        state = self._load_state()
        state["goal"] = "A different goal after acceptance."
        self._write_state(state)
        goal_result = self._run_harness(
            "check",
            "--final",
            "--requirement",
            "REQ-001",
        )

        self.assertNotEqual(plan_result.returncode, 0)
        self.assertIn(
            "evaluation inputs changed since begin-evaluation",
            plan_result.stdout,
        )
        self.assertNotEqual(implementation_result.returncode, 0)
        self.assertIn(
            "evaluation inputs changed since begin-evaluation",
            implementation_result.stdout,
        )
        self.assertNotEqual(goal_result.returncode, 0)
        self.assertIn(
            "evaluation inputs changed since begin-evaluation",
            goal_result.stdout,
        )

    def test_restart_records_plan_drift_as_an_evaluation_input_change(self) -> None:
        self._prepare_evaluation()
        self._write_plan(
            self._plan_body(
                [
                    ("AC-001", "A revised version behavior is required."),
                    ("AC-002", "The full test suite passes."),
                ]
            )
        )

        restarted = self._restart("Plan semantics changed after evaluation began.")

        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        interruption = json.loads(
            self._interruption_path().read_text(encoding="utf-8")
        )
        self.assertNotEqual(
            interruption["observed"]["plan_sha256"],
            interruption["evaluation"]["plan_sha256"],
        )
        self.assertEqual(self._load_state()["phase"], "BUILDING")

    def test_restart_accepts_changed_acceptance_ids_as_plan_drift(self) -> None:
        self._prepare_evaluation()
        self._write_plan(
            self._plan_body(
                [
                    ("AC-003", "A replacement public behavior is required."),
                    ("AC-004", "The replacement test suite passes."),
                ]
            )
        )

        restarted = self._restart(
            "Acceptance criteria changed after evaluation began."
        )

        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        interruption = json.loads(
            self._interruption_path().read_text(encoding="utf-8")
        )
        self.assertNotEqual(
            interruption["observed"]["plan_sha256"],
            interruption["evaluation"]["plan_sha256"],
        )
        state = self._load_state()
        self.assertEqual(state["status"], "ACTIVE")
        self.assertEqual(state["phase"], "BUILDING")
        self.assertEqual(state["active_round"], 2)

    def test_restart_accepts_missing_implementation_as_input_drift(self) -> None:
        self._prepare_evaluation()
        (self._round_root() / "implementation.md").unlink()

        restarted = self._restart(
            "The evaluated implementation handoff was removed."
        )

        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        interruption = json.loads(
            self._interruption_path().read_text(encoding="utf-8")
        )
        self.assertEqual(
            interruption["observed"]["implementation_sha256"],
            "missing",
        )
        state = self._load_state()
        self.assertEqual(state["phase"], "BUILDING")
        self.assertEqual(state["active_round"], 2)

    def test_restart_preserves_missing_plan_before_blocking_for_repair(self) -> None:
        self._prepare_evaluation()
        (self._requirement_root() / "plan.md").unlink()
        reason = "The durable plan was removed after evaluation began."

        blocked = self._restart(reason)

        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn(
            "preserved the interruption but requires a valid plan.md",
            blocked.stderr,
        )
        interruption = json.loads(
            self._interruption_path().read_text(encoding="utf-8")
        )
        self.assertEqual(interruption["observed"]["plan_sha256"], "missing")
        pending = self._load_state()
        self.assertEqual(pending["status"], "BLOCKED")
        self.assertEqual(pending["phase"], "EVALUATING")
        self.assertEqual(pending["active_round"], 1)
        self.assertIsInstance(pending["pending_interruption"], dict)

        self._write_plan()
        retried = self._restart(reason)

        self.assertEqual(retried.returncode, 0, retried.stderr)
        state = self._load_state()
        self.assertEqual(state["status"], "ACTIVE")
        self.assertEqual(state["phase"], "BUILDING")
        self.assertEqual(state["active_round"], 2)
        self.assertIsNone(state["pending_interruption"])

    def test_check_rejects_tampered_interruption_bytes(self) -> None:
        self._prepare_evaluation()
        (self.target / "README.md").write_text(
            "# Drift before interruption\n",
            encoding="utf-8",
        )
        reason = "Preserve exact interruption evidence."
        self.assertEqual(self._restart(reason).returncode, 0)
        interruption = json.loads(
            self._interruption_path().read_text(encoding="utf-8")
        )
        interruption["reason"] = "A different but structurally valid reason."
        self._interruption_path().write_text(
            json.dumps(interruption, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "interruption digest changed after restart-evaluation",
            result.stdout,
        )

    def test_check_rejects_tampered_archived_evaluation_inputs(self) -> None:
        snapshot = self._prepare_evaluation()
        failed = self._review_source(snapshot, "FAIL")
        self.assertEqual(self._record(failed).returncode, 0)
        archived = (
            self._round_root()
            / "evaluation-inputs"
            / "implementation.md"
        )
        archived.write_text(
            "# Implementation\n\nForged historical handoff.\n",
            encoding="utf-8",
        )

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("archived implementation digest changed", result.stdout)

    def test_review_attempt_must_match_its_round_transaction(self) -> None:
        snapshot = self._prepare_evaluation()
        unverified = self._review_source(
            snapshot,
            "UNVERIFIED",
            name="unverified.md",
        )
        passed = self._review_source(snapshot, "PASS", name="pass.md")
        self.assertEqual(self._record(unverified).returncode, 0)
        self.assertEqual(self._record(passed).returncode, 0)
        state = self._load_state()
        replacement_fingerprint = "sha256:" + "0" * 64
        state["review_attempts"][0]["evaluation"][
            "workspace_fingerprint"
        ] = replacement_fingerprint
        self._write_state(state)
        archive = self._attempt_path()
        archive.write_text(
            archive.read_text(encoding="utf-8").replace(
                snapshot["workspace_fingerprint"],
                replacement_fingerprint,
            ),
            encoding="utf-8",
        )

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "must match its round evaluation transaction",
            result.stdout,
        )

    def test_null_verdict_requires_empty_review_digest(self) -> None:
        self._prepare_evaluation()
        state = self._load_state()
        state["evaluation"]["review_sha256"] = "sha256:" + "0" * 64
        self._write_state(state)

        result = self._run_harness("check", "--requirement", "REQ-001")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "null latest_verdict requires an empty review_sha256",
            result.stdout,
        )

    def test_pathological_active_round_is_rejected_without_history_scan(
        self,
    ) -> None:
        self.assertEqual(self._init().returncode, 0)
        state = self._load_state()
        state["active_round"] = 1_000_000
        self._write_state(state)

        started = time.monotonic()
        result = self._run_harness("check", "--requirement", "REQ-001")
        elapsed = time.monotonic() - started

        self.assertNotEqual(result.returncode, 0)
        self.assertLess(elapsed, 2.0)
        self.assertIn("active_round must not exceed 999", result.stdout)

    def test_round_limit_rejects_fail_and_restart_before_artifact_writes(
        self,
    ) -> None:
        snapshot = self._prepare_evaluation()
        failed = self._review_source(snapshot, "FAIL")
        before = self._state_path().read_bytes()
        with mock.patch.object(HARNESS_RUNTIME, "MAX_ROUNDS", 1):
            with self.assertRaisesRegex(
                HARNESS_RUNTIME.HarnessError,
                "maximum evaluation rounds reached before FAIL",
            ):
                HARNESS_RUNTIME.record_review(
                    self.target,
                    "REQ-001",
                    str(failed),
                )
        self.assertEqual(self._state_path().read_bytes(), before)
        self.assertFalse(self._review_path().exists())

        (self.target / "README.md").write_text(
            "# Drift at round limit\n",
            encoding="utf-8",
        )
        with mock.patch.object(HARNESS_RUNTIME, "MAX_ROUNDS", 1):
            with self.assertRaisesRegex(
                HARNESS_RUNTIME.HarnessError,
                "maximum evaluation rounds reached before restart",
            ):
                HARNESS_RUNTIME.restart_evaluation(
                    self.target,
                    "REQ-001",
                    "Cannot advance beyond the round limit.",
                )
        self.assertEqual(self._state_path().read_bytes(), before)
        self.assertFalse(self._interruption_path().exists())

    @unittest.skipIf(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        "POSIX non-root permissions required",
    )
    def test_begin_reports_write_permission_error_without_traceback(self) -> None:
        self.assertEqual(self._init().returncode, 0)
        self._write_plan()
        self._enter_building()
        self._write_implementation()
        round_root = self._round_root()
        round_root.chmod(0o500)
        try:
            result = self._begin_evaluation()
        finally:
            round_root.chmod(0o700)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertIn("filesystem operation failed", result.stderr)
        state = self._load_state()
        self.assertEqual(state["phase"], "BUILDING")
        self.assertIsNone(state["evaluation"])


if __name__ == "__main__":
    unittest.main()
