---
name: vibe-coding-harness
description: Use when a software goal spans sessions and needs role-separated planning, implementation, independent evaluation, and durable recovery evidence.
---

# Vibe Coding Harness

Run software goals through schema 3 artifacts and isolated roles.

## Contract

- Treat Git, live behavior, and applicable `AGENTS.md` files as truth.
- Root only orchestrates agents, persists harness artifacts, validates the Goal Gate, and reports status. Root never writes business code.
- Planner and Evaluator are instruction-level read-only roles. Generator is the only business-code writer.
- After dispatch, Root uses `wait_agent`; roles run serially. Preserve unrelated changes and use scoped commits.
- If multi-agent tools are unavailable, record `BLOCKED`; never fall back to Root implementation.
- Schema 3 is a breaking format. Do not migrate or accept schema 2 state.

## Start or resume

Resolve this directory as `SKILL_ROOT` and the target Git root as `TARGET_ROOT`.

```bash
python3 "$SKILL_ROOT/scripts/harness.py" init \
  --target "$TARGET_ROOT" --goal "<user-visible goal>"

python3 "$SKILL_ROOT/scripts/harness.py" init --resume \
  --target "$TARGET_ROOT" --requirement REQ-NNN

python3 "$SKILL_ROOT/scripts/harness.py" snapshot --target "$TARGET_ROOT"
```

On resume, read all requirement artifacts, repository instructions, and Git status. Trust files over chat.

## Durable layout

```text
.vibe-coding/requirements/REQ-NNN/
├── state.json
├── plan.md
└── rounds/NNN/
    ├── evaluation-inputs/
    │   ├── plan.md
    │   └── implementation.md
    ├── implementation.md
    ├── attempts/NNN.md
    ├── review.md
    └── interruption.json
```

`evaluation-inputs/` freezes exact review inputs. Replacement reviews archive under `attempts/`; drift adds `interruption.json`. Preserve history.

## Snapshot and role audit

Planner and Evaluator are instruction-level read-only roles, not sandbox guarantees. Root runs `snapshot` before and after each role. For Evaluator, `begin-evaluation` supplies the before snapshot and `record-review` performs the authoritative after check.

Any product-workspace fingerprint change is repository drift. Preserve the diff, record `BLOCKED`, and do not attribute the writer without evidence. Existing dirty product files are allowed, but their raw tracked, staged, unstaged, and non-ignored untracked bytes become part of the snapshot. `.vibe-coding/` and ignored files are excluded.

## Planner: once per requirement

Planner runs once. `spawn_agent` gets Goal, paths, instructions, live code, and snapshot. Require scope, non-goals, behavior, design, ordered independently verifiable work units, and one `## Acceptance criteria` section. Each stable observable `AC-NNN` names success signal, canonical verifier, optional fast check, broader regression/public-path check, and actionable failure output.

After unchanged snapshot, persist `plan.md` and dispatch Generator. Re-plan only for Goal/specification change.

## Generator: build rounds

Create a workspace-write Generator via `spawn_agent` with state, plan, instructions, and previous review. Loop: choose the smallest unfinished step, implement, run the fastest deterministic check. Stop only when every `AC-NNN` has implementation and verification or a concrete external blocker; partial improvement, focused `PASS`, or unrelated failure alone do not stop.

Before handoff require regression/public-path checks, scoped revision if allowed, and large-log path, digest, actionable lines. Persist to `rounds/NNN/implementation.md`. Reuse the requirement's Generator with `followup_task` after `FAIL`; never Planner.

## Begin evaluation

After a complete Generator handoff, run `begin-evaluation`:

```bash
python3 "$SKILL_ROOT/scripts/harness.py" begin-evaluation \
  --target "$TARGET_ROOT" --requirement REQ-NNN
```

Give Evaluator the entire returned transaction. It binds requirement, round, exact Goal, plan, implementation, revision, workspace, and criterion IDs. Runtime writes `pending_evaluation` before input archives. If interrupted, rerun `begin-evaluation`; it reuses or reprepares current inputs. `init --resume` intentionally does not reconcile this marker.

## Evaluator and review

Create Evaluator via `spawn_agent` with transaction, archived inputs, commands, and evidence. It never edits files or relaxes criteria. Verify checks exercise each `AC-NNN`, inspect output, and cover regressions. Tests mirroring assumptions or skipping the public path are insufficient; use `UNVERIFIED` unless evidence distinguishes correct from plausible incorrect behavior. Reference, never create, SHA-256-bound logs. Reuse it with `followup_task`.

Require one `## Evaluation record` fenced Schema 2 JSON with every criterion, evidence, risks, plus `PASS` evidence IDs. Fields: `"schema_version"`, `"requirement_id"`, `"round"`, `"revision"`, `"workspace_fingerprint"`, `"goal_sha256"`, `"plan_sha256"`, `"implementation_sha256"`, and `"verdict": "PASS"`. Command evidence has `"kind": "command"`, `"observations"`, and typed `exact`, `metric`, or repository-relative `artifact` observations, including `"kind": "metric"`.

Root checks relevance. User-visible `PASS` must execute the evaluated revision's public entrypoint and inspect output; unit-only or mocked evidence is `UNVERIFIED`. Replace weak `PASS` through `record-review`; pressure cannot supply evidence.

Write output to a temporary source outside `TARGET_ROOT`, then run `record-review`. Runtime prepares state, atomically copies it, records its digest, and applies the verdict; never hand-edit transaction fields.

```bash
python3 "$SKILL_ROOT/scripts/harness.py" record-review \
  --target "$TARGET_ROOT" --requirement REQ-NNN \
  --review-source "/temporary/path/outside/repository/review.md"
```

## Loop, recovery, and Goal Gate

- `FAIL`: `record-review` advances to the next build round; send the persisted review to Generator.
- `UNVERIFIED`: keep the same snapshot and round, ask Evaluator for missing evidence, then replace the review through `record-review`.
- Every replacement archives prior exact review bytes. `init --resume` reconciles a runtime-prepared pending review deterministically.
- External impediment or drift: to record `BLOCKED`, edit only ordinary orchestration fields: `status`, `next_action`, and `residual_risks`; append rather than replace review risks, and leave `evaluation`, `accepted_revision`, `latest_verdict`, and review bytes unchanged.
- On product, Goal, plan, implementation, or evidence-artifact drift, run `restart-evaluation`; its prepared schema 2 interruption is digest-bound in history before a fresh `BUILDING` round. Missing or invalid current plan blocks only after preserving the interruption; repair it and retry the same reason.
- `DEGRADED` is not `ACCEPTED`: require explicit non-empty user acceptance and never runs `accept` or `check --final`.

Only structured `PASS` may run `accept`. It rejects any transaction-input or review drift. `check --final` rechecks all hashes and receipts. Rounds stop at 999.

```bash
python3 "$SKILL_ROOT/scripts/harness.py" restart-evaluation \
  --target "$TARGET_ROOT" --requirement REQ-NNN --reason "<observed drift>"

python3 "$SKILL_ROOT/scripts/harness.py" accept \
  --target "$TARGET_ROOT" --requirement REQ-NNN

python3 "$SKILL_ROOT/scripts/harness.py" check --final \
  --target "$TARGET_ROOT" --requirement REQ-NNN
```

## Cross-session role recovery

Use `list_agents` on the current Root agent tree. Reuse a role only when its handle is in that tree and `followup_task` can address it; never persist role handles.

For an unusable role, keep the round and select by persisted phase:

- `PLANNING`: spawn a replacement Planner only when `plan.md` is absent; otherwise continue from it.
- `BUILDING`: spawn a replacement Generator.
- `EVALUATING`: spawn a replacement Evaluator.

Replay every existing artifact, snapshots, repository instructions, and Git status, then use `wait_agent`. Replacement after interruption is not a normal-round role recreation.

## File maintenance

Do not create copied rules, fixed role configs, empty governance files, speculative ADRs, or duplicate progress logs. Update existing project documentation only when implementation facts changed.
