# External Controller Phase 6: Schema 3 Migration and Breaking Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve existing Schema 3 evidence through explicit, auditable, idempotent migration, then remove the Skill-era product and publish one coherent Schema 4 package, CLI, documentation set, and offline CI gate.

**Architecture:** The migration module embeds only the old read-only parser/validator needed to prove source integrity; it never calls Schema 3 mutation commands. A migration first validates and stages every selected requirement, writes a prepared batch manifest, then deterministically installs backups and Schema 4 mappings. The destructive repository cutover occurs only after migration and all new-runtime gates pass.

**Tech Stack:** Python 3.10+ standard library, immutable filesystem manifests, SHA-256 tree identity, real Git repositories, unittest, setuptools wheel/sdist, GitHub Actions.

## Global Constraints

- Detecting `.vibe-coding/requirements/REQ-NNN/` never triggers transparent loading or migration.
- Migration requires explicit `vibe migrate --requirement REQ-NNN --base COMMIT` or `--all --base COMMIT`.
- Validate every selected Schema 3 state and every hash-bound artifact before writing a Schema 4 mapping.
- Corrupt, missing, symlinked, duplicate-key, unsupported-schema, or tampered legacy evidence fails closed.
- Never modify `.vibe-coding/requirements/**`.
- Preserve the complete requirement tree, Schema 3 validation result, source hashes, base commit, target run ID, and migration time.
- `ACCEPTED` and `DEGRADED` become `IMPORTED_READ_ONLY`; they never become fabricated Schema 4 success.
- `ACTIVE` and `BLOCKED` become `PAUSED` with `SCHEMA3_REPLAN_REQUIRED`; no historical DAG or evaluation is invented.
- Dirty Schema 3 workspace content may be archived as historical evidence but never silently becomes a Schema 4 worktree base.
- `resume --replan` requires the current target to satisfy Schema 4 clean-baseline rules.
- A repeated identical source/base migration returns the existing result.
- Changed source bytes or a different base conflicts with the existing migration identity.
- `--all` validates and stages every requirement before committing any run mapping.
- Preserve Schema 3 evaluation-record Schema 2, interruptions, attempts, failed-round receipts, and hashes verbatim.
- Do not delete the old runtime or tests until the migration module and its regression tests are green.
- The final product contains no Skill entry, Root prompt metadata, or legacy runtime shim.
- Default CI remains offline; real Codex smoke is skipped unless explicitly enabled.
- Do not add an automated publish, merge, push, or PR workflow.

---

## File Map

- Create `src/vibe/migration/__init__.py`: public migration exports.
- Create `src/vibe/migration/schema3.py`: read-only legacy validation, staging, manifest, mapping, resume-replan bridge.
- Create `tests/fixtures/schema3/`: committed four-status/full-history legacy trees, deterministic source Git bundle, hashes, and provenance.
- Create `tests/test_migration_schema3.py`: four-status mapping, corruption, dirty archive, batch, idempotency.
- Modify `src/vibe/cli.py`: connect the existing `migrate` parser to the real implementation.
- Modify `src/vibe/controller.py`: allow `--replan` only for a valid migrated paused run on a clean base.
- Create `tests/test_package_metadata.py`: installed entry point and installed resource discovery.
- Modify `tests/test_repository_health.py`: Schema 4 repository contract.
- Modify `README.md`, `README.zh-CN.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, `.github/PULL_REQUEST_TEMPLATE.md`, `.github/dependabot.yml`, `.github/workflows/ci.yml`.
- Delete `SKILL.md`, `agents/openai.yaml`, `scripts/harness.py`, `tests/test_skill.py`, and `tests/test_harness.py`.

### Task 16: Explicit, Read-Only Schema 3 Migration

**Files:**
- Create: `src/vibe/migration/__init__.py`
- Create: `src/vibe/migration/schema3.py`
- Create: `tests/fixtures/schema3/README.md`
- Create: `tests/fixtures/schema3/fixture-manifest.json`
- Create: `tests/fixtures/schema3/source-repository.bundle`
- Create: `tests/fixtures/schema3/requirements/**`
- Create: `tests/test_migration_schema3.py`
- Modify: `src/vibe/cli.py`
- Modify: `src/vibe/controller.py`

**Interfaces:**
- Produces `Schema3Requirement`, `LegacyValidation`, `MigrationEntry`, and `MigrationManifest`.
- Produces `discover_requirements(target)`.
- Produces `validate_schema3_requirement(target, requirement_id)`.
- Produces `migrate_schema3(target, requirement_id, migrate_all, base, allow_project_commands=False)`.
- Produces `resume_migrated_with_replan(run_id)`.

- [ ] **Step 1: Freeze self-contained Schema 3 fixtures and write failing four-status tests**

Before changing or deleting `scripts/harness.py`, use it to create committed fixtures for `ACTIVE`, `BLOCKED`, `ACCEPTED`, `DEGRADED`, and a multi-round full-history requirement. Create the source repository with fixed author/committer names, timestamps, branch, and file bytes; export its baseline as `source-repository.bundle`. `fixture-manifest.json` records the harness SHA-256, source commit, every fixture file mode/SHA-256, state/evaluation/interruption schema versions, and the command sequence used. `README.md` explains provenance and regeneration, but tests consume only committed bytes.

In test `setUp()`, clone the bundle into a fresh temporary target, copy the selected committed requirement tree below `.vibe-coding/requirements/`, and verify every copied byte against `fixture-manifest.json` before invoking migration. No fixture builder imports or executes `scripts.harness`; this remains true after Task 17 deletes it.

Create `tests/test_migration_schema3.py`:

```python
def test_accepted_and_degraded_import_as_read_only_history(self) -> None:
    accepted = self.schema3_requirement("REQ-001", status="ACCEPTED")
    degraded = self.schema3_requirement("REQ-002", status="DEGRADED")

    results = migrate_schema3(
        target=self.target,
        requirement_id=None,
        migrate_all=True,
        base=self.head,
    )

    states = [self.load_run(item.run_id) for item in results]
    self.assertEqual(
        [state["status"] for state in states],
        ["IMPORTED_READ_ONLY", "IMPORTED_READ_ONLY"],
    )
    self.assertEqual(
        [item.source_status for item in results],
        ["ACCEPTED", "DEGRADED"],
    )
    for item in results:
        self.assert_tree_bytes_equal(
            self.target / ".vibe-coding/requirements" / item.requirement_id,
            self.target
            / ".vibe-coding/schema3-backups"
            / item.migration_id
            / item.requirement_id,
        )


def test_active_and_blocked_require_explicit_replan(self) -> None:
    self.schema3_requirement("REQ-001", status="ACTIVE")
    self.schema3_requirement("REQ-002", status="BLOCKED")
    results = migrate_schema3(
        self.target,
        requirement_id=None,
        migrate_all=True,
        base=self.head,
    )
    for result in results:
        state = self.load_run(result.run_id)
        self.assertEqual(state["status"], "PAUSED")
        self.assertEqual(
            state["last_error"]["code"],
            "SCHEMA3_REPLAN_REQUIRED",
        )
        self.assertEqual(state["plan_version"], 0)
        self.assertEqual(state["tasks"], {})


def test_evaluation_interruption_attempts_and_receipts_are_byte_preserved(self) -> None:
    fixture = self.schema3_requirement_with_full_history("REQ-001")
    result = migrate_schema3(
        self.target,
        requirement_id="REQ-001",
        migrate_all=False,
        base=self.head,
    )[0]
    backup = (
        self.target
        / ".vibe-coding/schema3-backups"
        / result.migration_id
        / "REQ-001"
    )
    self.assert_tree_bytes_equal(fixture, backup)
```

- [ ] **Step 2: Write failing corruption, dirty, idempotency, and batch tests**

Add:

```python
def test_corrupt_or_tampered_requirement_writes_no_backup_or_run(self) -> None:
    requirement = self.schema3_requirement("REQ-001", status="ACCEPTED")
    (requirement / "rounds/001/review.md").write_text(
        "tampered\n",
        encoding="utf-8",
    )
    before_runs = self.run_directories()
    with self.assertRaisesRegex(ContractError, "Schema 3 validation"):
        migrate_schema3(
            self.target,
            requirement_id="REQ-001",
            migrate_all=False,
            base=self.head,
        )
    self.assertEqual(self.run_directories(), before_runs)
    self.assertEqual(self.backup_directories(), [])


def test_dirty_schema3_snapshot_is_archived_but_not_used_as_base(self) -> None:
    self.schema3_requirement("REQ-001", status="ACTIVE")
    (self.target / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    result = migrate_schema3(
        self.target,
        requirement_id="REQ-001",
        migrate_all=False,
        base=self.head,
    )[0]
    state = self.load_run(result.run_id)
    self.assertEqual(state["repository"]["base_sha"], self.head)
    self.assertEqual(
        state["last_error"]["code"],
        "SCHEMA3_REPLAN_REQUIRED",
    )
    with self.assertRaisesRegex(ContractError, "clean product baseline"):
        self.controller.recover(result.run_id, replan=True)


def test_identical_migration_is_idempotent_but_changed_base_or_source_conflicts(self) -> None:
    self.schema3_requirement("REQ-001", status="ACCEPTED")
    first = migrate_schema3(
        self.target,
        "REQ-001",
        False,
        self.head,
    )[0]
    second = migrate_schema3(
        self.target,
        "REQ-001",
        False,
        self.head,
    )[0]
    self.assertEqual(first, second)

    other = self.commit("other.txt", "other\n")
    with self.assertRaisesRegex(StateConflictError, "different base"):
        migrate_schema3(self.target, "REQ-001", False, other)

    (self.requirement("REQ-001") / "goal.md").write_text(
        "changed\n",
        encoding="utf-8",
    )
    with self.assertRaisesRegex(StateConflictError, "source changed"):
        migrate_schema3(self.target, "REQ-001", False, self.head)


def test_all_preparation_failure_creates_no_run_mapping(self) -> None:
    self.schema3_requirement("REQ-001", status="ACCEPTED")
    broken = self.schema3_requirement("REQ-002", status="BLOCKED")
    (broken / "state.json").write_text("{broken", encoding="utf-8")
    before = self.run_directories()
    with self.assertRaises(ContractError):
        migrate_schema3(
            self.target,
            requirement_id=None,
            migrate_all=True,
            base=self.head,
        )
    self.assertEqual(self.run_directories(), before)


def test_strict_legacy_inputs_fail_closed_without_writes(self) -> None:
    mutations = (
        self.invalid_utf8_state,
        self.duplicate_json_key_state,
        self.unpaired_surrogate_state,
        self.unsupported_state_schema,
        self.unsupported_evaluation_schema,
        self.unsupported_interruption_schema,
        self.missing_required_artifact,
        self.symlinked_requirement_artifact,
    )
    for mutate in mutations:
        with self.subTest(mutate=mutate.__name__):
            self.reset_fixture("REQ-001")
            mutate(self.requirement("REQ-001"))
            before = self.control_tree_snapshot()
            with self.assertRaises(ContractError):
                migrate_schema3(self.target, "REQ-001", False, self.head)
            self.assertEqual(self.control_tree_snapshot(), before)


def test_requirement_discovery_uses_numeric_not_lexical_order(self) -> None:
    self.schema3_requirement("REQ-999", status="ACCEPTED")
    self.schema3_requirement("REQ-1000", status="ACCEPTED")
    results = migrate_schema3(self.target, None, True, self.head)
    self.assertEqual(
        [item.requirement_id for item in results],
        ["REQ-999", "REQ-1000"],
    )


def test_source_change_or_symlink_swap_after_validation_aborts_before_mapping(self) -> None:
    for mutation in (
        self.change_source_after_validation,
        self.swap_regular_file_for_symlink_after_validation,
    ):
        with self.subTest(mutation=mutation.__name__):
            self.reset_fixture("REQ-001")
            before = self.run_and_index_snapshot()
            with self.inject_after_validation(mutation):
                with self.assertRaisesRegex(ContractError, "source changed"):
                    migrate_schema3(
                        self.target,
                        "REQ-001",
                        False,
                        self.head,
                    )
            self.assertEqual(self.run_and_index_snapshot(), before)
```

`assert_tree_bytes_equal()` compares sorted relative names, `lstat` file type/mode, exact regular-file bytes, and symlink target text without following links; despite its historical name it must detect metadata/type drift too.

- [ ] **Step 3: Run migration tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_migration_schema3 -v
```

Expected: import failure because `vibe.migration.schema3` does not exist.

- [ ] **Step 4: Extract and prove a self-contained read-only Schema 3 validator**

Create `src/vibe/migration/schema3.py` as a source snapshot, not a line-range splice. Copy this complete dependency closure from the pre-cutover `scripts/harness.py`:

- standard-library imports used by the closure: `hashlib`, `json`, `math`, `os`, `re`, `stat`, `subprocess`, dataclasses, pathlib, and collection/typing helpers;
- constants `SCHEMA_VERSION`, `EVALUATION_RECORD_VERSION`, `INTERRUPTION_RECORD_VERSION`, `MAX_ROUNDS`, status/phase/verdict sets, every regex, observed-file states, and `WORKSPACE_PATHSPEC`;
- dataclasses `RoundPaths`, `RequirementPaths`, `ArtifactSnapshot`, `RepositorySnapshot`, and `MarkdownHeading`;
- strict scalar/JSON/symlink helpers;
- Git revision and the complete tracked/untracked/index/submodule workspace-fingerprint closure;
- observation/snapshot comparison helpers;
- requirement path/list/select, strict state/artifact readers;
- the complete Markdown, criteria, evaluation-record, observed-artifact, evidence-drift, review, requirement-symlink, interruption, and Schema 3 state/history validation closure.

Replace `HarnessError` with a private `LegacyValidationError(ContractError)` at the extraction boundary. Do not copy `_emit`, target CLI parsing, locks, any atomic writer, review transition/reconciliation, `init`, `begin_evaluation`, `record_review`, `restart_evaluation`, `accept`, `check`, or `main`. There is no import from `scripts.harness`.

The public `discover_requirements()` sorts by the integer captured by `REQUIREMENT_PATTERN`, with requirement text only as a deterministic tiebreaker; do not retain the legacy path-string ordering.

Add a closure regression that parses `schema3.py` with `ast`, compiles/imports it with `scripts/harness.py` temporarily absent, validates every committed fixture, and asserts no undefined global name on each public validation path. Run this same test again after Task 17 deletion. This is the extraction acceptance criterion; source line numbers are not.

Expose one read-only entry point:

```python
@dataclass(frozen=True)
class LegacyValidation:
    requirement_id: str
    status: str
    goal: str
    source_identity: str
    workspace_snapshot: dict[str, str]
    validated_state: dict[str, object]


def validate_schema3_requirement(
    target: Path,
    requirement_id: str,
) -> LegacyValidation:
    paths = _requirement_paths(
        target / ".vibe-coding" / "requirements",
        requirement_id,
    )
    state = _load_requirement_state(paths)
    errors = _validate_state(paths, target, state=state)
    if errors:
        raise ContractError(
            "Schema 3 validation failed: " + "; ".join(errors)
        )
    return LegacyValidation(
        requirement_id=requirement_id,
        status=state["status"],
        goal=state["goal"],
        source_identity=hash_tree(paths.root),
        workspace_snapshot=repository_snapshot(target).as_dict(),
        validated_state=state,
    )
```

Use the old schema constants exactly: state Schema `3`, evaluation record Schema `2`, interruption Schema `2`. Unsupported versions, duplicate JSON keys, invalid UTF-8, unpaired Unicode surrogates, non-regular files, and symlinks fail; do not add compatibility.

- [ ] **Step 5: Implement deterministic tree identity and migration manifests**

Hash each requirement tree by sorted repo-relative path, file type/mode, symlink target, and exact bytes. Legacy validation already rejects forbidden symlinks, but the hash function must still represent them rather than follow them.

Add:

```python
@dataclass(frozen=True)
class MigrationEntry:
    requirement_id: str
    source_status: str
    source_identity: str
    base_sha: str
    run_id: str
    migration_id: str


@dataclass(frozen=True)
class MigrationManifest:
    schema_version: int
    migration_id: str
    created_at: str
    target_repository_identity: str
    base_sha: str
    entries: tuple[MigrationEntry, ...]
```

Batch migration identity:

```python
identity_body = canonical_json_bytes(
    {
        "repository_identity": repository_identity,
        "base_sha": base_sha,
        "requirements": [
            {
                "requirement_id": item.requirement_id,
                "source_identity": item.source_identity,
            }
            for item in validations
        ],
    }
)
migration_id = "MIG-" + hashlib.sha256(identity_body).hexdigest()[:20]
```

The batch ID may change when selection/source/base changes, so it is not the
idempotency key. Under the global migration lock, maintain one stable
repository-local claim at
`.vibe-coding/migrations/index/<REQ-NNN>.json` for each requirement. The claim
contains exact repository identity, requirement ID, source identity, base SHA,
migration ID, run ID, backup manifest ref, and completion state. If a claim
exists, identical source/base enters the same replay path: it verifies every
batch output, finalizes any run still marked `MIGRATION_INSTALLING`, and only
then returns. Any changed source or base raises `StateConflictError` even if it
would compute another migration ID. Incomplete prepared manifests are also
scanned as claims before a new batch can be staged.

Manifest records each legacy validation result, original artifact hash inventory, `--base`, run ID, timestamp, and workspace snapshot. Hash file bytes incrementally in fixed-size chunks. Do not serialize source file contents into the manifest; the full backup preserves them.

- [ ] **Step 6: Implement prepare-all then deterministic commit**

`migrate_schema3()` holds `.vibe-coding/control/migration.lock` for the complete prepare/install transaction, then acquires `.vibe-coding/control/run-allocation.lock` before reserving any run ID/ref. The strict order is `migration.lock -> run-allocation.lock -> individual run lock`; native creation takes only `run-allocation.lock -> run lock`. It follows:

1. Require exactly one of `requirement_id` or `migrate_all`.
2. Resolve `--base^{commit}` to a full OID.
3. Discover `REQ-NNN` directories in numeric order.
4. Compute each selected tree's safe structural identity (sorted paths/types/modes/raw bytes, no semantic JSON parsing and no symlink following).
5. Before full validation, scan stable requirement claims and every incomplete prepared manifest. A claim with different structural identity or base raises `StateConflictError` even if the changed bytes would otherwise fail legacy validation; a completed identical claim verifies all outputs and returns; an incomplete identical claim resumes its exact reservation.
6. Only for new or identical-incomplete claims, perform complete Schema 3 semantic validation for every selected requirement before creating any run mapping.
7. Under `run-allocation.lock`, allocate all run IDs/refs and atomically publish
   immutable
   `.vibe-coding/migrations/reservations/<migration-id>.json`. It has exact
   Schema 1 fields:

   ```json
   {
     "schema_version": 1,
     "state": "PREPARED",
     "migration_id": "MIG-0123456789abcdef0123",
     "repository_identity": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
     "base_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
     "requirements": [
       {
         "requirement_id": "REQ-001",
         "source_identity": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
         "run_id": "RUN-20260723-001",
         "run_ref": "refs/heads/vibe/run-RUN-20260723-001"
       }
     ]
   }
   ```

   Write/fsync the file and reservation directory before allocating anything
   else. The artifact is never removed or mutated. Native and migration
   allocation use one strict parser and treat every reserved ID/ref as occupied
   even if `prepared.json` does not yet exist. An identical retry reuses the
   reservation; any identity/base/selection difference conflicts. Hold the
   allocator through batch install (the simple V1 protocol).
8. Stage full backup copies and imported run payloads under `.vibe-coding/migrations/<migration-id>/staging/`.
9. Re-hash each source immediately before copying, hash the staged copy afterward, and require both to equal the validation identity. Re-hash all sources once more before publishing `prepared.json`, again immediately before the first install, and on every prepared-batch recovery; any validate/copy/install TOCTOU or file-to-symlink/type swap aborts with no run/index mapping.
10. Fsync every new file and full newly-created directory chain.
11. Write immutable `prepared.json` containing all target paths/hashes and reserved run IDs/refs.
12. Install each backup directory with atomic rename and verify any already-installed identical target.
13. Create each imported run through `create_imported_run()` below.
14. Atomically install each stable requirement index claim.
15. Write immutable `completed.json`.
16. For each imported run, acquire its run lock, write immutable
    `migration-completion.json` binding the completed manifest and stable index
    claim, then atomically replace the installation pause with the mapped final
    status/reason. A retry verifies already-finalized runs and finalizes only
    the remainder.

Add fault hooks after reservation publication, staging each source, after
`prepared.json`, after each backup rename, after each imported run, after each
index claim, after `completed.json`, and after each run finalization. At every
fault point, an identical retry verifies installed bytes/refs and completes
exactly once; changed input conflicts. `--all` completes preparation for every
requirement before the first backup, run, or index is installed. A mismatched
installed target fails closed.

Add this real fault-injection matrix:

```python
MIGRATION_FAULT_POINTS = (
    "after_reservation",
    "after_staged_requirement",
    "after_prepared_manifest",
    "after_backup_install",
    "after_imported_run",
    "after_index_claim",
    "after_completed_manifest",
    "after_run_finalization",
)


def test_each_migration_crash_retries_to_one_exact_mapping(self) -> None:
    for point in MIGRATION_FAULT_POINTS:
        with self.subTest(point=point):
            scenario = self.fresh_migration_scenario(fault_at=point)
            with self.assertRaisesRegex(InjectedCrash, point):
                scenario.migrate_all()
            results = scenario.without_fault().migrate_all()
            self.assertEqual(len(results), scenario.requirement_count)
            self.assertEqual(
                scenario.installed_run_ids(),
                {item.run_id for item in results},
            )
            self.assertEqual(scenario.duplicate_backup_count(), 0)
            self.assertEqual(scenario.duplicate_index_claim_count(), 0)
            scenario.assert_all_digests_and_refs_valid()
```

Add a real two-process native `create_run()` versus `migrate --all` race. Across repeated barrier interleavings, every allocated `RUN-NNN` and `refs/heads/vibe/**` target is unique, native creation never enters a prepared migration reservation, and both resulting states/claims validate.

Add a crash immediately after reservation but before `prepared.json`, then run
native creation in another process. It must skip every reserved run ID/ref.
Retry migration and prove the same reservation is reused. Add a crash after one
imported run, race `resume --replan` against the still-incomplete migration, and
prove resume performs no state/ref/process change while migration retry
finishes claims/completed manifest and finalizes that run exactly once.

`create_imported_run()` is a dedicated protocol, not `Controller.create_run()`: migration explicitly permits a dirty product workspace. It resolves the supplied base commit, creates no task worktree, and never treats dirty bytes as the base. Under migration + run-allocation + the new run's non-reentrant lock, it uses `StateStore`, immutable creation intent/config/receipt/legacy-import artifacts, and hardened `git update-ref <run-ref> <base> <zero>` (or exact recovery equality). It creates the complete Schema 4 shape:

```python
{
    "schema_version": 4,
    "run_id": run_id,
    "revision": 1,
    "goal": legacy.goal,
    "repository": {
        "identity": repository_identity,
        "base_ref": base_sha,
        "base_sha": base_sha,
        "integration_ref": run_ref,
        "integration_head": base_sha,
    },
    "status": "PAUSED",
    "resume_status": None,
    "plan_version": 0,
    "repair_round": 0,
    "max_repair_rounds": config.repair_rounds,
    "max_workers": config.max_workers,
    "controller": None,
    "config": config_ref.as_dict(),
    "creation": {
        "intent": intent_ref.as_dict(),
        "receipt": receipt_ref.as_dict(),
    },
    "artifact_index": [
        intent_ref.as_dict(),
        config_ref.as_dict(),
        receipt_ref.as_dict(),
        legacy_import_ref.as_dict(),
    ],
    "plans": [],
    "role_attempts": {"planner": [], "evaluator": []},
    "role_runtime": empty_role_runtime(config),
    "evaluations": [],
    "verifications": [],
    "legacy_import": legacy_import_ref.as_dict(),
    "tasks": {},
    "pending_dispatches": {},
    "pending_source_commit": None,
    "pending_integration": None,
    "pending_evaluation": None,
    "latest_evaluation": None,
    "global_verification": None,
    "stop_receipts": [],
    "last_error": {
        "code": "MIGRATION_INSTALLING",
        "message": "Schema 3 import is not yet fully installed",
        "retryable": False,
    },
    "created_at": now,
    "updated_at": now,
}
```

An explicit migration always persists
`repository.base_ref = repository.base_sha = <resolved full OID>` regardless of
whether the user's selector was a branch, tag, abbreviation, or OID. For
crash-safe artifact ordering, initial revision `0` passed to
`StateStore.create()` binds intent/config/legacy import with
`creation.receipt=null`; publish the receipt in the next transaction, producing
the revision `1` installation shape above exactly as native creation does.
`MIGRATION_INSTALLING` is never resumable or replannable, and public
reconciliation preserves it. `legacy-import.json` binds the prepared batch,
backup inventory, source status/identity, workspace snapshot, base SHA, and
stable requirement claim.

Only after all index claims and immutable `completed.json` exist does
`finalize_imported_run()` transact revision 2+: it binds
`migration-completion.json`, verifies the run's reservation/claim/completed
digests, and maps accepted/degraded to `IMPORTED_READ_ONLY` or active/blocked
to `PAUSED` with `SCHEMA3_REPLAN_REQUIRED`. The batch is considered fully
returned only when every selected run is finalized. Completed-batch replay
scans/finalizes missing runs rather than returning early.

`empty_role_runtime(config)` returns both exact role entries with `{operation_id:null, attempt_no:0, failure_count:0, max_attempts:config.task_attempts, active_attempt_token:null, last_error:null}`; migration tests compare the full shape, not merely its keys.

Map statuses:

```python
if legacy.status in {"ACCEPTED", "DEGRADED"}:
    final_status = RunStatus.IMPORTED_READ_ONLY
    final_reason = None
else:
    final_status = RunStatus.PAUSED
    final_reason = {
        "code": "SCHEMA3_REPLAN_REQUIRED",
        "message": f"Schema 3 {legacy.status} requires a new Schema 4 plan",
        "retryable": True,
    }
```

Store no fake task, plan, global verification, or Evaluator object. Bind one immutable `legacy-import.json` that points to the backup manifest and retains accepted revision/degradation acceptance/blocker fields.

- [ ] **Step 7: Wire `migrate` and `resume --replan`**

Create `src/vibe/migration/__init__.py`:

```python
from vibe.migration.schema3 import migrate_schema3

__all__ = ["migrate_schema3"]
```

The CLI passes the exact source selector and resolved base. Human output lists source requirement, target run ID, mapped status, migration ID, and backup path. JSON output uses the normal command envelope plus an `imports` array.

For a fully finalized migrated `PAUSED` run,
`Controller.resume(replan=True)`:

1. Requires `last_error.code == SCHEMA3_REPLAN_REQUIRED`.
2. Requires no Schema 4 plan/tasks.
3. Requires repository identity and run ref match.
4. Requires the current product workspace to be clean.
5. Requires a bound `migration-completion.json`; rejects
   `MIGRATION_INSTALLING` even if `replan=True`.
6. In the same locked transaction used by normal resume, clears only the
   finalized migration pause reason, restores `PLANNING`, and registers the new
   Controller token.
7. Starts a fresh Planner at the explicit Schema 4 base.

`--replan` on any other state is a contract error.

Migration loads/freeze config before reservation. A non-empty project command
catalog requires `allow_project_commands=True`, passed only from the explicit
CLI flag; the authorization mode and exact source digest are persisted in every
imported run just as in native creation. Failure without the flag occurs before
reservation, backup, ref, run, or claim publication.

- [ ] **Step 8: Run migration and CLI tests and commit Task 16**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_migration_schema3 tests.test_cli \
  tests.test_controller_recovery -v
```

Expected: all four mappings, corruption rejection, dirty archive, idempotency, batch preparation, explicit replan, and CLI cases end in `OK`.

Commit:

```bash
git add src/vibe/migration/__init__.py src/vibe/migration/schema3.py \
  src/vibe/cli.py src/vibe/controller.py tests/test_migration_schema3.py \
  tests/fixtures/schema3
git diff --cached --check
git commit -m "feat: migrate schema 3 runs explicitly"
```

### Task 17: Breaking Product Cutover, Packaging, Documentation, and Release Gates

**Files:**
- Delete: `SKILL.md`
- Delete: `agents/openai.yaml`
- Delete: `scripts/harness.py`
- Delete: `tests/test_skill.py`
- Delete: `tests/test_harness.py`
- Create: `tests/test_package_metadata.py`
- Modify: `tests/test_repository_health.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `CONTRIBUTING.md`
- Modify: `SECURITY.md`
- Modify: `CHANGELOG.md`
- Modify: `.github/PULL_REQUEST_TEMPLATE.md`
- Modify: `.github/dependabot.yml`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces one public product entry: installed `vibe`.
- Produces source and installed-wheel access to nine Prompts and three Schemas.
- Produces offline CI across supported Python versions.
- Produces live bilingual documentation for Schema 4 and explicit migration.

- [ ] **Step 1: Add failing package and cutover-health tests before deleting legacy files**

Create `tests/test_package_metadata.py`:

```python
from __future__ import annotations

import importlib.metadata
import unittest

from vibe.prompt_registry import PromptRegistry


class PackageMetadataTests(unittest.TestCase):
    def test_distribution_and_console_entry_point_are_installed(self) -> None:
        distribution = importlib.metadata.distribution("vibe-coding-harness")
        self.assertEqual(distribution.version, "0.1.0.dev0")
        scripts = {
            item.name: item.value
            for item in distribution.entry_points
            if item.group == "console_scripts"
        }
        self.assertEqual(scripts["vibe"], "vibe.cli:main")

    def test_installed_prompt_and_schema_resources_are_complete(self) -> None:
        registry = PromptRegistry.default()
        for prompt_id in (
            "planner",
            "workers/base",
            "workers/implementation",
            "workers/testing",
            "workers/performance",
            "workers/code-quality",
            "workers/documentation",
            "workers/general",
            "evaluator",
        ):
            reference, body = registry.load(prompt_id, 1)
            self.assertTrue(body)
            self.assertTrue(reference.sha256.startswith("sha256:"))
        for schema_name in (
            "plan-v1.schema.json",
            "worker-result-v1.schema.json",
            "evaluation-v1.schema.json",
        ):
            self.assertTrue((registry.schema_root / schema_name).is_file())

    def test_runtime_and_distribution_versions_match(self) -> None:
        from vibe import __version__

        self.assertEqual(__version__, "0.1.0.dev0")
        self.assertEqual(
            __version__,
            importlib.metadata.version("vibe-coding-harness"),
        )


if __name__ == "__main__":
    unittest.main()
```

Replace old Skill assertions in `tests/test_repository_health.py` with:

```python
def test_schema_four_has_one_external_product_entry(self) -> None:
    self.assertTrue((ROOT / "pyproject.toml").is_file())
    self.assertTrue((ROOT / "src/vibe/cli.py").is_file())
    for removed in (
        "SKILL.md",
        "agents/openai.yaml",
        "scripts/harness.py",
        "tests/test_skill.py",
        "tests/test_harness.py",
    ):
        self.assertFalse((ROOT / removed).exists(), removed)


def test_readmes_document_install_all_commands_clean_baseline_and_migration(self) -> None:
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for command in ("vibe run", "vibe resume", "vibe status", "vibe stop", "vibe logs", "vibe migrate"):
            self.assertIn(command, text)
        self.assertIn("Python 3.10", text)
        self.assertIn("Codex CLI", text)
        self.assertIn("clean", text.lower())
        self.assertIn("Schema 3", text)
        self.assertNotIn("$vibe-coding-harness", text)
        self.assertNotIn("scripts/harness.py", text)


def test_ci_contract_is_pinned_offline_cross_platform_and_non_publishing(self) -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    uses_lines = re.findall(r"uses:\s*([^@\s]+)@([^\s]+)", workflow)
    self.assertTrue(uses_lines)
    self.assertTrue(
        all(re.fullmatch(r"[0-9a-f]{40}", ref) for _, ref in uses_lines)
    )
    for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        self.assertIn(f'"{version}"', workflow)
    for required in (
        "runs-on: macos-14",
        "python -m pip install --no-deps -e .",
        "python -m compileall -q src/vibe",
        "python -m unittest discover -s tests -p 'test_*.py' -v",
        "python -m build",
        "python -m twine check",
        "python -m pip check",
    ):
        self.assertIn(required, workflow)
    lowered = workflow.lower()
    for forbidden in ("pypi", "publish", "id-token:", "secrets."):
        self.assertNotIn(forbidden, lowered)
```

Replace the old exact two-action-list and `py_compile scripts/harness.py` assertions entirely with this contract-based check. Keep community-file, license, private-security-routing, and ignore tests.

- [ ] **Step 2: Run the new health tests and verify RED**

Run:

```bash
python3 -m pip install --no-deps -e .
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_package_metadata tests.test_repository_health -v
```

Expected: repository-health failures because legacy entries still exist and docs/CI still describe Schema 3 Skill behavior.

- [ ] **Step 3: Prove all reusable legacy tests were migrated, then delete the old product**

Before deletion, map every retained behavior:

| Old coverage | Replacement |
|---|---|
| `tests/test_harness.py:378-579` exact snapshot | `tests/test_repository_snapshot.py` |
| strict JSON/Unicode/symlink/write errors | `tests/test_state_store.py`, `tests/test_cli.py`, `tests/test_migration_schema3.py` |
| prepared-marker recovery | `tests/test_controller_recovery.py`, `tests/test_integrator.py` |
| structured evidence/criteria binding | `tests/test_runners.py`, `tests/test_verification.py`, `tests/test_controller_fake_provider.py` |
| legacy state/evaluation/interruption validation | `tests/test_migration_schema3.py` |
| Skill/root/serial-role contracts | intentionally removed and replaced by package/Controller/parallel contracts |

Run all replacement files once. Only then delete:

```bash
git rm SKILL.md agents/openai.yaml scripts/harness.py \
  tests/test_skill.py tests/test_harness.py
```

Do not leave a shell or Python compatibility shim. Immediately after deletion—and before editing docs to hide failures—run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_migration_schema3 \
  tests.test_repository_snapshot \
  tests.test_state_store \
  tests.test_controller_recovery -v
```

This proves migration fixtures and the extracted validator have no runtime dependency on the deleted harness.

- [ ] **Step 4: Rewrite live documentation around the external Controller**

`README.md` and `README.zh-CN.md` must contain matching sections:

```text
What it is
Architecture
Requirements: Git, Python 3.10+, Codex CLI
Install from a clone with python -m pip install .
Configuration: vibe.json
Run
Resume
Status
Stop
Logs
Schema 3 migration
Safety invariants
Development
```

Document:

- clean commit baseline and no `--allow-dirty`;
- `FAILED` is terminal and cannot be resumed; after checking out the desired clean commit, recovery requires an explicit new `vibe run`, which records that new baseline;
- Planner finite DAG;
- path/resource-safe parallel specialist Workers;
- fresh worktree/branch/context per Attempt;
- serial candidate verification and Git CAS;
- independent Evaluator verdict meanings;
- foreground stop/resume;
- no auto-merge/push/PR/publish;
- Schema 3 explicit backup/mapping;
- real Codex smoke opt-in.

`CONTRIBUTING.md` must replace Root/Generator/serial/Skill authoring rules with Controller sole-writer, Prompt resource, Provider adapter, worktree, verification, and recovery invariants.

`SECURITY.md` must describe Controller state, Worker path scope, Provider process identity, output-schema trust boundary, Git ref CAS, and private vulnerability reporting.

`CHANGELOG.md` under `Unreleased` must explicitly record:

- Schema 4 breaking generation;
- external `vibe` CLI;
- parallel specialist Workers;
- prepared dispatch and integration recovery;
- independent Evaluator and bounded repair;
- explicit Schema 3 migration;
- removal of Skill metadata and legacy script.

Update the PR template to require package resources, Schema 4 state/recovery tests, migration impact, offline suite, and real-provider disclosure.

- [ ] **Step 5: Update Dependabot and CI without adding publishing**

Keep pinned GitHub Action SHAs. Add a weekly `pip` ecosystem entry for `pyproject.toml` build tooling.

Update the Ubuntu test matrix to Python `3.10`, `3.11`, `3.12`, `3.13`, and `3.14`:

```yaml
- name: Install package
  run: python -m pip install --no-deps -e .

- name: Check environment
  run: python -m pip check

- name: Compile package
  run: python -m compileall -q src/vibe

- name: Run offline tests
  env:
    PYTHONDONTWRITEBYTECODE: "1"
  run: python -m unittest discover -s tests -p 'test_*.py' -v
```

Add a package job on Ubuntu/Python 3.12 and build into a fresh runner-temporary directory:

```yaml
- name: Install build checks
  run: python -m pip install 'build>=1.2,<2' 'twine>=5,<7'

- name: Build distributions
  run: python -m build --outdir "$RUNNER_TEMP/dist"

- name: Check distributions
  run: python -m twine check "$RUNNER_TEMP"/dist/*

- name: Install wheel outside source tree
  run: |
    python -m venv "$RUNNER_TEMP/vibe-wheel"
    "$RUNNER_TEMP/vibe-wheel/bin/python" -m pip install --no-deps "$RUNNER_TEMP"/dist/*.whl
    cd "$RUNNER_TEMP"
    "$RUNNER_TEMP/vibe-wheel/bin/vibe" --help
    "$RUNNER_TEMP/vibe-wheel/bin/python" -m pip check
    "$RUNNER_TEMP/vibe-wheel/bin/python" -c "import sys; from pathlib import Path; from vibe.prompt_registry import PromptRegistry; r=PromptRegistry.default(); expected=Path(sys.prefix)/'share'/'vibe'; assert expected in r.prompt_root.parents or r.prompt_root==expected; [r.load(i,1) for i in ('planner','workers/base','workers/implementation','workers/testing','workers/performance','workers/code-quality','workers/documentation','workers/general','evaluator')]; assert all((r.schema_root/n).is_file() for n in ('plan-v1.schema.json','worker-result-v1.schema.json','evaluation-v1.schema.json'))"

- name: Install sdist outside source tree
  run: |
    python -m venv "$RUNNER_TEMP/vibe-sdist"
    "$RUNNER_TEMP/vibe-sdist/bin/python" -m pip install --no-deps "$RUNNER_TEMP"/dist/*.tar.gz
    cd "$RUNNER_TEMP"
    "$RUNNER_TEMP/vibe-sdist/bin/vibe" --help
    "$RUNNER_TEMP/vibe-sdist/bin/python" -m pip check
```

Add a `macos-14` Python 3.12 job that installs editable, runs the offline
Provider/Controller tests (including the real
`proc_pidinfo(PROC_PIDTBSDINFO)` microsecond process-identity branch), and runs
the package metadata test. Do not add PyPI credentials, a release trigger, or
any publish step.

- [ ] **Step 6: Run focused docs, package, migration, and repository gates**

Run:

```bash
python3 -m pip install --no-deps -e .
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_package_metadata \
  tests.test_repository_health \
  tests.test_migration_schema3 \
  tests.test_cli -v
```

Expected: all tests end in `OK`; no live document refers to the Skill or old script as a product entry.

- [ ] **Step 7: Run the complete offline release gate**

Run:

```bash
release_root="$(mktemp -d)"
python3 -m pip install 'build>=1.2,<2' 'twine>=5,<7'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q src/vibe
python3 -m build --outdir "$release_root/dist"
python3 -m twine check "$release_root"/dist/*
```

Expected:

- Full suite ends in `OK`.
- `tests.test_codex_cli_smoke` is explicitly skipped unless the opt-in variable is set.
- Compileall exits `0`.
- One sdist and one `py3-none-any.whl` are built.
- Twine reports both distributions `PASSED`.

Install both the wheel and sdist into separate fresh virtual environments outside the source tree:

```bash
smoke_root="$(mktemp -d)"
for kind in wheel sdist; do
  python3 -m venv "$smoke_root/$kind"
done
"$smoke_root/wheel/bin/python" -m pip install --no-deps "$release_root"/dist/*.whl
"$smoke_root/sdist/bin/python" -m pip install --no-deps "$release_root"/dist/*.tar.gz
cd "$smoke_root"
for kind in wheel sdist; do
  "$smoke_root/$kind/bin/python" -m pip check
  "$smoke_root/$kind/bin/vibe" --help
  "$smoke_root/$kind/bin/python" -c \
    "import sys; from pathlib import Path; from vibe.prompt_registry import PromptRegistry; r=PromptRegistry.default(); expected=Path(sys.prefix)/'share'/'vibe'; assert expected in r.prompt_root.parents or r.prompt_root==expected; [r.load(i,1) for i in ('planner','workers/base','workers/implementation','workers/testing','workers/performance','workers/code-quality','workers/documentation','workers/general','evaluator')]; assert all((r.schema_root/n).is_file() for n in ('plan-v1.schema.json','worker-result-v1.schema.json','evaluation-v1.schema.json'))"
done
```

Expected: both `pip check` runs report no broken requirements, both installed `vibe --help` commands succeed outside the checkout, and all nine Prompt plus three Schema resources load from each installed artifact.

- [ ] **Step 8: Commit Task 17**

```bash
git add tests/test_package_metadata.py tests/test_repository_health.py \
  README.md README.zh-CN.md CONTRIBUTING.md SECURITY.md CHANGELOG.md \
  .github/PULL_REQUEST_TEMPLATE.md .github/dependabot.yml \
  .github/workflows/ci.yml
git diff --cached --check
git commit -m "feat: cut over to schema 4 external controller"
```

The five `git rm` paths from Step 3 remain staged for this commit. Inspect `git status --short` before committing and stage only the explicit Task 17 paths above; do not use `git add -A`.

## Final Cross-Plan Release Gate

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q src/vibe
release_root="$(mktemp -d)"
python3 -m pip install 'build>=1.2,<2' 'twine>=5,<7'
python3 -m build --outdir "$release_root/dist"
python3 -m twine check "$release_root"/dist/*
python3 -m venv "$release_root/wheel-venv"
python3 -m venv "$release_root/sdist-venv"
"$release_root/wheel-venv/bin/python" -m pip install --no-deps "$release_root"/dist/*.whl
"$release_root/sdist-venv/bin/python" -m pip install --no-deps "$release_root"/dist/*.tar.gz
cd "$release_root"
"$release_root/wheel-venv/bin/python" -m pip check
"$release_root/wheel-venv/bin/vibe" --help
"$release_root/sdist-venv/bin/python" -m pip check
"$release_root/sdist-venv/bin/vibe" --help
for kind in wheel sdist; do
  "$release_root/$kind-venv/bin/python" -c \
    "import sys; from pathlib import Path; from vibe.prompt_registry import PromptRegistry; r=PromptRegistry.default(); expected=Path(sys.prefix)/'share'/'vibe'; assert expected in r.prompt_root.parents or r.prompt_root==expected; [r.load(i,1) for i in ('planner','workers/base','workers/implementation','workers/testing','workers/performance','workers/code-quality','workers/documentation','workers/general','evaluator')]; assert all((r.schema_root/n).is_file() for n in ('plan-v1.schema.json','worker-result-v1.schema.json','evaluation-v1.schema.json'))"
done
git diff --check
git status --short
```

Then verify all approved outcomes:

1. No runtime reads `SKILL.md` or requires a Root Agent.
2. Controller launches Planner, Worker, and Evaluator through Provider Adapter.
3. Planner emits a finite validated DAG.
4. Independent tasks really overlap; conflicting tasks serialize.
5. Every Worker Attempt has a fresh context, branch, and worktree.
6. Failed candidates never pollute the integration ref.
7. Dispatch and Git CAS crash windows recover uniquely.
8. Task/global verification and Evaluator bind one immutable commit.
9. `UNVERIFIED` supplements evidence and `BLOCKED` pauses.
10. Stop and Controller never double-write state.
11. User branch/index/worktree/remotes remain unchanged.
12. Schema 3 migration is explicit, byte-preserving, idempotent, and non-forging.
13. Default CI covers Fake Provider, Git integration, fault injection, CLI, package resources, and migration without networked Agent calls.
