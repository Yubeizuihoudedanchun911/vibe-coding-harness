# External Controller Phase 3: Git, Scheduler, Verification, and Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic repository layer that accepts only clean commit baselines, validates finite Planner DAGs, dispatches only provably independent tasks, verifies immutable candidates, and advances the run ref with recoverable Git compare-and-swap.

**Architecture:** one hardened `GitRunner` owns every production Git invocation, `WorktreeManager` owns repository truth and Controller-authored task commits, `Scheduler` owns pure DAG/resource decisions, `VerificationGate` resolves only frozen authorized command IDs, and `Integrator` serializes candidate construction plus CAS. Worker claims are never trusted; every raw delta, source commit, path, command result, and integration head is reconstructed at the Controller boundary.

**Tech Stack:** Python 3.10+ standard library, Git CLI, isolated temporary repositories/worktrees, `unittest`, deterministic fault hooks.

## Global Constraints

- Resolve every base as a canonical full commit OID.
- Reject tracked, staged, unstaged, non-ignored untracked, and dirty submodule product content. Exclude `.vibe-coding/` and Git-ignored content.
- Never checkout, stage, reset, merge, or modify the user's current branch/index/worktree.
- Use `refs/heads/vibe/run-<run-id>` for the run and `refs/heads/vibe/<run-id>/<task-id>-a<attempt>` for Worker attempts.
- Every Worker attempt has a fresh reserved branch plus a detached worktree based on the integration head recorded at dispatch. Worker code cannot stage, commit, or advance that branch.
- The Controller audits the raw delta, creates exactly one deterministic non-merge source commit through a temporary index, and advances only the reserved task ref through prepared CAS.
- Derive changes with NUL-delimited Git output. Audit rename source/destination, deletions, gitlinks, and `.gitmodules`.
- Scope accepts only exact repo-relative files, directory prefixes ending `/`, or exclusive `.`. An uncertain overlap is an overlap.
- Planner output is a finite acyclic graph with unique IDs, full acceptance coverage, registered Worker types, authorized command IDs/scopes, and at most 128 tasks.
- Planner may append IDs from the frozen catalog but cannot remove required command IDs or introduce command specifications.
- A task becomes ready only after every dependency is `COMPLETED` in the integration ref.
- Task verification and frozen required commands run again on a candidate created from the current integration head.
- Verification never uses a shell and records commit, argv, cwd, environment names, timeout, exit code, stdout/stderr paths, and hashes.
- Source commit writes `pending_source_commit` before task-ref CAS; integration writes `pending_integration` before run-ref CAS.
- `VerificationGate` and `Integrator` never acquire the non-reentrant run lock. Controller code and focused tests hold one `StateStore.lock()` across their Artifact/state operations.
- CAS recovery has exactly three outcomes: retry from expected, complete from candidate, or pause on any other ref value.
- Conflict, scope escape, merge commit, or failed verification never moves the integration ref.
- Keep failed Worker and candidate worktrees for audit; do not destructively clean them.
- All production Git calls use the hardened runner. Hooks, signing, pager, prompts, credential helpers, fsmonitor, system/global config, executable filters, merge drivers, diff commands/textconv, and remote-capable subcommands are disabled or rejected before use.

---

## File Map

- Create `src/vibe/git_runner.py`: frozen Git binary/environment/argv policy and executable-integration rejection.
- Create `src/vibe/worktrees.py`: repository identity, exact snapshot, clean baseline, protected snapshots, refs/worktrees, raw-delta audits, deterministic source commits.
- Create `src/vibe/scheduler.py`: plan semantics, scope/resource overlap, ready and dispatchable sets.
- Create `src/vibe/verification.py`: no-shell command execution and commit-bound evidence.
- Create `src/vibe/integrator.py`: candidate cherry-pick, verification, prepared integration, Git CAS recovery.
- Create `tests/support/git_repo.py`: reusable real temporary repository fixture.
- Create `tests/support/fault_injector.py`: named deterministic crash hook.
- Create `tests/test_repository_snapshot.py`: migrated raw-byte fingerprint coverage.
- Create `tests/test_worktrees.py`: clean baseline, refs/worktrees, source range/path audit.
- Create `tests/test_git_runner.py`: hostile hook/filter/driver/prompt sentinels and argv policy.
- Create `tests/test_plan_validation.py`: Planner DAG semantics.
- Create `tests/test_scheduler.py`: readiness, dependencies, path/resource-safe concurrency.
- Create `tests/test_verification.py`: command policy and evidence binding.
- Create `tests/test_integrator.py`: candidate/CAS success, failure, and crash recovery.

### Task 6: Repository Identity, Clean Baseline, Worktrees, and Source Audit

**Files:**
- Modify: `docs/superpowers/specs/2026-07-23-external-controller-parallel-workers-design.md`
- Create: `src/vibe/git_runner.py`
- Create: `src/vibe/worktrees.py`
- Create: `tests/support/git_repo.py`
- Create: `tests/test_repository_snapshot.py`
- Create: `tests/test_worktrees.py`
- Create: `tests/test_git_runner.py`

**Interfaces:**
- Produces `RepositorySnapshot(revision, workspace_fingerprint)`.
- Produces `RepositoryBaseline(identity, base_ref, base_sha)`.
- Produces `TaskWorktree(path, branch, base_sha)` whose worktree HEAD is detached while `branch` is a separately reserved ref.
- Produces `ProtectedGitSnapshot(user_head, index_tree, status_digest, refs, packed_refs_digest, config_digest, remote_urls)`.
- Produces strict `AttemptPreflight(role, task_id, attempt_created_at, expected_base, branch, worktree, snapshot)` canonical bytes shared by all roles.
- Produces `SourceCommitMetadata(run_id, task_id, attempt_no, attempt_created_at)` with no caller-controlled author/message.
- Produces `PreparedSourceCommit` and `SourceAudit(task_base_sha, source_head, source_commits, changed_paths, gitlinks_changed, protected_before, protected_after)`.
- Produces `canonical_repository_identity(target)`.
- Produces `repository_snapshot(target)`.
- Produces `WorktreeManager.capture_worker_preflight(task, task_id, attempt_created_at, protected) -> AttemptPreflight`.
- Produces `GitReadOnlyAudit(manager)` implementing Phase 2 `ReadOnlyAudit.capture/assert_unchanged`.
- Produces `GitRunner.run_local()` and `WorktreeManager.assert_clean_baseline()`, ref/worktree creation, protected/read-only audit, `prepare_source_commit()`, source CAS, and three-way recovery classification.

- [ ] **Step 0: Record the two approved-spec authority corrections**

Before code, patch the approved design in two narrowly scoped places:

1. Worker owns scoped raw edits and handoff; Controller owns the audited
   deterministic task commit and prepared task-ref CAS. Add the rationale that
   Codex `workspace-write` does not write Git metadata.
2. Planner/Worker/Evaluator output may contain only stable command IDs selected
   from the frozen, explicitly user-authorized catalog. Remove every example or
   sentence that lets an Agent return `{argv,cwd,timeout,env}` command objects;
   only Controller-owned `vibe.json` parsing creates `CommandSpec`.

Do not change the external-controller architecture or create a Skill.

- [ ] **Step 1: Create a reusable real-Git test fixture**

Create `tests/support/git_repo.py`:

```python
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


class GitRepositoryFixture:
    def setUpGitRepository(self) -> None:
        self.git_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.git_temporary.cleanup)
        self.target = Path(self.git_temporary.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Vibe Tests")
        self.git("config", "user.email", "vibe-tests@example.invalid")
        (self.target / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "base")

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", "-C", str(cwd or self.target), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.fail(
                f"git {' '.join(arguments)} failed\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result.stdout.strip()
```

- [ ] **Step 2: Write failing repository and source-audit tests**

Create `tests/test_worktrees.py`:

```python
from __future__ import annotations

import unittest

from tests.support.git_repo import GitRepositoryFixture
from vibe.models import ContractError, TaskContract
from vibe.worktrees import WorktreeManager


class WorktreeManagerTests(GitRepositoryFixture, unittest.TestCase):
    def setUp(self) -> None:
        self.setUpGitRepository()
        self.manager = WorktreeManager(self.target)

    def test_clean_baseline_is_full_oid_and_does_not_change_user_checkout(self) -> None:
        branch = self.git("symbolic-ref", "--short", "HEAD")
        index = self.git("write-tree")
        baseline = self.manager.assert_clean_baseline()
        self.assertEqual(len(baseline.base_sha), 40)
        self.assertEqual(branch, "main")
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD"), branch)
        self.assertEqual(self.git("write-tree"), index)

    def test_detached_head_records_head_selector_and_full_oid(self) -> None:
        self.git("checkout", "--detach", "HEAD")
        baseline = self.manager.assert_clean_baseline()
        self.assertEqual(baseline.base_ref, "HEAD")
        self.assertRegex(baseline.base_sha, r"[0-9a-f]{40}")

    def test_dirty_product_is_rejected_but_control_and_ignored_paths_are_excluded(self) -> None:
        (self.target / "README.md").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "clean product baseline"):
            self.manager.assert_clean_baseline()

        self.git("checkout", "--", "README.md")
        (self.target / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore logs")
        (self.target / "ignored.log").write_text("ignored\n", encoding="utf-8")
        (self.target / ".vibe-coding").mkdir()
        (self.target / ".vibe-coding/state.tmp").write_text("control\n", encoding="utf-8")
        self.manager.assert_clean_baseline()

    def test_run_and_task_worktrees_do_not_checkout_the_user_branch(self) -> None:
        baseline = self.manager.assert_clean_baseline()
        run_ref = self.manager.create_run_ref(
            "RUN-20260723-001",
            baseline.base_sha,
        )
        task = self.manager.create_task_worktree(
            "RUN-20260723-001",
            "TASK-001",
            1,
            baseline.base_sha,
        )
        self.assertEqual(run_ref, "refs/heads/vibe/run-RUN-20260723-001")
        self.assertTrue(task.path.is_dir())
        self.assertEqual(
            self.git("symbolic-ref", "--short", "HEAD"),
            "main",
        )

    def test_controller_prepares_a_deterministic_commit_from_raw_worker_edits(self) -> None:
        baseline = self.manager.assert_clean_baseline()
        task = self.manager.create_task_worktree(
            "RUN-20260723-001",
            "TASK-001",
            1,
            baseline.base_sha,
        )
        self.git("mv", "README.md", "GUIDE.md", cwd=task.path)
        contract = TaskContract(
            id="TASK-001",
            objective="Rename the guide",
            worker_type="documentation",
            covers=("AC-001",),
            depends_on=(),
            path_scope=("README.md", "GUIDE.md"),
            exclusive_resources=(),
            acceptance_checks=(),
            max_attempts=3,
        )
        protected = self.manager.snapshot_protected_git(task.branch)
        preflight = self.manager.capture_worker_preflight(
            task=task,
            task_id=contract.id,
            attempt_created_at="2026-07-23T10:00:00.123456+00:00",
            protected=protected,
        )
        prepared = self.manager.prepare_source_commit(
            contract=contract,
            worktree=task,
            preflight=preflight,
            metadata=self.fixed_source_metadata(attempt_no=1),
        )
        self.assertEqual(
            prepared.source_audit.changed_paths,
            ("GUIDE.md", "README.md"),
        )
        self.assertEqual(
            self.git("rev-parse", task.branch),
            baseline.base_sha,
        )
        replay = self.manager.prepare_source_commit(
            contract,
            task,
            preflight,
            self.fixed_source_metadata(attempt_no=1),
        )
        self.assertEqual(replay.candidate_commit, prepared.candidate_commit)

    def test_scope_escape_and_protected_git_mutation_are_rejected(self) -> None:
        baseline = self.manager.assert_clean_baseline()
        task = self.manager.create_task_worktree(
            "RUN-20260723-001",
            "TASK-002",
            1,
            baseline.base_sha,
        )
        (task.path / "outside.txt").write_text("escape\n", encoding="utf-8")
        contract = TaskContract(
            id="TASK-002",
            objective="Edit source",
            worker_type="implementation",
            covers=("AC-001",),
            depends_on=(),
            path_scope=("src/",),
            exclusive_resources=(),
            acceptance_checks=(),
            max_attempts=3,
        )
        with self.assertRaisesRegex(ContractError, "outside path scope"):
            self.manager.prepare_source_commit(
                contract,
                task,
                self.manager.capture_worker_preflight(
                    task=task,
                    task_id=contract.id,
                    attempt_created_at="2026-07-23T10:00:00.123456+00:00",
                    protected=self.manager.snapshot_protected_git(task.branch),
                ),
                self.fixed_source_metadata(attempt_no=1),
            )

    def test_dispatch_preflight_rejects_later_ref_config_or_remote_mutation(self) -> None:
        baseline = self.manager.assert_clean_baseline()
        for mutation in ("ref", "config", "remote"):
            with self.subTest(mutation=mutation):
                task, contract = self.task_and_contract_for_mutation(
                    baseline,
                    mutation,
                )
                preflight = self.manager.capture_worker_preflight(
                    task=task,
                    task_id=contract.id,
                    attempt_created_at="2026-07-23T10:00:00.123456+00:00",
                    protected=self.manager.snapshot_protected_git(task.branch),
                )
                self.mutate_protected_surface_after_dispatch(mutation)
                with self.assertRaisesRegex(ContractError, "protected Git"):
                    self.manager.prepare_source_commit(
                        contract=contract,
                        worktree=task,
                        preflight=preflight,
                        metadata=self.fixed_source_metadata(attempt_no=1),
                    )


if __name__ == "__main__":
    unittest.main()
```

Create `tests/test_repository_snapshot.py` by moving the exact scenarios currently covered in `tests/test_harness.py:378-579` and changing only the call site from `HARNESS_RUNTIME.snapshot(target)` to `repository_snapshot(target).as_dict()`. Preserve these named behaviors:

```text
test_snapshot_is_stable_and_excludes_control_state
test_snapshot_detects_tracked_unstaged_content
test_snapshot_detects_staged_content
test_snapshot_detects_nonignored_untracked_content_and_bytes
test_snapshot_excludes_ignored_content
test_snapshot_disables_textconv_helpers_and_hashes_exact_bytes
test_snapshot_hashes_raw_tracked_bytes_before_clean_filters
test_snapshot_hashes_assume_unchanged_tracked_bytes
test_snapshot_hashes_dirty_submodule_content_recursively
```

Add real-Git tests for authorized and unauthorized deletion, a scope whose existing symlink ancestor resolves outside the product worktree, scope `.` with a force-added `.vibe-coding/**` file, and a local bare-remote sentinel. Source audit must reject both control-path cases, record an authorized deletion, reject an out-of-scope deletion, and leave remote config plus every remote ref byte-for-byte unchanged.

Do not weaken the old raw-byte, filter, `assume-unchanged`, or recursive submodule assertions.

- [ ] **Step 3: Run repository tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_repository_snapshot tests.test_worktrees -v
```

Expected: import failure because `vibe.worktrees` does not exist.

- [ ] **Step 4: Implement and adversarially test the single hardened Git runner**

Every production Git call, including fingerprinting, worktree creation, local config inspection, `read-tree`, `write-tree`, `commit-tree`, cherry-pick, and `update-ref`, goes through `GitRunner`. Resolve the Git executable once at Controller construction and reject later substitution. Start from a minimal frozen environment; explicitly remove `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`, every `GIT_SSH*`, and inherited identity/signing variables. Set:

```text
GIT_CONFIG_NOSYSTEM=1
GIT_CONFIG_GLOBAL=/dev/null
GIT_TERMINAL_PROMPT=0
GIT_ASKPASS=/bin/false
GIT_PAGER=cat
```

Every argv begins with:

```text
<resolved-git> --no-pager
  -c core.hooksPath=/dev/null
  -c core.fsmonitor=false
  -c commit.gpgSign=false
  -c tag.gpgSign=false
  -c credential.helper=
```

Allowlist local-only subcommands/flags needed by this plan and reject `push`,
`fetch`, `pull`, `clone`, `ls-remote`, `send-email`, arbitrary aliases, `-c`
from callers, external protocols, and any caller-supplied environment override.
The public runner has no environment mapping. It exposes only two typed
internal options:

```python
@dataclass(frozen=True)
class GitInternalOptions:
    index_file: Path | None = None
    commit_timestamp: str | None = None


def run_local(
    self,
    cwd: Path,
    *argv: str,
    internal: GitInternalOptions = GitInternalOptions(),
) -> CompletedProcess[bytes]:
    ...
```

`index_file` is accepted only for the exact `read-tree`, literal-pathspec
`add`, `write-tree`, and diff-audit calls used by source preparation. Validate
every ancestor without following symlinks and require the file below the
current run's `.vibe-coding/tmp/indexes/<operation-id>/` directory; only the
runner translates it to `GIT_INDEX_FILE`. `commit_timestamp` is accepted only
for `commit-tree`, must equal the already-persisted active Attempt
`created_at`, and sets both author and committer dates together with the fixed
Controller identity. Any other option/subcommand combination is rejected.

Inspect repository-local config and `.gitattributes` as raw data before product
operations; reject active `filter.*.{clean,smudge,process}`,
`merge.*.driver`, `diff.*.{command,textconv}`, external fsmonitor, includes, or
aliases that could execute code. Controller commits never inherit user identity
or signing.

`tests/test_git_runner.py` installs hostile pre-commit/post-checkout hooks, filter clean/smudge/process commands, merge/diff drivers, fsmonitor, aliases, askpass, signing, and credential-helper sentinels. Exercise baseline, worktree, snapshot, source preparation, verification candidate, and CAS paths and assert every sentinel count remains zero. A required custom filter is rejected before any content is staged. Also assert remote URLs/config/refs are byte-identical and every remote-capable command is rejected.

- [ ] **Step 5: Migrate the exact fingerprint implementation**

Move the behavior of these existing functions from `scripts/harness.py` into `src/vibe/worktrees.py` without changing their byte-stream semantics:

```text
_git_bytes
_revision
_resolve_revision
_hash_part
_untracked_fingerprint_stream
_index_modes
_tracked_worktree_fingerprint_stream
_submodule_fingerprint_stream
_repository_snapshot
```

Rename the public boundary to:

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


def repository_snapshot(target: Path) -> RepositorySnapshot:
    return _repository_snapshot(target.resolve())
```

Keep this exact product pathspec:

```python
PRODUCT_PATHSPEC = (
    "--",
    ".",
    ":(exclude).vibe-coding",
    ":(exclude).vibe-coding/**",
)
```

Keep `--binary`, `--no-ext-diff`, `--no-textconv`, `--no-color`, and `--ignore-submodules=none` on fingerprint diffs.

- [ ] **Step 6: Implement repository identity, detached worktrees, protected snapshots, and prepared source commits**

Add these immutable types:

```python
@dataclass(frozen=True)
class RepositoryBaseline:
    identity: str
    base_ref: str
    base_sha: str


@dataclass(frozen=True)
class TaskWorktree:
    path: Path
    branch: str
    base_sha: str


@dataclass(frozen=True)
class ProtectedGitSnapshot:
    user_head: str
    index_tree: str
    status_digest: str
    refs: tuple[tuple[str, str], ...]
    packed_refs_digest: str
    config_digest: str
    remote_urls: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class AttemptPreflight:
    role: str
    task_id: str | None
    attempt_created_at: str
    expected_base: str
    branch: str | None
    worktree: str
    snapshot: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "role": self.role,
            "task_id": self.task_id,
            "attempt_created_at": self.attempt_created_at,
            "expected_base": self.expected_base,
            "branch": self.branch,
            "worktree": self.worktree,
            "snapshot": self.snapshot,
        }


@dataclass(frozen=True)
class SourceCommitMetadata:
    run_id: str
    task_id: str
    attempt_no: int
    attempt_created_at: str

    @property
    def message(self) -> str:
        return (
            f"vibe({self.run_id}): {self.task_id} "
            f"attempt {self.attempt_no}"
        )


@dataclass(frozen=True)
class SourceAudit:
    task_base_sha: str
    source_head: str
    source_commits: tuple[str, ...]
    changed_paths: tuple[str, ...]
    gitlinks_changed: bool
    protected_before: ProtectedGitSnapshot
    protected_after: ProtectedGitSnapshot


@dataclass(frozen=True)
class PreparedSourceCommit:
    tree_oid: str
    candidate_commit: str
    source_audit: SourceAudit
    source_audit_body: bytes
```

Implement canonical identity:

```python
def canonical_repository_identity(target: Path) -> str:
    root = Path(
        _git_text(target, "rev-parse", "--show-toplevel")
    ).resolve()
    common = Path(
        _git_text(target, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    try:
        root_bytes = str(root).encode("utf-8")
        common_bytes = str(common).encode("utf-8")
    except UnicodeEncodeError as error:
        raise ContractError(
            "repository paths must be representable as UTF-8"
        ) from error
    body = root_bytes + b"\0" + common_bytes
    return "sha256:" + hashlib.sha256(body).hexdigest()
```

`assert_clean_baseline()` must require:

```python
snapshot = repository_snapshot(self.target)
status = _git_bytes(
    self.target,
    "status",
    "--porcelain=v2",
    "-z",
    "--untracked-files=all",
    "--ignore-submodules=none",
    *PRODUCT_PATHSPEC,
)
if status:
    raise ContractError("run requires a clean product baseline")
```

Resolve `HEAD^{commit}` to a canonical OID and capture the symbolic ref or literal `HEAD` when detached.

Create the run ref normally. For each task, reserve its branch ref but attach no worktree to it; the Worker worktree is detached so task-ref CAS cannot desynchronize its real index:

```python
_git(self.target, "update-ref", run_ref, base_sha, ZERO_OID)
_git(self.target, "update-ref", task_ref, base_sha, ZERO_OID)
_git(self.target, "worktree", "add", "--detach", str(path), base_sha)
```

Use `--detach <commit>` for task and disposable worktrees. Validate that every
worktree path resolves below `.vibe-coding/worktrees/<run-id>/`. Before Worker
launch capture a `ProtectedGitSnapshot` covering user HEAD/index/status, every
ref except the exact reserved attempt ref, packed-refs bytes, effective local
config, and remote URLs. Serialize it with the persisted Attempt creation
timestamp as immutable `preflight.json`; bind its ArtifactRef in both the active
Attempt and pending dispatch before Provider start. Source preparation must use
that persisted snapshot, never a newly captured value supplied by the caller.

`prepare_source_commit()` follows this exact no-ref-mutation protocol:

1. Require detached worktree `HEAD == task.base_sha`, reserved task ref still equals base, and the real worktree index equals the base tree (the Worker never staged).
2. Load and digest-verify the active Attempt's immutable preflight, capture the after `ProtectedGitSnapshot`, require exact equality to its protected snapshot for all protected values, and require no unexpected ref/config/remote mutation. Never roll back or force a mismatch.
3. Enumerate the raw tracked/untracked/deleted/renamed/submodule delta with NUL-delimited status/diff output. Include both rename ends, `.gitmodules`, mode `160000`, and reject empty output, conflicts, staged files, ignored/control paths, unsafe symlink ancestors, or anything outside scope.
4. Create a temporary index outside the product worktree; hardened `read-tree <base>`, then stage only the already-audited authorized pathspecs with literal pathspec mode and `git add -A -- <paths>`. Re-enumerate the temporary-index diff and require it equals the audited raw delta exactly.
5. `write-tree`, then `commit-tree <tree> -p <base>` with fixed `Vibe Controller <vibe-controller@localhost>`, the exact `attempt_created_at` loaded from that preflight/active Attempt, and a deterministic message. No wall-clock read is permitted here. This creates one unreachable non-merge commit and no ref change.
6. Reconstruct the commit and require exact parent/tree/author/committer/date/message. Produce canonical `source-audit.json` bytes binding protected snapshots, raw delta, scopes, base/tree/candidate, and verdict.
7. Return `PreparedSourceCommit`; repeated preparation from identical bytes/metadata returns the same tree/commit OIDs.

Before accepting a scope, resolve every existing path prefix in the immutable base worktree with `lstat()`/`resolve()` and require it to remain below that worktree. This rejects `linked-dir/new.py` when `linked-dir` is a symlink outside even if the final file does not yet exist.

Do not read `WorkerResult.changed_paths` for authorization.

The Controller persists `source-audit.json` and the exact roadmap `pending_source_commit` in one StateStore transaction, then calls `after_worker_edit_before_controller_commit`. `apply_source_commit_cas()` performs only `update-ref <task-ref> <candidate> <base>`, then calls `after_controller_commit_before_verification_binding`. Recovery returns `RETRY_CAS` for base, `COMPLETE_STATE` for candidate after exact commit/source-audit verification, and `PAUSE` for every other ref. The completion transaction binds `source_commits=[candidate]`, clears the marker, and moves the still-active `VERIFYING` Task to `READY_TO_INTEGRATE`.

- [ ] **Step 7: Add read-only audit and original-workspace invariants**

`WorktreeManager.capture_read_only_audit()` reuses `ProtectedGitSnapshot` and
additionally includes the disposable worktree product fingerprint. Add the
concrete Phase 2 adapter:

```python
@dataclass(frozen=True)
class GitReadOnlyAudit:
    manager: WorktreeManager

    def capture(self, worktree: Path) -> dict[str, object]:
        return self.manager.capture_read_only_audit(worktree)

    def assert_unchanged(
        self,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        if after != before:
            raise ContractError("read-only role changed repository state")
```

Controller constructs one `GitReadOnlyAudit` from its WorktreeManager and
injects it into both PlannerRunner and EvaluatorRunner. Their before/after
equality covers all refs, local config, remote URLs, the user's
HEAD/index/status, and the disposable product tree. A fake role that moves any
ref or changes config/remotes must be rejected even when files are unchanged.
VerificationGate keeps its separate audit because it may tolerate only
independently proven ignored cache files while still rejecting product/ref
changes.

Snapshot the user's symbolic ref, index tree, status bytes, and HEAD before every worktree/ref test and assert they are identical afterward.

- [ ] **Step 8: Run repository tests and commit Task 6**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_repository_snapshot tests.test_git_runner tests.test_worktrees -v
```

Expected: all exact-byte, clean-baseline, worktree, ancestry, rename, scope, and user-workspace tests end in `OK`.

Commit:

```bash
git add docs/superpowers/specs/2026-07-23-external-controller-parallel-workers-design.md \
  src/vibe/git_runner.py src/vibe/worktrees.py tests/support/git_repo.py \
  tests/test_git_runner.py \
  tests/test_repository_snapshot.py tests/test_worktrees.py
git diff --cached --check
git commit -m "feat: add isolated git worktree manager"
```

### Task 8: Planner DAG and Append-Only Repair Validation

**Files:**
- Create: `src/vibe/scheduler.py`
- Create: `tests/test_plan_validation.py`
- Modify: `src/vibe/runners/planner.py`

**Interfaces:**
- Consumes `PlanDocument`, `TaskContract`, `FrozenRunConfig`, and registered Worker types.
- Produces `normalize_scope(value: str) -> str`.
- Produces `path_matches_scope(path: str, scope: str) -> bool`.
- Produces `Scheduler.validate_plan(document, config, prior_plans)`.
- Produces stable `topological_order(document) -> tuple[str, ...]`.

- [ ] **Step 1: Write failing finite-DAG and repair tests**

Create `tests/test_plan_validation.py` with helpers that construct full `PlanDocument` values and these exact cases:

```python
def test_valid_plan_has_stable_topological_order_and_full_coverage(self) -> None:
    plan = self.plan(
        tasks=(
            self.task("TASK-002", depends_on=("TASK-001",), covers=("AC-002",)),
            self.task("TASK-001", covers=("AC-001",)),
        )
    )
    self.scheduler.validate_plan(plan, self.config, ())
    self.assertEqual(
        self.scheduler.topological_order(plan),
        ("TASK-001", "TASK-002"),
    )


def test_cycle_missing_dependency_duplicate_id_and_uncovered_ac_are_rejected(self) -> None:
    invalid = (
        self.plan(tasks=(self.task("TASK-001", depends_on=("TASK-001",)),)),
        self.plan(tasks=(self.task("TASK-001", depends_on=("TASK-999",)),)),
        self.plan(tasks=(self.task("TASK-001"), self.task("TASK-001"))),
        self.plan(tasks=(self.task("TASK-001", covers=("AC-001",)),), ac_ids=("AC-001", "AC-002")),
    )
    for plan in invalid:
        with self.subTest(plan=plan), self.assertRaises(ContractError):
            self.scheduler.validate_plan(plan, self.config, ())


def test_scope_and_worker_type_and_attempt_limits_fail_closed(self) -> None:
    for task in (
        self.task("TASK-001", path_scope=("../escape",)),
        self.task("TASK-001", path_scope=("/absolute",)),
        self.task("TASK-001", path_scope=(".vibe-coding/state.json",)),
        self.task("TASK-001", worker_type="security"),
        self.task("TASK-001", max_attempts=4),
    ):
        with self.subTest(task=task), self.assertRaises(ContractError):
            self.scheduler.validate_plan(self.plan(tasks=(task,)), self.config, ())


def test_repair_plan_increments_version_and_cannot_rewrite_goal_criteria_or_old_tasks(self) -> None:
    original = self.plan(tasks=(self.task("TASK-001"),), plan_version=1)
    valid_repair = self.plan(
        tasks=(self.task("TASK-002", depends_on=("TASK-001",)),),
        plan_version=2,
    )
    self.scheduler.validate_plan(valid_repair, self.config, (original,))

    rewritten = self.plan(
        tasks=(self.task("TASK-001", objective="rewrite history"),),
        plan_version=2,
    )
    with self.assertRaisesRegex(ContractError, "completed or prior task"):
        self.scheduler.validate_plan(rewritten, self.config, (original,))
```

The helper sets two real acceptance criteria, command-ID arrays drawn from the frozen test catalog, and the six registered Worker types. Do not use partial dictionaries.

- [ ] **Step 2: Run plan-validation tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_plan_validation -v
```

Expected: import failure or missing `Scheduler`.

- [ ] **Step 3: Implement path normalization and semantic plan validation**

Implement exact scope forms:

```python
def normalize_scope(value: str) -> str:
    if value == ".":
        return value
    if not value or value.startswith("/") or "\\" in value:
        raise ContractError(f"invalid path scope: {value!r}")
    directory = value.endswith("/")
    raw = value[:-1] if directory else value
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or raw != path.as_posix()
        or path.parts[0] == ".vibe-coding"
        or path.parts[0] == ".git"
    ):
        raise ContractError(f"invalid path scope: {value!r}")
    return path.as_posix() + ("/" if directory else "")


def path_matches_scope(path: str, scope: str) -> bool:
    if scope == ".":
        return True
    if scope.endswith("/"):
        return path.startswith(scope)
    return path == scope
```

`validate_plan()` must:

- require schema version `1`;
- require initial plan version `1` or prior max plus `1`;
- reject when the total globally unique task count across every prior Plan plus the candidate exceeds `config.max_plan_tasks`;
- require unique stable acceptance and task IDs;
- require repair acceptance criteria to exactly equal the original IDs/descriptions;
- require every task ID globally new across plan versions;
- require every dependency in current or prior tasks and reject self-dependency;
- run a deterministic Kahn topological sort and reject cycles;
- require every acceptance criterion covered by at least one task across active plan versions;
- require registered Worker type;
- normalize every scope and reject duplicates;
- validate exclusive-resource names and duplicates;
- require `1 <= max_attempts <= config.task_attempts`;
- require every task/global command ID to be unique within its array and resolvable in `config.command_catalog`;
- merge global verification by stable ID while always retaining `config.required_command_ids`.

Produce:

```python
def effective_global_verification(
    config: FrozenRunConfig,
    plans: Sequence[PlanDocument],
) -> tuple[str, ...]:
    command_ids = config.required_command_ids
    for plan in sorted(plans, key=lambda item: item.plan_version):
        resolve_command_ids(config, plan.global_verification)
        command_ids = tuple(
            dict.fromkeys(command_ids + plan.global_verification)
        )
    resolve_command_ids(config, command_ids)
    return command_ids
```

`validate_plan()` calls `resolve_command_ids()` on each Agent array before returning, so an unknown/duplicate ID cannot reach `VerificationGate`. Add regression cases for 100 prior tasks plus 29 new tasks when the limit is 128, for an initial required/global gate that remains after a repair Plan adds none, and for malicious IDs such as `"python-c-delete"`/`"curl"` that are absent from the catalog.

Update `PlannerRunner.parse_result()` to construct a complete `PlanDocument`, call `Scheduler.validate_plan()`, and only then return it.

- [ ] **Step 4: Prove read-only Planner audit rejects source or ref changes**

Add two Runner tests:

```python
def test_planner_source_change_invalidates_an_otherwise_valid_plan(self) -> None:
    before = self.audit.capture(self.worktree)
    (self.worktree / "README.md").write_text("changed\n", encoding="utf-8")
    after = self.audit.capture(self.worktree)
    with self.assertRaisesRegex(ContractError, "read-only role changed"):
        self.audit.assert_unchanged(before, after)


def test_planner_ref_change_invalidates_an_otherwise_valid_plan(self) -> None:
    before = self.audit.capture(self.worktree)
    self.git("update-ref", "refs/heads/unauthorized", "HEAD")
    after = self.audit.capture(self.worktree)
    with self.assertRaisesRegex(ContractError, "read-only role changed"):
        self.audit.assert_unchanged(before, after)
```

- [ ] **Step 5: Run plan tests and commit Task 8**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_plan_validation tests.test_runners -v
```

Expected: all finite-DAG, append-only repair, and read-only audit tests end in `OK`.

Commit:

```bash
git add src/vibe/scheduler.py src/vibe/runners/planner.py \
  tests/test_plan_validation.py tests/test_runners.py
git diff --cached --check
git commit -m "feat: validate finite planner task graphs"
```

### Task 9: Conservative Parallel Scheduler and Attempt Lifecycle

**Files:**
- Modify: `src/vibe/scheduler.py`
- Create: `tests/test_scheduler.py`

**Interfaces:**
- Produces `scopes_overlap(left, right)`.
- Produces `resources_overlap(left, right)`.
- Produces `Scheduler.promote_ready(state, plan) -> list[str]`.
- Produces `Scheduler.dispatchable(state, plan) -> list[str]`.
- Produces `new_task_state(contract)`.
- Produces `start_attempt(task_state, task_base_sha, task_worktree, attempt_token)` as a pure state allocation that does not require the recorded ref/worktree to exist yet.
- Produces `bind_attempt_preflight(task_state, attempt_token, preflight_ref)`.
- Produces `close_attempt(task_state, status, error, retryable)`.

- [ ] **Step 1: Write failing readiness and concurrency tests**

Create `tests/test_scheduler.py`:

```python
def test_independent_tasks_fill_worker_slots_in_topological_order(self) -> None:
    state, plan = self.state_and_plan(
        self.task("TASK-001", path_scope=("src/a/",)),
        self.task("TASK-002", path_scope=("src/b/",)),
    )
    promoted = self.scheduler.promote_ready(state, plan)
    self.assertEqual(promoted, ["TASK-001", "TASK-002"])
    self.assertEqual(
        self.scheduler.dispatchable(state, plan),
        ["TASK-001", "TASK-002"],
    )


def test_dependency_must_be_integrated_before_ready(self) -> None:
    state, plan = self.state_and_plan(
        self.task("TASK-001", path_scope=("src/a/",)),
        self.task(
            "TASK-002",
            depends_on=("TASK-001",),
            path_scope=("src/b/",),
        ),
    )
    self.scheduler.promote_ready(state, plan)
    self.assertEqual(self.scheduler.dispatchable(state, plan), ["TASK-001"])
    state["tasks"]["TASK-001"]["status"] = "COMPLETED"
    self.scheduler.promote_ready(state, plan)
    self.assertIn("TASK-002", self.scheduler.dispatchable(state, plan))


def test_file_directory_and_whole_repo_scopes_overlap_conservatively(self) -> None:
    cases = (
        (("src/a.py",), ("src/a.py",)),
        (("src/",), ("src/",)),
        (("src",), ("src/",)),
        (("src/",), ("src/a.py",)),
        (("src/",), ("src/api/",)),
        ((".",), ("docs/",)),
    )
    for left, right in cases:
        with self.subTest(left=left, right=right):
            self.assertTrue(scopes_overlap(left, right))
    self.assertFalse(scopes_overlap(("src/",), ("tests/",)))


def test_exclusive_resource_and_active_scope_force_serial_dispatch(self) -> None:
    state, plan = self.state_and_plan(
        self.task(
            "TASK-001",
            path_scope=("src/a/",),
            exclusive_resources=("port:8000",),
        ),
        self.task(
            "TASK-002",
            path_scope=("src/b/",),
            exclusive_resources=("port:8000",),
        ),
    )
    self.scheduler.promote_ready(state, plan)
    state["tasks"]["TASK-001"]["status"] = "RUNNING"
    self.assertEqual(self.scheduler.dispatchable(state, plan), [])


def test_attempt_failure_retries_same_task_without_consuming_repair_round(self) -> None:
    task = new_task_state(self.task("TASK-001"))
    start_attempt(task, "a" * 40, self.task_worktree(), "ATTEMPT-1")
    self.assertEqual(task["active_attempt"]["status"], "STARTING")
    self.assertIsNone(task["active_attempt"]["preflight"])
    bind_attempt_preflight(
        task,
        "ATTEMPT-1",
        self.artifact_ref("tasks/TASK-001/attempts/001/preflight.json"),
    )
    close_attempt(task, "FAILED", {"code": "TEST_FAILED"}, retryable=True)
    self.assertEqual(task["status"], "READY")
    self.assertEqual(task["attempt_no"], 1)
    self.assertIsNone(task["active_attempt"])
```

Helpers construct complete plan/state/task objects and set `max_workers=2`.

- [ ] **Step 2: Run Scheduler tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_scheduler -v
```

Expected: missing scheduling functions.

- [ ] **Step 3: Implement overlap, ready promotion, and slot selection**

Use exact overlap semantics:

```python
def _scope_contains(scope: str, path: str) -> bool:
    if scope == ".":
        return True
    if scope.endswith("/"):
        return path.startswith(scope)
    return scope == path


def scopes_overlap(
    left: tuple[str, ...],
    right: tuple[str, ...],
) -> bool:
    for first in left:
        for second in right:
            if first == "." or second == ".":
                return True
            if first.rstrip("/") == second.rstrip("/"):
                return True
            first_directory = first.endswith("/")
            second_directory = second.endswith("/")
            if not first_directory and not second_directory and first == second:
                return True
            if first_directory and _scope_contains(first, second.rstrip("/")):
                return True
            if second_directory and _scope_contains(second, first.rstrip("/")):
                return True
    return False
```

`dispatchable()` gathers active `RUNNING`, `READY_TO_INTEGRATE`, and `INTEGRATING` tasks, computes available slots, then walks stable topological order. A candidate is admitted only when its scopes and resources do not conflict with active tasks or candidates already selected in this call.

Run states other than `EXECUTING` return an empty list.

- [ ] **Step 4: Implement Task/Attempt state mutations**

Use a fresh token and worktree for every call to `start_attempt()`. Persist:

```json
{
  "attempt_token": "ATTEMPT-7b73a927-2df8-445d-bf37-f539d40296bc",
  "status": "STARTING",
  "created_at": "2026-07-23T10:00:00.123456+00:00",
  "task_base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "branch": "refs/heads/vibe/RUN-20260723-001/TASK-001-a1",
  "worktree": ".vibe-coding/worktrees/RUN-20260723-001/TASK-001-a1",
  "preflight": null,
  "provider_handle": null,
  "result_path": "tasks/TASK-001/attempts/001/result.json"
}
```

`start_attempt()` is the first transaction of a two-phase start protocol. It
allocates semantic `attempt_no`, the first Provider-owner token, `created_at`,
expected base, reserved branch, worktree path, and stable semantic result path,
changes the Task to `RUNNING`, and persists the active `STARTING` allocation
with `preflight=null`. All fields except the current owner token are immutable
for that semantic Attempt; only a transient Provider retry may rotate that
token. It performs no Git or filesystem side effect. The Controller then
idempotently ensures that the exact recorded ref/worktree exists at the
recorded base, captures the protected/read-only snapshot, and uses one
artifact-first transaction to write immutable `preflight.json` and call
`bind_attempt_preflight()`. That function requires the current token/status,
accepts only the canonical path for the recorded identity, and is
byte-idempotent. Only after the preflight ref is attached may DispatchLedger
prepare a Provider intent; handle binding changes the active status to
`RUNNING`.

A crash with `STARTING/preflight=null` is therefore recoverable from state:
recovery repeats only the idempotent ref/worktree ensure and preflight capture.
A ref at any value other than the recorded base, a worktree registered at a
different path, or a snapshot mismatch pauses the run rather than adopting or
overwriting it. A stop before materialization freezes `CANCELLED` with the
roadmap's narrowly allowed null-preflight terminal shape; no other terminal
Attempt may omit preflight.

`accept_worker_handoff()` strictly parses the current raw Provider result,
artifact-first publishes the identical bytes at the stable
`active.result_path`, proves the two digests match, binds that stable ref to
`Task.result`, clears the active Provider handle, and changes the active
Attempt from `RUNNING` to `VERIFYING` in the same transaction. It does not trust
reported commits, close the Attempt, or change `failure_count`. Source-marker
reconciliation later moves the Task to `READY_TO_INTEGRATE` while keeping that
Attempt active. `close_attempt()` is used immediately for failure/cancellation,
or by Integrator after successful candidate verification. It freezes a
manifest containing semantic identity plus every Provider-subattempt,
prompt-version, request, launch, stdout/stderr, exit, stable result,
source-audit, failure, and verification ref; it appends that manifest before
clearing active state. A retryable semantic failure increments `failure_count`
exactly once, leaves `attempt_no` as the identity just closed, and returns Task
to `READY`; the next start increments `attempt_no`. Cancellation freezes/closes
the identity but does not increment `failure_count`. Successful candidate
verification closes as `SUCCEEDED` without incrementing it and atomically
creates `pending_integration`. Reaching `failure_count >= max_attempts` sets
Task `FAILED`. Do not modify run `repair_round`.

- [ ] **Step 5: Run Scheduler tests and commit Task 9**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_scheduler tests.test_plan_validation -v
```

Expected: all dependency, scope, resource, slot, and attempt-counter tests end in `OK`.

Commit:

```bash
git add src/vibe/scheduler.py tests/test_scheduler.py
git diff --cached --check
git commit -m "feat: schedule independent worker attempts"
```

### Task 10: Structured Verification Gate and Commit-Bound Evidence

**Files:**
- Create: `src/vibe/verification.py`
- Create: `tests/test_verification.py`

**Interfaces:**
- Produces `CommandEvidence`.
- Produces `VerificationResult(commit_sha, passed, commands, manifest_ref)`.
- Produces `VerificationEnvironmentError`.
- Produces `VerificationGate(config, store, process_factory)` and `run(commit_sha, worktree, command_ids, artifact_prefix)`.

- [ ] **Step 1: Write failing command-policy and evidence tests**

Create `tests/test_verification.py`:

```python
def test_verification_runs_argv_without_shell_and_binds_exact_commit(self) -> None:
    self.authorize(CommandSpec(
        id="unit",
        purpose="Run the verification fixture",
        argv=(sys.executable, "-c", "print('verified')"),
        cwd=".",
        timeout_seconds=30,
        env_allowlist=(),
    ))
    result = self.run_gate(
        self.head,
        self.target,
        ("unit",),
        "verification/tasks/TASK-001-a1/VERIFY-00000000-0000-4000-8000-000000000001",
    )
    self.assertTrue(result.passed)
    manifest = self.read_artifact(result.manifest_ref)
    self.assertEqual(manifest["commit_sha"], self.head)
    self.assertEqual(manifest["commands"][0]["exit_code"], 0)
    self.assertEqual(manifest["commands"][0]["env_names"], [])
    self.assertIn("verified", self.read_artifact_bytes(manifest["commands"][0]["stdout"]))


def test_configured_missing_command_is_an_environment_error(self) -> None:
    command = CommandSpec(
        "missing",
        "Exercise configured-missing executable handling",
        ("definitely-missing-vibe-command",),
        ".",
        30,
        (),
    )
    self.authorize(command)
    with self.assertRaises(VerificationEnvironmentError):
        self.run_gate(
            self.head,
            self.target,
            (command.id,),
            f"verification/global/VERIFY-{uuid.uuid4()}",
        )


def test_invalid_configured_cwd_is_rejected_before_gate_start(self) -> None:
    with self.assertRaises(ContractError):
        self.authorize_from_raw(
            {
                "id": "escape",
                "purpose": "Invalid escape fixture",
                "argv": [sys.executable, "-c", "print(1)"],
                "cwd": "../",
            }
        )
    self.assertEqual(self.process_factory.calls, [])


def test_timeout_is_a_failed_executed_gate_with_evidence(self) -> None:
    self.authorize(CommandSpec(
        id="timeout",
        purpose="Exercise gate timeout handling",
        argv=(sys.executable, "-c", "import time; time.sleep(5)"),
        cwd=".",
        timeout_seconds=1,
        env_allowlist=(),
    ))
    result = self.run_gate(
        self.head,
        self.target,
        ("timeout",),
        f"verification/global/VERIFY-{uuid.uuid4()}",
    )
    self.assertFalse(result.passed)
    self.assertTrue(result.commands[0].timed_out)
    self.assertIsNone(result.commands[0].exit_code)


def test_timeout_terminates_the_complete_descendant_process_group(self) -> None:
    pid_file = self.store.root / "diagnostics" / "verification-child.pid"
    pid_file.parent.mkdir(parents=True)
    script = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='utf-8');"
        "time.sleep(60)"
    )
    self.authorize(CommandSpec(
        id="descendant",
        purpose="Exercise descendant cleanup",
        argv=(sys.executable, "-c", script, str(pid_file)),
        cwd=".",
        timeout_seconds=1,
        env_allowlist=(),
    ))
    result = self.run_gate(
        self.head,
        self.target,
        ("descendant",),
        f"verification/global/VERIFY-{uuid.uuid4()}",
    )
    self.assertTrue(result.commands[0].timed_out)
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    self.assert_process_absent(child_pid, timeout=2)


def test_head_or_product_change_during_command_invalidates_evidence(self) -> None:
    self.authorize(CommandSpec(
        id="mutating",
        purpose="Exercise product-mutation detection",
        argv=(sys.executable, "-c", "open('changed.txt','w').write('x')"),
        cwd=".",
        timeout_seconds=30,
        env_allowlist=(),
    ))
    with self.assertRaisesRegex(ContractError, "verification changed"):
        self.run_gate(
            self.head,
            self.target,
            ("mutating",),
            f"verification/global/VERIFY-{uuid.uuid4()}",
        )


def test_unknown_or_duplicate_agent_ids_spawn_nothing(self) -> None:
    for command_ids in (("python-c-delete",), ("unit", "unit")):
        with self.subTest(command_ids=command_ids), self.assertRaises(ContractError):
            self.run_gate(
                self.head,
                self.target,
                command_ids,
                f"verification/global/VERIFY-{uuid.uuid4()}",
            )
    self.assertEqual(self.process_factory.calls, [])
```

The test fixture builds a frozen `command_catalog`, constructs `VerificationGate` from it, and uses a recording `process_factory`. Its `run_gate()` helper wraps exactly one `with self.store.lock(): return self.gate.run(...)`. Verification outputs are written below the run root, never into the product worktree. Every prefix contains a fresh `VERIFY-<uuid>` operation ID.

- [ ] **Step 2: Run Verification tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_verification -v
```

Expected: import failure because `vibe.verification` does not exist.

- [ ] **Step 3: Implement no-shell execution and immutable evidence**

Before resolving or starting any executable, call `resolve_command_ids(config, command_ids)` once for the complete array. Unknown/duplicate IDs fail with zero `process_factory` calls. Resolve each authorized executable once:

```python
executable = shutil.which(command.argv[0])
if executable is None:
    raise VerificationEnvironmentError(
        f"required command not found: {command.argv[0]}"
    )
argv = (executable, *command.argv[1:])
```

Resolve `cwd` and require `resolved == worktree` or `worktree in resolved.parents`; reject symlinks that resolve outside.

Build environment only from allowed names:

```python
environment = {
    name: os.environ[name]
    for name in command.env_allowlist
    if name in os.environ
}
```

Run in a fresh process group and convert an executed timeout into ordinary failed-gate evidence:

```python
process = subprocess.Popen(
    argv,
    cwd=resolved_cwd,
    env=environment,
    shell=False,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    start_new_session=True,
)
try:
    stdout, stderr = process.communicate(
        timeout=command.timeout_seconds,
    )
    timed_out = False
    exit_code = process.returncode
except subprocess.TimeoutExpired:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        stdout, stderr = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stdout, stderr = process.communicate()
    timed_out = True
    exit_code = None
```

Only executable lookup failure, an invalid/escaping `cwd`, or an inability to start the process raises `VerificationEnvironmentError` and pauses the Controller. A started command that times out has `timed_out=true`, `exit_code=null`, `passed=false`, and follows the same repair route as any other executed failing gate. The real descendant test reads the spawned PID from Controller-owned diagnostics and polls `os.kill(pid, 0)` until `ProcessLookupError`; it is not satisfied by killing only the direct command process.

For each command, persist stdout and stderr first. The manifest records:

```json
{
  "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "command_id": "unit",
  "argv": ["python3", "-m", "unittest", "tests.test_models"],
  "resolved_executable": "/usr/bin/python3",
  "cwd": ".",
  "env_names": [],
  "started_at": "RFC3339",
  "finished_at": "RFC3339",
  "timeout_seconds": 30,
  "timed_out": false,
  "exit_code": 0,
  "stdout": {
    "path": "verification/task-001/command-001.stdout",
    "sha256": "sha256:1111111111111111111111111111111111111111111111111111111111111111"
  },
  "stderr": {
    "path": "verification/task-001/command-001.stderr",
    "sha256": "sha256:2222222222222222222222222222222222222222222222222222222222222222"
  }
}
```

Capture `WorktreeManager.capture_read_only_audit()` before the first command and after the last. Require the expected HEAD both times and unchanged product/protected Git fingerprint. A command may create ignored test caches only when Git still reports them ignored; non-ignored output invalidates verification.

`artifact_prefix` must end in a canonical fresh `VERIFY-<uuid>` component. Task, global, and supplemental executions use respectively `verification/tasks/<task>-aN/VERIFY-<uuid>`, `verification/global/VERIFY-<uuid>`, and `verification/supplemental/VERIFY-<uuid>`. A crash before the manifest is state-bound leaves only orphan artifacts; retry allocates a new operation ID and never rewrites timestamped bytes. Once a verification ref appears in `pending_integration` or state history, recovery adopts that exact ref and never reruns it. Add a non-fixed-clock fault test proving an orphaned run and retry do not conflict.

- [ ] **Step 4: Prove project gates are retained on task candidates**

Add:

```python
def test_task_command_ids_append_to_required_ids(self) -> None:
    self.configure_catalog(
        required=(self.command("required", "print('required')"),),
        optional=(self.command("task", "print('task')"),),
    )
    command_ids = effective_command_ids(self.config, ("task",))
    result = self.run_gate(
        self.head,
        self.target,
        command_ids,
        f"verification/tasks/TASK-001-a1/VERIFY-{uuid.uuid4()}",
    )
    self.assertEqual(len(result.commands), 2)
    self.assertTrue(result.passed)
```

The Integrator must call this resolved ID sequence, not task IDs alone. It resolves the complete list before spawning the first command.

- [ ] **Step 5: Run Verification tests and commit Task 10**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_verification -v
```

Expected: all command, timeout, mutation, evidence-hash, and required-gate tests end in `OK`.

Commit:

```bash
git add src/vibe/verification.py tests/test_verification.py
git diff --cached --check
git commit -m "feat: add commit-bound verification gate"
```

### Task 11: Candidate Integration and Recoverable Git CAS

**Files:**
- Create: `src/vibe/integrator.py`
- Create: `tests/support/fault_injector.py`
- Create: `tests/test_integrator.py`
- Modify: `src/vibe/models.py`

**Interfaces:**
- Produces `PendingIntegration`.
- Produces `CandidateIntegration`.
- Produces `IntegrationRecovery(RETRY_CAS | COMPLETE_STATE | PAUSE)`.
- Produces `Integrator.prepare()`, `integrate()`, `apply_cas()`, `classify_recovery()`, and `recover()`; constructor requires the canonical `fault_hook`.

- [ ] **Step 1: Add a named fault injector**

Create `tests/support/fault_injector.py`:

```python
from __future__ import annotations


class InjectedCrash(RuntimeError):
    pass


class FaultInjector:
    def __init__(self, crash_at: str | None = None) -> None:
        self.crash_at = crash_at
        self.seen: list[str] = []

    def __call__(self, point: str) -> None:
        self.seen.append(point)
        if point == self.crash_at:
            raise InjectedCrash(point)
```

- [ ] **Step 2: Write failing candidate, CAS, and recovery tests**

Create `tests/test_integrator.py` with real temporary Git repositories:

```python
def test_successful_candidate_verification_advances_ref_and_completes_task(self) -> None:
    result = self.integrate_valid_worker()
    state = self.store.load()
    self.assertEqual(
        self.git("rev-parse", state["repository"]["integration_ref"]),
        result.candidate_head,
    )
    self.assertEqual(state["tasks"]["TASK-001"]["status"], "COMPLETED")
    self.assertIsNone(state["pending_integration"])


def test_conflict_scope_escape_and_failed_gate_leave_run_ref_unchanged(self) -> None:
    for scenario in ("conflict", "scope_escape", "verification_failure"):
        with self.subTest(scenario=scenario):
            fixture = self.new_scenario(scenario)
            before = fixture.integration_head()
            with fixture.store.lock(), self.assertRaises(IntegrationRejected):
                fixture.integrator.integrate(
                    fixture.contract,
                    fixture.source_audit,
                )
            self.assertEqual(fixture.integration_head(), before)


def test_crash_after_pending_before_cas_retries_exact_candidate(self) -> None:
    fixture = self.new_scenario(
        "valid",
        crash_at="after_pending_integration_before_update_ref",
    )
    with fixture.store.lock(), self.assertRaises(InjectedCrash):
        fixture.integrator.integrate(fixture.contract, fixture.source_audit)
    pending = fixture.store.load()["pending_integration"]
    self.assertEqual(fixture.integration_head(), pending["expected_head"])

    with fixture.store.lock():
        fixture.integrator.recover()
    self.assertEqual(fixture.integration_head(), pending["candidate_head"])
    self.assertIsNone(fixture.store.load()["pending_integration"])


def test_crash_after_cas_before_state_completes_state_without_reapplying(self) -> None:
    fixture = self.new_scenario(
        "valid",
        crash_at="after_update_ref_before_state_completion",
    )
    with fixture.store.lock(), self.assertRaises(InjectedCrash):
        fixture.integrator.integrate(fixture.contract, fixture.source_audit)
    pending = fixture.store.load()["pending_integration"]
    self.assertEqual(fixture.integration_head(), pending["candidate_head"])

    with fixture.store.lock():
        fixture.integrator.recover()
    self.assertEqual(
        fixture.store.load()["tasks"]["TASK-001"]["status"],
        "COMPLETED",
    )


def test_external_ref_move_pauses_without_force_update(self) -> None:
    fixture = self.new_scenario(
        "valid",
        crash_at="after_pending_integration_before_update_ref",
    )
    with fixture.store.lock(), self.assertRaises(InjectedCrash):
        fixture.integrator.integrate(fixture.contract, fixture.source_audit)
    external = fixture.commit_external_change()
    fixture.git(
        "update-ref",
        fixture.integration_ref,
        external,
        fixture.integration_head(),
    )

    with fixture.store.lock():
        fixture.integrator.recover()
    state = fixture.store.load()
    self.assertEqual(state["status"], "PAUSED")
    self.assertEqual(fixture.integration_head(), external)
```

Add explicit tests for merge commit rejection, rename both ends, gitlink, `.gitmodules`, full multi-commit order, and original user branch/index/worktree unchanged.

- [ ] **Step 3: Run Integrator tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_integrator -v
```

Expected: import failure because `vibe.integrator` does not exist.

- [ ] **Step 4: Implement candidate preparation**

Add `PendingIntegration` to `models.py` with the exact approved fields:

```python
@dataclass(frozen=True)
class PendingIntegration:
    operation_id: str
    task_id: str
    attempt_no: int
    expected_head: str
    candidate_head: str
    source_base: str
    source_head: str
    verification: ArtifactRef
```

`integrate_valid_worker()` first completes the prepared source-commit CAS, then acquires the fixture store lock. `Integrator.integrate(contract, source_audit)` requires the caller-held lock, loads current state, calls `prepare(state, contract, source_audit)`, persists the prepared marker, calls `apply_cas()`, and completes state. `Integrator.prepare()`:

1. Loads state and requires no existing pending integration.
2. Requires task `READY_TO_INTEGRATE`.
3. Loads the state-bound immutable source-audit artifact, requires equality to the supplied `SourceAudit`, verifies the Controller-created source commit has the exact base parent/tree/metadata, and requires the reserved task ref at `source_head`.
4. Creates a disposable candidate at current `repository.integration_head`.
5. Cherry-picks the single audited Controller-created source commit with the hardened Git runner.
6. On conflict, records stderr evidence, leaves the failed candidate worktree, and raises `IntegrationRejected`.
7. Re-audits candidate changed paths against the task contract.
8. Computes required IDs plus task check IDs, resolves the complete sequence from frozen config before spawning, and runs them under a fresh task `VERIFY-<uuid>` prefix.
9. Requires a passed verification bound to candidate HEAD.
10. Returns a `CandidateIntegration` containing the candidate path/head, source audit, and verification ref.

Do not update the run ref in `prepare()`.

- [ ] **Step 5: Implement prepared marker, CAS, and exact three-way recovery**

The private prepared-marker step inside `integrate()` first commits this state:

```python
pending = PendingIntegration(
    operation_id=f"INT-{uuid.uuid4()}",
    task_id=contract.id,
    attempt_no=task_state["attempt_no"],
    expected_head=state["repository"]["integration_head"],
    candidate_head=candidate.head,
    source_base=audit.task_base_sha,
    source_head=audit.source_head,
    verification=candidate.verification.manifest_ref,
)
```

Call `after_candidate_verification_before_pending_integration` after the passed manifest exists but before state binds it. In one transaction require the matching active Worker Attempt to be `VERIFYING`, freeze its terminal `SUCCEEDED` manifest with Provider/source/verification refs, append it to `task.attempts`, clear active state, persist `pending_integration`, append the candidate manifest to `verifications`, and set Task `INTEGRATING`. Then call `after_pending_integration_before_update_ref`, followed by:

```python
self.worktrees.update_ref(
    state["repository"]["integration_ref"],
    pending.candidate_head,
    pending.expected_head,
)
```

After CAS call `after_update_ref_before_state_completion`, then commit state with:

- `repository.integration_head = candidate_head`;
- Task `COMPLETED`;
- set Task `verification` to the candidate verification ArtifactRef;
- append exact integrated commit identities;
- clear `pending_integration`.

Implement recovery:

```python
if actual_head == pending.expected_head:
    verify_candidate_object_and_manifest(pending)
    apply_cas(pending)
    complete_state(pending)
elif actual_head == pending.candidate_head:
    verify_candidate_object_and_manifest(pending)
    complete_state(pending)
else:
    evidence = persist_ref_movement_evidence(
        pending=pending,
        actual_head=actual_head,
    )
    pause_with_error(code="INTEGRATION_REF_MOVED", evidence=evidence)
```

The immutable evidence contains expected/candidate/actual OIDs; strict `last_error` contains only code/message/retryable/evidence. Never call `update-ref` without the expected old OID and never force an externally moved ref.

`Integrator` calls exactly the three canonical hook names in this task; there are no aliases such as `after_pending_integration` or `after_update_ref`. Fault tests assert the hook order and that retry never reruns a state-bound verification manifest.

- [ ] **Step 6: Run all Git/integration tests and commit Task 11**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_git_runner \
  tests.test_repository_snapshot \
  tests.test_worktrees \
  tests.test_plan_validation \
  tests.test_scheduler \
  tests.test_verification \
  tests.test_integrator -v
```

Expected: all tests end in `OK`; both integration crash windows recover uniquely and no failure scenario changes the run ref.

Commit:

```bash
git add src/vibe/integrator.py src/vibe/models.py \
  tests/support/fault_injector.py tests/test_integrator.py
git diff --cached --check
git commit -m "feat: integrate verified worker commits with git cas"
```

## Phase 3 Completion Gate

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_repository_snapshot \
  tests.test_worktrees \
  tests.test_plan_validation \
  tests.test_scheduler \
  tests.test_verification \
  tests.test_integrator \
  tests.test_runners -v
```

Expected:

- Clean baselines, exact fingerprints, refs, branches, and worktrees use real Git.
- The user's current branch, index, worktree, and remotes are unchanged.
- Planner DAG and repair-plan history are finite, acyclic, covered, and append-only.
- Independent scopes/resources can be selected together; overlapping work is conservatively serialized.
- Verification evidence is no-shell, immutable, and bound to a commit.
- Candidate conflict, scope escape, merge commit, gitlink violation, and failed checks do not move the integration ref.
- Prepared integration recovers both sides of the `update-ref` crash window and pauses on external ref movement.
