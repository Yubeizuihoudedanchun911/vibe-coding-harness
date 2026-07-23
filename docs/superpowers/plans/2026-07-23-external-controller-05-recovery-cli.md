# External Controller Phase 5: Stop, Recovery, and CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make foreground runs safely stoppable and recoverable across Controller crashes, then expose the complete `run/resume/status/stop/logs/migrate` command surface with stable output and exit codes.

**Architecture:** The active Controller keeps the run lock; a second `stop` process writes only an atomic nonce request and signals a verified Controller identity. Resume reconciles prepared dispatch, Provider receipts, worktrees, Git CAS, and evaluation bindings from durable truth before returning to `resume_status`. CLI is a thin adapter and never edits lifecycle state directly.

**Tech Stack:** Python 3.10+ standard library, POSIX locks/signals/process groups, argparse, JSON Lines, real Git fault injection, `unittest`.

## Global Constraints

- The stop CLI never writes `state.json` while an active Controller holds the run lock.
- A stop request contains run ID, observed revision, the observed Controller token, requested time, and a unique nonce.
- Signal only when PID, process-start identity, and process group all match.
- Stop every active Provider through its adapter, wait for exit, and force the process group only after the grace period.
- Mark `STOPPED` only after every relevant Provider is confirmed terminated or proven unrelated to the current attempt.
- Archive a consumed stop request as an immutable receipt and bind its ArtifactRef in state.
- Duplicate nonce consumption is idempotent; stale requests cannot stop a later resumed Controller.
- `PAUSED` and `STOPPED` preserve the previous active state in `resume_status`.
- Resume uses only state, immutable artifacts, Provider receipts, worktree/Git truth, and prepared markers; never logs or chat memory.
- PID reuse must never be signaled, polled as current, or adopted.
- A stale-token result is archived but cannot complete the current attempt.
- Pending source-commit and integration recovery each use their exact three-way CAS classification.
- Missing required tools/auth/config pause; semantic attempt or repair exhaustion fails.
- Human CLI errors contain no traceback and JSON mode emits machine-stable envelopes.
- `run`/`resume` remain foreground commands.

---

## File Map

- Modify `src/vibe/controller.py`: stop request consumption, dispatch/worktree/integration reconciliation, resume.
- Modify `src/vibe/providers/base.py`: persistent-handle reconstruction API.
- Modify `src/vibe/providers/codex_cli.py`: identity-safe reattach/poll/stop.
- Modify `src/vibe/models.py`: `StopRequest`, `StopReceipt`, and recovery action types.
- Replace the Phase 1 shell in `src/vibe/cli.py`: full public parser, output, exit mapping, log follow.
- Create `tests/test_controller_recovery.py`: six crash windows, PID reuse, stale token, stop nonce.
- Create `tests/test_cli.py`: all command contracts, outputs, errors, and exits.
- Modify `tests/support/controller_scenario.py`: restartable Controller and crash injection.

### Task 14: Stop Protocol and Deterministic Resume

**Files:**
- Modify: `src/vibe/controller.py`
- Modify: `src/vibe/providers/base.py`
- Modify: `src/vibe/providers/codex_cli.py`
- Modify: `src/vibe/models.py`
- Modify: `tests/support/controller_scenario.py`
- Create: `tests/test_controller_recovery.py`

**Interfaces:**
- Produces `StopRequest(run_id, observed_revision, controller_token, requested_at, nonce)`.
- Produces `StopReceipt(request, outcome, stopped_tokens, forced_tokens, completed_at)`.
- Produces `Controller.request_stop(run_id) -> StopRequest`.
- Produces `Controller.recover(run_id, replan=False) -> dict[str, object]` as a lock-owned reconciliation-only API for focused tests/diagnostics.
- Produces `Controller.resume(run_id, replan=False) -> dict[str, object]` as the only foreground recovery-and-execution API.
- Produces `Controller.recover_and_stop(run_id, request)`.
- Produces Provider `handle_from_pending(pending, trusted_run_root)` and `identity_matches()`.

- [ ] **Step 1: Write the six mandatory crash-window tests**

Create `tests/test_controller_recovery.py`. Each case runs a real scenario until a named `FaultInjector` point raises, constructs a new Controller object with no in-memory process handles, calls `recover()`, and asserts no duplicate start/integration:

```python
DISPATCH_CRASH_POINTS = (
    "after_dispatch_intent_before_provider_start",
    "after_provider_start_before_handle_binding",
    "after_worker_edit_before_controller_commit",
    "after_controller_commit_before_verification_binding",
    "after_candidate_verification_before_pending_integration",
    "after_pending_integration_before_update_ref",
    "after_update_ref_before_state_completion",
)
STOP_CRASH_POINT = "after_stop_request_before_worker_exit"


def test_all_dispatch_crash_windows_resume_to_success_without_duplicate_work(self) -> None:
    for point in DISPATCH_CRASH_POINTS:
        with self.subTest(point=point):
            scenario = self.restartable_scenario(crash_at=point)
            with self.assertRaisesRegex(InjectedCrash, point):
                scenario.controller.execute(scenario.run_id)
            starts_before = scenario.provider.start_count
            integrations_before = scenario.integration_attempts

            controller = scenario.new_controller()
            terminal = controller.resume(scenario.run_id)

            self.assertEqual(terminal["status"], "SUCCEEDED")
            self.assertEqual(
                scenario.provider.start_count - starts_before,
                scenario.expected_restarts(point),
            )
            self.assertEqual(
                scenario.integration_attempts - integrations_before,
                scenario.expected_integration_replays(point),
            )
            self.assertEqual(
                scenario.git_duplicate_patch_count(),
                0,
            )


def test_crash_during_stop_finishes_stop_without_resurrecting_worker(self) -> None:
    scenario = self.restartable_stop_scenario(
        crash_at=STOP_CRASH_POINT,
    )
    thread = scenario.start_controller()
    scenario.wait_for_worker_start()
    request = scenario.controller.request_stop(scenario.run_id)
    thread.join(timeout=20)
    scenario.assert_thread_error(InjectedCrash, STOP_CRASH_POINT)

    starts_before = scenario.provider.start_count
    state = scenario.new_controller().recover_and_stop(
        scenario.run_id,
        request,
    )
    self.assertEqual(state["status"], "STOPPED")
    self.assertEqual(scenario.provider.start_count, starts_before)
    self.assertFalse(scenario.provider_process_is_alive())
    self.assertEqual(
        scenario.stop_receipt(request.nonce)["outcome"],
        "STOPPED",
    )


def test_starting_worker_allocation_is_materialized_once_after_restart(self) -> None:
    for already_materialized in (False, True):
        with self.subTest(already_materialized=already_materialized):
            scenario = self.starting_worker_allocation_scenario(
                already_materialized=already_materialized,
            )
            before = scenario.store.load()
            active = before["tasks"]["TASK-001"]["active_attempt"]
            self.assertEqual(active["status"], "STARTING")
            self.assertIsNone(active["preflight"])
            self.assertNotIn(active["attempt_token"], before["pending_dispatches"])

            terminal = scenario.new_controller().resume(scenario.run_id)

            self.assertEqual(terminal["status"], "SUCCEEDED")
            scenario.assert_attempt_identity_unchanged(
                token=active["attempt_token"],
                created_at=active["created_at"],
                branch=active["branch"],
                worktree=active["worktree"],
            )
            self.assertEqual(scenario.worktree_create_count, 1)
            self.assertEqual(scenario.provider.start_count, 1)
            scenario.assert_one_preflight_for(active["attempt_token"])
```

Use stronger point-specific assertions:

- after dispatch intent/no launch: one start of the prepared token;
- after launch/no state handle: reconstruct from matching `launch.json`, no second start;
- after Worker edit/source marker/no task-ref CAS: verify immutable source audit/object and replay the exact task-ref CAS;
- after Controller commit/no verification binding: require task ref/candidate and exact commit metadata, then bind the source audit without recommitting;
- after candidate verification/no pending marker: candidate is orphan and must be rebuilt, never adopted from logs;
- after pending/no CAS: retry the exact candidate CAS;
- after CAS/no state: complete state without another cherry-pick or ref update.

- [ ] **Step 2: Write stop, PID reuse, late-token, and resume-status tests**

Add:

```python
def test_live_controller_consumes_stop_request_as_the_only_state_writer(self) -> None:
    scenario = self.running_worker_scenario()
    thread = scenario.start_controller()
    scenario.wait_for_worker_start()
    request = scenario.controller.request_stop(scenario.run_id)
    thread.join(timeout=20)

    self.assertFalse(thread.is_alive())
    state = scenario.store.load()
    self.assertEqual(state["status"], "STOPPED")
    self.assertEqual(state["resume_status"], "EXECUTING")
    self.assertEqual(
        state["stop_receipts"][-1]["nonce"],
        request.nonce,
    )
    self.assertEqual(
        state["stop_receipts"][-1]["receipt"]["path"],
        f"control/receipts/{request.nonce}.json",
    )
    self.assertFalse(scenario.stop_request_path.exists())


def test_resume_after_stop_uses_a_new_attempt_identity_without_failure_budget(self) -> None:
    scenario = self.running_worker_scenario()
    first = scenario.current_task_attempt()
    stopped = scenario.stop_and_join()
    self.assertEqual(stopped["tasks"]["TASK-001"]["failure_count"], 0)

    terminal = scenario.new_controller().resume(scenario.run_id)
    second = scenario.task_attempts("TASK-001")[-1]
    self.assertEqual(terminal["status"], "SUCCEEDED")
    self.assertEqual(second["attempt_no"], first["attempt_no"] + 1)
    self.assertNotEqual(second["branch"], first["branch"])
    self.assertNotEqual(second["worktree"], first["worktree"])
    self.assertEqual(terminal["tasks"]["TASK-001"]["failure_count"], 0)


def test_stop_before_worker_materialization_freezes_narrow_cancel_shape(self) -> None:
    scenario = self.starting_worker_allocation_scenario(
        already_materialized=False,
    )
    state = scenario.new_controller().recover_and_stop(
        scenario.run_id,
        scenario.stop_request(),
    )
    manifest = scenario.last_worker_attempt(state)
    self.assertEqual(manifest["status"], "CANCELLED")
    self.assertIsNone(manifest["preflight"])
    self.assertEqual(
        manifest["last_error"]["code"],
        "CANCELLED_BEFORE_MATERIALIZATION",
    )
    for field in (
        "request",
        "launch",
        "stdout",
        "stderr",
        "exit",
        "result",
        "source_audit",
        "verification",
    ):
        self.assertIsNone(manifest[field])
    self.assertEqual(scenario.worktree_create_count, 0)
    self.assertEqual(scenario.provider.start_count, 0)


def test_dead_controller_stop_acquires_lock_and_finishes_recovery_stop(self) -> None:
    scenario = self.crashed_controller_with_live_wrapper()
    request = scenario.controller.request_stop(scenario.run_id)
    state = scenario.new_controller().recover_and_stop(
        scenario.run_id,
        request,
    )
    self.assertEqual(state["status"], "STOPPED")
    self.assertFalse(scenario.provider_process_is_alive())


def test_pid_reuse_is_never_signalled_or_adopted(self) -> None:
    scenario = self.pending_dispatch_scenario()
    scenario.replace_process_identity("linux:different-start")
    with mock.patch("os.killpg") as killpg:
        state = scenario.new_controller().recover(scenario.run_id)
    killpg.assert_not_called()
    self.assertEqual(
        scenario.closed_attempt_status(state),
        "ABANDONED",
    )


def test_late_stale_token_result_is_archived_without_completing_current_attempt(self) -> None:
    scenario = self.retried_worker_with_late_first_result()
    state = scenario.new_controller().recover(scenario.run_id)
    self.assertEqual(
        state["tasks"]["TASK-001"]["active_attempt"]["attempt_token"],
        "ATTEMPT-SECOND",
    )
    self.assertTrue(scenario.stale_result_archive_exists("ATTEMPT-FIRST"))


def test_resume_returns_to_recorded_status_not_file_count_guess(self) -> None:
    scenario = self.paused_scenario(resume_status="GLOBAL_VERIFYING")
    reconciled = scenario.new_controller().recover(scenario.run_id)
    self.assertEqual(reconciled["status"], "PAUSED")
    self.assertEqual(reconciled["resume_status"], "GLOBAL_VERIFYING")

    terminal = scenario.new_controller().resume(scenario.run_id)
    self.assertEqual(terminal["status"], "SUCCEEDED")
    scenario.assert_first_resumed_tick_started_from("GLOBAL_VERIFYING")


def test_stale_controller_stop_request_is_receipted_but_does_not_stop_resume(self) -> None:
    scenario = self.stopped_then_resumed_scenario()
    stale = scenario.stop_request_for_controller_token("CONTROLLER-OLD")
    state = scenario.new_controller().recover(scenario.run_id)
    self.assertNotEqual(state["status"], "STOPPED")
    self.assertEqual(
        scenario.stop_receipt(stale.nonce)["outcome"],
        "IGNORED_STALE_CONTROLLER",
    )


def test_consumed_nonce_replay_verifies_receipt_and_unlinks_request(self) -> None:
    scenario = self.scenario_with_consumed_stop_nonce()
    before = scenario.store.load()
    state = scenario.new_controller().recover(scenario.run_id)
    self.assertEqual(state["revision"], before["revision"])
    self.assertFalse(scenario.stop_request_path.exists())
    self.assertTrue(scenario.stop_receipt_digest_is_valid())


def test_future_revision_stop_request_is_rejected_without_signalling(self) -> None:
    scenario = self.pending_dispatch_scenario()
    scenario.write_stop_request(observed_revision=10_000)
    with mock.patch("os.killpg") as killpg:
        with self.assertRaisesRegex(ContractError, "future revision"):
            scenario.new_controller().recover(scenario.run_id)
    killpg.assert_not_called()


def test_untrusted_state_corruption_is_read_only(self) -> None:
    scenario = self.corrupt_state_json_scenario()
    before = scenario.state_path.read_bytes()
    with self.assertRaisesRegex(RecoveryReadOnlyError, "UNTRUSTED_STATE"):
        scenario.new_controller().recover(scenario.run_id)
    self.assertEqual(scenario.state_path.read_bytes(), before)
    self.assertEqual(scenario.run_tree_snapshot(), scenario.before_snapshot)


def test_valid_state_with_proven_external_invariant_failure_can_fail_safely(self) -> None:
    scenario = self.valid_state_missing_bound_git_object_scenario()
    state = scenario.new_controller().recover(scenario.run_id)
    self.assertEqual(state["status"], "FAILED")
    self.assertEqual(state["last_error"]["code"], "MISSING_GIT_OBJECT")
    self.assertTrue(scenario.bound_recovery_diagnostic_exists())
```

- [ ] **Step 3: Run recovery tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_controller_recovery -v
```

Expected: missing stop/recovery APIs and failing crash-window cases.

- [ ] **Step 4: Add exact stop request and receipt contracts**

Add to `src/vibe/models.py`:

```python
@dataclass(frozen=True)
class StopRequest:
    run_id: str
    observed_revision: int
    controller_token: str
    requested_at: str
    nonce: str

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "observed_revision": self.observed_revision,
            "controller_token": self.controller_token,
            "requested_at": self.requested_at,
            "nonce": self.nonce,
        }


@dataclass(frozen=True)
class StopReceipt:
    request: StopRequest
    outcome: str
    stopped_tokens: tuple[str, ...]
    forced_tokens: tuple[str, ...]
    completed_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "request": self.request.as_dict(),
            "outcome": self.outcome,
            "stopped_tokens": list(self.stopped_tokens),
            "forced_tokens": list(self.forced_tokens),
            "completed_at": self.completed_at,
        }
```

`outcome` is exactly `STOPPED` or `IGNORED_STALE_CONTROLLER`. `request_stop()` reads one atomic state snapshot without acquiring the long-running lock, validates the run ID and exact observed Controller identity, copies `state.controller.controller_token`, then atomically creates `control/stop.request`. If the file already contains a valid unconsumed request, return that request rather than overwrite it with a new nonce.

Request bytes:

```json
{
  "run_id": "RUN-20260723-001",
  "observed_revision": 31,
  "controller_token": "CONTROLLER-6f67c4b1-b436-4c0e-aa6d-1644e3554398",
  "requested_at": "2026-07-23T18:00:00+08:00",
  "nonce": "STOP-uuid"
}
```

After writing and directory fsync, call injected `send_wake_signal(controller_identity, SIGUSR1)`. The production implementation sends only when start identity and `getpgid(pid)` both equal state. The signal handler only sets an in-memory wake event; it does not write state. `ControllerScenario` injects an Event-based sender and never sends `SIGUSR1` to the unit-test process; a separate CLI subprocess test proves the real signal path.

- [ ] **Step 5: Implement Controller-side stop consumption**

At the start of every tick:

1. Read `control/stop.request` if it is a regular non-symlink file.
2. Reject a different run ID. Reject `observed_revision > current revision` as an invalid future request; normal revision advancement after the request is allowed.
3. If the nonce is already in `stop_receipts`, validate the bound receipt bytes/digest, unlink the replayed request, fsync `control/`, and return without a state revision.
4. Compare `controller_token` with the persisted Controller token before registering any new Controller identity. A mismatch writes an `IGNORED_STALE_CONTROLLER` receipt, appends its ref, unlinks the request, and continues recovery without stopping any process.
5. For a matching token, stop dispatching new work.
6. Call `self.dependencies.fault_hook("after_stop_request_before_worker_exit")`.
7. Call `ProviderAdapter.stop()` for every current pending handle reconstructed from trusted relative state paths.
8. Verify the durable exit receipt (including `stop_requested`/`stop_forced`) or identity-safe process absence.
9. Freeze each active role Attempt as `CANCELLED`; append the Attempt ref, clear its active token, and return incomplete Worker tasks to `READY`. The closed identity remains the current `attempt_no`; cancellation does not increment `failure_count`. The next scheduler start always allocates `attempt_no + 1`.
10. Persist `control/receipts/<nonce>.json` with outcome `STOPPED`.
11. Transition to `STOPPED`, preserving the prior active state in `resume_status`.
12. Bind receipt path/SHA/nonce in `stop_receipts`.
13. Unlink `stop.request` and fsync `control/` only after state commits the receipt.

Each state entry has exactly this shape; the full `StopReceipt.as_dict()` is the immutable artifact body:

```json
{
  "nonce": "STOP-4ff3c355-8e13-4cad-a42c-e29db374ec7d",
  "receipt": {
    "path": "control/receipts/STOP-4ff3c355-8e13-4cad-a42c-e29db374ec7d.json",
    "sha256": "sha256:3333333333333333333333333333333333333333333333333333333333333333"
  }
}
```

If the Controller is already dead, `recover_and_stop()` obtains the run lock and performs the same sequence. It must not mark `STOPPED` while a matching Provider process group remains alive.

- [ ] **Step 6: Implement persistent dispatch reconstruction**

Add to Provider types. Never reconstruct absolute paths from `provider_handle`—state intentionally stores only identity fields:

```python
def handle_from_pending(
    pending: object,
    trusted_run_root: Path,
) -> ProviderHandle:
    value = require_exact_pending_dispatch(pending)
    identity = require_exact_provider_handle(value["provider_handle"])
    prefix = require_safe_relative_path(value["provider_prefix"])
    request = request_from_pending(value, trusted_run_root)

    def expand(field: str) -> str:
        relative = require_safe_relative_path(value[field])
        if relative.parent != prefix:
            raise ContractError(f"{field} is outside provider prefix")
        return str(resolve_lexically_without_symlinks(
            trusted_run_root,
            relative,
        ))

    return ProviderHandle(
        adapter=require_string(identity, "adapter"),
        attempt_token=require_string(identity, "attempt_token"),
        pid=require_positive_int(identity, "pid"),
        process_start_identity=require_string(
            identity,
            "process_start_identity",
        ),
        process_group=require_positive_int(identity, "process_group"),
        child_pid=require_positive_int(identity, "child_pid"),
        child_process_start_identity=require_string(
            identity,
            "child_process_start_identity",
        ),
        child_process_group=require_positive_int(
            identity,
            "child_process_group",
        ),
        codex_version=request.codex_version,
        execution_policy_sha256=request.execution_policy_sha256,
        launch_path=expand("launch_path"),
        stdout_path=expand("stdout_path"),
        stderr_path=expand("stderr_path"),
        exit_path=expand("exit_path"),
        result_path=expand("result_path"),
    )
```

`resolve_lexically_without_symlinks()` uses the same descriptor-relative `O_NOFOLLOW` walk as Phase 2 rather than check-then-open paths. The launch ArtifactRef and token-bound launch/exit receipts repeat adapter/token plus both wrapper and child PID/start/group identities. Recovery independently classifies each identity as matching-live, absent, or mismatch; it may stop one matching group when the other is already absent, but never signals a mismatched/reused PID. Recovery for every pending dispatch:

```text
matching launch + matching wrapper/child identities -> reconstruct handle and poll
matching complete exit + complete result -> parse and accept
no launch after prepared intent -> start the recorded request once
one matching-live identity + other absent/mismatched -> stop the matching group, prove it absent, then ABANDONED
both identities absent + incomplete output -> ABANDONED
same PID + different start/group identity -> never signal that identity; stop any other matching group before ABANDONED
result token != current pending token -> archive stale result only
```

No path may freeze `ABANDONED`, clear a pending dispatch, or return from
recovery while either recorded identity still classifies `MATCHING_LIVE`.
Identity mismatch remains fail-closed for the mismatched PID, but does not
excuse leaking the independently matching wrapper/child group. Cleanup writes
or verifies a durable forced exit receipt before closing the semantic Attempt.

For `no launch after prepared intent`, do not deserialize absolute request
paths as authority. `request_from_pending()` rebuilds the expected
`ProviderRequest` from trusted target/run roots, relative
worktree/provider paths, bound prompt/schema refs, counters, sandbox policy,
the Adapter-probed Codex version, and the frozen execution-policy digest; it
then requires the immutable `request` Artifact bytes to equal
`canonical_json_bytes(asdict(rebuilt))`. Handle reconstruction uses this same
function even when a launch exists, so launch/exit version-policy identity
cannot drift. Only that rebuilt request may be passed to `start()`. A path,
schema, sandbox, token, version, policy, or byte mismatch is trusted-state
corruption and is never launched.

For an abandoned Worker, never accept a Worker-created commit or externally moved task ref. If there is no `pending_source_commit`, freeze `ABANDONED` and create a fresh semantic Attempt. If a prepared source marker exists, reconcile only its Controller-created candidate using the exact marker/audit protocol.

- [ ] **Step 7: Implement ordered resume reconciliation**

`_recover_locked()` under the caller-held run lock performs this order. Public `recover()` is a thin focused-test wrapper that acquires the lock, calls `_recover_locked()`, and returns without registering a Controller or driving ticks. Provider handle reconstruction deliberately precedes stop consumption so a crash after the request cannot lose the processes that must be terminated:

```text
1. validate complete Schema 4 and repository identity
2. validate integration ref and controller process identity
3. reconstruct all pending handles/receipts without starting, retrying, or signalling
4. classify nonce replay, future revision, stale Controller token, or applicable stop
5. consume an applicable stop before any normal reconciliation can restart work
6. recover `pending_evaluation` first by rebuilding and routing its byte-identical envelope; do not start any dispatch, verification, repair, or evaluation while it exists
7. otherwise reconcile every Worker `STARTING/preflight=null` allocation by idempotently creating or validating only its recorded base/ref/worktree, then artifact-first attach its exact immutable preflight; `recover()` does not prepare or start a Provider
8. reconcile pending dispatches and Provider receipts
9. validate detached task worktree bases against their immutable dispatch preflight and protected Git snapshots
10. recover `pending_source_commit` by exact task-ref/object/audit comparison
11. recover `pending_integration` by exact run-ref comparison
12. validate latest task/global verification commit binding
13. validate latest evaluation envelope/evidence catalog and integration binding
14. enforce provider/evidence/repair limits and semantic `failure_count` bounds; never compare monotonic attempt identity with `max_attempts`
15. classify corruption, then clear stale Controller identity
16. preserve `PAUSED`/`STOPPED` and their `resume_status` for reconciliation-only callers
```

Foreground `resume()` is one indivisible lock incarnation:

```python
def resume(self, run_id: str, *, replan: bool = False) -> dict[str, object]:
    store = self.store_for(run_id)
    with store.lock():
        state = self._recover_locked(store, replan=replan)
        status = RunStatus(state["status"])
        if status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.IMPORTED_READ_ONLY,
        }:
            return state
        if status not in {RunStatus.PAUSED, RunStatus.STOPPED}:
            raise StateConflictError("resume requires PAUSED or STOPPED state")
        state = self._restore_and_register_controller(state)
        return self._drive_locked(state)
```

`_restore_and_register_controller()` performs one StateStore transaction that
validates `resume_status`, restores exactly that active status, clears
`resume_status`, and registers the new process identity/token. Public
`recover()` never calls it and therefore cannot manufacture an apparently
active state without a live foreground loop. `_recover_locked()` never
registers a fresh Controller token; `_drive_locked()` never registers one
either. Thus reconciliation, stale-stop classification, atomic restoration plus
one token registration, and all resumed ticks occur without releasing the run
lock. Add a concurrency test with two resume callers: the second cannot enter
while the first is active, no second token is written, and a stop request
observed after registration binds the one live token rather than being
misclassified stale.

Canceled dispatches are never resurrected. Recovery clears their old handle/token, preserves the immutable `CANCELLED` artifact, and allocates a fresh Provider context, token, `attempt_no + 1`, branch, worktree, and artifact prefix when that role is scheduled again. An operator stop leaves `failure_count` unchanged.

`IMPORTED_READ_ONLY`, `SUCCEEDED`, and `FAILED` are not resumable. A Schema 3 migrated run requiring replan is handled in Phase 6 only when `replan=True`.

Recovery corruption has two explicit classes:

- If `state.json`, a bound artifact, or its digest cannot be trusted, raise `RecoveryReadOnlyError` with a machine-stable diagnostic and make no file, ref, log, or state change. There is no trusted revision on which to record failure.
- If state and bound artifacts are valid but an independently proven external invariant is irrecoverably false (for example, a bound Git object is gone), persist an immutable diagnostic and transition to `FAILED` in one normal transaction.

- [ ] **Step 8: Run recovery tests and commit Task 14**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_controller_recovery \
  tests.test_controller \
  tests.test_integrator \
  tests.test_provider_contract -v
```

Expected: all six crash windows, live/dead Controller stop, nonce idempotency, PID reuse, stale result, and resume-status cases end in `OK`.

Commit:

```bash
git add src/vibe/controller.py src/vibe/models.py \
  src/vibe/providers/base.py src/vibe/providers/codex_cli.py \
  tests/support/controller_scenario.py tests/test_controller_recovery.py
git diff --cached --check
git commit -m "feat: recover controller runs and stop safely"
```

### Task 15: Public Foreground CLI and Stable Output Contracts

**Files:**
- Modify: `src/vibe/cli.py`
- Create: `tests/test_cli.py`
- Modify: `src/vibe/__main__.py`

**Interfaces:**
- Produces exact parsers for `run`, `resume`, `status`, `stop`, `logs`, and `migrate`.
- Produces `main(argv=None) -> int`.
- Produces human and JSON output envelopes.
- Produces exit codes `0`, `2`, `3`, `4`, and `130`.

- [ ] **Step 1: Write failing parser, output, and exit-code tests**

Create `tests/test_cli.py`:

```python
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe.cli import main
from vibe.models import ContractError


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name)

    def test_run_requires_exactly_one_goal_source(self) -> None:
        self.assertEqual(
            main(["run", "--target", str(self.target)]),
            2,
        )
        goal_file = self.target / "goal.md"
        goal_file.write_text("goal\n", encoding="utf-8")
        self.assertEqual(
            main(
                [
                    "run",
                    "--target",
                    str(self.target),
                    "--goal",
                    "goal",
                    "--goal-file",
                    str(goal_file),
                ]
            ),
            2,
        )

    def test_foreground_terminal_states_map_to_stable_exit_codes(self) -> None:
        expected = {
            "SUCCEEDED": 0,
            "PAUSED": 3,
            "FAILED": 4,
            "STOPPED": 130,
        }
        for status, code in expected.items():
            controller = mock.Mock()
            controller.create_run.return_value = "RUN-20260723-001"
            controller.execute.return_value = {"status": status}
            with self.subTest(status=status), mock.patch(
                "vibe.cli.controller_for_target",
                return_value=controller,
            ):
                self.assertEqual(
                    main(
                        [
                            "run",
                            "--target",
                            str(self.target),
                            "--goal",
                            "goal",
                        ]
                    ),
                    code,
                )

    def test_status_json_is_one_machine_stable_object(self) -> None:
        state = {
            "run_id": "RUN-20260723-001",
            "status": "EXECUTING",
            "revision": 7,
            "plan_version": 1,
            "repair_round": 0,
            "tasks": {},
            "last_error": None,
        }
        with mock.patch("vibe.cli.load_status", return_value=state), mock.patch(
            "sys.stdout"
        ) as stdout:
            code = main(
                [
                    "status",
                    "--target",
                    str(self.target),
                    "RUN-20260723-001",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        rendered = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertEqual(json.loads(rendered)["status"], "EXECUTING")

    def test_expected_error_has_no_traceback(self) -> None:
        with mock.patch(
            "vibe.cli.load_status",
            side_effect=ContractError("invalid state"),
        ), mock.patch("sys.stderr") as stderr:
            code = main(
                [
                    "status",
                    "--target",
                    str(self.target),
                    "RUN-20260723-001",
                ]
            )
        self.assertEqual(code, 2)
        rendered = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("invalid state", rendered)
        self.assertNotIn("Traceback", rendered)
```

Add subprocess tests that run `python -m vibe --help`, `vibe --help`, and every subcommand `--help`.

- [ ] **Step 2: Run CLI tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_cli -v
```

Expected: parser and command behavior failures from the Phase 1 shell.

- [ ] **Step 3: Build the exact argparse command tree**

Use one target parent helper and explicit mutually exclusive groups:

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vibe")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0.dev0")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run")
    run.add_argument("--target", default=".")
    goal = run.add_mutually_exclusive_group(required=True)
    goal.add_argument("--goal")
    goal.add_argument("--goal-file")
    run.add_argument("--max-workers", type=positive_int)
    run.add_argument("--task-attempts", type=positive_int)
    run.add_argument("--provider-retries", type=positive_int)
    run.add_argument("--evidence-rounds", type=positive_int)
    run.add_argument("--repair-rounds", type=positive_int)
    run.add_argument("--allow-project-commands", action="store_true")
    run.add_argument("--json", action="store_true")

    resume = commands.add_parser("resume")
    resume.add_argument("--target", default=".")
    resume.add_argument("run_id")
    resume.add_argument("--replan", action="store_true")
    resume.add_argument("--json", action="store_true")

    status = commands.add_parser("status")
    status.add_argument("--target", default=".")
    status.add_argument("run_id")
    status.add_argument("--json", action="store_true")

    stop = commands.add_parser("stop")
    stop.add_argument("--target", default=".")
    stop.add_argument("run_id")
    stop.add_argument("--json", action="store_true")

    logs = commands.add_parser("logs")
    logs.add_argument("--target", default=".")
    logs.add_argument("run_id")
    logs.add_argument("--task")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--json", action="store_true")

    migrate = commands.add_parser("migrate")
    migrate.add_argument("--target", default=".")
    source = migrate.add_mutually_exclusive_group(required=True)
    source.add_argument("--requirement")
    source.add_argument("--all", action="store_true")
    migrate.add_argument("--base", required=True)
    migrate.add_argument("--allow-project-commands", action="store_true")
    migrate.add_argument("--json", action="store_true")
    return parser
```

Override `ArgumentParser.error()` with `CliUsageError` so `main()` returns `2` in direct unit tests without terminating the test process. Preserve normal concise stderr output.

- [ ] **Step 4: Implement command handlers and output envelopes**

`run`:

1. Resolve target to the Git root.
2. Read Goal text directly or from a regular non-symlink UTF-8 file.
3. Load/freeze config with CLI overrides. Pass
   `allow_project_commands=True` only when the run flag is present; the value is
   a creation acknowledgement and is not persisted as a mutable override.
4. On the main thread install a temporary `SIGUSR1` handler that only sets the Controller wake Event.
5. `create_run()`, then `execute()`, restoring the prior handler in `finally`.

`resume`: install/restore the same main-thread wake handler and call only `Controller.resume(run_id, replan=flag)`. Never compose public `recover()` plus `execute()`, because that would release the lock and register two Controller tokens. Unit scenarios inject an Event sender; a real subprocess test starts `vibe run`, issues `vibe stop`, and proves `SIGUSR1` wakes the foreground loop without state writes from the signal handler.

`status`: validate and return a compact projection:

```json
{
  "schema_version": 1,
  "run_id": "RUN-20260723-001",
  "status": "EXECUTING",
  "revision": 7,
  "plan_version": 1,
  "repair_round": 0,
  "tasks": {
    "completed": 1,
    "running": 2,
    "pending": 3,
    "failed": 0
  },
  "last_error": null
}
```

`status` must call `StateStore.load()` so every state-bound artifact digest is validated before rendering. It captures `state.json` bytes before and after the read in tests and proves they are identical; neither `status` nor `logs` may call `transact()`, change the revision, or repair malformed state as a side effect.

`stop`: call `request_stop()`. If the Controller is dead and the caller obtains the run lock, call `recover_and_stop()`. Otherwise return request acceptance.

`migrate`: pass the acknowledgement boolean to
`migrate_schema3(..., allow_project_commands=flag)`. `resume`, `status`, `stop`,
and `logs` deliberately expose no such flag because they consume frozen
authority only. CLI tests create a non-empty project catalog and prove both
`run` and `migrate` fail before any process/ref/run allocation without the
flag, accept it with the flag, and persist the identical authorization
mode/source digest expected by Phase 1.

`logs`: read `controller.jsonl` and optional task stdout/stderr paths from immutable state pointers. `--follow` polls until the run enters a static state; it never uses logs to decide state. `--json` emits one valid JSON object per line.

`migrate`: parse now and delegate to `migrate_schema3()` added in Phase 6. Until Phase 6 lands, its focused test may patch that function; do not ship the breaking cutover before the real implementation exists.

JSON command envelope:

```json
{
  "schema_version": 1,
  "command": "run",
  "ok": true,
  "run_id": "RUN-20260723-001",
  "status": "SUCCEEDED",
  "detail": null
}
```

Expected errors use `ok=false` and exit `2`; do not print a Python traceback.

- [ ] **Step 5: Add in-process CLI lifecycle coverage with a Fake Provider**

Patch `controller_for_target()` in the test process to return the real Controller wired to `ScenarioProvider`; do not add a runtime environment hook. Use a real temporary Git target and invoke `main([...])` directly so the same durable run is exercised without Codex:

```python
def test_run_status_logs_and_resume_use_one_durable_run(self) -> None:
    scenario = self.paused_then_successful_cli_scenario()
    with mock.patch(
        "vibe.cli.controller_for_target",
        return_value=scenario.controller,
    ):
        run_code, run_output = self.call_main(
            "run",
            "--target",
            str(scenario.target),
            "--goal",
            "Create two files",
            "--json",
        )
        run_id = json.loads(run_output)["run_id"]
        self.assertEqual(run_code, 3)

        status_code, status_output = self.call_main(
            "status", "--target", str(scenario.target), run_id, "--json"
        )
        self.assertEqual(status_code, 0)
        self.assertEqual(json.loads(status_output)["run_id"], run_id)

        logs_code, logs_output = self.call_main(
            "logs", "--target", str(scenario.target), run_id, "--json"
        )
        self.assertEqual(logs_code, 0)
        log_lines = logs_output.splitlines()
        self.assertTrue(log_lines)
        self.assertTrue(
            all(isinstance(json.loads(line), dict) for line in log_lines)
        )

        resume_code, resume_output = self.call_main(
            "resume", "--target", str(scenario.target), run_id, "--json"
        )
        self.assertEqual(resume_code, 0)
        self.assertEqual(json.loads(resume_output)["status"], "SUCCEEDED")


def test_stop_follow_task_filter_migrate_and_imported_state_contracts(self) -> None:
    scenario = self.running_cli_scenario_with_task_logs("TASK-001")
    scenario.start_controller()
    scenario.wait_for_worker_start()
    with mock.patch(
        "vibe.cli.controller_for_target",
        return_value=scenario.controller,
    ):
        stop_code, stop_output = self.call_main(
            "stop", "--target", str(scenario.target), scenario.run_id, "--json"
        )
        self.assertIn(stop_code, (0, 130))
        self.assertTrue(json.loads(stop_output)["ok"])
    scenario.join_controller()

    logs_code, logs_output = self.call_main(
        "logs",
        "--target",
        str(scenario.target),
        scenario.run_id,
        "--task",
        "TASK-001",
        "--follow",
        "--json",
    )
    self.assertEqual(logs_code, 0)
    lines = [json.loads(line) for line in logs_output.splitlines()]
    self.assertTrue(lines)
    self.assertTrue(all(line.get("task_id") == "TASK-001" for line in lines))

    imported = self.imported_read_only_scenario()
    status_code, status_output = self.call_main(
        "status", "--target", str(imported.target), imported.run_id, "--json"
    )
    self.assertEqual(status_code, 0)
    self.assertEqual(
        json.loads(status_output)["status"],
        "IMPORTED_READ_ONLY",
    )
    resume_code, resume_output = self.call_main(
        "resume", "--target", str(imported.target), imported.run_id, "--json"
    )
    self.assertEqual(resume_code, 2)
    self.assertFalse(json.loads(resume_output)["ok"])

    migration = self.migration_result()
    with mock.patch("vibe.cli.migrate_schema3", return_value=[migration]):
        code, output = self.call_main(
            "migrate",
            "--target",
            str(self.target),
            "--requirement",
            "REQ-001",
            "--base",
            "HEAD",
            "--json",
        )
    self.assertEqual(code, 0)
    self.assertEqual(json.loads(output)["imports"][0]["run_id"], migration.run_id)


def test_json_usage_error_and_version_are_machine_stable(self) -> None:
    code, output = self.call_main(
        "status",
        "--target",
        str(self.target),
        "NOT-A-RUN",
        "--json",
    )
    value = json.loads(output)
    self.assertEqual(code, 2)
    self.assertFalse(value["ok"])
    self.assertEqual(value["schema_version"], 1)

    version_code, version_output = self.call_main("--version")
    self.assertEqual(version_code, 0)
    self.assertEqual(version_output.strip(), "vibe 0.1.0.dev0")
```

Keep subprocess coverage limited to `python -m vibe --help`, `python -m vibe --version`, installed `vibe --help`, and each subcommand `--help`; those paths need no Provider. No `VIBE_TEST_PROVIDER_FACTORY` or other hidden runtime API is introduced.

- [ ] **Step 6: Run CLI and recovery tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_cli tests.test_controller_recovery -v
```

Expected: every command parser/output/exit test and every recovery test ends in `OK`.

- [ ] **Step 7: Commit Task 15**

```bash
git add src/vibe/cli.py src/vibe/__main__.py tests/test_cli.py
git diff --cached --check
git commit -m "feat: expose foreground controller cli"
```

## Phase 5 Completion Gate

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_controller_recovery \
  tests.test_controller_fake_provider \
  tests.test_cli \
  tests.test_integrator \
  tests.test_provider_contract -v
python3 -m vibe --help
vibe --help
```

Expected:

- Every required crash point resumes without duplicate completed work.
- A live Controller, not the stop CLI, writes `STOPPED`.
- A dead Controller can be safely recovered and stopped under the run lock.
- PID reuse and stale tokens never change current state or receive a signal.
- Stop nonce and receipt behavior is idempotent.
- Resume returns to the recorded deterministic stage.
- All six commands and their `--json` modes parse.
- Foreground terminal states return `0`, `3`, `4`, or `130` as specified.
- Expected CLI failures return `2` without tracebacks.
