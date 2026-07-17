# Schema V3 Evaluation Transactions Design

Date: 2026-07-17

## Goal

Strengthen the harness so that:

- an interrupted review write can be resumed deterministically;
- `ACCEPTED` is bound to the exact evaluated repository snapshot, not only `HEAD`;
- a PASS review contains machine-checkable criterion results and evidence;
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
    "revision": "full-commit-oid",
    "workspace_fingerprint": "sha256:...",
    "review_sha256": ""
  },
  "residual_risks": []
}
```

Rules:

- `evaluation` is null outside an active or completed evaluation.
- `begin-evaluation` creates `evaluation` and clears `latest_verdict`.
- `record-review` fills `review_sha256` and applies the verdict transition.
- `accepted_revision` is empty unless `status=ACCEPTED`.
- `ACCEPTED` requires `accepted_revision == evaluation.revision == HEAD`.
- `check --final` also requires the current workspace fingerprint and review hash to equal the values in `evaluation`.
- Schema 2 state is rejected with an explicit no-migration error.

`last_good_revision` is removed. It conflates the requirement's initial revision with the snapshot actually evaluated.

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
4. every untracked path, file mode, and content digest;
5. submodule status exposed by Git.

All commands disable external diff and text-conversion helpers. Paths under `.vibe-coding/` are excluded because recording harness state must not invalidate the evaluated product snapshot.

The command returns only the digest and revision; it does not expose repository file contents.

Any change to tracked, staged, unstaged, or untracked repository content outside `.vibe-coding/` changes the fingerprint. Existing unrelated dirty changes are allowed, but they become part of the evaluated snapshot. If they change after evaluation, the requirement must be evaluated again.

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

- status is `ACTIVE`, or is already `ACCEPTED` with the same evaluation for an idempotent recheck;
- phase is `BUILDING`;
- non-empty `plan.md` and current `implementation.md` exist;
- current `review.md` does not exist.

Effects:

- capture the current snapshot;
- set `phase=EVALUATING`;
- set `latest_verdict=null`;
- populate `evaluation` with empty `review_sha256`;
- set `next_action` to record the Evaluator review.

The returned snapshot is included in the Evaluator task.

### `record-review`

```bash
python3 scripts/harness.py record-review \
  --target "$TARGET_ROOT" --requirement REQ-NNN \
  --review-source /path/to/evaluator-review.md
```

The command:

1. reads and validates the complete review source;
2. requires its evaluation record to match the pending snapshot;
3. requires the current workspace fingerprint still to match;
4. writes `review.md` through a temporary file and atomic rename;
5. computes the exact review SHA-256;
6. applies the verdict transition to `state.json`.

Verdict transitions:

- `PASS`: remain `ACTIVE/EVALUATING`, set `latest_verdict=PASS`, and require the Goal Gate next.
- `UNVERIFIED`: remain on the same round in `ACTIVE/EVALUATING`.
- `FAIL`: preserve the failed review, increment `active_round`, enter `ACTIVE/BUILDING`, and clear `evaluation`.

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

If the snapshot changed, `accept` fails without mutating state. Root starts a new evaluation attempt against the new snapshot.

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
  "schema_version": 1,
  "revision": "full-commit-oid",
  "workspace_fingerprint": "sha256:...",
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
      "result": "Created report.csv with the expected header and rows."
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
- command evidence requires a non-empty command, integer exit code, and non-empty result;
- inspection evidence requires a non-empty subject and result;
- a bare command, path, or “tests passed” sentence is not sufficient;
- the review revision and fingerprint must match `state.evaluation`.

Human-readable details may follow under one `## Attempts` section. The JSON evaluation record is the machine authority.

## Interrupted-write recovery

`record-review` writes the validated review before updating state. This intentionally leaves one recoverable crash window:

```text
phase=EVALUATING
latest_verdict=null
review.md exists and is complete
```

`init --resume` detects this shape before normal validation. It parses the review, verifies it against `state.evaluation`, computes its hash, and applies the same deterministic transition as `record-review`.

Because the final review rename is atomic, an incomplete review never appears at `review.md`. A malformed or snapshot-mismatched review is not reconciled; resume reports an actionable error and preserves every artifact.

If state already contains a review hash but `review.md` is missing or differs, validation fails without reconstructing evidence.

## Role boundary audit

Planner and Evaluator remain instruction-level read-only roles because current `spawn_agent` does not expose a role-specific filesystem sandbox.

Root therefore:

1. records a snapshot before dispatch;
2. runs the role strictly serially;
3. records a snapshot after `wait_agent`;
4. requires the two snapshots to match before accepting the role output.

For Evaluator, `record-review` performs the after-snapshot comparison. For Planner, Root compares two `snapshot` outputs.

If the snapshot changes, Root marks the requirement `BLOCKED`, preserves the diff, and reports that repository drift occurred during a read-only role. The Skill must not claim which actor wrote the change without separate evidence.

## Error handling

- Snapshot command failure blocks evaluation; no partial state is written.
- Invalid review input does not create or replace `review.md`.
- Snapshot drift during evaluation returns an error and leaves the requirement in `EVALUATING` with no verdict.
- Review reconciliation never accepts malformed or mismatched evidence.
- `accept` is idempotent only when the accepted snapshot and review remain unchanged.
- Existing schema 2 requirements fail fast with an explicit unsupported-schema message.

## Testing

Implementation follows RED-GREEN-REFACTOR.

Required automated tests:

- initialization writes schema 3 and the new fields;
- schema 2 is rejected;
- snapshot changes for tracked, staged, unstaged, and untracked product changes;
- `.vibe-coding/` changes do not change the snapshot;
- `begin-evaluation` captures the exact pending snapshot;
- `record-review` rejects revision, fingerprint, criteria, evidence, and verdict mismatches;
- bare commands and result paths are rejected as PASS evidence;
- interrupted review persistence is reconciled by `init --resume`;
- `accept` rejects any post-evaluation workspace drift;
- `check --final` verifies revision, fingerprint, and review hash;
- Skill text requires before/after read-only role audits and explicit lifecycle commands.

Required behavioral verification:

- a pressured Root must not accept an old PASS after workspace drift;
- a resumed Root must reconcile a complete persisted review instead of deleting it;
- a read-only role that changes repository content must cause `BLOCKED`;
- a PASS lacking real-path evidence must be rejected or returned to Evaluator as UNVERIFIED.

## Documentation updates

Update `SKILL.md`, both READMEs, `CHANGELOG.md`, and `agents/openai.yaml` where needed so that public claims match the audited, not sandbox-enforced, role boundary.
