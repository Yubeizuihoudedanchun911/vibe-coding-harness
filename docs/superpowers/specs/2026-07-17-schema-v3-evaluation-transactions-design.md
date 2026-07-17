# Schema V3 Evaluation Transactions Design

Date: 2026-07-17

## Goal

Strengthen the harness so that:

- an interrupted review write can be resumed deterministically;
- `ACCEPTED` is bound to the exact evaluated repository snapshot, not only `HEAD`;
- evaluation identity includes the requirement, round, goal, plan, and implementation bytes;
- a PASS review contains machine-checkable criterion results and evidence;
- historical review, failure, interruption, and transaction-input records are tamper-evident;
- Planner and Evaluator read-only behavior is audited even when the host cannot enforce role-specific filesystem permissions.

Schema 2 compatibility and migration are intentionally out of scope. Existing schema 2 requirements are rejected after this change.

## Non-goals

- Replacing the requirement directory with an append-only event store.
- Adding language-specific test policies or universal acceptance criteria.
- Identifying whether repository drift came from an agent, the user, or another process.
- Persisting agent handles across Root sessions.
- Enforcing role-specific operating-system sandboxes when the host does not expose that capability.

## Selected approach

Keep `scripts/harness.py` as the single runtime, but move evaluation lifecycle mutations behind explicit commands:

- `snapshot`
- `begin-evaluation`
- `record-review`
- `restart-evaluation`
- `accept`

Root may continue to persist Planner and Generator prose artifacts, but it must not manually construct evaluation or acceptance state.

## Schema 3 state

`state.json` uses this minimum shape:

```json
{
  "schema_version": 3,
  "requirement_id": "REQ-001",
  "goal": "User-visible goal",
  "status": "ACTIVE",
  "phase": "EVALUATING",
  "active_round": 1,
  "next_action": "Record the Evaluator review.",
  "latest_verdict": null,
  "accepted_revision": "",
  "evaluation": {
    "requirement_id": "REQ-001",
    "round": 1,
    "goal": "User-visible goal",
    "revision": "full-commit-oid",
    "workspace_fingerprint": "sha256:...",
    "goal_sha256": "sha256:...",
    "plan_sha256": "sha256:...",
    "implementation_sha256": "sha256:...",
    "acceptance_criteria": ["AC-001"],
    "review_sha256": ""
  },
  "residual_risks": [],
  "failed_evaluations": [],
  "review_attempts": [],
  "interruption_history": [],
  "pending_evaluation": null,
  "pending_review": null,
  "pending_interruption": null
}
```

Rules:

- `evaluation` is null outside an active or completed evaluation.
- `begin-evaluation` creates the full transaction identity and clears `latest_verdict`.
- `record-review` fills `review_sha256` and applies the verdict transition.
- `restart-evaluation` preserves a drifted attempt and enters a fresh build round.
- `failed_evaluations`, `review_attempts`, and `interruption_history` are ordered, digest-bound receipts for historical files.
- `pending_evaluation`, `pending_review`, and `pending_interruption` are two-phase commit markers. Ordinary validation rejects unresolved markers; only their matching lifecycle command may reconcile them.
- `accepted_revision` is empty unless `status=ACCEPTED`.
- `ACCEPTED` requires `accepted_revision == evaluation.revision == HEAD`.
- `check --final` also rechecks the goal, plan, implementation, archived transaction inputs, workspace fingerprint, review hash, and every historical receipt.
- Persisted free-form strings must be non-empty valid Unicode scalar text; surrogate escapes are rejected before any write.
- `active_round` is bounded to `1..999`; a transition that would exceed the limit fails before any lifecycle file is written.
- Schema 2 state is rejected with an explicit no-migration error.

`last_good_revision` is removed. It conflates the requirement's initial revision with the snapshot actually evaluated.

Each round uses this durable layout:

```text
rounds/NNN/
├── implementation.md
├── evaluation-inputs/
│   ├── plan.md
│   └── implementation.md
├── attempts/
│   └── NNN.md
├── review.md
└── interruption.json
```

`implementation.md` is the current Generator handoff. `evaluation-inputs/`
contains the exact plan and handoff bytes frozen by `begin-evaluation`.
`attempts/` contains exact review bytes replaced on the same transaction.
The corresponding hashes and transaction identity remain in `state.json`.

## Repository snapshot

The snapshot contains:

```json
{
  "revision": "full-commit-oid",
  "workspace_fingerprint": "sha256:..."
}
```

The fingerprint is a SHA-256 digest over a canonical byte stream containing:

1. `git status --porcelain=v2 -z --untracked-files=all`;
2. the staged binary diff against `HEAD`;
3. the unstaged binary diff;
4. every tracked non-gitlink worktree path's raw lstat type, mode, and bytes;
5. every non-ignored untracked path, file mode, and content digest;
6. each initialized submodule's revision and recursively computed exact workspace fingerprint.

All commands disable external diff and text-conversion helpers. Raw tracked bytes are hashed independently so clean filters, `assume-unchanged`, and similar Git presentation controls cannot hide content changes. An absent or empty uninitialized submodule is represented canonically; a non-empty path that is not an independent submodule worktree is rejected. Paths under `.vibe-coding/` are excluded because recording harness state must not invalidate the evaluated product snapshot.

The command returns only the digest and revision; it does not expose repository file contents.

Any change to tracked, staged, unstaged, or non-ignored untracked repository
content outside `.vibe-coding/` changes the fingerprint. Git-ignored content is
not part of the product fingerprint; command evidence records generated-output
checks separately. Existing unrelated dirty changes are allowed, but they
become part of the evaluated snapshot. If they change after evaluation, the
requirement must be evaluated again.

## Lifecycle commands

### `snapshot`

Read-only command:

```bash
python3 scripts/harness.py snapshot --target "$TARGET_ROOT"
```

Root uses it before and after read-only roles. A changed fingerprint means the read-only boundary was violated or external drift occurred. Root records `BLOCKED` with the before/after snapshots and does not attribute the writer without evidence.

### `begin-evaluation`

```bash
python3 scripts/harness.py begin-evaluation \
  --target "$TARGET_ROOT" --requirement REQ-NNN
```

Preconditions:

- status is `ACTIVE`;
- phase is `BUILDING`;
- non-empty `plan.md` and current `implementation.md` exist;
- current `review.md` does not exist.

Effects:

- read the exact plan and implementation, capture the current snapshot, and
  construct the complete evaluation transaction;
- persist `pending_evaluation` with the exact input bodies and transaction
  before touching `evaluation-inputs/`;
- atomically create or replace archived `plan.md` and `implementation.md` bytes
  under `evaluation-inputs/`;
- recheck the live and archived inputs against the prepared transaction;
- set `phase=EVALUATING`;
- set `latest_verdict=null`;
- populate `evaluation` with requirement ID, round, exact goal text, revision,
  workspace fingerprint, goal/plan/implementation hashes, acceptance IDs, and
  an empty `review_sha256`;
- set `next_action` to record the Evaluator review.

The complete returned evaluation object is included in the Evaluator task. If
the operation stops after preparing state or either archive write, rerunning
`begin-evaluation` completes the matching transaction. If current inputs
changed, the same command first replaces the uncommitted marker and archives
with a new transaction. `init --resume` intentionally reports, but does not
reconcile, `pending_evaluation`.

### `record-review`

```bash
python3 scripts/harness.py record-review \
  --target "$TARGET_ROOT" --requirement REQ-NNN \
  --review-source /path/to/evaluator-review.md
```

The review source is a temporary UTF-8 file outside `TARGET_ROOT`; otherwise
creating the source would itself change the pending product snapshot.

The command:

1. reads and validates the complete review source;
2. requires its evaluation record to match the complete pending transaction;
3. rehashes the current and archived transaction inputs;
4. writes a `pending_review` marker containing the exact proposed bytes and digest;
5. archives the exact prior `review.md` under `attempts/` when replacing one;
6. writes `review.md` through a temporary file and atomic rename;
7. applies the verdict transition and clears `pending_review`.

The first review requires `latest_verdict=null` and no `review.md`. A later
evidence attempt may replace `review.md` while the requirement remains on the
same transaction with `latest_verdict=PASS` or `UNVERIFIED`. The replacement is
a complete review, not a partial append. Exact prior bytes are stored at
`attempts/NNN.md`, while `state.review_attempts` repeats the complete transaction
binding and expected review hash for that archive.

Verdict transitions:

- `PASS`: remain `ACTIVE/EVALUATING`, set `latest_verdict=PASS`, and require the Goal Gate next.
- `UNVERIFIED`: remain on the same round in `ACTIVE/EVALUATING`.
- `FAIL`: preserve the failed review, append its full transaction and review
  hash to `failed_evaluations`, increment `active_round`, enter
  `ACTIVE/BUILDING`, and clear `evaluation`.

### `restart-evaluation`

```bash
python3 scripts/harness.py restart-evaluation \
  --target "$TARGET_ROOT" --requirement REQ-NNN \
  --reason "Describe the observed workspace drift"
```

Preconditions:

- status is `ACTIVE` or `BLOCKED`;
- phase is `EVALUATING`;
- latest verdict is null, `PASS`, or `UNVERIFIED`;
- the current snapshot differs from `state.evaluation`, or a current-review artifact observation no longer matches its recorded bytes;
- `reason` is non-empty and any recorded review remains valid.

The command first stores the complete interruption record as
`pending_interruption`, then atomically writes the same record to the current
round's `interruption.json`. The record contains schema version 2, the reason,
prior verdict, full prior evaluation object, observed revision/workspace and
goal/plan/implementation states, plus a machine-readable `artifact_drift` list.
Each artifact drift entry records the canonical product path, expected SHA-256,
and observed digest or file state. Schema 1 interruption records are
unsupported.

After the file is durable, the command records its exact digest in
`interruption_history`, increments `active_round`, enters `ACTIVE/BUILDING`,
clears `evaluation`, `latest_verdict`, and the pending marker, and requires a
new implementation handoff before `begin-evaluation`.

If the current plan is missing, non-regular, unreadable, or no longer has valid
acceptance IDs, the command still persists the prepared interruption and exact
observed plan state, then enters `BLOCKED/EVALUATING`. After repairing
`plan.md`, rerunning `restart-evaluation` with the same reason completes the
recorded transition. A missing or otherwise changed implementation handoff is
historical drift; the archived implementation input remains authoritative.

A review already bound to the prior transaction remains immutable. An attempt
interrupted before review is valid history through `interruption.json`.
Rerunning the command with the same reason completes either two-phase crash
window. An orphan `interruption.json` without its prepared state marker is
rejected rather than trusted. Rerunning after completion is idempotent.

### `accept`

```bash
python3 scripts/harness.py accept \
  --target "$TARGET_ROOT" --requirement REQ-NNN
```

Preconditions:

- status is `ACTIVE`;
- phase is `EVALUATING`;
- latest verdict is `PASS`;
- review metadata, review hash, and state agree;
- every planned acceptance criterion is PASS;
- the current revision and workspace fingerprint equal the evaluated snapshot.

Effects:

- set `status=ACCEPTED`;
- set `accepted_revision` to the evaluated full commit OID;
- set an actionable terminal `next_action`;
- run the same invariants as `check --final`.

An already accepted requirement that still satisfies every invariant is returned unchanged.

If the snapshot changed, `accept` fails without mutating state. Root runs
`restart-evaluation`, persists a new implementation handoff, and starts a new
snapshot-bound evaluation.

## Review contract

Planner assigns stable acceptance identifiers under exactly one section:

```markdown
## Acceptance criteria

- AC-001: CSV export is available from the public CLI.
- AC-002: Quoting and UTF-8 data round-trip correctly.
```

Evaluator returns one authoritative JSON record under exactly one `## Evaluation record` heading:

````markdown
## Evaluation record

```json
{
  "schema_version": 2,
  "requirement_id": "REQ-001",
  "round": 1,
  "revision": "full-commit-oid",
  "workspace_fingerprint": "sha256:...",
  "goal_sha256": "sha256:...",
  "plan_sha256": "sha256:...",
  "implementation_sha256": "sha256:...",
  "verdict": "PASS",
  "criteria": [
    {
      "id": "AC-001",
      "verdict": "PASS",
      "evidence_ids": ["EV-001"]
    }
  ],
  "evidence": [
    {
      "id": "EV-001",
      "kind": "command",
      "command": "tool export --format csv",
      "exit_code": 0,
      "summary": "The export command created the expected report.",
      "observations": [
        {
          "kind": "exact",
          "name": "stdout",
          "value": "Created report.csv"
        },
        {
          "kind": "artifact",
          "path": "report.csv",
          "sha256": "sha256:..."
        },
        {
          "kind": "metric",
          "name": "data_rows",
          "value": 42,
          "unit": "rows"
        }
      ]
    }
  ],
  "residual_risks": []
}
```
````

Validation rules:

- criterion IDs must exactly match the IDs in `plan.md`;
- every criterion appears once;
- the overall verdict is derived from criterion verdicts:
  - any FAIL means FAIL;
  - otherwise any UNVERIFIED means UNVERIFIED;
- otherwise all criteria must PASS;
- every PASS criterion references at least one existing evidence item;
- evaluation-record schema 2 is required; schema 1 is rejected without compatibility;
- command evidence requires a non-empty command, integer exit code, summary, and non-empty typed observations;
- inspection evidence requires a non-empty subject, summary, and non-empty typed observations;
- summaries are explanatory and never satisfy evidence by themselves;
- an `exact` observation names a non-empty exact string, a `metric` observation names a finite number and unit, and an `artifact` observation binds a canonical repository-relative product path to the SHA-256 of its current regular-file bytes;
- `record-review`, current-round validation, and acceptance re-read artifact bytes; historical rounds validate the persisted artifact path/digest structure without comparing it to a path that a later build round may legitimately replace;
- the review requirement ID, round, revision, fingerprint, and
  goal/plan/implementation hashes must exactly match `state.evaluation`.

Human-readable details may follow the record, but the JSON evaluation record is
the machine authority. Replacing the record never rewrites history: the runtime
archives the complete prior review as a numbered attempt and binds it through
`state.review_attempts`.

## Interrupted-write recovery

`begin-evaluation` writes `pending_evaluation` before either input archive.
The marker contains the exact plan and implementation bodies plus the complete
candidate evaluation object. A retry validates the marker, compares it to the
current goal, inputs, revision, and workspace, then either completes it or
state-first prepares a replacement. Orphan archive bytes are never accepted as
transaction authority. Recovery requires rerunning `begin-evaluation`;
`init --resume` returns the marker-specific error.

`record-review` writes a prepared state marker before changing review history or
`review.md`. This intentionally leaves recoverable crash shapes:

```text
phase=EVALUATING
pending_review contains exact proposed body and digest
review.md is missing or still contains the prior review
```

```text
phase=EVALUATING
pending_review contains exact proposed body and digest
attempt archive and/or review.md already contains prepared bytes
```

`init --resume` detects both shapes before normal validation. It validates the
prepared bytes against `state.evaluation`, creates or verifies any required
attempt archive and `review.md`, applies the same transition, and clears the
marker. A direct retry of `record-review` is accepted only when its source
matches the prepared digest.

Because each file replacement is atomic, an incomplete review never appears at
`review.md`. A malformed or transaction-mismatched prepared review is not
reconciled; resume reports an actionable error and preserves every artifact.

If state has no prepared marker, an orphan `review.md` is not treated as a
recoverable transaction. If committed state already contains a review hash but
`review.md` is missing or differs, validation fails without reconstructing
evidence. `restart-evaluation` uses the equivalent
`pending_interruption`/`interruption.json` protocol and must be rerun with the
same reason to reconcile its pending marker.

## Role boundary audit

Planner and Evaluator remain instruction-level read-only roles because current `spawn_agent` does not expose a role-specific filesystem sandbox.

Root therefore:

1. records a snapshot before dispatch;
2. runs the role strictly serially;
3. records a snapshot after `wait_agent`;
4. requires the two snapshots to match before accepting the role output.

For Evaluator, `record-review` performs the after-snapshot comparison. For Planner, Root compares two `snapshot` outputs.

If the snapshot changes, Root marks the requirement `BLOCKED`, preserves the diff, and reports that repository drift occurred during a read-only role. The Skill must not claim which actor wrote the change without separate evidence.

Blocking may change only ordinary orchestration fields. When a review already
exists, state-level drift risks are appended after the review's
`residual_risks`; the recorded review risks, evaluation object, verdict,
acceptance fields, and review bytes remain unchanged.

## Error handling

- Snapshot command failure blocks evaluation; no partial state is written.
- Invalid review input does not create or replace `review.md`.
- Snapshot drift during evaluation returns an error and leaves the requirement in `EVALUATING` with no verdict.
- Drift recovery requires `restart-evaluation`; it never silently clears or reuses an old evaluation.
- Pending evaluation/review/interruption markers block unrelated lifecycle commands until their matching operation reconciles them.
- Orphan lifecycle files and mutated historical receipts fail closed.
- Review reconciliation never accepts malformed or mismatched evidence.
- `accept` is idempotent only when the accepted snapshot and review remain unchanged.
- `FAIL` and restart transitions reject round 999 before writing lifecycle files.
- filesystem, JSON numeric, and Unicode failures are normalized to structured JSON errors without tracebacks.
- Existing schema 2 requirements fail fast with an explicit unsupported-schema message.

## Testing

Implementation follows RED-GREEN-REFACTOR.

Required automated tests:

- initialization writes schema 3 and the new fields;
- schema 2 is rejected;
- snapshot changes for tracked, staged, unstaged, and untracked product changes;
- `.vibe-coding/` changes do not change the snapshot;
- `begin-evaluation` captures the exact pending snapshot;
- `begin-evaluation` freezes and revalidates exact plan and implementation input bytes;
- an interrupted `begin-evaluation` can replace its uncommitted transaction and archives when current inputs changed;
- `record-review` rejects requirement, round, goal/plan/implementation hash, revision, fingerprint, criteria, evidence, and verdict mismatches;
- prepared review recovery preserves replacements and rejects forged orphan reviews;
- `restart-evaluation` preserves pre-review and post-PASS drift, is crash-recoverable, and requires real drift plus a reason;
- prepared interruption recovery rejects forged orphan files and binds exact interruption bytes in history;
- restart records missing/invalid plan and missing implementation states before blocking or advancing;
- snapshot changes when a textconv-masked tracked file or dirty submodule content changes;
- schema 1/free-text-only evidence is rejected, while exact HTTP/MIME values, finite metrics, and hash-bound artifact paths are accepted;
- interrupted review persistence is reconciled by `init --resume`;
- replaced review attempts, failed evaluations, archived inputs, and interruption history are tamper-evident;
- `accept` rejects any post-evaluation product or transaction-input drift;
- `check --final` verifies all transaction hashes, current evidence, and historical receipts;
- pathological rounds and expected filesystem/Unicode failures remain bounded, structured errors;
- Skill text requires before/after read-only role audits and explicit lifecycle commands.

Required behavioral verification:

- a pressured Root must not accept an old PASS after workspace drift;
- a resumed Root must reconcile a complete persisted review instead of deleting it;
- a read-only role that changes repository content must cause `BLOCKED`;
- a PASS lacking real-path evidence must be rejected or returned to Evaluator as UNVERIFIED.

## Documentation updates

Update `SKILL.md`, both READMEs, `CHANGELOG.md`, and `agents/openai.yaml` where needed so that public claims match the audited, not sandbox-enforced, role boundary.
