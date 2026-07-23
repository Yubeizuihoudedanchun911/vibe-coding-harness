# External Controller and Parallel Workers Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this roadmap plan-by-plan. Every implementation plan uses checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Schema 3 Skill runtime with a recoverable external Python Controller that plans finite DAGs, runs safe specialist Workers in parallel, serially integrates verified commits, and independently evaluates the immutable result.

**Architecture:** Schema 4 separates immutable Agent artifacts from one revisioned `state.json`, protects every external side effect with a prepared intent, and makes the Controller the sole state and integration-ref authority. Six ordered implementation plans isolate the independent failure models: file transactions, Provider processes, Git/worktree integration, Controller/evaluation semantics, stop/recovery, and legacy migration/cutover.

**Tech Stack:** Python 3.10+ standard library, `unittest`, Git CLI, Codex CLI subprocess adapter, JSON/Markdown/TOML packaging metadata, POSIX file locks and process groups.

## Global Constraints

- Schema 4 is a breaking product generation. Delete `SKILL.md`, `agents/openai.yaml`, `scripts/harness.py`, and the Skill contract tests only after the replacement gates pass; do not keep a compatibility shim.
- Keep the historical design documents unchanged. The approved Schema 4 design supersedes their runtime constraints without rewriting history.
- The Controller is the sole `state.json` writer and sole manager of `refs/heads/vibe/**`. A CLI process may only write an atomic `control/stop.request` outside the Controller lock protocol.
- Planner and Evaluator are source-read-only roles audited before and after execution. A Worker may modify only its declared path scope in its isolated worktree; it never stages, commits, updates refs, or writes Git metadata.
- Every Planner, Worker, and Evaluator semantic attempt starts a fresh `codex exec --ephemeral` context.
- Every Worker attempt gets a unique branch and worktree. After the Provider exits, the Controller independently audits the raw worktree delta and protected Git state, then creates at most one non-merge source commit on that task branch. Only provably independent path scopes and exclusive resources may run concurrently; integration is always serial.
- A new run requires a canonical full commit OID and a clean product baseline. There is no `--allow-dirty`; `.vibe-coding/` and Git-ignored paths are excluded from product dirtiness.
- Never modify the user's checked-out branch, index, working tree, or remotes. Never auto-merge, push, open a PR, publish, create external resources, or delete failed worktrees.
- `state.json` is the only mutable truth. Plan, task, prompt, launch, result, verification, evaluation, receipt, and migration files are immutable artifacts bound by repository-relative path and SHA-256.
- State commits require an OS run lock, expected-revision comparison, artifact-first fsync/rename, state fsync/rename, parent-directory fsync, and `revision += 1`.
- The run lock is non-reentrant. `create_run`, `execute`, `recover`, `resume`, and `recover_and_stop` acquire it; `tick`, `DispatchLedger`, `VerificationGate`, and `Integrator` require the caller to hold that same `StateStore.lock()` and never acquire it themselves. Focused tests hold the lock explicitly.
- Native creation and migration share `.vibe-coding/control/run-allocation.lock` for run-ID/ref allocation. Migration acquires `migration.lock` first and then `run-allocation.lock`; native creation acquires only `run-allocation.lock`. Neither path may reserve `RUN-NNN` outside this allocator. Every migration batch first publishes immutable `migrations/reservations/<migration-id>.json`; both allocators use the same strict parser and treat its IDs/refs as occupied even before the batch prepared manifest exists.
- `pending_dispatches` covers Planner, Worker, and Evaluator processes. `pending_source_commit` is persisted before advancing an attempt branch, `pending_integration` before advancing the run integration ref, and `pending_evaluation` between Evaluator Attempt closure and verdict routing.
- Worker start is a two-phase state protocol: persist immutable Attempt identity/time/base/branch/worktree allocation with `STARTING` and `preflight=null`; idempotently materialize the exact ref/worktree; then artifact-first attach the captured immutable preflight before preparing any Provider dispatch.
- A stale attempt token may be archived but must never change run state. PID reuse is detected with a process-start identity before any poll, signal, or recovery action.
- Keep provider retries, semantic-attempt identity, semantic failure budget, evidence rounds, and repair rounds separate. Worker `attempt_no` is monotonic per Task; Planner/Evaluator `attempt_no` is monotonic only inside a unique `operation_id` and resets for a new operation. `task_attempts` bounds `failure_count` independently for Planner output, each Worker task, and Evaluator output. A transient Provider retry stays inside the same semantic attempt; an operator cancellation freezes that attempt identity but does not increment `failure_count`.
- Verification commands are a frozen, explicitly user-authorized catalog in `FrozenRunConfig`, keyed by stable command ID, plus a frozen ordered list of required command IDs. A non-empty repository `vibe.json` catalog is rejected unless run or migration creation includes `--allow-project-commands`; the canonical source digest and authorization mode are persisted. Planner tasks and Evaluator `evidence_requests` may only reference catalog IDs; Agent output can never introduce or mutate executable paths, arguments, working directories, timeouts, or environment-variable names. Global verification always prepends every required ID, so a Plan cannot weaken the project/CLI gate.
- Catalog entries use `argv[]`, repo-relative `cwd`, a positive timeout, and an environment-variable-name allowlist. They never use a shell. Unknown, duplicate, or non-exact command IDs are contract errors and no command is executed.
- Provider secrets and environment values are never serialized. Evidence records allowed environment variable names, not secret values. Codex runs with ignored user/rule config, strict explicit config, empty MCP, disabled web/tool-network/apps/browser/computer-use/hooks/image/multi-agent/plugins/remote-plugin/proxy surfaces, an isolated HOME/auth-only CODEX_HOME, and an allowlisted minimal environment. Request, launch, and exit bind the exact CLI version and canonical execution-policy digest.
- Worker-reported paths and test results are untrusted handoff data. Reconstruct the complete raw delta, create the source commit, audit commit ancestry, changed paths, and verification results independently.
- All Git calls go through one hardened runner with a frozen environment/argv policy: disable hooks, signing, pagers, prompts, credential helpers, fsmonitor, global/system configuration, and other executable integrations. Reject repositories whose required clean/smudge/process filters cannot be safely disabled; tests prove malicious hook/filter sentinels are never executed.
- Rename source and destination, deletion, gitlink changes, and `.gitmodules` changes all participate in path-scope auditing.
- Candidate failure, conflict, path escape, merge commit, or verification failure must leave the integration ref unchanged and create a fresh same-type Worker attempt when the task remains retryable.
- Global verification and Evaluator evidence must bind the same immutable integration commit.
- Immediately before and after verification, before Evaluator result acceptance, and before the final success transaction, compare the actual run ref with the bound integration head. A no-op `git update-ref <ref> <expected> <expected>` is the final success CAS guard.
- Evaluator verdicts remain distinct: `PASS`, `NEEDS_REPAIR`, `UNVERIFIED`, and `BLOCKED`.
- `UNVERIFIED` supplements evidence and reevaluates the same commit without consuming a repair round. `BLOCKED` pauses the run.
- Defaults are `max_workers=4`, `task_attempts=3`, `provider_retries=3`, `evidence_rounds=3`, `repair_rounds=3`, and `max_plan_tasks=128`.
- The V1 runtime has no third-party Python dependencies and supports POSIX Linux/macOS. Windows support is not claimed until lock, process-group, and process-identity behavior is implemented and tested there.
- Default CI is offline and uses a Fake Provider. The real Codex CLI smoke test runs only when `VIBE_CODEX_CLI_SMOKE=1`.
- Follow RED-GREEN-REFACTOR for each task and commit after every independently reviewable green task.

### Implementation correction to the approved design

The approved design assigned source-commit creation to the Worker and allowed Agents to return complete command objects. The implementation plans deliberately move the mechanical commit responsibility to the Controller because Codex `workspace-write` does not grant write access to Git metadata, including `.git`; see the upstream [Codex workspace-write Git boundary](https://github.com/openai/codex/issues/15505). They also replace Agent-authored commands with selection from an explicitly authorized frozen catalog. These changes do not broaden product authority: the Worker still owns scoped source edits, the Controller owns audited commit/ref mechanics, and only the operator authorizes executable definitions. T06 begins by updating both corrections in the approved spec before implementation.

The exact canonical crash hooks used by all phases are:

```text
after_dispatch_intent_before_provider_start
after_provider_start_before_handle_binding
after_worker_edit_before_controller_commit
after_controller_commit_before_verification_binding
after_candidate_verification_before_pending_integration
after_pending_integration_before_update_ref
after_update_ref_before_state_completion
after_stop_request_before_worker_exit
```

These eight names are the only production runtime crash hooks. Creation and
migration have separate protocol-scoped test injectors whose exact names are
frozen in Phases 4 and 6; they are not accepted by the runtime Controller hook
dispatcher.

---

## Frozen Wire Decisions

The approved design intentionally left several implementation-level fields open. These decisions close them once for all six plans.

### Package and resources

- Distribution name: `vibe-coding-harness`.
- Unreleased package version: `0.1.0.dev0`; Schema version and package version are independent.
- Console entry point: `vibe = "vibe.cli:main"`.
- Runtime dependencies: none.
- Build backend: `setuptools.build_meta`.
- Source layout: `src/vibe/**`.
- Root `prompts/**` and `schemas/**` remain the single source of truth and are installed below `share/vibe/` with `data-files`.
- `PromptRegistry.default()` first uses a source checkout root containing both directories, then falls back to `Path(sysconfig.get_path("data")) / "share" / "vibe"`.
- Add `src/vibe/config.py`; this is the only functional module beyond the approved tree and keeps parsing/freezing out of `controller.py`.

### Supported CLI and exit codes

Public commands:

```text
vibe run [--target PATH] (--goal TEXT | --goal-file FILE)
         [--max-workers N] [--task-attempts N]
         [--provider-retries N] [--evidence-rounds N]
         [--repair-rounds N] [--allow-project-commands] [--json]
vibe resume [--target PATH] RUN_ID [--replan] [--json]
vibe status [--target PATH] RUN_ID [--json]
vibe stop [--target PATH] RUN_ID [--json]
vibe logs [--target PATH] RUN_ID [--task TASK_ID] [--follow] [--json]
vibe migrate [--target PATH] (--requirement REQ_ID | --all)
             --base COMMIT [--allow-project-commands] [--json]
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | command succeeded; `run`/`resume` reached `SUCCEEDED` or `IMPORTED_READ_ONLY` |
| `2` | usage, contract, schema, repository, or configuration error |
| `3` | foreground `run`/`resume` reached `PAUSED` |
| `4` | foreground `run`/`resume` reached `FAILED` |
| `130` | foreground `run`/`resume` reached `STOPPED` |

`status`, `stop`, `logs`, and `migrate` use `0` when their own operation succeeds, regardless of the observed run status. Human output is concise text. `--json` emits exactly one JSON object except `logs --json`, which emits JSON Lines.

### Repository and process identity

`canonical_repository_identity(target)` returns:

```text
sha256(<real target root UTF-8> + NUL + <real git common-dir UTF-8>)
```

The target root and common directory must resolve without symlink ambiguity and both must encode as strict UTF-8; unsupported local path bytes are a contract error. A moved checkout is treated as a different local repository identity and pauses resume.

`repository.base_ref` stores a symbolic `refs/**` name when available, literal `HEAD` for a detached checkout, or the full resolved OID for an explicitly selected migration base. `repository.base_sha` is always the independently resolved full OID.

`process_start_identity(pid)` returns:

- Linux: `linux:<field-22-from-/proc/<pid>/stat>`.
- macOS: `darwin:<pbi_start_tvsec>:<pbi_start_tvusec>` from
  `proc_pidinfo(PROC_PIDTBSDINFO)`, after verifying the returned PID.

The macOS kernel timeval avoids the one-second collision of `ps -o lstart`; group identity is still checked independently with `getpgid`. Unsupported platforms raise `ProviderConfigurationError`; they do not fall back to PID-only identity.

### Limits and supplemental evidence

`limits` freezes:

```json
{
  "task_attempts": 3,
  "provider_retries": 3,
  "evidence_rounds": 3,
  "repair_rounds": 3,
  "max_plan_tasks": 128
}
```

An `UNVERIFIED` Evaluator result may include `evidence_requests`, each containing only a command ID from the frozen authorized catalog. The Controller resolves and runs those exact frozen commands in a disposable worktree at the unchanged integration commit, then starts a fresh Evaluator attempt. Exceeding `evidence_rounds` enters `PAUSED` with `EVIDENCE_EXHAUSTED`; it does not fabricate `NEEDS_REPAIR`.

### State artifact and role indexes

Every run state contains these history and prepared-operation fields in addition to the approved minimum:

```json
{
  "artifact_index": [],
  "plans": [],
  "role_attempts": {
    "planner": [],
    "evaluator": []
  },
  "role_runtime": {
    "planner": {
      "operation_id": null,
      "attempt_no": 0,
      "failure_count": 0,
      "max_attempts": 3,
      "active_attempt_token": null,
      "last_error": null
    },
    "evaluator": {
      "operation_id": null,
      "attempt_no": 0,
      "failure_count": 0,
      "max_attempts": 3,
      "active_attempt_token": null,
      "last_error": null
    }
  },
  "evaluations": [],
  "verifications": [],
  "legacy_import": null,
  "pending_evaluation": null
}
```

Entries in `artifact_index`, `plans`, both `role_attempts` arrays, `evaluations`, `verifications`, and each Task `attempts` array are `ArtifactRef` objects. `StateStore.transact()` appends every supplied ArtifactRef to `artifact_index` before the lifecycle mutator runs and rejects removal, reordering, or digest changes to prior entries. Semantic indexes are also append-only:

- `plans` contains every initial/repair Plan in version order;
- `role_attempts` contains terminal Planner/Evaluator Attempt manifests;
- `evaluations` and `verifications` contain all accepted envelopes/manifests, not only the latest pointer;
- `legacy_import` is null for native runs and one `ArtifactRef` for migrated runs.

Each Task state has this exact shape:

```json
{
  "task": {
    "path": "tasks/TASK-003/task.json",
    "sha256": "sha256:1111111111111111111111111111111111111111111111111111111111111111"
  },
  "status": "READY_TO_INTEGRATE",
  "attempt_no": 2,
  "failure_count": 1,
  "max_attempts": 3,
  "active_attempt": {
    "attempt_token": "ATTEMPT-uuid",
    "status": "VERIFYING",
    "created_at": "2026-07-23T10:00:00+08:00",
    "task_base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "branch": "refs/heads/vibe/RUN-20260723-001/TASK-003-a2",
    "worktree": ".vibe-coding/worktrees/RUN-20260723-001/TASK-003-a2",
    "preflight": {
      "path": "tasks/TASK-003/attempts/002/preflight.json",
      "sha256": "sha256:abababababababababababababababababababababababababababababababab"
    },
    "provider_handle": null,
    "result_path": "tasks/TASK-003/attempts/002/result.json"
  },
  "attempts": [
    {
      "path": "tasks/TASK-003/attempts/001/attempt.json",
      "sha256": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  ],
  "result": {
    "path": "tasks/TASK-003/attempts/002/result.json",
    "sha256": "sha256:2222222222222222222222222222222222222222222222222222222222222222"
  },
  "verification": null,
  "source_commits": ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
  "integrated_commits": [],
  "last_error": null
}
```

`attempts` contains terminal Attempt manifest refs. Before dispatch, a Worker
Attempt first exists as a durable `STARTING` allocation with immutable
identity/time/base/branch/worktree and `preflight=null`; this is the only active
shape that may lack preflight or a pending dispatch. Recovery materializes or
validates exactly those recorded Git identities and attaches one immutable
preflight before dispatch. After a successful Worker handoff, the active
Attempt remains `VERIFYING` while the Controller creates and binds the source
commit and verifies the candidate. `READY_TO_INTEGRATE` therefore still has
that matching active Attempt. Only successful candidate verification freezes
the terminal Attempt and clears it in the transaction that creates
`pending_integration`; an `INTEGRATING` Task has `active_attempt=null`. Prompt,
launch, exit, result, logs, source-audit, and verification refs remain directly
protected by `artifact_index` after current pointers are cleared.

Task `attempt_no` is a monotonically increasing identity sequence and is never
reused for an immutable attempt path, branch, or worktree. Planner/Evaluator
numbering is operation-local. One exact builder returns
`roles/<planner|evaluator>/operations/<operation_id>/attempts/<NNN>` and
uniqueness is the tuple `(role, operation_id, attempt_no)`. `failure_count`
alone is compared with `max_attempts`. A `FAILED`/`ABANDONED` semantic attempt
increments it once; `CANCELLED` does not.

`attempt_token` is the current Provider-ownership fencing token, not the
semantic Attempt identity. A transient Provider retry rotates it while
preserving Worker `(task_id, attempt_no)` or read-only-role
`(role, operation_id, attempt_no)`, `created_at`, preflight, base, and failure
budget. The terminal manifest records the final owner token and its
`provider_attempts` history binds every superseded token.

Each terminal Attempt Artifact is strict Schema 1 with exactly:

```json
{
  "schema_version": 1,
  "role": "worker",
  "operation_id": "WORK-TASK-003-a2",
  "task_id": "TASK-003",
  "attempt_no": 2,
  "attempt_token": "ATTEMPT-uuid",
  "status": "SUCCEEDED",
  "created_at": "2026-07-23T10:00:00+08:00",
  "completed_at": "2026-07-23T10:05:00+08:00",
  "expected_base": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "branch": "refs/heads/vibe/RUN-20260723-001/TASK-003-a2",
  "worktree": ".vibe-coding/worktrees/RUN-20260723-001/TASK-003-a2",
  "preflight": {
    "path": "tasks/TASK-003/attempts/002/preflight.json",
    "sha256": "sha256:abababababababababababababababababababababababababababababababab"
  },
  "prompt_versions": [
    {
      "id": "workers/base",
      "version": 1,
      "sha256": "sha256:1010101010101010101010101010101010101010101010101010101010101010"
    },
    {
      "id": "workers/implementation",
      "version": 1,
      "sha256": "sha256:2020202020202020202020202020202020202020202020202020202020202020"
    }
  ],
  "provider_attempts": [],
  "request": {
    "path": "tasks/TASK-003/attempts/002/providers/001-ATTEMPT-uuid/request.json",
    "sha256": "sha256:3030303030303030303030303030303030303030303030303030303030303030"
  },
  "launch": {
    "path": "tasks/TASK-003/attempts/002/providers/001-ATTEMPT-uuid/launch.json",
    "sha256": "sha256:4040404040404040404040404040404040404040404040404040404040404040"
  },
  "stdout": {
    "path": "tasks/TASK-003/attempts/002/providers/001-ATTEMPT-uuid/stdout.log",
    "sha256": "sha256:5050505050505050505050505050505050505050505050505050505050505050"
  },
  "stderr": {
    "path": "tasks/TASK-003/attempts/002/providers/001-ATTEMPT-uuid/stderr.log",
    "sha256": "sha256:6060606060606060606060606060606060606060606060606060606060606060"
  },
  "exit": {
    "path": "tasks/TASK-003/attempts/002/providers/001-ATTEMPT-uuid/exit.json",
    "sha256": "sha256:7070707070707070707070707070707070707070707070707070707070707070"
  },
  "result": {
    "path": "tasks/TASK-003/attempts/002/result.json",
    "sha256": "sha256:2222222222222222222222222222222222222222222222222222222222222222"
  },
  "source_audit": {
    "path": "tasks/TASK-003/attempts/002/source-audit.json",
    "sha256": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  },
  "verification": {
    "path": "verification/tasks/TASK-003-a2/VERIFY-00000000-0000-4000-8000-000000000001/manifest.json",
    "sha256": "sha256:6666666666666666666666666666666666666666666666666666666666666666"
  },
  "last_error": null
}
```

Planner/Evaluator use null `task_id` and `branch`, but retain the canonical
target-relative path of their disposable detached `worktree`; terminal status
is one of `SUCCEEDED`, `FAILED`, `CANCELLED`, or `ABANDONED`. Artifact-bearing
fields are null or exact `ArtifactRef` values. StateStore verifies these
manifests after digest validation, including history ownership, token
uniqueness, active-vs-terminal exclusion, and Task result/source/verification
pointer agreement.

`preflight` binds the dispatch-time `created_at`, immutable base/worktree
identity, and the role-appropriate protected/read-only snapshot. It is never
null after dispatch preparation. The only terminal null exception is a Worker
allocation cancelled before ref/worktree materialization; that manifest must
be `CANCELLED`, have no request/launch/process/result/source/verification refs,
and carry `CANCELLED_BEFORE_MATERIALIZATION`. Every other active, pending, and
terminal view must reference the same ArtifactRef.

### State-side pending dispatch

Each `pending_dispatches[attempt_token]` has this exact shape:

```json
{
  "attempt_token": "ATTEMPT-uuid",
  "role": "worker",
  "operation_id": "WORK-TASK-003-a2",
  "task_id": "TASK-003",
  "attempt_no": 2,
  "attempt_created_at": "2026-07-23T10:00:00+08:00",
  "provider_retry_no": 0,
  "expected_base": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "branch": "refs/heads/vibe/RUN-20260723-001/TASK-003-a2",
  "worktree": ".vibe-coding/worktrees/RUN-20260723-001/TASK-003-a2",
  "provider_prefix": "tasks/TASK-003/attempts/002/providers/001-ATTEMPT-uuid",
  "prompt": {
    "path": "tasks/TASK-003/attempts/002/prompt.md",
    "sha256": "sha256:1111111111111111111111111111111111111111111111111111111111111111"
  },
  "schema": {
    "path": "tasks/TASK-003/attempts/002/output.schema.json",
    "sha256": "sha256:2222222222222222222222222222222222222222222222222222222222222222"
  },
  "preflight": {
    "path": "tasks/TASK-003/attempts/002/preflight.json",
    "sha256": "sha256:abababababababababababababababababababababababababababababababab"
  },
  "prompt_versions": [
    {
      "id": "workers/base",
      "version": 1,
      "sha256": "sha256:3333333333333333333333333333333333333333333333333333333333333333"
    },
    {
      "id": "workers/implementation",
      "version": 1,
      "sha256": "sha256:4444444444444444444444444444444444444444444444444444444444444444"
    }
  ],
  "request": {
    "path": "tasks/TASK-003/attempts/002/providers/001-ATTEMPT-uuid/request.json",
    "sha256": "sha256:5555555555555555555555555555555555555555555555555555555555555555"
  },
  "launch_path": "tasks/TASK-003/attempts/002/providers/001-ATTEMPT-uuid/launch.json",
  "stdout_path": "tasks/TASK-003/attempts/002/providers/001-ATTEMPT-uuid/stdout.log",
  "stderr_path": "tasks/TASK-003/attempts/002/providers/001-ATTEMPT-uuid/stderr.log",
  "exit_path": "tasks/TASK-003/attempts/002/providers/001-ATTEMPT-uuid/exit.json",
  "result_path": "tasks/TASK-003/attempts/002/providers/001-ATTEMPT-uuid/result.json",
  "launch": null,
  "stdout": null,
  "stderr": null,
  "exit": null,
  "result": null,
  "provider_attempts": [],
  "provider_handle": null,
  "prepared_revision": 17
}
```

`provider_handle` in state contains exactly `{adapter, attempt_token, pid, process_start_identity, process_group, child_pid, child_process_start_identity, child_process_group}`. The first PID/group identifies the durable wrapper; the second independently identifies the Codex child group. `start()` returns only after a matching launch receipt containing all eight fields plus the request-bound Codex version and execution-policy digest is durable. Absolute paths never enter state; recovery combines those identities (or the same fields from `launch.json`) with the pending entry's relative paths and the trusted run root.

For Worker dispatch, handle binding atomically writes that exact value to both
the pending entry and matching active Attempt while changing `STARTING` to
`RUNNING`. A transient Provider retry first rotates both views to null and the
fresh token in one transaction, then binds the replacement handle to both.
Worker result acceptance removes pending ownership and clears the active handle
while changing the active Attempt to `VERIFYING`.

The pending `result` is the immutable raw Provider artifact under its
token-specific provider prefix. Worker acceptance reads those already bounded
bytes, strictly parses them, and artifact-first publishes the identical bytes
once at the stable semantic `active.result_path`. `Task.result` and the
terminal Worker Attempt bind that stable ref; the final Provider raw ref
remains directly protected by `artifact_index`, and semantic validation proves
the raw and stable result digests are identical.

For Planner/Evaluator, `task_id` and `branch` are `null`; `worktree` is a disposable detached worktree. `provider_handle` and `launch` are filled in one later state transaction when `start()` returns. Recovery may reconstruct them from a complete matching launch receipt if the Controller crashed between process launch and that transaction.

Every transient Provider retry uses a fresh token and a new `providers/<retry>-<token>/` prefix while keeping the same semantic `attempt_no`. Before ownership rotates, an immutable summary of the old subattempt is appended to `provider_attempts`; that history is carried into the new pending entry and finally copied into the semantic Attempt manifest. It never overwrites an earlier request or receipt.

The terminal receipt is parsed by one strict exact-field parser. Adapter observations and ledger binding must agree byte-for-byte on token and exactly on exit code, timeout flag, result-publication flag, `stop_requested`, `stop_forced`, and both process identities. These last two fields make repeated/recovered `StopResult` deterministic. A forced stop may create an `exit.json` only with atomic create-if-absent semantics after both verified processes are dead; a racing wrapper receipt wins without overwrite.

### Pending source commit, integration, and stop wire

`pending_source_commit` is null or:

```json
{
  "operation_id": "SRC-4bd3370d-67b6-4687-a032-834951e0c522",
  "task_id": "TASK-003",
  "attempt_no": 2,
  "expected_base": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "task_ref": "refs/heads/vibe/RUN-20260723-001/TASK-003-a2",
  "tree_oid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "candidate_commit": "cccccccccccccccccccccccccccccccccccccccc",
  "author_name": "Vibe Controller",
  "author_email": "vibe-controller@localhost",
  "timestamp": "2026-07-23T10:00:00+08:00",
  "message": "vibe(RUN-20260723-001): TASK-003 attempt 2",
  "source_audit": {
    "path": "tasks/TASK-003/attempts/002/source-audit.json",
    "sha256": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  }
}
```

The Controller first audits the raw worktree/protected Git snapshot and creates only unreachable content-addressed objects through a temporary index. It derives the commit deterministically from `active_attempt.created_at`—persisted in the first start transaction before worktree creation, then bound into preflight and copied into dispatch—and the fixed identity/message. The marker timestamp must equal that Attempt field. It writes the immutable `source_audit` and commits the wire above while the Task remains `RUNNING` with active status `VERIFYING`, `result` bound, and `source_commits=[]`. It then calls `after_worker_edit_before_controller_commit` and performs `git update-ref <task_ref> <candidate_commit> <expected_base>`. The `after_controller_commit_before_verification_binding` hook follows. Recovery classifies exact task-ref values: base replays the same CAS; candidate verifies the commit object against every marker field, binds `source_audit`/`source_commits`, clears the marker, and moves the Task to `READY_TO_INTEGRATE`; any other value pauses without rollback or force. Integrator never runs while this marker exists. Before the intent transaction, a crash may leave unreachable Git objects only.

`pending_integration` is null or:

```json
{
  "operation_id": "INT-4bd3370d-67b6-4687-a032-834951e0c522",
  "task_id": "TASK-003",
  "attempt_no": 2,
  "expected_head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "candidate_head": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "source_base": "cccccccccccccccccccccccccccccccccccccccc",
  "source_head": "dddddddddddddddddddddddddddddddddddddddd",
  "verification": {
    "path": "verification/tasks/TASK-003-a2/VERIFY-00000000-0000-4000-8000-000000000001/manifest.json",
    "sha256": "sha256:6666666666666666666666666666666666666666666666666666666666666666"
  }
}
```

`pending_evaluation` is null or:

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

The first Evaluator acceptance transaction freezes the terminal Attempt and this marker together. Recovery rebuilds the byte-identical Controller envelope only from the marker plus immutable state, then the verdict transaction appends the envelope and clears the marker. No new dispatch, verification, repair, or success transition is allowed while it exists, and Goal Gate requires it to be null.

An active Controller identity is exactly `{pid, process_start_identity, process_group, controller_token}`. A StopRequest binds `run_id`, `observed_revision`, that `controller_token`, timestamp, and nonce. A revision may advance after the request, but only the same Controller token may consume it. After resume registers a fresh token, an older request is persisted as an `IGNORED_STALE_CONTROLLER` receipt and cannot stop the new Controller.

### Evaluation envelope

The Agent-authored `evaluation-v1` result is wrapped by the Controller:

```json
{
  "schema_version": 1,
  "run_id": "RUN-20260723-001",
  "evaluation_round": 2,
  "evidence_round": 1,
  "integration_head": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "goal_sha256": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
  "plan_manifest_sha256": "sha256:4444444444444444444444444444444444444444444444444444444444444444",
  "task_result_manifest_sha256": "sha256:5555555555555555555555555555555555555555555555555555555555555555",
  "verification_manifest_sha256": "sha256:6666666666666666666666666666666666666666666666666666666666666666",
  "prompt_versions": {
    "planner": [
      {
        "id": "planner",
        "version": 1,
        "sha256": "sha256:8888888888888888888888888888888888888888888888888888888888888888"
      }
    ],
    "workers": [
      {
        "task_id": "TASK-003",
        "attempt_no": 2,
        "prompts": [
          {
            "id": "workers/base",
            "version": 1,
            "sha256": "sha256:9999999999999999999999999999999999999999999999999999999999999999"
          },
          {
            "id": "workers/implementation",
            "version": 1,
            "sha256": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
          }
        ]
      }
    ],
    "evaluator": [
      {
        "id": "evaluator",
        "version": 1,
        "sha256": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      }
    ]
  },
  "evidence_catalog": {
    "verification:global:unit": {
      "kind": "global",
      "verification": {
        "path": "verification/global/VERIFY-00000000-0000-4000-8000-000000000002/manifest.json",
        "sha256": "sha256:6666666666666666666666666666666666666666666666666666666666666666"
      },
      "integration_head": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "command_id": "unit",
      "task_id": null,
      "attempt_no": null,
      "criterion_ids": ["AC-001"]
    }
  },
  "raw_result_sha256": "sha256:7777777777777777777777777777777777777777777777777777777777777777",
  "verdict": "PASS",
  "criteria": [
    {
      "id": "AC-001",
      "verdict": "PASS",
      "evidence_ids": ["verification:global:unit"]
    }
  ],
  "findings": [],
  "evidence_requests": [],
  "residual_risks": []
}
```

The envelope is immutable and `latest_evaluation` in state stores only its `ArtifactRef`, verdict, round, evidence round, and integration commit. Criterion completeness is read from the validated immutable envelope, not duplicated in state. Each criterion `evidence_id` resolves through a Controller-built evidence catalog to a bound task/global/supplemental verification entry at the same integration commit; unknown, stale-commit, or criterion-ineligible IDs fail closed.

## Shared Python Interfaces

All phase plans use these names and signatures.

### `src/vibe/models.py`

| Name | Exact contract |
|---|---|
| `VibeError` | expected base error deriving from `ValueError` |
| `ContractError` | invalid persisted, configured, or Agent-provided contract |
| `StateConflictError` | expected revision, identity, or ref no longer matches |
| `RunStatus` | `CREATED`, `PLANNING`, `EXECUTING`, `GLOBAL_VERIFYING`, `EVALUATING`, `REPAIRING`, `PAUSED`, `STOPPED`, `SUCCEEDED`, `FAILED`, `IMPORTED_READ_ONLY` |
| `TaskStatus` | `PENDING`, `READY`, `RUNNING`, `READY_TO_INTEGRATE`, `INTEGRATING`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `AttemptStatus` | `STARTING`, `RUNNING`, `VERIFYING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `ABANDONED` |
| `ProviderStatus` | `RUNNING`, `SUCCEEDED`, `FAILED` |
| `EvaluationVerdict` | `PASS`, `NEEDS_REPAIR`, `UNVERIFIED`, `BLOCKED` |
| `ArtifactRef` | immutable `path: str`, `sha256: str` |
| `CommandSpec` | immutable `id`, human-readable `purpose`, `argv`, `cwd`, `timeout_seconds`, `env_allowlist`; constructed only from trusted project/CLI config |
| `FrozenRunConfig` | immutable Provider name, worker/attempt/evidence/repair/task limits, authorized command catalog, required command IDs, and exact command-authorization mode/source digest |
| `TaskContract` | immutable task identity, objective, Worker type, coverage, dependencies, scopes, resources, checks, attempt limit |
| `PlanDocument` | immutable schema/plan versions, summary, criteria, global verification, tasks |
| `WorkerResult` | immutable task/attempt identity, status, base, reported paths/checks, risks, blocker; contains no commit/ref authority |
| `EvaluationResult` | immutable verdict, criterion results, findings, authorized evidence command IDs, risks |
| `validate_run_state(value)` | returns a deep validated `dict[str, object]` |
| `transition_run(state, target)` | returns a transitioned deep copy and preserves/clears `resume_status` |
| `goal_gate_satisfied(state, evaluation_envelope, actual_integration_head)` | returns `True` only for the complete atomic success predicate, direct criterion evidence, no blocking finding, and actual-ref equality |

### `src/vibe/state_store.py`

| Method | Exact signature |
|---|---|
| `StateStore.for_run` | `(target: Path, run_id: str) -> StateStore` |
| `StateStore.lock` | `(*, blocking: bool = True) -> context manager` |
| `StateStore.prepare_artifact` | `(relative_path: str, body: bytes) -> ArtifactRef`; requires the run lock and may leave only an orphan on crash |
| `StateStore.create` | `(initial_state: dict[str, object], artifacts: Mapping[str, bytes]) -> dict[str, object]` |
| `StateStore.load` | `() -> dict[str, object]`; validates every bound ArtifactRef by default |
| `StateStore.transact` | `(expected_revision: int, artifacts: Mapping[str, bytes], mutate: Callable) -> dict[str, object]` |
| `StateStore.append_log` | `(event: Mapping[str, object]) -> None` |

### `src/vibe/providers/base.py`

| Method | Exact signature |
|---|---|
| `ProviderAdapter.execution_identity` | `() -> ProviderExecutionIdentity`; returns exact Codex CLI version plus canonical execution-policy digest before a durable request is written |
| `ProviderAdapter.start` | `(request: ProviderRequest) -> ProviderHandle` |
| `ProviderAdapter.poll` | `(handle: ProviderHandle) -> ProviderStatus` |
| `ProviderAdapter.stop` | `(handle: ProviderHandle, grace_period: float) -> StopResult` |
| `ProviderAdapter.completion` | `(handle: ProviderHandle) -> ProviderCompletion`; exposes terminal exit, timed-out, `result_published`, and failure evidence without requiring success |
| `ProviderAdapter.result` | `(handle: ProviderHandle) -> ProviderResult` |

### Git, scheduling, and verification

| Method | Exact signature |
|---|---|
| `WorktreeManager.assert_clean_baseline` | `(base: str = "HEAD") -> RepositoryBaseline` |
| `WorktreeManager.create_run_ref` | `(run_id: str, base_sha: str) -> str` |
| `WorktreeManager.create_task_worktree` | `(run_id: str, task_id: str, attempt_no: int, base_sha: str) -> TaskWorktree` |
| `WorktreeManager.create_disposable_worktree` | `(run_id: str, purpose: str, commit_sha: str) -> Path` |
| `WorktreeManager.snapshot_protected_git` | `(excluded_task_ref: str) -> ProtectedGitSnapshot`; captures user HEAD/index/status, refs outside the exact attempt branch, packed refs, config, and remote URLs |
| `WorktreeManager.prepare_source_commit` | `(contract: TaskContract, worktree: TaskWorktree, preflight: AttemptPreflight, metadata: SourceCommitMetadata) -> PreparedSourceCommit`; verifies the persisted dispatch snapshot, audits raw changes, and creates only unreachable tree/commit objects through a temporary index |
| `WorktreeManager.apply_source_commit_cas` | `(pending: PendingSourceCommit) -> None`; advances only the exact task ref from expected base to deterministic candidate |
| `WorktreeManager.classify_source_commit_recovery` | `(pending: PendingSourceCommit, actual_task_head: str) -> SourceCommitRecovery` |
| `WorktreeManager.update_ref` | `(ref: str, candidate: str, expected: str) -> None` |
| `Scheduler.validate_plan` | `(document: PlanDocument, config: FrozenRunConfig, prior_plans: Sequence[PlanDocument]) -> None` |
| `Scheduler.promote_ready` | `(state: dict[str, object], plan: PlanDocument) -> list[str]` |
| `Scheduler.dispatchable` | `(state: dict[str, object], plan: PlanDocument) -> list[str]` |
| `VerificationGate.run` | `(commit_sha: str, worktree: Path, command_ids: Sequence[str], artifact_prefix: str) -> VerificationResult`; resolves only the frozen catalog |
| `Integrator.prepare` | `(state: dict[str, object], contract: TaskContract, source: SourceAudit) -> CandidateIntegration` |
| `Integrator.integrate` | `(contract: TaskContract, source: SourceAudit) -> CandidateIntegration` |
| `Integrator.apply_cas` | `(pending: PendingIntegration) -> None` |
| `Integrator.classify_recovery` | `(pending: PendingIntegration, actual_head: str) -> IntegrationRecovery` |

### Controller and CLI

| Method | Exact signature |
|---|---|
| `Controller.create_run` | `(goal: str, config: FrozenRunConfig) -> str` |
| `Controller.execute` | `(run_id: str, *, replan: bool = False) -> dict[str, object]` |
| `Controller.request_stop` | `(run_id: str) -> StopRequest` |
| `Controller.recover` | `(run_id: str, *, replan: bool = False) -> dict[str, object]` |
| `Controller.resume` | `(run_id: str, *, replan: bool = False) -> dict[str, object]`; one lock-owned reconcile/register/drive incarnation |
| `build_parser` | `() -> argparse.ArgumentParser` |
| `main` | `(argv: Sequence[str] | None = None) -> int` |

## Ordered Plan Set

| Order | Plan | Global tasks | Review gate |
|---|---|---|---|
| 1 | `2026-07-23-external-controller-01-schema4-foundation.md` | T01–T03 | package, models, StateStore, frozen config |
| 2 | `2026-07-23-external-controller-02-prompts-providers-runners.md` | T04, T05, T07 | resources, durable Provider protocol, role runners |
| 3 | `2026-07-23-external-controller-03-git-scheduler-integration.md` | T06, T08–T11 | clean Git/worktrees, DAG, scheduler, verification, CAS |
| 4 | `2026-07-23-external-controller-04-controller-evaluation-loop.md` | T12–T13 | end-to-end main loop, repair, independent evaluation |
| 5 | `2026-07-23-external-controller-05-recovery-cli.md` | T14–T15 | stop/resume fault recovery and public CLI |
| 6 | `2026-07-23-external-controller-06-migration-cutover.md` | T16–T17 | explicit Schema 3 migration, breaking cutover, release gates |

Execution dependencies:

```text
Plan 01
  ├── Plan 02
  └── Plan 03 foundation lanes
Plan 02 + Plan 03
  -> Plan 04
  -> Plan 05
Plan 01 + Plan 03 repository layer
  -> Plan 06 migration lane
Plans 01-05 + Plan 06 migration lane
  -> Plan 06 breaking cutover
```

Within one working session, do not run tasks that edit the same file in parallel. The plan-level parallel opportunities are explicitly called out in the relevant plan.

## Spec Coverage Matrix

| Approved design area | Owning tasks |
|---|---|
| package and external CLI | T01, T15, T17 |
| Schema 4 state, Task/Attempt split, counters | T01–T03 |
| atomic artifacts and revision CAS | T02 |
| Prompt Registry and specialist overlays | T04 |
| Provider Adapter and Codex CLI wrapper | T05 |
| prepared Agent dispatch | T07, T14 |
| clean baseline, hardened Git runner, run/task refs, protected-state audit, Controller-owned source commit | T06 |
| Planner finite DAG and repair append-only rules | T08 |
| path/resource-safe parallel Scheduler | T09 |
| frozen command catalog and task/global deterministic verification | T03, T08, T10, T13 |
| candidate cherry-pick and Git CAS | T11, T14 |
| Controller foreground lifecycle | T12 |
| Evaluator envelope, verdict routing, Goal Gate | T13 |
| stop nonce, process identity, crash recovery | T14 |
| run/resume/status/stop/logs/migrate UX | T15, T16 |
| explicit Schema 3 validation, backup, mapping | T16 |
| removal of Skill/runtime script and docs/CI | T17 |

## Cross-Plan Completion Gate

Do not perform the breaking deletion in T17 until all of these are true:

1. Package, models, config, and StateStore tests pass on Python 3.10.
2. Prompt/schema resources load from both source checkout and installed wheel.
3. Fake Provider exercises Planner, multiple Worker types, and Evaluator without network.
4. Real Git tests prove independent Worker concurrency and serialized candidate integration.
5. Every specified dispatch and integration crash window resumes without duplication.
6. Evaluator `PASS`, `NEEDS_REPAIR`, `UNVERIFIED`, and `BLOCKED` routes are independently tested.
7. `stop` cannot double-write state and cannot signal a PID-reused process.
8. Schema 3 migration validates and preserves the existing evidence model before the old runtime is removed.
9. Default offline suite passes and the real Codex smoke remains opt-in.
10. README, README.zh-CN, CONTRIBUTING, SECURITY, CHANGELOG, CI, and repository-health assertions describe the live Schema 4 product.
