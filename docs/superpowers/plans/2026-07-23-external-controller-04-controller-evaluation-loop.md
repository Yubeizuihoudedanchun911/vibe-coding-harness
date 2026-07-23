# External Controller Phase 4: Main Lifecycle and Evaluation Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the deterministic components into a foreground Controller that plans once, runs independent specialist Workers concurrently, integrates them serially, performs global verification, independently evaluates the immutable commit, and completes a bounded repair loop.

**Architecture:** `Controller.tick()` is a deterministic state-machine step and `Controller.execute()` is the lock-owning foreground loop. Agent processes may overlap, but only one task enters `INTEGRATING`; all state writes flow through `StateStore`. Fake Provider end-to-end scenarios prove actual overlap with events/barriers rather than timing thresholds.

**Tech Stack:** Python 3.10+ standard library, dependency injection, Fake Provider, real temporary Git repositories, `unittest`, immutable JSON evidence.

## Global Constraints

- The Controller is the only lifecycle and `state.json` writer.
- `run` and `resume` hold the run lock for the foreground lifecycle.
- The run lock is non-reentrant: `tick()` requires the caller-held lock and never acquires it; direct unit tests use a `tick_locked()` helper.
- Planner, Worker, and Evaluator start through the Provider Adapter and prepared dispatch ledger.
- Each semantic attempt uses a fresh token, context, reserved branch, and detached worktree.
- `provider_retry_no`, monotonic semantic `attempt_no`, semantic `failure_count`, Evaluator `evidence_round`, and `repair_round` are independent. `task_attempts` bounds failures, not identity allocation, separately for each Planner/Evaluator operation and each Worker task.
- Dispatch every currently safe task up to `max_workers`; never trade correctness for maximum parallelism.
- Poll multiple active Providers without blocking on one process.
- Integrate only one `READY_TO_INTEGRATE` task at a time and use the current integration head for the candidate.
- Worker self-reported verification is handoff evidence only.
- Global required commands and Planner global verification run at the immutable integration head.
- Deterministic global verification failure enters repair planning and can never be accepted as success.
- Evaluator sees original Goal/criteria, all immutable plans, Git truth, task results, and Controller evidence.
- `PASS`, `NEEDS_REPAIR`, `UNVERIFIED`, and `BLOCKED` have distinct routes.
- Repair Planner appends a new plan version; it never mutates prior plans/tasks or weakens Goal/criteria.
- `UNVERIFIED` runs only supplemental evidence on the same commit and does not increment repair round.
- A run reaches `SUCCEEDED` only when the complete Goal Gate predicate is true and the final state is atomically committed.
- Success never merges or pushes a user branch.

---

## File Map

- Create `src/vibe/controller.py`: dependency graph, run creation, lock-owning loop, state handlers, evaluation envelope.
- Create `tests/support/controller_scenario.py`: real-Git scripted end-to-end fixture.
- Create `tests/test_controller.py`: state-machine and bound-counter unit tests.
- Create `tests/test_controller_fake_provider.py`: offline end-to-end parallel, repair, evaluation, and success/failure scenarios.
- Modify `tests/support/fake_provider.py`: role-aware scripts, start events, completion barriers, Worker edit callback.
- Modify `src/vibe/models.py`: Controller dependencies, evaluation envelope helpers, final state invariants.

### Task 12: Foreground Controller Main Lifecycle

**Files:**
- Create: `src/vibe/controller.py`
- Create: `tests/support/controller_scenario.py`
- Create: `tests/test_controller.py`
- Create: `tests/test_controller_fake_provider.py`
- Modify: `tests/support/fake_provider.py`
- Modify: `src/vibe/models.py`

**Interfaces:**
- Produces `ControllerDependencies`.
- Produces `Controller.create_run(goal, config) -> str`.
- Produces `Controller.execute(run_id, replan=False) -> dict[str, object]`.
- Produces private `Controller._drive_locked(state) -> dict[str, object]` for one already-registered, lock-owned foreground incarnation.
- Produces `Controller.tick(state) -> TickResult`.
- Produces handlers for `CREATED`, `PLANNING`, `EXECUTING`, and `GLOBAL_VERIFYING`.
- Produces a role-aware scripted Provider fixture.

- [ ] **Step 1: Extend the Fake Provider with role scripts and overlap evidence**

Add these test-only types to `tests/support/fake_provider.py`:

```python
@dataclass
class ProviderScript:
    role: str
    result_body: bytes
    on_start: Callable[[ProviderRequest], None] | None = None
    release: threading.Event | None = None
    failure: ProviderFailure | None = None


class ScenarioProvider(ScriptedProvider):
    def __init__(self, scripts: list[ProviderScript]) -> None:
        super().__init__()
        self.scripts = scripts
        self.started: list[str] = []
        self.active: set[str] = set()
        self.maximum_active = 0
        self._scenario_lock = threading.Lock()

    def start(self, request: ProviderRequest) -> ProviderHandle:
        with self._scenario_lock:
            script = self.scripts.pop(0)
        if script.role != request.role:
            raise AssertionError(
                f"expected role {script.role}, received {request.role}"
            )
        if script.on_start is not None:
            script.on_start(request)
        handle = super().start(request)
        with self._scenario_lock:
            self.started.append(request.attempt_token)
            self.active.add(request.attempt_token)
            self.maximum_active = max(self.maximum_active, len(self.active))
        if script.failure is not None:
            self.fail(request.attempt_token, script.failure)
            with self._scenario_lock:
                self.active.discard(request.attempt_token)
            return handle
        Path(request.result_path).write_bytes(script.result_body)
        if script.release is None or script.release.is_set():
            self.complete(request.attempt_token)
            with self._scenario_lock:
                self.active.discard(request.attempt_token)
        else:
            threading.Thread(
                target=self._complete_when_released,
                args=(request.attempt_token, script.release),
                daemon=True,
            ).start()
        return handle

    def _complete_when_released(
        self,
        attempt_token: str,
        release: threading.Event,
    ) -> None:
        if not release.wait(timeout=10):
            self.record_background_failure(
                AssertionError(f"release timed out for {attempt_token}")
            )
            with self._scenario_lock:
                self.active.discard(attempt_token)
            return
        self.complete(attempt_token)
        with self._scenario_lock:
            self.active.discard(attempt_token)
```

Use events only for coordination. Do not assert concurrency from elapsed milliseconds.

Create a single reusable fixture API in `tests/support/controller_scenario.py`; Phase 4–6 tests build scenario specifications rather than inventing separate orchestration helpers:

```python
@dataclass(frozen=True)
class ScenarioSpec:
    goal: str
    config: FrozenRunConfig
    scripts: tuple[ProviderScript, ...]
    worker_edits: Mapping[
        str,
        Callable[[Path, ProviderRequest], None],
    ]
    fault_points: frozenset[str] = frozenset()


@dataclass
class ControllerScenario:
    target: Path
    run_id: str
    store: StateStore
    provider: ScenarioProvider
    controller: Controller
    dependencies: ControllerDependencies
    thread_errors: queue.Queue[BaseException]
    integration_count: int = 0
    active_integrations: int = 0
    maximum_simultaneous_integrations: int = 0
```

| Fixture method | Exact contract |
|---|---|
| `ControllerScenario.build(temporary_root, spec) -> ControllerScenario` | initializes the real repository/dependency graph and calls production `create_run()` |
| `start_controller() -> threading.Thread` | starts exactly one foreground `execute()` and captures every thread exception |
| `wait_until(predicate, timeout=10) -> None` | monotonic event wait that surfaces thread exceptions and times out with diagnostics |
| `join_controller(timeout=20) -> dict` | joins, rejects leaks/errors, and returns validated state |
| `assert_thread_error(error_type, text) -> None` | consumes exactly one matching injected crash |
| `new_controller() -> Controller` | creates fresh in-memory orchestration over the same durable target and Provider truth |
| `close() -> None` | idempotently stops fake runs, joins threads, removes disposable worktrees, and rejects leaks |

`build()` initializes a real `git init -b main` repository with fixed local author identity, commits the baseline, constructs the real StateStore/WorktreeManager/Scheduler/VerificationGate/Integrator/Runners, and injects only the scripted Provider, clock/sleep, and fault hook. Worker edit callbacks receive only their real detached assigned worktree and modify scoped files; they never stage, commit, or move a ref. Production `WorktreeManager` performs protected-state audit and Controller-owned source preparation/CAS. Integrator instrumentation is a lock-protected decorator around the real Integrator, not a fake.

`start_controller()` catches every `BaseException` into `thread_errors`; `join_controller()` fails on a live thread or queued exception and returns loaded terminal state. `wait_until()` uses a monotonic deadline and reports queued thread failures immediately. `new_controller()` creates fresh Controller memory while sharing only durable target/provider process truth. `close()` stops remaining fake runs, joins background threads, removes disposable worktrees, calls `provider.assert_no_background_failures()`, and fails on leaked threads/processes. Every test registers `scenario.close` with `addCleanup`.

Add `test_controller_scenario_smoke_reaches_success_and_cleans_up()` using one Planner, one Worker raw edit, one Controller-owned source commit, and one PASS Evaluator. Repair/evidence/recovery helpers in test classes may construct different `ScenarioSpec` values, but must return this same `ControllerScenario` type.

- [ ] **Step 2: Write failing state-machine and end-to-end tests**

Create `tests/test_controller.py` with dependency fakes and direct tick cases:

```python
def test_created_starts_one_planner_and_records_planning_state(self) -> None:
    state = self.tick_locked(self.state("CREATED")).state
    self.assertEqual(state["status"], "PLANNING")
    self.assertEqual(
        [item["role"] for item in state["pending_dispatches"].values()],
        ["planner"],
    )


def test_run_creation_binds_intent_config_and_receipt(self) -> None:
    run_id = self.controller.create_run("Implement criterion C1", self.config)
    store = self.store_for(run_id)
    state = store.load()
    self.assertEqual(state["goal"], "Implement criterion C1")
    self.assertEqual(state["creation"]["intent"]["path"], "creation.intent.json")
    self.assertEqual(
        state["creation"]["receipt"]["path"],
        "creation.receipt.json",
    )
    self.assertEqual(state["config"]["path"], "config.json")
    self.assertFalse((store.root / "goal.json").exists())


def test_executing_polls_existing_workers_before_dispatching_new_work(self) -> None:
    state = self.executing_state_with_one_completed_provider()
    result = self.tick_locked(state)
    self.assertIn("TASK-001", result.completed_provider_tokens)
    self.assertEqual(
        result.state["tasks"]["TASK-001"]["status"],
        "RUNNING",
    )
    self.assertEqual(
        result.state["tasks"]["TASK-001"]["active_attempt"]["status"],
        "VERIFYING",
    )


def test_one_tick_integrates_at_most_one_task(self) -> None:
    state = self.executing_state_with_two_ready_to_integrate_tasks()
    result = self.tick_locked(state)
    integrated = [
        task_id
        for task_id, task in result.state["tasks"].items()
        if task["status"] == "COMPLETED"
    ]
    self.assertEqual(len(integrated), 1)
```

Create `tests/test_controller_fake_provider.py` with a real Git scenario:

```python
def test_two_independent_workers_are_active_together_then_integrated_serially(self) -> None:
    release = threading.Event()
    scenario = self.scenario(
        planner_plan=self.two_independent_task_plan(),
        worker_scripts=(
            self.worker_script(
                "TASK-001",
                "src/a.txt",
                "a\n",
                release=release,
            ),
            self.worker_script(
                "TASK-002",
                "src/b.txt",
                "b\n",
                release=release,
            ),
        ),
        evaluator_result=self.pass_evaluation(),
    )
    controller_thread = scenario.start_controller()
    scenario.wait_until(lambda: scenario.provider.maximum_active == 2)
    self.assertEqual(scenario.provider.maximum_active, 2)
    self.assertEqual(scenario.integration_count, 0)
    release.set()
    controller_thread.join(timeout=20)

    self.assertFalse(controller_thread.is_alive())
    state = scenario.store.load()
    self.assertEqual(state["status"], "SUCCEEDED")
    self.assertEqual(scenario.maximum_simultaneous_integrations, 1)
    self.assertEqual(
        scenario.git_changed_paths(state["repository"]["integration_head"]),
        {"src/a.txt", "src/b.txt"},
    )


def test_overlapping_scope_and_resource_tasks_never_run_together(self) -> None:
    scenario = self.scenario(
        planner_plan=self.conflicting_task_plan(),
        worker_scripts=self.successful_conflicting_workers(),
        evaluator_result=self.pass_evaluation(),
    )
    state = scenario.controller.execute(scenario.run_id)
    self.assertEqual(state["status"], "SUCCEEDED")
    self.assertEqual(scenario.provider.maximum_active, 1)


def test_worker_failure_creates_fresh_attempt_on_latest_integration_head(self) -> None:
    scenario = self.scenario_with_first_worker_failure()
    state = scenario.controller.execute(scenario.run_id)
    attempts = scenario.worker_launches("TASK-001")
    self.assertEqual(len(attempts), 2)
    self.assertNotEqual(attempts[0]["attempt_token"], attempts[1]["attempt_token"])
    self.assertEqual(
        attempts[1]["expected_base"],
        attempts[1]["integration_head_at_dispatch"],
    )
    self.assertTrue(
        scenario.is_ancestor(
            attempts[1]["expected_base"],
            state["repository"]["integration_head"],
        )
    )


def test_failed_provider_has_no_published_or_bound_result(self) -> None:
    scenario = self.scenario_with_first_worker_failure()
    scenario.controller.execute(scenario.run_id)
    failed = scenario.worker_launches("TASK-001")[0]
    self.assertFalse(scenario.provider_result_path(failed).exists())
    pending_or_attempt = scenario.provider_record(failed["attempt_token"])
    self.assertIsNone(pending_or_attempt["result"])
    scenario.provider.assert_no_background_failures()
```

`tick_locked(state)` enters exactly one `with self.store.lock():` and calls `controller.tick(state)`. The scenario helper makes Worker `on_start` callbacks edit only assigned paths and explicitly asserts task worktree HEAD/index/ref stay at the recorded base until production source-CAS code runs. It never stages, commits, or fakes a source head. Every test calls `scenario.provider.assert_no_background_failures()` before accepting a terminal state.

- [ ] **Step 3: Run Controller tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_controller tests.test_controller_fake_provider -v
```

Expected: import failure because `vibe.controller` does not exist.

- [ ] **Step 4: Implement dependency injection and run creation**

Create these types in `src/vibe/controller.py`:

```python
@dataclass(frozen=True)
class ControllerDependencies:
    store_factory: Callable[[Path, str], StateStore]
    worktrees: WorktreeManager
    scheduler: Scheduler
    planner: PlannerRunner
    worker: WorkerRunner
    evaluator: EvaluatorRunner
    verification: VerificationGate
    integrator: Integrator
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    fault_hook: Callable[[str], None]


@dataclass(frozen=True)
class TickResult:
    state: dict[str, object]
    progressed: bool
    completed_provider_tokens: tuple[str, ...]
```

`create_run()` performs this ordered protocol under the shared `fcntl.flock` allocator at `.vibe-coding/control/run-allocation.lock`; reject symlinked control ancestors:

1. Validate non-empty Unicode Goal and frozen config.
2. `WorktreeManager.assert_clean_baseline("HEAD")`.
3. Compute `creation_fingerprint = sha256(canonical_json({repository_identity, base_sha, goal_sha256, config_sha256}))`; it intentionally excludes run ID and timestamps.
4. While holding the global lock, scan only canonical `runs/RUN-*/creation.intent.json` files. If exactly one matching run is incomplete, or is still pristine `CREATED` with no Controller/Plan, validate its ref/state and finish or return that same run. More than one match is a contract conflict.
5. Otherwise allocate `RUN-YYYYMMDD-NNN` without reusing an existing directory or ref.
6. Canonically encode `config.json` and `creation.intent.json`. The intent contains run ID, `creation_fingerprint`, Goal SHA-256, repository identity/base SHA/run ref, and config SHA-256.
7. Under the new run's `StateStore.lock()`, call `prepare_artifact()` for intent and config and keep the returned refs.
8. Create `refs/heads/vibe/run-<run-id>` with old OID all-zero, or on recovery require the existing ref to equal the recorded base.
9. Create revision `0` Schema 4 state bound to the intent and config refs; Goal text lives only in state and is not duplicated as `goal.json`.
10. Canonically encode `creation.receipt.json` with run ID, fingerprint, intent ref, run ref, base SHA, and creation timestamp; publish it in a revision transaction that binds `creation.receipt`.

Release the run lock and then `run-allocation.lock`. If a crash leaves intent,
ref, state, or receipt at any prefix of this protocol, a same-input retry finds
the fingerprint and continues idempotently. Once a run has left pristine
`CREATED`, an identical later `vibe run` intentionally allocates a new run. A
mismatched intent, config, repository identity, reservation, or ref fails
closed and never overwrites it. Native allocation treats every prepared
migration reservation as occupied.

Creation tests use a protocol-scoped injector with exactly
`after_creation_intent_before_ref`, `after_creation_ref_before_state`,
`after_creation_state_before_receipt`, and
`after_creation_receipt_before_return`. These are not production runtime hook
names and are accepted only by `create_run()`'s test seam. Add all four fault
tests plus a native-create/migration race proving unique run IDs/refs.

Initial state uses:

```python
{
    "schema_version": 4,
    "run_id": run_id,
    "revision": 0,
    "goal": goal,
    "repository": {
        "identity": baseline.identity,
        "base_ref": baseline.base_ref,
        "base_sha": baseline.base_sha,
        "integration_ref": run_ref,
        "integration_head": baseline.base_sha,
    },
    "status": "CREATED",
    "resume_status": None,
    "plan_version": 0,
    "repair_round": 0,
    "max_repair_rounds": config.repair_rounds,
    "max_workers": config.max_workers,
    "controller": None,
    "config": config_ref.as_dict(),
    "creation": {
        "intent": intent_ref.as_dict(),
        "receipt": None,
    },
    "artifact_index": [
        intent_ref.as_dict(),
        config_ref.as_dict(),
    ],
    "plans": [],
    "role_attempts": {"planner": [], "evaluator": []},
    "role_runtime": {
        role: {
            "operation_id": None,
            "attempt_no": 0,
            "failure_count": 0,
            "max_attempts": config.task_attempts,
            "active_attempt_token": None,
            "last_error": None,
        }
        for role in ("planner", "evaluator")
    },
    "evaluations": [],
    "verifications": [],
    "legacy_import": None,
    "tasks": {},
    "pending_dispatches": {},
    "pending_source_commit": None,
    "pending_integration": None,
    "pending_evaluation": None,
    "latest_evaluation": None,
    "global_verification": None,
    "stop_receipts": [],
    "last_error": None,
    "created_at": now,
    "updated_at": now,
}
```

After `StateStore.create(initial_state, {})`, publish the receipt with:

```python
state = store.transact(
    state["revision"],
    {"creation.receipt.json": receipt_body},
    lambda current, refs: current["creation"].update(
        {"receipt": refs["creation.receipt.json"].as_dict()}
    ),
)
```

`_register_controller()` commits `{pid, process_start_identity, process_group, controller_token}` with a new random `controller_token` for every foreground `execute()`/resume incarnation. That token remains stable across ticks and is replaced only after recovery has classified any stale stop request.

- [ ] **Step 5: Implement the deterministic foreground loop**

`execute()`:

```python
with store.lock():
    state = self._register_controller(store.load())
    return self._drive_locked(state)
```

`_drive_locked()` contains the loop formerly shown inline:

```python
while RunStatus(state["status"]) not in STATIC_RUN_STATUSES:
    result = self.tick(state)
    state = result.state
    if not result.progressed:
        self.dependencies.sleep(0.1)
return state
```

It requires the caller-held non-reentrant run lock and a Controller token already registered for this incarnation; it never acquires a lock or registers/replaces a token. Phase 5 `resume()` reuses this exact method after locked reconciliation so recovery and execution have no unlock or double-registration gap.

Use:

```python
STATIC_RUN_STATUSES = {
    RunStatus.PAUSED,
    RunStatus.STOPPED,
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.IMPORTED_READ_ONLY,
}
```

`tick()` dispatches by state:

```python
handlers = {
    RunStatus.CREATED: self._start_initial_planner,
    RunStatus.PLANNING: self._poll_planner,
    RunStatus.EXECUTING: self._execute_tasks,
    RunStatus.GLOBAL_VERIFYING: self._run_global_verification,
    RunStatus.EVALUATING: self._evaluate,
    RunStatus.REPAIRING: self._repair,
}
```

Every handler makes at most one coherent lifecycle decision per tick and must not edit the caller's state object in place. A prepared side-effect protocol may use the multiple small transactions explicitly frozen by DispatchLedger or Integrator (intent, external effect, completion); those transactions together implement that one decision.

- [ ] **Step 6: Implement planning, task artifacts, and parallel Worker dispatch**

On accepted initial Plan:

1. Persist `plan/plan-v001.json`.
2. Persist one immutable `tasks/<id>/task.json` per task.
3. Close the Planner semantic attempt with an immutable Attempt manifest, append its ref to `role_attempts.planner`, and clear `role_runtime.planner.active_attempt_token`.
4. Append the Plan ref to `plans`; create task runtime state with the full exact Task shape, `PENDING`, `attempt_no=0`, `failure_count=0`, empty `attempts`, null `result`/`verification`, and config-bounded `max_attempts`.
5. Set `plan_version=1`, remove only the matching Planner pending dispatch, and enter `EXECUTING`.

In `_execute_tasks()` use this order per tick:

1. Reconcile an existing `pending_source_commit` before any other side effect. Base replays its exact task-ref CAS, candidate verifies/binds the immutable audit, and any other ref pauses.
2. Reconcile an existing `pending_integration` before considering a new candidate.
3. Poll every active Worker handle; bind/accept only current matching token and exact receipt. A `COMPLETED` handoff strictly parses the final raw Provider result, artifact-first publishes the identical bytes at the immutable semantic `active.result_path`, proves digest equality, binds that stable ref to the Task, clears the active handle, leaves the Task `RUNNING`, and changes the active Attempt to `VERIFYING`; it does not close the Attempt or trust commit/path/check claims.
4. For at most one handoff-ready `RUNNING` Task, load its dispatch-time immutable preflight, capture the current protected Git state and compare it to that persisted snapshot, audit the raw detached-worktree delta, prepare the deterministic source object/audit using the persisted `active_attempt.created_at`, persist `pending_source_commit`, fire `after_worker_edit_before_controller_commit`, task-ref CAS, fire `after_controller_commit_before_verification_binding`, then bind/clear the marker and move to `READY_TO_INTEGRATE`.
5. For at most one `READY_TO_INTEGRATE` Task, call Integrator. Successful candidate verification freezes the `SUCCEEDED` Attempt and writes `pending_integration`; failure freezes `FAILED`, increments `failure_count` once, and schedules a fresh identity when allowed.
6. Close invalid output, timeout, non-transient started-process failure, or cancellation with an immutable Attempt manifest before clearing active state. A Worker result `BLOCKED` enters `PAUSED` with blocker evidence and does not churn through blind retries.
7. Before selecting new work, scan every existing active `STARTING` Worker Attempt, including identities left by a previous Controller. If preflight is null, idempotently create/validate only its recorded base/ref/worktree and artifact-first attach the exact snapshot. If preflight is attached and no pending dispatch exists, prepare/start that same token from its recorded identity. Never allocate a replacement or require this `RUNNING` Task to re-enter `dispatchable()`.
8. Promote dependency-satisfied tasks and select new `READY` work within the remaining slots.
9. For each newly selected Task, allocate the semantic identity and `created_at` once and persist a `STARTING` active Attempt containing the recorded base, reserved branch, and detached-worktree path with `preflight=null`. This allocation transaction occurs before and independently of any Git/filesystem side effect.
10. Idempotently ensure each newly recorded branch/worktree exists at its recorded base. An unexpected ref value or registration/path mismatch pauses; the Controller never adopts or overwrites it.
11. Capture the protected/read-only snapshot only after that exact worktree exists, then atomically publish immutable `preflight.json` and attach its ref to the still-current `STARTING` Attempt.
12. Prepare dispatch intents only for Attempts with attached preflight, copying the same `operation_id`, `attempt_created_at`, and preflight ref; then start all newly selected Providers without waiting for one to finish.
13. When every task across all plan versions is `COMPLETED` and no dispatch/source/integration marker is pending, enter `GLOBAL_VERIFYING`.

One tick performs at most one source-CAS or integration-CAS side effect. It never invokes Integrator while `pending_source_commit` is non-null. The production fault hook receives only the roadmap's canonical names.

The two-phase start is replayable without a new production fault hook:
focused tests may stop after the allocation transaction, reconstruct the
Controller, and prove that it creates or validates the exact recorded
ref/worktree, attaches one byte-identical preflight, and starts the token once.

On every new semantic context—including after cancellation—preserve task ID and Worker type but allocate `attempt_no + 1`, a new token, branch suffix, worktree, and Provider context from the latest integration head. Increment `failure_count` only when closing a semantic failure; cancellation leaves it unchanged. Exhaustion compares `failure_count` with `max_attempts`, never `attempt_no`.

Route Provider and semantic failures identically in every role:

| Failure | Counter and transition |
|---|---|
| transient Provider failure | increment only `provider_retry_no`; use a fresh launch token for the same semantic attempt |
| authentication, Provider configuration, missing executable, or invalid execution environment | `PAUSED` with the current active state in `resume_status` |
| timeout, invalid output, or non-transient started-process failure | close the semantic attempt, increment `failure_count` once, and retry with fresh identity while below `task_attempts` |
| Worker result `BLOCKED` | `PAUSED` with `resume_status=EXECUTING`; do not consume another attempt automatically |
| Planner/Evaluator malformed-output exhaustion or Worker task-attempt exhaustion | `FAILED` |

Provider retry exhaustion is a configuration/availability pause, not a fabricated semantic failure.

Planner/Evaluator counters are operation-scoped. Initial planning, each
repair-planning round, each initial evaluation, and each supplemental-evidence
evaluation allocate a fresh `operation_id` and reset that operation's
`attempt_no=0` and `failure_count=0`; append-only `role_attempts` history is
never cleared. Provider/semantic retries stay within that operation. Their
immutable artifacts use
`roles/<role>/operations/<operation_id>/attempts/<attempt_no:03d>/...`, so the same local
attempt number in two operations cannot collide. Add a success case where
`task_attempts` is lower than the total number of repair/evidence operations,
proving successful first attempts in later operations are not mistaken for
exhaustion.

- [ ] **Step 7: Implement global verification transition**

Resolve `repository.integration_ref` immediately before creating the disposable worktree and require its actual OID to equal `repository.integration_head`. Load every Plan referenced by `state.plans`, validate their digests, and run the cumulative frozen gates:

```python
command_ids = effective_global_verification(
    config,
    all_plans,
)
result = verification.run(
    integration_head,
    worktree,
    command_ids,
    f"verification/global/VERIFY-{uuid.uuid4()}",
)
```

The gate resolves all IDs from frozen config before spawning its first process.
Resolve the run ref again after the gate. If either read differs, discard the
gate as non-authoritative and enter `PAUSED` with `INTEGRATION_REF_MOVED`; never
accept evidence for a commit that is no longer the actual run ref. Otherwise
persist the immutable verification manifest and append its ref to
`verifications`. A passing gate sets `global_verification`, sets
`status=EVALUATING`, and starts no Evaluator until the next tick. An
unknown/duplicate Agent ID is invalid role output and consumes that role's
bounded semantic failure policy with zero command starts. Only a configured
catalog command whose executable is missing/unstartable, or whose authorized
local environment cannot be established, enters `PAUSED` with
`VERIFICATION_ENVIRONMENT`. If an executed command fails, call the same
`_open_repair()` transition used by Evaluator `NEEDS_REPAIR`.

- [ ] **Step 8: Run main-lifecycle tests and commit Task 12**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_controller tests.test_controller_fake_provider -v
```

Expected: Planner-to-global-verification flow passes; independent Workers overlap, conflicts serialize, integration remains serial, and retries use a fresh identity.

Commit:

```bash
git add src/vibe/controller.py src/vibe/models.py \
  tests/support/fake_provider.py tests/support/controller_scenario.py \
  tests/test_controller.py tests/test_controller_fake_provider.py
git diff --cached --check
git commit -m "feat: orchestrate parallel worker task graphs"
```

### Task 13: Independent Evaluation, Supplemental Evidence, Repair, and Goal Gate

**Files:**
- Modify: `src/vibe/controller.py`
- Modify: `src/vibe/models.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_controller_fake_provider.py`

**Interfaces:**
- Produces `evaluation_envelope(state, evaluation_round, evidence_round, raw_result_ref, result, plan_manifest_sha256, task_result_manifest_sha256, verification_manifest_sha256, prompt_versions, evidence_catalog) -> dict[str, object]`.
- Produces `_evaluate()` verdict routing.
- Produces `_repair()` append-only repair Planner flow.
- Produces final atomic `SUCCEEDED` transition guarded by `goal_gate_satisfied`.

- [ ] **Step 1: Write failing verdict-routing and bounded-loop tests**

Add:

```python
def test_needs_repair_appends_plan_and_succeeds_after_new_worker(self) -> None:
    scenario = self.repair_scenario(
        first_evaluation=self.needs_repair_evaluation(),
        repair_plan=self.one_repair_task_plan(plan_version=2),
        final_evaluation=self.pass_evaluation(),
    )
    state = scenario.controller.execute(scenario.run_id)
    self.assertEqual(state["status"], "SUCCEEDED")
    self.assertEqual(state["plan_version"], 2)
    self.assertEqual(state["repair_round"], 1)
    self.assertTrue(scenario.artifact_exists("plan/repair-v002.json"))
    self.assertTrue(scenario.artifact_exists("plan/plan-v001.json"))


def test_unverified_runs_supplemental_evidence_on_same_commit(self) -> None:
    scenario = self.unverified_then_pass_scenario()
    state = scenario.controller.execute(scenario.run_id)
    evaluations = scenario.evaluation_envelopes()
    self.assertEqual(state["status"], "SUCCEEDED")
    self.assertEqual(state["repair_round"], 0)
    self.assertEqual(
        {item["integration_head"] for item in evaluations},
        {state["repository"]["integration_head"]},
    )
    self.assertEqual([item["evidence_round"] for item in evaluations], [0, 1])


def test_unverified_without_requests_reevaluates_the_same_commit(self) -> None:
    scenario = self.unverified_without_requests_then_pass_scenario()
    state = scenario.controller.execute(scenario.run_id)
    evaluations = scenario.evaluation_envelopes()
    self.assertEqual(state["status"], "SUCCEEDED")
    self.assertEqual(state["repair_round"], 0)
    self.assertEqual(len(scenario.supplemental_command_runs), 0)
    self.assertEqual([item["evidence_round"] for item in evaluations], [0, 1])
    self.assertEqual(
        {item["integration_head"] for item in evaluations},
        {state["repository"]["integration_head"]},
    )


def test_repeated_unverified_pauses_when_evidence_limit_is_exhausted(self) -> None:
    scenario = self.repeated_unverified_scenario(evidence_rounds=2)
    state = scenario.controller.execute(scenario.run_id)
    self.assertEqual(state["status"], "PAUSED")
    self.assertEqual(state["last_error"]["code"], "EVIDENCE_EXHAUSTED")
    self.assertEqual(state["repair_round"], 0)


def test_blocked_pauses_without_repair_or_success(self) -> None:
    scenario = self.blocked_evaluation_scenario()
    state = scenario.controller.execute(scenario.run_id)
    self.assertEqual(state["status"], "PAUSED")
    self.assertEqual(state["repair_round"], 0)


def test_attempt_or_repair_limit_exhaustion_fails(self) -> None:
    worker_exhausted = self.worker_failure_scenario(max_attempts=2)
    repair_exhausted = self.repair_failure_scenario(max_repairs=1)
    self.assertEqual(
        worker_exhausted.controller.execute(worker_exhausted.run_id)["status"],
        "FAILED",
    )
    self.assertEqual(
        repair_exhausted.controller.execute(repair_exhausted.run_id)["status"],
        "FAILED",
    )


def test_repeated_global_verification_failure_exhausts_repair_rounds(self) -> None:
    scenario = self.repeated_global_gate_failure_scenario(max_repairs=2)
    state = scenario.controller.execute(scenario.run_id)
    self.assertEqual(state["status"], "FAILED")
    self.assertEqual(state["repair_round"], 2)
    self.assertEqual(scenario.repair_plan_count(), 2)


def test_external_run_ref_move_prevents_evaluator_acceptance_and_success(self) -> None:
    scenario = self.scenario_that_moves_run_ref_before_evaluator_acceptance()
    state = scenario.controller.execute(scenario.run_id)
    self.assertEqual(state["status"], "PAUSED")
    self.assertEqual(state["last_error"]["code"], "INTEGRATION_REF_MOVED")
    self.assertNotEqual(state["status"], "SUCCEEDED")


def test_repair_keeps_prior_global_gate_and_append_only_histories(self) -> None:
    scenario = self.repair_scenario_with_distinct_global_gates()
    state = scenario.controller.execute(scenario.run_id)
    self.assertEqual(scenario.executed_global_gate_names(), ["base", "repair"])
    self.assertEqual(len(state["plans"]), 2)
    self.assertGreaterEqual(len(state["role_attempts"]["planner"]), 2)
    self.assertGreaterEqual(len(state["role_attempts"]["evaluator"]), 2)
    self.assertEqual(
        state["latest_evaluation"]["evaluation"],
        state["evaluations"][-1],
    )
```

- [ ] **Step 2: Run verdict tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_controller tests.test_controller_fake_provider -v
```

Expected: failures because evaluation and repair handlers are incomplete.

- [ ] **Step 3: Build the non-forgeable evaluation envelope**

After the Evaluator Runner returns a valid `EvaluationResult`, build:

```python
def evaluation_envelope(
    state: dict[str, object],
    evaluation_round: int,
    evidence_round: int,
    raw_result_ref: ArtifactRef,
    result: EvaluationResult,
    plan_manifest_sha256: str,
    task_result_manifest_sha256: str,
    verification_manifest_sha256: str,
    prompt_versions: dict[str, object],
    evidence_catalog: dict[str, object],
) -> dict[str, object]:
    repository = state["repository"]
    return {
        "schema_version": 1,
        "run_id": state["run_id"],
        "evaluation_round": evaluation_round,
        "evidence_round": evidence_round,
        "integration_head": repository["integration_head"],
        "goal_sha256": "sha256:"
        + hashlib.sha256(state["goal"].encode("utf-8")).hexdigest(),
        "plan_manifest_sha256": plan_manifest_sha256,
        "task_result_manifest_sha256": task_result_manifest_sha256,
        "verification_manifest_sha256": verification_manifest_sha256,
        "prompt_versions": prompt_versions,
        "evidence_catalog": evidence_catalog,
        "raw_result_sha256": raw_result_ref.sha256,
        "verdict": result.verdict.value,
        "criteria": [asdict(item) for item in result.criteria],
        "findings": [asdict(item) for item in result.findings],
        "evidence_requests": list(result.evidence_requests),
        "residual_risks": list(result.residual_risks),
    }
```

Hash manifests with canonical JSON and sorted ArtifactRefs. Bind every plan version, completed task result, task verification, global/supplemental verification, and actual prompt version. `prompt_versions` is derived only by loading the immutable Planner, Worker, and Evaluator Attempt manifests referenced by `role_attempts` and `tasks[*].attempts`; never reconstruct it from the current registry or hard-code `@v1`. Preserve the frozen `{id, version, sha256}` records, grouped by role/task/attempt, so a later installed prompt cannot rewrite evaluation provenance.

Build `evidence_catalog` exclusively from Controller-bound verification manifests. Each stable evidence ID maps exactly to `{kind, verification: ArtifactRef, integration_head, command_id, task_id, attempt_no, criterion_ids}`. Task evidence is eligible only for that task's `covers`; global evidence is eligible for the original criterion set; supplemental evidence is eligible only for criteria that were `UNVERIFIED` in the result that requested it. Every entry must reference `state.verifications` and the exact integration head. Evaluator context includes this catalog; result acceptance rejects unknown IDs, wrong-commit entries, or a criterion using evidence whose `criterion_ids` does not include it. Goal Gate independently repeats the same membership checks.

Avoid a provenance cycle with a two-transaction prepared acceptance protocol:

1. After exact Provider completion and role-result parsing, verify the actual
   run ref, then atomically freeze/append the current Evaluator Attempt manifest
   (including prompt/preflight refs and raw result), clear its pending token,
   and write the exact Schema 4 `pending_evaluation` marker:

   ```json
   {
     "operation_id": "EVAL-4bd3370d-67b6-4687-a032-834951e0c522",
     "attempt_no": 1,
     "attempt_token": "ATTEMPT-EVALUATOR-1",
     "evaluation_round": 2,
     "evidence_round": 1,
     "integration_head": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
     "attempt": {
       "path": "roles/evaluator/operations/EVAL-4bd3370d-67b6-4687-a032-834951e0c522/attempts/001/attempt.json",
       "sha256": "sha256:7777777777777777777777777777777777777777777777777777777777777777"
     },
     "raw_result": {
       "path": "roles/evaluator/operations/EVAL-4bd3370d-67b6-4687-a032-834951e0c522/attempts/001/providers/001-ATTEMPT-EVALUATOR-1/result.json",
       "sha256": "sha256:8888888888888888888888888888888888888888888888888888888888888888"
     }
   }
   ```
2. Build `prompt_versions`, hashes, and evidence catalog from that committed
   state, now including the current Evaluator Attempt. Canonically derive the
   envelope with no new clock/random inputs.
3. In the next transaction persist/append the envelope, apply exactly one
   verdict route, and clear `pending_evaluation`. A crash between steps is
   recovered from this marker before any new dispatch/verification/repair
   action. It rebuilds the byte-identical envelope, never relaunches that
   Evaluator, and never appends its Attempt twice.

- [ ] **Step 4: Implement exact verdict routing**

`PASS`:

1. Require every original criterion exactly once with criterion verdict `PASS` and at least one evidence ID.
2. Require every evidence ID to resolve through the Controller catalog to the same commit and permitted criterion.
3. Resolve the actual run ref and require equality with the envelope and state integration head before accepting the envelope.
4. Persist/append the immutable envelope, update `latest_evaluation`, and clear the exact matching `pending_evaluation`; the Evaluator Attempt was already frozen by the marker-creation transaction.
5. Call `goal_gate_satisfied(candidate_state, envelope, actual_head)` against that complete candidate state.
6. Immediately before the final state transaction, perform the no-op compare-and-swap `git update-ref <integration_ref> <expected_head> <expected_head>`. Enter `SUCCEEDED` only if the CAS and Goal Gate both succeed and all pending dispatch/source/integration fields are empty; otherwise pause on ref movement or fail closed with `GOAL_GATE_INVARIANT`.

`NEEDS_REPAIR`:

1. Require at least one actionable finding.
2. Persist and append the envelope after the prepared Evaluator Attempt step.
3. Call `_open_repair()` with findings plus frozen evidence.

`UNVERIFIED`:

1. Persist and append the envelope after the prepared Evaluator Attempt step.
2. Require `evidence_round < config.evidence_rounds`.
3. Resolve the complete `evidence_requests` ID list against frozen config before spawning any process. If present, run it in a disposable worktree at the unchanged integration head under `verification/supplemental/VERIFY-<uuid>` and append the supplemental verification ref to `verifications`.
4. If the request list is empty, run no commands and start a fresh Evaluator with the already-bound evidence; this is a legitimate request for another independent judgment, not malformed output.
5. Resolve the actual run ref before and after any supplemental commands and before accepting the next result.
6. Start a fresh Evaluator semantic attempt at `evidence_round + 1`; never increment repair round.

`BLOCKED`:

1. Persist and append the envelope and blocker findings after the prepared Evaluator Attempt step.
2. Enter `PAUSED` with `resume_status=EVALUATING`.
3. Never create a repair plan.

`_open_repair()` is the only transition that starts a repair cycle, whether triggered by deterministic global verification or Evaluator `NEEDS_REPAIR`:

1. If `repair_round >= max_repair_rounds`, enter `FAILED` without launching Planner.
2. Otherwise increment `repair_round` exactly once in the same transaction that sets `REPAIRING` and records the triggering evidence.
3. The repair Planner and subsequent repair Plan reuse that already-open round; neither increments it again.

Tests cover alternating global/Evaluator triggers and prove each opened cycle increments once, while a repeated global failure cannot loop past the bound.

- [ ] **Step 5: Implement append-only repair planning**

Repair Planner input includes:

- original Goal and criteria;
- all prior plan ArtifactRefs;
- current integration head;
- failed global verification or Evaluator findings;
- existing completed task IDs;
- frozen command/limit config.

On valid repair output:

1. Require `plan_version = previous + 1`.
2. Require original criteria unchanged.
3. Validate against every prior Plan; require only globally new task IDs, retain all prior global gates, and enforce `max_plan_tasks` over the cumulative task set.
4. Close and append the repair Planner Attempt; persist `plan/repair-vNNN.json` and new task artifacts.
5. Append the new Plan ref to `plans`; keep old tasks, results, plans, evaluations, and verification immutable.
6. Add only new runtime task state with empty append-only histories.
7. Enter `EXECUTING`.

If Planner output is invalid, close its semantic attempt; retry with fresh context within the role attempt limit. Exhaustion enters `FAILED`.

- [ ] **Step 6: Add Evaluator read-only and immutable-commit assertions**

Add tests that:

- modify a tracked file during Evaluator execution and reject the result;
- move any `refs/heads` ref during evaluation and reject the result;
- move the run ref before/after global verification, before Evaluator acceptance, and immediately before final success; every case pauses without success;
- return an envelope bound to a different integration head and reject it;
- return `PASS` with a missing criterion/evidence ID and reject it;
- return an unknown evidence ID, a bound ID from an older commit, and a task evidence ID for the wrong criterion; reject all three before Goal Gate;
- crash after `pending_evaluation` is durable but before envelope persistence and prove recovery appends one Attempt and one byte-identical envelope;
- prove canonical global verification and Evaluator see the same actual commit;
- prove an `UNVERIFIED` with zero evidence requests gets one fresh same-commit evaluation and is bounded by `evidence_rounds`;
- prove every terminal semantic attempt, Plan, evaluation, and verification extends its history by one and no prior entry changes.

- [ ] **Step 7: Run evaluation/repair end-to-end tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_controller tests.test_controller_fake_provider -v
```

Expected: all four verdict routes pass; repair and evidence bounds are enforced; success requires the complete Goal Gate.

- [ ] **Step 8: Commit Task 13**

```bash
git add src/vibe/controller.py src/vibe/models.py \
  tests/test_controller.py tests/test_controller_fake_provider.py
git diff --cached --check
git commit -m "feat: add independent evaluation and repair loop"
```

## Phase 4 Completion Gate

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_controller \
  tests.test_controller_fake_provider \
  tests.test_integrator \
  tests.test_runners \
  tests.test_verification -v
```

Expected:

- Planner, Workers, and Evaluator all launch through prepared Provider dispatch.
- Two independent Workers are observably active at the same time.
- Scope/resource conflicts never overlap.
- Candidate integration is strictly one-at-a-time.
- Worker retry, global failure repair, Evaluator repair, and supplemental evidence use separate counters.
- Every Evaluator envelope binds the immutable integration commit and evidence manifests.
- `BLOCKED` and evidence exhaustion pause; task/repair exhaustion fails.
- `SUCCEEDED` is atomically committed only after the complete Goal Gate.
- No remote, user branch, index, or user working tree changes.
