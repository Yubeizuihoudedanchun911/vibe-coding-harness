# Schema V3 Evaluation Transactions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make evaluation persistence crash-recoverable, bind acceptance to the exact evaluated Git workspace, require structured criterion evidence, and audit read-only role drift.

**Architecture:** `scripts/harness.py` remains the single standard-library runtime. Schema 3 introduces repository snapshots and explicit `snapshot`, `begin-evaluation`, `record-review`, and `accept` commands; `record-review` writes evidence before state and `init --resume` deterministically reconciles that crash window. `SKILL.md` owns role dispatch while the runtime enforces snapshot, review, and final-gate invariants.

**Tech Stack:** Python 3.10+ standard library, `unittest`, Git CLI, Markdown/JSON, Codex multi-agent tools.

## Global Constraints

- Schema 2 compatibility and migration are out of scope; schema 2 state must fail with an explicit error.
- Keep `scripts/harness.py` as the only runtime script.
- Exclude `.vibe-coding/` from product workspace fingerprints.
- Include tracked, staged, unstaged, non-ignored untracked, and submodule status in the fingerprint.
- Do not expose repository file contents in snapshot output.
- Root never writes business code.
- Planner and Evaluator are instruction-level read-only roles with before/after snapshot auditing.
- Generator remains the only authorized business-code writer.
- Preserve unrelated user changes; existing dirty content is evaluated as part of the snapshot.
- Use Python standard library only.
- Follow RED-GREEN-REFACTOR for every behavior change.

---

## File map

- Modify `scripts/harness.py`: schema 3, workspace snapshots, structured review parsing, lifecycle commands, reconciliation, and final validation.
- Modify `tests/test_harness.py`: subprocess and direct-function coverage for every new invariant and recovery transition.
- Modify `SKILL.md`: explicit lifecycle commands, stable acceptance IDs, review JSON contract, and audited read-only roles.
- Modify `tests/test_skill.py`: static contract checks for the schema 3 workflow and removal of obsolete schema 2 language.
- Modify `README.md`: public schema 3 workflow and safety claims.
- Modify `README.zh-CN.md`: Chinese public schema 3 workflow and safety claims.
- Modify `CHANGELOG.md`: breaking schema and evaluation transaction changes.
- Modify `agents/openai.yaml`: default prompt describes audited role boundaries and explicit Goal Gate commands.

### Task 1: Schema 3 and deterministic workspace snapshots

**Files:**
- Modify: `scripts/harness.py`
- Modify: `tests/test_harness.py`

**Interfaces:**
- Produces: `RepositorySnapshot(revision: str, workspace_fingerprint: str)`
- Produces: `_repository_snapshot(target: Path) -> RepositorySnapshot`
- Produces: `_review_sha256(body: str) -> str`
- Produces: `snapshot(target: Path) -> dict[str, str]`
- Produces CLI: `snapshot --target TARGET`

- [ ] **Step 1: Add failing schema and snapshot tests**

Add these imports to `tests/test_harness.py`:

```python
import os
```

Replace the schema 2 initialization assertions with:

```python
def test_init_creates_schema_three_state(self) -> None:
    result = self._init("Add --version")

    self.assertEqual(result.returncode, 0, result.stderr)
    state = self._load_state()
    self.assertEqual(state["schema_version"], 3)
    self.assertEqual(state["accepted_revision"], "")
    self.assertIsNone(state["evaluation"])
    self.assertNotIn("last_good_revision", state)

def test_schema_two_is_rejected_without_migration(self) -> None:
    self.assertEqual(self._init().returncode, 0)
    state = self._load_state()
    state["schema_version"] = 2
    self._write_state("REQ-001", state)

    result = self._run_harness("init", "--resume", "--requirement", "REQ-001")

    self.assertNotEqual(result.returncode, 0)
    self.assertIn("schema_version 2 is unsupported; expected 3", result.stderr)
```

Add a helper and snapshot tests:

```python
def _snapshot(self) -> dict[str, str]:
    result = self._run_harness("snapshot")
    self.assertEqual(result.returncode, 0, result.stderr)
    return json.loads(result.stdout)

def test_snapshot_changes_for_tracked_staged_and_untracked_content(self) -> None:
    clean = self._snapshot()

    (self.target / "README.md").write_text("# unstaged\n", encoding="utf-8")
    unstaged = self._snapshot()
    self.assertNotEqual(unstaged["workspace_fingerprint"], clean["workspace_fingerprint"])

    self._git("add", "README.md")
    staged = self._snapshot()
    self.assertNotEqual(staged["workspace_fingerprint"], unstaged["workspace_fingerprint"])

    (self.target / "new.txt").write_text("untracked\n", encoding="utf-8")
    untracked = self._snapshot()
    self.assertNotEqual(untracked["workspace_fingerprint"], staged["workspace_fingerprint"])

def test_snapshot_hashes_untracked_content_not_only_its_path(self) -> None:
    path = self.target / "new.txt"
    path.write_text("one\n", encoding="utf-8")
    first = self._snapshot()
    path.write_text("two\n", encoding="utf-8")
    second = self._snapshot()
    self.assertNotEqual(first["workspace_fingerprint"], second["workspace_fingerprint"])

def test_harness_state_does_not_change_the_product_snapshot(self) -> None:
    before = self._snapshot()
    self.assertEqual(self._init().returncode, 0)
    after = self._snapshot()
    self.assertEqual(after, before)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_harness.HarnessCliTests.test_init_creates_schema_three_state \
  tests.test_harness.HarnessCliTests.test_schema_two_is_rejected_without_migration \
  tests.test_harness.HarnessCliTests.test_snapshot_changes_for_tracked_staged_and_untracked_content \
  tests.test_harness.HarnessCliTests.test_snapshot_hashes_untracked_content_not_only_its_path \
  tests.test_harness.HarnessCliTests.test_harness_state_does_not_change_the_product_snapshot -v
```

Expected: failures because schema remains 2 and the `snapshot` command does not exist.

- [ ] **Step 3: Implement schema 3 and snapshot primitives**

Add imports:

```python
import hashlib
```

Replace the schema constant and add fingerprint constants:

```python
SCHEMA_VERSION = 3
FINGERPRINT_PREFIX = "sha256:"
FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
```

Add the immutable snapshot type:

```python
@dataclass(frozen=True)
class RepositorySnapshot:
    revision: str
    workspace_fingerprint: str

    def as_dict(self) -> dict[str, str]:
        return {
            "revision": self.revision,
            "workspace_fingerprint": self.workspace_fingerprint,
        }
```

Add deterministic Git helpers:

```python
def _git_bytes(target: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(target), *arguments],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise HarnessError(message or f"git {' '.join(arguments)} failed")
    return result.stdout


def _hash_part(digest: Any, label: bytes, value: bytes) -> None:
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _untracked_fingerprint_stream(target: Path) -> bytes:
    raw_paths = _git_bytes(
        target,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        ".",
        ":(exclude).vibe-coding",
        ":(exclude).vibe-coding/**",
    )
    output = bytearray()
    for raw_path in sorted(path for path in raw_paths.split(b"\0") if path):
        relative = os.fsdecode(raw_path)
        path = target / relative
        metadata = path.lstat()
        output.extend(len(raw_path).to_bytes(4, "big"))
        output.extend(raw_path)
        output.extend(metadata.st_mode.to_bytes(8, "big"))
        if path.is_symlink():
            body = os.fsencode(os.readlink(path))
        elif path.is_file():
            body = hashlib.sha256(path.read_bytes()).digest()
        else:
            body = b"special"
        output.extend(len(body).to_bytes(8, "big"))
        output.extend(body)
    return bytes(output)


def _repository_snapshot(target: Path) -> RepositorySnapshot:
    digest = hashlib.sha256()
    pathspec = (".", ":(exclude).vibe-coding", ":(exclude).vibe-coding/**")
    status = _git_bytes(
        target,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--",
        *pathspec,
    )
    staged = _git_bytes(
        target,
        "diff",
        "--cached",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--",
        *pathspec,
    )
    unstaged = _git_bytes(
        target,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--",
        *pathspec,
    )
    _hash_part(digest, b"status", status)
    _hash_part(digest, b"staged", staged)
    _hash_part(digest, b"unstaged", unstaged)
    _hash_part(digest, b"untracked", _untracked_fingerprint_stream(target))
    return RepositorySnapshot(
        revision=_revision(target),
        workspace_fingerprint=FINGERPRINT_PREFIX + digest.hexdigest(),
    )


def snapshot(target: Path) -> dict[str, str]:
    return _repository_snapshot(target).as_dict()


def _review_sha256(body: str) -> str:
    return FINGERPRINT_PREFIX + hashlib.sha256(body.encode("utf-8")).hexdigest()
```

Change initialization state:

```python
state = {
    "schema_version": SCHEMA_VERSION,
    "requirement_id": new_id,
    "goal": goal,
    "status": "ACTIVE",
    "phase": "PLANNING",
    "active_round": 1,
    "next_action": "Dispatch Planner and persist plan.md.",
    "accepted_revision": "",
    "evaluation": None,
    "latest_verdict": None,
    "residual_risks": [],
}
```

In `_validate_state`, reject old schemas before other field checks:

```python
schema_version = state.get("schema_version")
if schema_version != SCHEMA_VERSION:
    if schema_version == 2:
        errors.append("schema_version 2 is unsupported; expected 3; migration is disabled")
    else:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
```

Remove `last_good_revision` validation and require:

```python
accepted_revision = state.get("accepted_revision")
if not isinstance(accepted_revision, str):
    errors.append("accepted_revision must be a string")
if "evaluation" not in state:
    errors.append("evaluation field is required")
```

Add the CLI parser and dispatch:

```python
snapshot_parser = subparsers.add_parser(
    "snapshot", help="fingerprint the current product workspace"
)
snapshot_parser.add_argument("--target", required=True)
```

```python
if args.command == "snapshot":
    _emit(snapshot(target))
    return 0
```

- [ ] **Step 4: Run focused tests and all harness tests**

Run the focused command from Step 2.

Expected: all five focused tests pass.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_harness -v
```

Expected: the focused tests pass. Before committing, add this fixture helper and
replace accepted-state dictionaries so every accepted fixture has schema 3
snapshot fields:

```python
def _accepted_snapshot_fields(self, review_body: str) -> dict[str, object]:
    current = self._snapshot()
    return {
        "accepted_revision": current["revision"],
        "evaluation": {
            **current,
            "review_sha256": HARNESS_MODULE._review_sha256(review_body),
        },
    }
```

Use `**self._accepted_snapshot_fields(review_body)` in each accepted fixture
instead of `last_good_revision`. Run the entire harness suite and require zero
failures before Step 5.

- [ ] **Step 5: Commit Task 1**

```bash
git add scripts/harness.py tests/test_harness.py
git diff --cached --check
git commit -m "feat: add schema v3 workspace snapshots"
```

### Task 2: Structured evaluation records

**Files:**
- Modify: `scripts/harness.py`
- Modify: `tests/test_harness.py`

**Interfaces:**
- Consumes: `RepositorySnapshot`
- Produces: `_acceptance_criteria(body: str) -> list[str]`
- Produces: `_evaluation_record(body: str) -> dict[str, Any]`
- Produces: `_validate_evaluation_record(record, criterion_ids, expected_snapshot) -> list[str]`
- Consumes: `_review_sha256(body: str) -> str`

- [ ] **Step 1: Add failing plan and review validation tests**

Add helpers:

```python
def _plan_body(self) -> str:
    return (
        "# Plan\n\n"
        "## Acceptance criteria\n\n"
        "- AC-001: Public CLI exports CSV.\n"
        "- AC-002: UTF-8 values round-trip.\n"
    )

def _review_body(
    self,
    snapshot: dict[str, str],
    *,
    verdict: str = "PASS",
    first_verdict: str = "PASS",
    second_verdict: str = "PASS",
) -> str:
    record = {
        "schema_version": 1,
        "revision": snapshot["revision"],
        "workspace_fingerprint": snapshot["workspace_fingerprint"],
        "verdict": verdict,
        "criteria": [
            {
                "id": "AC-001",
                "verdict": first_verdict,
                "evidence_ids": ["EV-001"],
            },
            {
                "id": "AC-002",
                "verdict": second_verdict,
                "evidence_ids": ["EV-002"],
            },
        ],
        "evidence": [
            {
                "id": "EV-001",
                "kind": "command",
                "command": "tool export --format csv",
                "exit_code": 0,
                "result": "Created report.csv with the expected rows.",
            },
            {
                "id": "EV-002",
                "kind": "inspection",
                "subject": "report.csv",
                "result": "UTF-8 text parsed and matched the fixture.",
            },
        ],
        "residual_risks": [],
    }
    return (
        "# Review\n\n## Evaluation record\n\n```json\n"
        + json.dumps(record, ensure_ascii=False, indent=2)
        + "\n```\n"
    )
```

Add tests:

```python
def test_acceptance_criteria_require_stable_unique_ids(self) -> None:
    valid = HARNESS_MODULE._acceptance_criteria(self._plan_body())
    self.assertEqual(valid, ["AC-001", "AC-002"])
    with self.assertRaisesRegex(
        HARNESS_MODULE.HarnessError, "duplicate acceptance criterion AC-001"
    ):
        HARNESS_MODULE._acceptance_criteria(
            "## Acceptance criteria\n\n- AC-001: one\n- AC-001: two\n"
        )

def test_review_record_must_cover_every_planned_criterion(self) -> None:
    snapshot = self._snapshot()
    record = json.loads(
        HARNESS_MODULE._markdown_level_two_section(
            self._review_body(snapshot), "Evaluation record"
        ).split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    record["criteria"].pop()

    errors = HARNESS_MODULE._validate_evaluation_record(
        record,
        ["AC-001", "AC-002"],
        HARNESS_MODULE.RepositorySnapshot(**snapshot),
    )

    self.assertIn("criteria must exactly match plan acceptance IDs", errors)

def test_bare_command_is_not_sufficient_pass_evidence(self) -> None:
    snapshot = self._snapshot()
    record = HARNESS_MODULE._evaluation_record(self._review_body(snapshot))
    record["evidence"][0].pop("result")

    errors = HARNESS_MODULE._validate_evaluation_record(
        record,
        ["AC-001", "AC-002"],
        HARNESS_MODULE.RepositorySnapshot(**snapshot),
    )

    self.assertIn("command evidence EV-001 requires a non-empty result", errors)

def test_overall_verdict_is_derived_from_criterion_verdicts(self) -> None:
    snapshot = self._snapshot()
    record = HARNESS_MODULE._evaluation_record(
        self._review_body(snapshot, verdict="PASS", second_verdict="UNVERIFIED")
    )

    errors = HARNESS_MODULE._validate_evaluation_record(
        record,
        ["AC-001", "AC-002"],
        HARNESS_MODULE.RepositorySnapshot(**snapshot),
    )

    self.assertIn("overall verdict must be UNVERIFIED", errors)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_harness.HarnessCliTests.test_acceptance_criteria_require_stable_unique_ids \
  tests.test_harness.HarnessCliTests.test_review_record_must_cover_every_planned_criterion \
  tests.test_harness.HarnessCliTests.test_bare_command_is_not_sufficient_pass_evidence \
  tests.test_harness.HarnessCliTests.test_overall_verdict_is_derived_from_criterion_verdicts -v
```

Expected: errors because the structured parsers do not exist.

- [ ] **Step 3: Implement exact plan and review parsers**

Add:

```python
ACCEPTANCE_PATTERN = re.compile(r"^-\s+(AC-\d{3}):\s+\S.*$", re.MULTILINE)
REVIEW_SCHEMA_VERSION = 1
EVIDENCE_KINDS = {"command", "inspection"}


def _acceptance_criteria(body: str) -> list[str]:
    section = _markdown_level_two_section(body, "Acceptance criteria")
    if section is None:
        raise HarnessError("plan requires exactly one ## Acceptance criteria section")
    identifiers = ACCEPTANCE_PATTERN.findall(section)
    if not identifiers:
        raise HarnessError("plan requires at least one AC-NNN acceptance criterion")
    seen: set[str] = set()
    for identifier in identifiers:
        if identifier in seen:
            raise HarnessError(f"duplicate acceptance criterion {identifier}")
        seen.add(identifier)
    return identifiers


def _evaluation_record(body: str) -> dict[str, Any]:
    section = _markdown_level_two_section(body, "Evaluation record")
    if section is None:
        raise HarnessError("review requires exactly one ## Evaluation record section")
    match = re.fullmatch(
        r"\s*```json[ \t]*\n(?P<payload>.*)\n```[ \t]*\s*",
        section,
        flags=re.DOTALL,
    )
    if match is None:
        raise HarnessError("Evaluation record must contain one JSON fence")
    try:
        value = json.loads(match.group("payload"))
    except json.JSONDecodeError as error:
        raise HarnessError(f"invalid Evaluation record JSON: {error.msg}") from error
    if not isinstance(value, dict):
        raise HarnessError("Evaluation record must be a JSON object")
    return value

```

Implement `_validate_evaluation_record` with these exact checks:

```python
def _validate_evaluation_record(
    record: dict[str, Any],
    criterion_ids: list[str],
    expected_snapshot: RepositorySnapshot,
) -> list[str]:
    errors: list[str] = []
    if record.get("schema_version") != REVIEW_SCHEMA_VERSION:
        errors.append("evaluation record schema_version must be 1")
    if record.get("revision") != expected_snapshot.revision:
        errors.append("evaluation revision must match the pending snapshot")
    if record.get("workspace_fingerprint") != expected_snapshot.workspace_fingerprint:
        errors.append("evaluation fingerprint must match the pending snapshot")

    criteria = record.get("criteria")
    if not isinstance(criteria, list):
        errors.append("criteria must be a list")
        criteria = []
    criterion_map: dict[str, dict[str, Any]] = {}
    for item in criteria:
        if not isinstance(item, dict) or not _non_empty_string(item.get("id")):
            errors.append("each criterion must be an object with an id")
            continue
        identifier = item["id"]
        if identifier in criterion_map:
            errors.append(f"duplicate criterion result {identifier}")
        criterion_map[identifier] = item
    if list(criterion_map) != criterion_ids:
        errors.append("criteria must exactly match plan acceptance IDs")

    evidence = record.get("evidence")
    if not isinstance(evidence, list):
        errors.append("evidence must be a list")
        evidence = []
    evidence_map: dict[str, dict[str, Any]] = {}
    for item in evidence:
        if not isinstance(item, dict) or not _non_empty_string(item.get("id")):
            errors.append("each evidence item must be an object with an id")
            continue
        identifier = item["id"]
        if identifier in evidence_map:
            errors.append(f"duplicate evidence item {identifier}")
        evidence_map[identifier] = item
        kind = item.get("kind")
        if kind not in EVIDENCE_KINDS:
            errors.append(f"evidence {identifier} kind must be command or inspection")
        elif kind == "command":
            if not _non_empty_string(item.get("command")):
                errors.append(f"command evidence {identifier} requires a command")
            if not isinstance(item.get("exit_code"), int) or isinstance(
                item.get("exit_code"), bool
            ):
                errors.append(
                    f"command evidence {identifier} requires an integer exit_code"
                )
            if not _non_empty_string(item.get("result")):
                errors.append(
                    f"command evidence {identifier} requires a non-empty result"
                )
        else:
            if not _non_empty_string(item.get("subject")):
                errors.append(
                    f"inspection evidence {identifier} requires a subject"
                )
            if not _non_empty_string(item.get("result")):
                errors.append(
                    f"inspection evidence {identifier} requires a non-empty result"
                )

    verdicts: list[str] = []
    for identifier in criterion_ids:
        item = criterion_map.get(identifier)
        if item is None:
            continue
        verdict = item.get("verdict")
        if verdict not in {"PASS", "FAIL", "UNVERIFIED"}:
            errors.append(f"criterion {identifier} has an invalid verdict")
            continue
        verdicts.append(verdict)
        evidence_ids = item.get("evidence_ids")
        if not isinstance(evidence_ids, list):
            errors.append(f"criterion {identifier} evidence_ids must be a list")
            continue
        if verdict == "PASS" and not evidence_ids:
            errors.append(f"PASS criterion {identifier} requires evidence")
        for evidence_id in evidence_ids:
            if evidence_id not in evidence_map:
                errors.append(
                    f"criterion {identifier} references unknown evidence {evidence_id}"
                )

    derived = (
        "FAIL"
        if "FAIL" in verdicts
        else "UNVERIFIED"
        if "UNVERIFIED" in verdicts
        else "PASS"
        if len(verdicts) == len(criterion_ids)
        else None
    )
    if record.get("verdict") != derived:
        errors.append(f"overall verdict must be {derived}")
    if not isinstance(record.get("residual_risks"), list):
        errors.append("residual_risks must be a list")
    return errors
```

- [ ] **Step 4: Run focused and harness tests**

Run the focused command from Step 2, then:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_harness -v
```

Expected: focused tests pass. Existing old-heading review tests fail and are replaced in Tasks 3 and 4 with evaluation-record tests rather than relaxed.

- [ ] **Step 5: Commit Task 2**

```bash
git add scripts/harness.py tests/test_harness.py
git diff --cached --check
git commit -m "feat: validate structured evaluation records"
```

### Task 3: Evaluation lifecycle commands and crash reconciliation

**Files:**
- Modify: `scripts/harness.py`
- Modify: `tests/test_harness.py`

**Interfaces:**
- Consumes: repository snapshot and structured review interfaces from Tasks 1 and 2
- Produces: `begin_evaluation(target, requirement_id) -> dict[str, Any]`
- Produces: `record_review(target, requirement_id, review_source) -> dict[str, Any]`
- Produces: `_reconcile_pending_review(paths, target, state) -> tuple[dict[str, Any], bool]`
- Produces CLI: `begin-evaluation`, `record-review`

- [ ] **Step 1: Add failing lifecycle tests**

Add a fixture helper:

```python
def _prepare_building(self) -> dict[str, str]:
    self.assertEqual(self._init().returncode, 0)
    self._write_artifact(
        ".vibe-coding/requirements/REQ-001/plan.md", self._plan_body()
    )
    self._write_artifact(
        ".vibe-coding/requirements/REQ-001/rounds/001/implementation.md",
        "# Implementation\n\nImplemented and tested.\n",
    )
    state = self._load_state()
    state.update(
        {
            "phase": "BUILDING",
            "next_action": "Begin evaluation.",
        }
    )
    self._write_state("REQ-001", state)
    return self._snapshot()

def _review_source(self, body: str) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".md",
        delete=False,
    ) as handle:
        handle.write(body)
        path = Path(handle.name)
    self.addCleanup(path.unlink, missing_ok=True)
    return path
```

Add tests:

```python
def test_begin_evaluation_captures_pending_snapshot(self) -> None:
    expected = self._prepare_building()

    result = self._run_harness(
        "begin-evaluation", "--requirement", "REQ-001"
    )

    self.assertEqual(result.returncode, 0, result.stderr)
    state = self._load_state()
    self.assertEqual(state["phase"], "EVALUATING")
    self.assertIsNone(state["latest_verdict"])
    self.assertEqual(
        state["evaluation"],
        {**expected, "review_sha256": ""},
    )

def test_record_review_rejects_workspace_drift(self) -> None:
    self._prepare_building()
    begin = self._run_harness(
        "begin-evaluation", "--requirement", "REQ-001"
    )
    pending = json.loads(begin.stdout)["evaluation"]
    review = self._review_source(self._review_body(pending))
    (self.target / "README.md").write_text("# drift\n", encoding="utf-8")

    result = self._run_harness(
        "record-review",
        "--requirement",
        "REQ-001",
        "--review-source",
        str(review),
    )

    self.assertNotEqual(result.returncode, 0)
    self.assertIn("workspace changed during evaluation", result.stderr)
    self.assertFalse((self._round_root() / "review.md").exists())

def test_record_review_requires_source_outside_target(self) -> None:
    self._prepare_building()
    begin = self._run_harness(
        "begin-evaluation", "--requirement", "REQ-001"
    )
    pending = json.loads(begin.stdout)["evaluation"]
    review = self.target / "candidate-review.md"
    review.write_text(self._review_body(pending), encoding="utf-8")

    result = self._run_harness(
        "record-review",
        "--requirement",
        "REQ-001",
        "--review-source",
        str(review),
    )

    self.assertNotEqual(result.returncode, 0)
    self.assertIn("review source must be outside the target repository", result.stderr)

def test_resume_reconciles_review_written_before_state(self) -> None:
    self._prepare_building()
    begin = self._run_harness(
        "begin-evaluation", "--requirement", "REQ-001"
    )
    pending = json.loads(begin.stdout)["evaluation"]
    review_body = self._review_body(pending)
    self._write_artifact(
        ".vibe-coding/requirements/REQ-001/rounds/001/review.md",
        review_body,
    )

    result = self._run_harness(
        "init", "--resume", "--requirement", "REQ-001"
    )

    self.assertEqual(result.returncode, 0, result.stderr)
    state = self._load_state()
    self.assertEqual(state["latest_verdict"], "PASS")
    self.assertEqual(
        state["evaluation"]["review_sha256"],
        HARNESS_MODULE._review_sha256(review_body),
    )

def test_resume_reconciles_replacement_review_before_state(self) -> None:
    self._prepare_building()
    begin = self._run_harness(
        "begin-evaluation", "--requirement", "REQ-001"
    )
    pending = json.loads(begin.stdout)["evaluation"]
    first = self._review_body(
        pending,
        verdict="UNVERIFIED",
        first_verdict="UNVERIFIED",
        second_verdict="UNVERIFIED",
    )
    source = self._review_source(first)
    recorded = self._run_harness(
        "record-review",
        "--requirement",
        "REQ-001",
        "--review-source",
        str(source),
    )
    self.assertEqual(recorded.returncode, 0, recorded.stderr)

    replacement = self._review_body(pending)
    (self._round_root() / "review.md").write_text(
        replacement, encoding="utf-8"
    )
    resumed = self._run_harness(
        "init", "--resume", "--requirement", "REQ-001"
    )

    self.assertEqual(resumed.returncode, 0, resumed.stderr)
    state = self._load_state()
    self.assertEqual(state["latest_verdict"], "PASS")
    self.assertEqual(
        state["evaluation"]["review_sha256"],
        HARNESS_MODULE._review_sha256(replacement),
    )
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_harness.HarnessCliTests.test_begin_evaluation_captures_pending_snapshot \
  tests.test_harness.HarnessCliTests.test_record_review_rejects_workspace_drift \
  tests.test_harness.HarnessCliTests.test_record_review_requires_source_outside_target \
  tests.test_harness.HarnessCliTests.test_resume_reconciles_review_written_before_state \
  tests.test_harness.HarnessCliTests.test_resume_reconciles_replacement_review_before_state -v
```

Expected: failures because the lifecycle commands do not exist and resume rejects the crash state.

- [ ] **Step 3: Implement lifecycle state helpers**

Add:

```python
def _snapshot_from_evaluation(value: Any) -> RepositorySnapshot:
    if not isinstance(value, dict):
        raise HarnessError("evaluation must be an object")
    revision = value.get("revision")
    fingerprint = value.get("workspace_fingerprint")
    if not isinstance(revision, str) or not re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision
    ):
        raise HarnessError("evaluation revision must be a canonical full commit OID")
    if not isinstance(fingerprint, str) or not FINGERPRINT_PATTERN.fullmatch(
        fingerprint
    ):
        raise HarnessError("evaluation workspace_fingerprint is invalid")
    return RepositorySnapshot(revision, fingerprint)


def _apply_review_transition(
    state: dict[str, Any],
    record: dict[str, Any],
    review_sha256: str,
) -> None:
    verdict = record["verdict"]
    state["residual_risks"] = record["residual_risks"]
    if verdict == "FAIL":
        state["latest_verdict"] = "FAIL"
        state["active_round"] += 1
        state["phase"] = "BUILDING"
        state["evaluation"] = None
        state["next_action"] = "Dispatch Generator with the failed review."
        return
    state["latest_verdict"] = verdict
    state["evaluation"]["review_sha256"] = review_sha256
    state["phase"] = "EVALUATING"
    state["next_action"] = (
        "Run the Goal Gate and accept the evaluated snapshot."
        if verdict == "PASS"
        else "Ask Evaluator for evidence on the same snapshot."
    )
```

Add an atomic text writer reusing `_write_state` semantics:

```python
def _write_text_atomic(path: Path, body: str, label: str) -> None:
    _reject_symlink(path, label)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    _reject_symlink(temporary, label + " temporary file")
    try:
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
```

- [ ] **Step 4: Implement `begin_evaluation`, `record_review`, and reconciliation**

Add:

```python
def begin_evaluation(target: Path, requirement_id: str) -> dict[str, Any]:
    _, requirements_root = _control_paths(target)
    paths = _select_requirement(requirements_root, requirement_id)
    state = _load_requirement_state(paths)
    if state.get("status") != "ACTIVE" or state.get("phase") != "BUILDING":
        raise HarnessError("begin-evaluation requires ACTIVE/BUILDING")
    plan = _read_artifact(paths.plan)
    implementation = _read_artifact(paths.round(state["active_round"]).implementation)
    if not plan.body or not plan.body.strip():
        raise HarnessError("begin-evaluation requires non-empty plan.md")
    _acceptance_criteria(plan.body)
    if not implementation.body or not implementation.body.strip():
        raise HarnessError("begin-evaluation requires current implementation.md")
    current = paths.round(state["active_round"])
    if current.review.exists() or current.review.is_symlink():
        raise HarnessError("begin-evaluation requires current review.md to be absent")
    pending = _repository_snapshot(target)
    state["phase"] = "EVALUATING"
    state["latest_verdict"] = None
    state["evaluation"] = {**pending.as_dict(), "review_sha256": ""}
    state["next_action"] = "Record the Evaluator review."
    _write_state(paths.state, state)
    return {
        "requirement_id": paths.root.name,
        "evaluation": state["evaluation"],
    }


def _validated_review(
    body: str,
    plan_body: str,
    expected: RepositorySnapshot,
) -> dict[str, Any]:
    record = _evaluation_record(body)
    errors = _validate_evaluation_record(
        record, _acceptance_criteria(plan_body), expected
    )
    if errors:
        raise HarnessError("invalid evaluation record: " + "; ".join(errors))
    return record


def record_review(
    target: Path,
    requirement_id: str,
    review_source: Path,
) -> dict[str, Any]:
    _, requirements_root = _control_paths(target)
    paths = _select_requirement(requirements_root, requirement_id)
    state = _load_requirement_state(paths)
    if state.get("status") != "ACTIVE" or state.get("phase") != "EVALUATING":
        raise HarnessError("record-review requires ACTIVE/EVALUATING")
    if state.get("latest_verdict") not in {None, "PASS", "UNVERIFIED"}:
        raise HarnessError(
            "record-review requires a pending, PASS, or UNVERIFIED evaluation"
        )
    if review_source.is_symlink():
        raise HarnessError("review source must be a regular non-symlink file")
    try:
        source = review_source.resolve(strict=True)
    except OSError as error:
        raise HarnessError(f"cannot resolve review source: {error}") from error
    if source.is_relative_to(target):
        raise HarnessError("review source must be outside the target repository")
    if not source.is_file():
        raise HarnessError("review source must be a regular non-symlink file")
    expected = _snapshot_from_evaluation(state.get("evaluation"))
    current = _repository_snapshot(target)
    if current != expected:
        raise HarnessError("workspace changed during evaluation")
    try:
        body = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise HarnessError(f"cannot read review source: {error}") from error
    plan_body = _read_artifact(paths.plan).body
    if plan_body is None:
        raise HarnessError("record-review requires plan.md")
    record = _validated_review(body, plan_body, expected)
    review_path = paths.round(state["active_round"]).review
    if state.get("latest_verdict") is None and (
        review_path.exists() or review_path.is_symlink()
    ):
        raise HarnessError("first record-review requires review.md to be absent")
    if state.get("latest_verdict") in {"PASS", "UNVERIFIED"} and not review_path.is_file():
        raise HarnessError("replacement record-review requires existing review.md")
    _write_text_atomic(review_path, body, "review.md")
    digest = _review_sha256(body)
    _apply_review_transition(state, record, digest)
    _write_state(paths.state, state)
    return {
        "requirement_id": paths.root.name,
        "verdict": record["verdict"],
        "review_sha256": digest,
        "active_round": state["active_round"],
        "phase": state["phase"],
    }
```

Implement `_reconcile_pending_review` with the same parser and transition:

```python
def _reconcile_pending_review(
    paths: RequirementPaths,
    target: Path,
    state: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    if state.get("phase") != "EVALUATING" or state.get("latest_verdict") not in {
        None,
        "PASS",
        "UNVERIFIED",
    }:
        return state, False
    review = _read_artifact(paths.round(state["active_round"]).review)
    if not review.exists:
        return state, False
    if review.error is not None or review.body is None:
        raise HarnessError(f"cannot reconcile review.md: {review.error}")
    digest = _review_sha256(review.body)
    evaluation = state.get("evaluation")
    if (
        isinstance(evaluation, dict)
        and evaluation.get("review_sha256") == digest
    ):
        return state, False
    expected = _snapshot_from_evaluation(state.get("evaluation"))
    plan = _read_artifact(paths.plan)
    if plan.body is None:
        raise HarnessError("cannot reconcile review.md without plan.md")
    record = _validated_review(review.body, plan.body, expected)
    _apply_review_transition(state, record, digest)
    _write_state(paths.state, state)
    return state, True
```

Call it in `init(..., resume=True)` after loading state and before `_validate_state`.

Add CLI parsers and dispatch for both commands. Require `--requirement` on both and `--review-source` on `record-review`.

- [ ] **Step 5: Run focused tests, replace obsolete crash rejection, and run harness suite**

Run the focused command from Step 2.

Expected: all pass.

Delete `test_evaluating_null_rejects_an_existing_current_review`; its expected
behavior is replaced by the reconciliation tests. Retain malformed UTF-8 and
directory rejection tests, updating expected errors to `cannot reconcile
review.md`.

Replace historical verdict parsing with the structured record:

```python
try:
    history_verdict = _evaluation_record(history_review)["verdict"]
except (HarnessError, KeyError) as error:
    errors.append(
        f"history round {history_number:03d} has invalid review.md: {error}"
    )
    history_verdict = None
if history_verdict != "FAIL":
    errors.append(
        f"history round {history_number:03d} verdict must be FAIL"
    )
```

For current PASS or UNVERIFIED state, parse the current record, require its
verdict to equal `latest_verdict`, and require `_review_sha256(review_body)` to
equal `state["evaluation"]["review_sha256"]`.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_harness -v
```

Expected: all lifecycle tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add scripts/harness.py tests/test_harness.py
git diff --cached --check
git commit -m "feat: add recoverable evaluation transitions"
```

### Task 4: Acceptance command and exact final gate

**Files:**
- Modify: `scripts/harness.py`
- Modify: `tests/test_harness.py`

**Interfaces:**
- Consumes: completed PASS evaluation state from Task 3
- Produces: `accept(target, requirement_id) -> dict[str, Any]`
- Produces CLI: `accept`
- Strengthens: `_validate_state(..., require_current_snapshot: bool)`

- [ ] **Step 1: Add failing acceptance drift and review-binding tests**

Add:

```python
def _prepare_pass_evaluation(self) -> None:
    self._prepare_building()
    begin = self._run_harness(
        "begin-evaluation", "--requirement", "REQ-001"
    )
    pending = json.loads(begin.stdout)["evaluation"]
    review = self._review_source(self._review_body(pending))
    result = self._run_harness(
        "record-review",
        "--requirement",
        "REQ-001",
        "--review-source",
        str(review),
    )
    self.assertEqual(result.returncode, 0, result.stderr)

def test_accept_rejects_uncommitted_tracked_drift(self) -> None:
    self._prepare_pass_evaluation()
    (self.target / "README.md").write_text("# drift\n", encoding="utf-8")

    result = self._run_harness("accept", "--requirement", "REQ-001")

    self.assertNotEqual(result.returncode, 0)
    self.assertIn("workspace no longer matches the evaluated snapshot", result.stderr)
    self.assertEqual(self._load_state()["status"], "ACTIVE")

def test_accept_rejects_changed_review_bytes(self) -> None:
    self._prepare_pass_evaluation()
    review = self._round_root() / "review.md"
    review.write_text(review.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")

    result = self._run_harness("accept", "--requirement", "REQ-001")

    self.assertNotEqual(result.returncode, 0)
    self.assertIn("review.md hash does not match evaluation", result.stderr)

def test_accept_records_exact_evaluated_revision(self) -> None:
    self._prepare_pass_evaluation()

    result = self._run_harness("accept", "--requirement", "REQ-001")

    self.assertEqual(result.returncode, 0, result.stderr)
    state = self._load_state()
    self.assertEqual(state["status"], "ACCEPTED")
    self.assertEqual(
        state["accepted_revision"], state["evaluation"]["revision"]
    )
    final = self._run_harness(
        "check", "--requirement", "REQ-001", "--final"
    )
    self.assertEqual(final.returncode, 0, final.stdout + final.stderr)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_harness.HarnessCliTests.test_accept_rejects_uncommitted_tracked_drift \
  tests.test_harness.HarnessCliTests.test_accept_rejects_changed_review_bytes \
  tests.test_harness.HarnessCliTests.test_accept_records_exact_evaluated_revision -v
```

Expected: errors because `accept` does not exist.

- [ ] **Step 3: Implement review binding and `accept`**

Add:

```python
def _validated_completed_review(
    paths: RequirementPaths,
    state: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    evaluation = state.get("evaluation")
    expected = _snapshot_from_evaluation(evaluation)
    review = _read_artifact(paths.round(state["active_round"]).review)
    if review.error is not None or review.body is None:
        raise HarnessError("completed evaluation requires readable review.md")
    digest = _review_sha256(review.body)
    if evaluation.get("review_sha256") != digest:
        raise HarnessError("review.md hash does not match evaluation")
    plan = _read_artifact(paths.plan)
    if plan.body is None:
        raise HarnessError("completed evaluation requires plan.md")
    return _validated_review(review.body, plan.body, expected), digest


def accept(target: Path, requirement_id: str) -> dict[str, Any]:
    _, requirements_root = _control_paths(target)
    paths = _select_requirement(requirements_root, requirement_id)
    state = _load_requirement_state(paths)
    if state.get("status") == "ACCEPTED":
        errors = _validate_state(
            paths, target, state=state, require_current_snapshot=True
        )
        if errors:
            raise HarnessError("accepted state is no longer valid: " + "; ".join(errors))
        return {
            "requirement_id": paths.root.name,
            "status": "ACCEPTED",
            "accepted_revision": state["accepted_revision"],
        }
    if (
        state.get("status") != "ACTIVE"
        or state.get("phase") != "EVALUATING"
        or state.get("latest_verdict") != "PASS"
    ):
        raise HarnessError("accept requires ACTIVE/EVALUATING with PASS")
    record, _ = _validated_completed_review(paths, state)
    expected = _snapshot_from_evaluation(state["evaluation"])
    current = _repository_snapshot(target)
    if current != expected:
        raise HarnessError("workspace no longer matches the evaluated snapshot")
    candidate = dict(state)
    candidate["status"] = "ACCEPTED"
    candidate["accepted_revision"] = expected.revision
    candidate["next_action"] = "Delivery accepted at the evaluated snapshot."
    errors = _validate_state(
        paths, target, state=candidate, require_current_snapshot=True
    )
    if errors:
        raise HarnessError("acceptance invariant failed: " + "; ".join(errors))
    _write_state(paths.state, candidate)
    return {
        "requirement_id": paths.root.name,
        "status": "ACCEPTED",
        "accepted_revision": candidate["accepted_revision"],
        "verdict": record["verdict"],
    }
```

Rename `_validate_state` parameter to `require_current_snapshot`. For accepted state add:

```python
if status == "ACCEPTED":
    if verdict != "PASS":
        errors.append("ACCEPTED requires latest_verdict PASS")
    if accepted_revision != evaluation_snapshot.revision:
        errors.append("accepted_revision must equal evaluation revision")
    if require_current_snapshot:
        if _repository_snapshot(target) != evaluation_snapshot:
            errors.append("ACCEPTED workspace no longer matches evaluation")
        try:
            _validated_completed_review(paths, state)
        except HarnessError as error:
            errors.append(str(error))
```

Avoid recursion by making `_validated_completed_review` perform artifact checks without calling `_validate_state`.

Add the `accept` CLI parser and dispatch. Change `check --final` to pass `require_current_snapshot=True`.

- [ ] **Step 4: Run focused and full harness tests**

Run the focused command from Step 2.

Expected: all pass.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_harness -v
```

Expected: all harness tests pass, including a rewritten final-gate test that covers both committed HEAD drift and uncommitted workspace drift.

- [ ] **Step 5: Commit Task 4**

```bash
git add scripts/harness.py tests/test_harness.py
git diff --cached --check
git commit -m "feat: bind acceptance to evaluated workspace"
```

### Task 5: Skill protocol, public documentation, and static contracts

**Files:**
- Modify: `SKILL.md`
- Modify: `tests/test_skill.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `CHANGELOG.md`
- Modify: `agents/openai.yaml`

**Interfaces:**
- Consumes CLI commands and review contract from Tasks 1-4
- Produces the Root orchestration instructions users install

- [ ] **Step 1: Replace obsolete Skill assertions with failing schema 3 assertions**

Add or replace tests in `tests/test_skill.py`:

```python
def test_skill_uses_explicit_evaluation_commands(self) -> None:
    for command in ("snapshot", "begin-evaluation", "record-review", "accept"):
        self.assertIn(f"`{command}`", self.skill)
    self.assertNotIn("last_good_revision", self.skill)

def test_skill_requires_stable_acceptance_ids_and_evaluation_json(self) -> None:
    self.assertIn("`AC-NNN`", self.skill)
    self.assertIn("`## Evaluation record`", self.skill)
    self.assertIn("workspace_fingerprint", self.skill)
    self.assertIn("review_sha256", self.skill)

def test_read_only_roles_are_audited_not_claimed_as_sandboxed(self) -> None:
    self.assertIn("instruction-level read-only", self.skill)
    self.assertIn("snapshot before and after", self.skill)
    self.assertIn("record `BLOCKED`", self.skill)
    self.assertNotIn("independent read-only Evaluator", self.skill)

def test_crash_recovery_reconciles_a_complete_review(self) -> None:
    self.assertIn("reconciles the persisted review", self.skill)
    self.assertNotIn(
        "EVALUATING with null latest_verdict requires current review.md to be absent",
        self.skill,
    )
```

- [ ] **Step 2: Run Skill tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_skill -v
```

Expected: new schema 3 contract tests fail against the current Skill.

- [ ] **Step 3: Rewrite the bounded Skill protocol**

Keep the frontmatter trigger-only. Replace schema 2 lifecycle prose with these required sections:

```markdown
## Snapshot and role audit

Planner and Evaluator are instruction-level read-only roles. Root runs
`snapshot` before and after each read-only role. Any product-workspace
fingerprint change is repository drift: preserve the diff, record `BLOCKED`,
and do not attribute the writer without evidence.

## Planner

Require exactly one `## Acceptance criteria` section with stable `AC-NNN`
identifiers. Every criterion describes observable user behavior or a concrete
repository invariant.

## Begin evaluation

After Generator returns a complete handoff, Root runs `begin-evaluation`.
Pass the returned revision and `workspace_fingerprint` to Evaluator.

## Evaluator and review

Require exactly one `## Evaluation record` JSON section matching the runtime
schema. Every planned criterion appears once. PASS criteria reference command
or inspection evidence with concrete results.

Persist Evaluator output through `record-review`; never write evaluation state
by hand. A complete review persisted before an interruption is authoritative,
and `init --resume` reconciles the persisted review.

## Goal Gate

Only a structured PASS may run `accept`. `accept` rejects changed review bytes,
revision drift, or any tracked, staged, unstaged, or non-ignored untracked
product change.
Run `check --final` after acceptance.
```

Retain the Planner-once, serial role, scoped commit, replacement-agent, and historical artifact requirements. Keep the Skill body at or below 1,000 words by deleting obsolete machine-heading and manual-state sequences.

- [ ] **Step 4: Update public docs and agent metadata**

Update both READMEs to:

- state Python 3.10+ and use `python3` in every command;
- show `snapshot`, `begin-evaluation`, `record-review`, `accept`, and `check --final`;
- describe schema 3 as a breaking change with no schema 2 migration;
- describe Planner/Evaluator as audited instruction-level read-only roles;
- state that existing dirty files are allowed but become part of the evaluated fingerprint;
- require the temporary `record-review` source file to be outside the target repository.

Add CHANGELOG entries:

```markdown
### Changed

- Replaced schema 2 with schema 3 evaluation transactions; schema 2 state is
  intentionally unsupported.
- Replaced manual evaluation state edits with explicit snapshot,
  begin-evaluation, record-review, and accept commands.
- Changed Planner and Evaluator claims from sandboxed read-only to audited
  instruction-level read-only boundaries.

### Security

- Bind PASS evidence to the evaluated revision, complete workspace fingerprint,
  and exact review bytes.
- Reconcile complete review writes after interruption without deleting evidence.
```

Update `agents/openai.yaml`:

```yaml
interface:
  display_name: "Vibe Coding Harness"
  short_description: "Run durable, snapshot-bound coding goals"
  default_prompt: "Use $vibe-coding-harness as Root: dispatch isolated role contexts serially, audit read-only roles with workspace snapshots, persist schema 3 evaluation records through lifecycle commands, and accept only the exact evidenced repository snapshot."
```

- [ ] **Step 5: Run Skill and repository-health tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_skill tests.test_repository_health -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add SKILL.md tests/test_skill.py README.md README.zh-CN.md CHANGELOG.md agents/openai.yaml
git diff --cached --check
git commit -m "docs: document schema v3 evaluation protocol"
```

### Task 6: Full verification and behavioral pressure tests

**Files:**
- Verify: `scripts/harness.py`
- Verify: `tests/test_harness.py`
- Verify: `SKILL.md`
- Verify: `tests/test_skill.py`
- Verify: public documentation

**Interfaces:**
- Consumes the complete schema 3 implementation
- Produces release evidence; no production file changes unless a test reveals a defect

- [ ] **Step 1: Run the complete automated suite**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 2: Run static repository checks**

Run:

```bash
git diff --check
rg -n "schema_version.?2|last_good_revision|python \\\"\\$SKILL_ROOT" \
  SKILL.md README.md README.zh-CN.md agents scripts tests
```

Expected: `git diff --check` is silent. The search finds only explicit schema 2 rejection tests and changelog/design history, not active protocol instructions.

- [ ] **Step 3: Run three fresh-context pressure scenarios without the Skill**

Use three fresh read-only agents. Give each one scenario without `SKILL.md`:

1. old PASS plus uncommitted product drift and time pressure;
2. review persisted before state update and a request to delete it and restart;
3. Evaluator modifies a file while reporting PASS.

Record whether each baseline agent accepts, deletes evidence, or misses the role violation. If a baseline scenario does not exhibit the targeted failure, do not claim the Skill corrected that scenario; retain it only as an application check.

- [ ] **Step 4: Run the same scenarios with the complete Skill**

Use fresh agents and provide the full `SKILL.md`. Required outcomes:

1. refuse acceptance and require a new snapshot-bound evaluation;
2. resume and reconcile the complete persisted review;
3. record `BLOCKED`, preserve the diff, and avoid unsupported writer attribution.

Read every response manually. Confirm the output converges on the explicit lifecycle commands and does not invent manual state edits.

- [ ] **Step 5: Obtain an independent code review**

Dispatch a read-only reviewer with:

- the approved design;
- this implementation plan;
- `git diff` from the pre-implementation revision;
- full automated test output;
- the three guided pressure-test results.

Ask for Critical/Important findings only and a `Ready` or `Not ready` verdict. If it reports a valid issue, add a focused failing test, implement the minimum correction, rerun Steps 1-5, and obtain a fresh review.

- [ ] **Step 6: Verify final Git state**

Run:

```bash
git status --short --branch
git log --oneline --decorate -8
```

Expected: only intentional commits exist and no uncommitted implementation files remain.
