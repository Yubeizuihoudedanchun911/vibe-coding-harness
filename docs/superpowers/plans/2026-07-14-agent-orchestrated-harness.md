# Agent-Orchestrated Vibe Coding Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the global progress-file harness with requirement-scoped state and a Skill-driven Planner-once, Generator/Evaluator-loop orchestration model.

**Architecture:** `scripts/harness.py` remains the only runtime script and manages `.vibe-coding/requirements/REQ-NNN/` discovery, initialization, recovery, and validation. `SKILL.md` owns agent orchestration: Root persists artifacts and performs Goal Gate, while distinct Planner, Generator, and Evaluator agents do planning, business-code writing, and independent evaluation.

**Tech Stack:** Python 3 standard library, `unittest`, Markdown/YAML, Git CLI, Codex multi-agent tools (`spawn_agent`, `followup_task`, `wait_agent`).

## Global Constraints

- Obey every applicable `AGENTS.md`; all user-visible responses remain Chinese in this repository.
- Before every file-content edit call `beforeEditFile` with the absolute path; after that single edit call the matching `afterEditFile`. Never combine pairs.
- Preserve unrelated dirty and untracked work. Stage only paths named by the current task.
- Do not create a global `.vibe-coding/progress.md`.
- Each Goal owns `.vibe-coding/requirements/REQ-NNN/state.json`, one `plan.md`, and per-round `implementation.md` / `review.md` files.
- Planner runs once per requirement. Normal repair loops reuse the requirement's distinct Generator and Evaluator role agents.
- Root may write harness state and evidence documents, but never business code. Generator is the only business-code writer; Evaluator and Planner are read-only.
- Agent role tasks run serially. If multi-agent support is unavailable, mark the requirement `BLOCKED`; do not fall back to Root implementation.
- Initialize stage documents lazily after a role returns meaningful content. Do not create empty Markdown placeholders.
- Do not restore fixed Sprint scaffolding, universal acceptance gates, copied coding rules, or fixed `.codex/agents/*.toml` role configurations.
- Schema 1 global state is not migrated automatically. Detect it and return an actionable error.

---

## File Map

- Modify `scripts/harness.py`: requirement paths, ID allocation, selection, state validation, artifact validation, symlink boundaries, and CLI output.
- Rewrite `tests/test_harness.py`: subprocess-level coverage for requirement initialization, recovery, lifecycle validation, Git binding, locking, and symlinks.
- Modify `SKILL.md`: explicit Root/Planner/Generator/Evaluator orchestration and artifact protocol.
- Modify `agents/openai.yaml`: default prompt identifies Root as orchestrator.
- Rewrite `tests/test_skill.py`: static contract tests for the new orchestration and minimal bundle.
- Delete the already-obsolete files under `assets/agents/`, `assets/iteration/`, `references/`, and the three replaced scripts; do not introduce replacement templates.

### Task 1: Requirement-scoped initialization and recovery

**Files:**
- Modify: `tests/test_harness.py`
- Modify: `scripts/harness.py`

**Interfaces:**
- Produces: `RequirementPaths`, `RoundPaths`, `_requirement_paths()`, `_list_requirements()`, `_next_requirement_id()`, `_select_requirement()`.
- Produces: `init(target, goal, resume, requirement_id)`; Task 2 produces the final `check()` implementation.
- Later tasks rely on paths shaped as `.vibe-coding/requirements/REQ-NNN/rounds/NNN/`.

- [ ] **Step 1: Replace global-path test helpers and add failing initialization tests**

Use requirement-aware helpers and tests with these exact assertions:

```python
@property
def requirements_root(self) -> Path:
    return self.target / ".vibe-coding" / "requirements"

def _requirement_root(self, requirement_id: str = "REQ-001") -> Path:
    return self.requirements_root / requirement_id

def _state_path(self, requirement_id: str = "REQ-001") -> Path:
    return self._requirement_root(requirement_id) / "state.json"

def _load_state(self, requirement_id: str = "REQ-001") -> dict[str, object]:
    return json.loads(self._state_path(requirement_id).read_text(encoding="utf-8"))

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

def test_resume_requires_an_id_when_multiple_requirements_are_nonterminal(self) -> None:
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
```

Change `_write_state` to accept `requirement_id` as its first argument. Keep the existing temporary Git repository fixture and subprocess invocation. Remove obsolete schema-1 tests for global `progress.md`, item/check arrays, and in-place terminal-state mutation; replace their still-required safety coverage with the schema-2 tests in Tasks 1 and 2.

- [ ] **Step 2: Run the focused tests and verify the old global layout fails**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_harness.HarnessCliTests.test_init_creates_only_requirement_state \
  tests.test_harness.HarnessCliTests.test_init_allocates_monotonic_requirement_ids \
  tests.test_harness.HarnessCliTests.test_resume_requires_an_id_when_multiple_requirements_are_nonterminal \
  tests.test_harness.HarnessCliTests.test_resume_auto_selects_the_only_nonterminal_requirement -v
```

Expected: all four tests fail because the current script writes global `state.json` / `progress.md` and has no `--requirement` option.

- [ ] **Step 3: Add focused path types and requirement discovery**

Add `re` and `dataclass` imports, replace `_paths()`, and use these definitions:

```python
import re
from dataclasses import dataclass

SCHEMA_VERSION = 2
RUN_STATUSES = {"ACTIVE", "BLOCKED", "DEGRADED", "ACCEPTED"}
TERMINAL_STATUSES = {"DEGRADED", "ACCEPTED"}
PHASES = {"PLANNING", "BUILDING", "EVALUATING"}
VERDICTS = {None, "PASS", "FAIL", "UNVERIFIED"}
REQUIREMENT_PATTERN = re.compile(r"REQ-(\d+)\Z")


@dataclass(frozen=True)
class RoundPaths:
    root: Path
    implementation: Path
    review: Path


@dataclass(frozen=True)
class RequirementPaths:
    root: Path
    state: Path
    plan: Path
    rounds: Path

    def round(self, number: int) -> RoundPaths:
        root = self.rounds / f"{number:03d}"
        return RoundPaths(
            root=root,
            implementation=root / "implementation.md",
            review=root / "review.md",
        )


def _control_paths(target: Path) -> tuple[Path, Path]:
    control_root = target / ".vibe-coding"
    return control_root, control_root / "requirements"


def _requirement_paths(requirements_root: Path, requirement_id: str) -> RequirementPaths:
    root = requirements_root / requirement_id
    return RequirementPaths(
        root=root,
        state=root / "state.json",
        plan=root / "plan.md",
        rounds=root / "rounds",
    )


def _list_requirements(requirements_root: Path) -> list[RequirementPaths]:
    if not requirements_root.is_dir():
        return []
    paths: list[RequirementPaths] = []
    for child in requirements_root.iterdir():
        if REQUIREMENT_PATTERN.fullmatch(child.name) and child.is_dir():
            paths.append(_requirement_paths(requirements_root, child.name))
    return sorted(paths, key=lambda item: item.root.name)


def _next_requirement_id(requirements_root: Path) -> str:
    numbers = [
        int(match.group(1))
        for paths in _list_requirements(requirements_root)
        if (match := REQUIREMENT_PATTERN.fullmatch(paths.root.name))
    ]
    return f"REQ-{max(numbers, default=0) + 1:03d}"


def _select_requirement(
    requirements_root: Path, requirement_id: str | None
) -> RequirementPaths:
    if requirement_id:
        if not REQUIREMENT_PATTERN.fullmatch(requirement_id):
            raise HarnessError("requirement must match REQ-NNN")
        selected = _requirement_paths(requirements_root, requirement_id)
        if not selected.state.is_file():
            raise HarnessError(f"requirement does not exist: {requirement_id}")
        return selected

    nonterminal: list[RequirementPaths] = []
    for paths in _list_requirements(requirements_root):
        state = _load_state(paths.state)
        if state.get("status") not in TERMINAL_STATUSES:
            nonterminal.append(paths)
    if not nonterminal:
        raise HarnessError("no nonterminal requirement exists to resume")
    if len(nonterminal) > 1:
        ids = ", ".join(paths.root.name for paths in nonterminal)
        raise HarnessError(
            "multiple nonterminal requirements exist; use --requirement: " + ids
        )
    return nonterminal[0]
```

- [ ] **Step 4: Replace global initialization, resume, and CLI selection**

Use one state file per requirement and do not create stage documents:

```python
def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def init(
    target: Path,
    goal: str | None,
    resume: bool,
    requirement_id: str | None,
) -> dict[str, Any]:
    control_root, requirements_root = _control_paths(target)
    if resume:
        paths = _select_requirement(requirements_root, requirement_id)
        state = _load_state(paths.state)
        if goal and goal != state.get("goal"):
            raise HarnessError("resume goal does not match the selected requirement")
        return {
            "resumed": True,
            "requirement_id": paths.root.name,
            "goal": state["goal"],
            "status": state["status"],
            "phase": state["phase"],
            "active_round": state["active_round"],
            "next_action": state["next_action"],
            "last_good_revision": state["last_good_revision"],
        }

    if requirement_id is not None:
        raise HarnessError("--requirement is only valid with --resume")
    if not _non_empty_string(goal):
        raise HarnessError("--goal is required when starting a requirement")

    with _init_lock(control_root):
        requirements_root.mkdir(parents=True, exist_ok=True)
        new_id = _next_requirement_id(requirements_root)
        paths = _requirement_paths(requirements_root, new_id)
        paths.root.mkdir()
        revision = _revision(target)
        state = {
            "schema_version": SCHEMA_VERSION,
            "requirement_id": new_id,
            "goal": goal,
            "status": "ACTIVE",
            "phase": "PLANNING",
            "active_round": 1,
            "next_action": "Dispatch Planner and persist plan.md.",
            "last_good_revision": revision,
            "latest_verdict": None,
            "residual_risks": [],
        }
        _write_state(paths.state, state)

    return {
        "created": [f".vibe-coding/requirements/{new_id}/state.json"],
        "requirement_id": new_id,
        "goal": goal,
        "status": "ACTIVE",
        "phase": "PLANNING",
        "active_round": 1,
        "last_good_revision": revision,
    }
```

Add `--requirement` to both subcommands. Pass `args.requirement` into `init()` and the selected `check()` implementation. Keep `init --resume` for backward CLI familiarity.

- [ ] **Step 5: Run the focused tests and the existing lock test**

Run the command from Step 2 plus:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_harness.HarnessCliTests.test_init_refuses_to_write_while_another_init_holds_the_lock -v
```

Expected: all selected tests pass.

Then run the retained Task-1 suite:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_harness -v
```

Expected: every retained test passes; do not commit with schema-1 tests still failing.

- [ ] **Step 6: Commit the requirement layout slice**

```bash
git add scripts/harness.py tests/test_harness.py
git diff --cached --check
git commit -m "feat: add requirement-scoped harness state"
```

Expected staged paths: only `scripts/harness.py` and `tests/test_harness.py`.

### Task 2: Lifecycle, evidence, and path-boundary validation

**Files:**
- Modify: `tests/test_harness.py`
- Modify: `scripts/harness.py`

**Interfaces:**
- Consumes: `RequirementPaths.round(number)`, `_select_requirement()`, schema 2 state fields.
- Produces: `_validate_state(paths, target, require_current_head=False) -> list[str]`.
- Produces: `check(target, requirement_id, require_current_head=False) -> tuple[dict[str, Any], bool]`.
- CLI adds `check --final`, which enables current-HEAD binding at Goal Gate without invalidating historical accepted requirements after later commits.

- [ ] **Step 1: Add artifact helpers and failing phase-transition tests**

Add these helpers and representative state tests:

```python
def _write_state(
    self, requirement_id: str, state: dict[str, object]
) -> None:
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

def test_failed_review_advances_to_a_build_round_with_previous_evidence(self) -> None:
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
        "# Review\n\n## Attempt 1\n\nUNVERIFIED\n",
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
```

- [ ] **Step 2: Add failing Goal Gate, legacy-layout, and symlink tests**

Add tests that assert:

```python
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
        "-c", "user.name=Harness Test",
        "-c", "user.email=harness@example.com",
        "commit", "-qm", "advance fixture",
    )
    historical = self._run_harness(
        "check", "--requirement", "REQ-001"
    )
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
```

Retain equivalent coverage for the init lock, invalid revision, missing degradation acceptance, and dangling leaf symlinks. Update those tests to use requirement-scoped paths.

- [ ] **Step 3: Run the new lifecycle tests and verify they fail**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_harness.HarnessCliTests.test_building_requires_a_nonempty_plan \
  tests.test_harness.HarnessCliTests.test_evaluating_requires_current_implementation \
  tests.test_harness.HarnessCliTests.test_failed_review_advances_to_a_build_round_with_previous_evidence \
  tests.test_harness.HarnessCliTests.test_unverified_keeps_the_same_evaluation_round \
  tests.test_harness.HarnessCliTests.test_final_check_requires_pass_review_and_current_head \
  tests.test_harness.HarnessCliTests.test_final_check_rejects_pass_without_evidence \
  tests.test_harness.HarnessCliTests.test_init_rejects_legacy_global_state \
  tests.test_harness.HarnessCliTests.test_init_rejects_a_requirement_directory_symlink -v
```

Expected: failures mention missing schema-2 validation, unsupported `--final`, or followed symlinks.

- [ ] **Step 4: Implement state and artifact validation**

Replace item/check validation with schema-2 phase validation. Use small helpers:

```python
def _non_empty_file(path: Path) -> bool:
    try:
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _require_artifact(
    errors: list[str], path: Path, message: str
) -> None:
    if not _non_empty_file(path):
        errors.append(message)


def _pass_review_has_evidence(path: Path) -> bool:
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not re.search(r"(?m)^PASS\s*$", body):
        return False
    marker = "## Evidence"
    if marker not in body:
        return False
    return bool(body.split(marker, 1)[1].strip())


def _validate_state(
    paths: RequirementPaths,
    target: Path,
    *,
    require_current_head: bool = False,
) -> list[str]:
    state = _load_state(paths.state)
    errors: list[str] = []

    if state.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if state.get("requirement_id") != paths.root.name:
        errors.append("requirement_id must match its directory")
    if not _non_empty_string(state.get("goal")):
        errors.append("goal must be a non-empty string")
    status = state.get("status")
    phase = state.get("phase")
    verdict = state.get("latest_verdict")
    round_number = state.get("active_round")
    if status not in RUN_STATUSES:
        errors.append(f"status must be one of {sorted(RUN_STATUSES)}")
    if phase not in PHASES:
        errors.append(f"phase must be one of {sorted(PHASES)}")
    if verdict not in VERDICTS:
        errors.append("latest_verdict must be PASS, FAIL, UNVERIFIED, or null")
    if not isinstance(round_number, int) or isinstance(round_number, bool) or round_number < 1:
        errors.append("active_round must be a positive integer")
        round_number = 1
    if not _non_empty_string(state.get("next_action")):
        errors.append("next_action must be a non-empty string")
    if not isinstance(state.get("residual_risks"), list):
        errors.append("residual_risks must be a list")

    revision = state.get("last_good_revision")
    if not isinstance(revision, str):
        errors.append("last_good_revision must be a string")
    elif revision and not _revision_exists(target, revision):
        errors.append("last_good_revision does not resolve in the target repository")

    current = paths.round(round_number)
    if phase in {"BUILDING", "EVALUATING"} or status in {"DEGRADED", "ACCEPTED"}:
        _require_artifact(errors, paths.plan, "BUILDING requires non-empty plan.md")
    if phase == "EVALUATING" or status == "ACCEPTED":
        _require_artifact(
            errors,
            current.implementation,
            "EVALUATING requires current implementation.md",
        )
    if verdict in {"PASS", "UNVERIFIED"} or status == "ACCEPTED":
        _require_artifact(errors, current.review, "verdict requires current review.md")

    if verdict == "FAIL":
        if phase != "BUILDING" or round_number < 2:
            errors.append("FAIL requires the next BUILDING round")
        previous = paths.round(max(round_number - 1, 1))
        _require_artifact(
            errors,
            previous.implementation,
            "FAIL requires previous implementation.md",
        )
        _require_artifact(errors, previous.review, "FAIL requires previous review.md")
    if verdict == "UNVERIFIED" and phase != "EVALUATING":
        errors.append("UNVERIFIED must remain in EVALUATING")
    if verdict == "PASS" and phase != "EVALUATING":
        errors.append("PASS must remain in EVALUATING until Goal Gate")

    if status == "DEGRADED" and not _non_empty_string(
        state.get("degradation_acceptance")
    ):
        errors.append("DEGRADED requires degradation_acceptance from the user")
    if status == "ACCEPTED":
        if verdict != "PASS":
            errors.append("ACCEPTED requires latest_verdict PASS")
        elif not _pass_review_has_evidence(current.review):
            errors.append("PASS review requires evidence")
        if not _non_empty_string(revision):
            errors.append("ACCEPTED requires last_good_revision")
        elif require_current_head and revision != _revision(target):
            errors.append("ACCEPTED requires last_good_revision to match current HEAD")
    return errors
```

Use phase-specific error text for plan requirements: when status is terminal, report `terminal requirement requires non-empty plan.md` rather than the BUILDING message.

- [ ] **Step 5: Harden discovery and leaf paths against symlinks**

Before reading or writing, reject symlinks for `.vibe-coding`, `requirements`, each matching `REQ-*` entry, the selected requirement, `state.json`, `plan.md`, `rounds`, the current round directory, `implementation.md`, and `review.md`.

```python
def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise HarnessError(f"{label} must not be a symbolic link")


def _reject_legacy_state(control_root: Path) -> None:
    legacy = [control_root / "state.json", control_root / "progress.md"]
    if any(path.exists() or path.is_symlink() for path in legacy):
        raise HarnessError(
            "legacy global harness state detected; finish or migrate it before schema 2"
        )
```

Call `_reject_legacy_state()` before new initialization and resume discovery. In `_list_requirements()`, check `child.is_symlink()` before `child.is_dir()` so dangling and directory links cannot be followed.

- [ ] **Step 6: Wire structural and final checks into the CLI**

```python
def check(
    target: Path,
    requirement_id: str | None,
    require_current_head: bool = False,
) -> tuple[dict[str, Any], bool]:
    _, requirements_root = _control_paths(target)
    try:
        paths = _select_requirement(requirements_root, requirement_id)
        errors = _validate_state(
            paths,
            target,
            require_current_head=require_current_head,
        )
        state = _load_state(paths.state)
    except HarnessError as error:
        return {"valid": False, "errors": [str(error)]}, False
    result = {
        "valid": not errors,
        "errors": errors,
        "requirement_id": paths.root.name,
        "goal": state.get("goal"),
        "status": state.get("status"),
        "phase": state.get("phase"),
        "active_round": state.get("active_round"),
        "latest_verdict": state.get("latest_verdict"),
    }
    return result, not errors
```

Add `check_parser.add_argument("--final", action="store_true")` and pass it as `require_current_head`. Make `init --resume` call `_validate_state(..., require_current_head=False)` before returning state.

- [ ] **Step 7: Run the complete CLI suite**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_harness -v
```

Expected: all harness tests pass. No test or CLI output refers to global `progress.md` except the explicit legacy-state rejection test.

- [ ] **Step 8: Commit lifecycle validation**

```bash
git add scripts/harness.py tests/test_harness.py
git diff --cached --check
git commit -m "feat: validate requirement lifecycle artifacts"
```

### Task 3: Skill-driven role orchestration

**Files:**
- Modify: `tests/test_skill.py`
- Modify: `SKILL.md`
- Modify: `agents/openai.yaml`
- Delete: `assets/agents/vibe-planner.toml`
- Delete: `assets/agents/vibe-generator.toml`
- Delete: `assets/agents/vibe-evaluator.toml`
- Delete: all tracked files under `assets/iteration/` and `references/`
- Delete: `scripts/bootstrap_harness.py`
- Delete: `scripts/detect_project_profile.py`
- Delete: `scripts/validate_harness.py`

**Interfaces:**
- Consumes: schema-2 CLI commands from Tasks 1-2.
- Produces: Root orchestration instructions using `spawn_agent`, `followup_task`, and requirement artifacts.
- Produces: one Planner role task per requirement and reusable Generator/Evaluator role sessions per requirement.

- [ ] **Step 1: Replace old conditional-agent assertions with failing orchestration assertions**

Rewrite `tests/test_skill.py` around the approved contract:

```python
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

    def test_requirement_artifacts_replace_global_progress(self) -> None:
        self.assertIn("requirements/REQ-NNN", self.skill)
        self.assertIn("rounds/NNN/implementation.md", self.skill)
        self.assertIn("rounds/NNN/review.md", self.skill)
        self.assertNotIn("progress.md", self.skill)

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
```

- [ ] **Step 2: Run the Skill tests and verify the old single-root policy fails**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_skill -v
```

Expected: failures for Root writer boundary, `spawn_agent`, `followup_task`, `wait_agent`, Planner-once wording, reusable role sessions, and requirement artifact paths.

- [ ] **Step 3: Rewrite `SKILL.md` with the complete orchestration contract**

Replace the file with this bounded structure and preserve the exact test phrases:

```markdown
---
name: vibe-coding-harness
description: Use when a software goal spans sessions and needs role-separated planning, implementation, independent evaluation, and durable recovery evidence.
---

# Vibe Coding Harness

Run long software goals through requirement-scoped artifacts and three isolated role agents.

## Contract

- Treat Git, live behavior, and applicable `AGENTS.md` files as truth.
- Root only orchestrates agents, persists harness artifacts, validates the Goal, and reports status. Root never writes business code.
- Planner and Evaluator are read-only. Generator is the only business-code writer.
- Run one role task at a time and use `wait_agent` before the next dispatch. Preserve unrelated user changes and use scoped commits.
- If multi-agent tools are unavailable, record `BLOCKED`; do not fall back to Root implementation.

## Start or resume

Resolve this directory as `SKILL_ROOT` and the target Git root as `TARGET_ROOT`.

Start a requirement only when the goal needs durable multi-session work:

```bash
python "$SKILL_ROOT/scripts/harness.py" init \
  --target "$TARGET_ROOT" --goal "<user-visible goal>"
```

Resume the only nonterminal requirement, or select one explicitly:

```bash
python "$SKILL_ROOT/scripts/harness.py" init --resume --target "$TARGET_ROOT"
python "$SKILL_ROOT/scripts/harness.py" init --resume \
  --target "$TARGET_ROOT" --requirement REQ-NNN
python "$SKILL_ROOT/scripts/harness.py" check \
  --target "$TARGET_ROOT" --requirement REQ-NNN
```

Read the selected `state.json`, `plan.md`, current round artifacts, Git status, and `last_good_revision`. Trust files over prior chat.

## Durable layout

Each goal owns:

```text
.vibe-coding/requirements/REQ-NNN/
├── state.json
├── plan.md
└── rounds/NNN/implementation.md
             review.md
```

Create Markdown files only after their role returns meaningful content. Root writes role output verbatim enough to preserve scope, commands, evidence, risks, and next action.

## Planner: once per requirement

Planner runs once per requirement. Use `spawn_agent` to create a distinct read-only Planner with the user Goal, requirement path, repository instructions, and live code context. Require a self-contained plan containing scope, non-goals, user-visible behavior, high-level design, and testable acceptance criteria. Do not request granular speculative implementation.

After Planner returns, Root writes `plan.md`, updates `phase` to `BUILDING`, and sets `next_action` to dispatch Generator. Re-run Planner only when the Goal changes or evidence shows the product specification itself is invalid.

## Generator: Build rounds

Use `spawn_agent` once to create the requirement's workspace-write Generator. Give it `state.json`, `plan.md`, applicable repository instructions, and for repairs the previous `review.md`. Require the minimum repository-native implementation, focused tests, relevant real-path checks, a scoped revision when allowed, and a structured handoff.

Root writes the handoff to `rounds/NNN/implementation.md`, changes `phase` to `EVALUATING`, and dispatches Evaluator. Reuse the requirement's Generator with `followup_task` after a failed review; do not create another Planner for normal repair.

## Evaluator: QA rounds

Use `spawn_agent` once to create an independent read-only Evaluator. Provide `state.json`, `plan.md`, the current `implementation.md`, evaluated revision or diff, canonical commands, and raw evidence. Require criterion-level `PASS`, `FAIL`, or `UNVERIFIED`, reproduction details, and residual risks. Evaluator never edits or relaxes criteria.

Root writes `rounds/NNN/review.md`. Reuse the requirement's Evaluator with `followup_task` for later QA rounds or missing evidence. Persistent files remain authoritative over role-chat history.

## Loop and recovery

- `PASS`: Root checks Goal alignment and evidence, sets `ACCEPTED`, then runs `check --final`.
- `FAIL`: Root increments `active_round`, sets `phase=BUILDING`, and sends the review to Generator.
- `UNVERIFIED`: keep the round and `phase=EVALUATING`; append the next evaluation attempt to the same review file.
- Agent interruption: keep phase and round; redispatch the same role. Create a replacement only if that role is unusable.
- External impediment: record `BLOCKED`, reason, and actionable `next_action`.
- `DEGRADED`: require non-empty `degradation_acceptance` from the user.

Never infer completion from file existence. `ACCEPTED` requires the current round's evidenced PASS and the evaluated current Git revision.

## File maintenance

Do not create copied rules, fixed role configs, empty governance files, speculative ADRs, or duplicate progress logs. Update existing project documentation only when implementation facts changed. Keep historical requirement directories and round evidence intact.
```

- [ ] **Step 4: Update the OpenAI entry prompt**

Replace `agents/openai.yaml` with:

```yaml
interface:
  display_name: "Vibe Coding Harness"
  short_description: "Orchestrate durable Planner, Generator, Evaluator loops"
  default_prompt: "Use $vibe-coding-harness as the Root orchestrator: delegate planning, implementation, and evaluation to isolated role agents, persist requirement-scoped evidence, and enforce the Goal Gate."
```

- [ ] **Step 5: Remove obsolete fixed-role and governance resources**

Confirm the deleted paths match the design and do not delete `.weaver` recorder data manually. The tracked deletions are:

```text
assets/agents/*.toml
assets/iteration/*.md
references/*.md
scripts/bootstrap_harness.py
scripts/detect_project_profile.py
scripts/validate_harness.py
```

- [ ] **Step 6: Run the Skill tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_skill -v
```

Expected: all Skill structure tests pass; bundle contains only `scripts/harness.py` as runtime Python.

- [ ] **Step 7: Commit the orchestration surface**

Stage only the Skill surface and intended deletions:

```bash
git add SKILL.md agents/openai.yaml tests/test_skill.py
git add -u -- assets/agents assets/iteration references \
  scripts/bootstrap_harness.py scripts/detect_project_profile.py \
  scripts/validate_harness.py
git diff --cached --check
git diff --cached --name-status
git commit -m "feat: orchestrate planner generator evaluator roles"
```

Verify the staged name list contains no design/plan documents or unrelated files.

### Task 4: Full validation and independent review

**Files:**
- Verify: `scripts/harness.py`
- Verify: `SKILL.md`
- Verify: `agents/openai.yaml`
- Verify: `tests/test_harness.py`
- Verify: `tests/test_skill.py`

**Interfaces:**
- Consumes all previous task outputs.
- Produces fresh evidence that the CLI, Skill metadata, minimal bundle, and orchestration contract are ready.

- [ ] **Step 1: Run the complete unit suite**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Expected: every test passes with zero failures and zero errors.

- [ ] **Step 2: Run the official Skill validator**

```bash
/opt/anaconda3/envs/py311env/bin/python \
  /Users/ybzhddc_911/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
```

Expected: `Skill is valid!`

- [ ] **Step 3: Check stale references, whitespace, and the final tree**

```bash
git diff --check
rg -n "progress\.md|Default to the root agent alone|Use an independent Evaluator only when|bootstrap_harness|detect_project_profile|validate_harness" \
  SKILL.md agents scripts tests
find . -maxdepth 3 -type f \
  -not -path './.git/*' \
  -not -path '*/.weaver/*' \
  -not -path '*/__pycache__/*' \
  -not -name '.DS_Store' | sort
```

Expected: `git diff --check` is silent; `rg` only finds the explicit legacy `progress.md` rejection test where applicable; the production tree contains `SKILL.md`, `agents/openai.yaml`, and `scripts/harness.py` plus docs/tests.

- [ ] **Step 4: Perform an independent read-only review**

Dispatch an Evaluator/reviewer with the approved design document, implementation diff, full test output, and validator output. Ask it to report only Critical/Important findings and a `Ready` or `Not ready` verdict. Do not let it edit files.

Expected: no Critical/Important findings and verdict `Ready`. If it finds a valid issue, add a focused failing test, implement the minimum correction, rerun Steps 1-3, and obtain a fresh review.

- [ ] **Step 5: Record final repository state**

```bash
git status --short --branch
git log --oneline --decorate -5
```

Expected: no staged changes remain from completed implementation commits; any remaining dirty paths are explicitly identified as unrelated or intentionally uncommitted documentation.
