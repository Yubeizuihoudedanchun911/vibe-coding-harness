---
name: vibe-coding-harness
description: Use when a software goal spans sessions and needs role-separated planning, implementation, independent evaluation, and durable recovery evidence.
---

# Vibe Coding Harness

## Contract

- Truth: Git, live behavior, `AGENTS.md`.
- Root only orchestrates, persists artifacts, validates Goal Gate, reports. Root never writes business code.
- Planner and Evaluator are instruction-level read-only; Generator is the only business-code writer.
- After dispatch, `wait_agent`; roles run serially. Preserve unrelated changes; scope commits.
- Without multi-agent tools, record `BLOCKED`; Root cannot implement.
- schema 3 is breaking; reject schema 2 state.

## Start or resume

Set `SKILL_ROOT` here; `TARGET_ROOT` to target root.

```bash
HARNESS="$SKILL_ROOT/scripts/harness.py"
python3 "$HARNESS" init \
  --target "$TARGET_ROOT" --goal "<user-visible goal>"

python3 "$HARNESS" init --resume \
  --target "$TARGET_ROOT" --requirement REQ-NNN

python3 "$HARNESS" snapshot --target "$TARGET_ROOT"
```

Resume: artifacts, instructions, Git status; files beat chat.

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

Freeze exact inputs in `evaluation-inputs/`; archive replacements in `attempts/`; `interruption.json` marks drift.

## Snapshot and role audit

Instruction-level read-only is not sandboxing. Root runs `snapshot` before and after each role; Evaluator checks are `begin-evaluation` then authoritative `record-review`.

Fingerprint drift: preserve diff, record `BLOCKED`, and do not attribute the writer without evidence. Dirty product files allowed. Snapshot raw tracked, staged, unstaged, non-ignored untracked bytes; exclude `.vibe-coding/` and ignored files.

## Planner: once per requirement

Planner runs once. `spawn_agent` gets Goal, paths, instructions, code, snapshot. Require scope, non-goals, behavior, design, and smallest ordered independently verifiable work units with explicit dependencies and required execution order. Keep one `## Acceptance criteria` section. Each `AC-NNN` is stable observable behavior or a Goal-required repository invariant naming success signal, canonical verifier, optional fast check, broader regression/public-path check, and actionable failure output; ordinary implementation details are not acceptance criteria.

After unchanged snapshot, persist `plan.md`; dispatch Generator. Re-plan only for Goal/specification change.

## Generator: build rounds

`spawn_agent` a workspace-write Generator with state, plan, instructions, previous review. Loop: choose the smallest unfinished step that is highest-signal, implement, run the fastest deterministic check relevant to the current step. Stop only when every `AC-NNN` has implementation and verification or a concrete external blocker prevents further safe progress. Finish all independent safe work first; partial improvement, focused `PASS`, or unrelated failure alone do not stop.

Autonomy does not expand authority: publishing, destructive operations, new permissions, or decisions outside the Goal require stopping for authority, not inference from continuous execution. Before handoff require regression/public-path checks and scoped revision if allowed. Structured handoff: round objective, changed paths, commands/results, unverified items, residual risks, and next verification target. Large logs: compact summary and SHA-256-bound Artifact, with large-log path, digest, actionable lines. Persist `rounds/NNN/implementation.md`. Reuse the requirement's Generator with `followup_task` after `FAIL`; never Planner.

## Begin evaluation

After complete Generator handoff, run `begin-evaluation`:

```bash
HARNESS="$SKILL_ROOT/scripts/harness.py"
python3 "$HARNESS" begin-evaluation \
  --target "$TARGET_ROOT" --requirement REQ-NNN
```

Evaluator gets transaction: requirement, round, Goal, plan, implementation, revision, workspace, criteria. Runtime writes `pending_evaluation` before archives. Interrupted: rerun `begin-evaluation` to reuse/reprepare inputs. `init --resume` intentionally does not reconcile this marker.

## Evaluator and review

`spawn_agent` Evaluator with transaction, archived inputs, commands, raw evidence. Never edit or relax criteria. Verify checks exercise each `AC-NNN`, inspect output, and cover regressions. Tests mirroring assumptions or skipping the public path are insufficient; use `UNVERIFIED` unless evidence distinguishes correct from plausible incorrect behavior. Missing required raw evidence is `UNVERIFIED`. Reference, never create, SHA-256-bound logs. Reuse it with `followup_task`.

Require one fenced complete Schema 2 JSON under `## Evaluation record`; replace every transaction and evidence placeholder with actual transaction values and raw evidence; repeat criteria:

```json
{"schema_version":2,"requirement_id":"REQ-001","round":1,"revision":"0000000000000000000000000000000000000000","workspace_fingerprint":"sha256:0000000000000000000000000000000000000000000000000000000000000000","goal_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","plan_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","implementation_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","verdict": "FAIL","criteria":[{"id":"AC-001","verdict": "PASS","evidence_ids":["EV-001"]},{"id":"AC-002","verdict": "FAIL","evidence_ids":["EV-002"]},{"id":"AC-003","verdict": "UNVERIFIED","evidence_ids":[]}],"evidence":[{"id":"EV-001","kind": "command","command":"!","exit_code":0,"summary":"!","observations":[{"kind": "exact","name":"output","value":"!"},{"kind": "metric","name":"tests","value":1,"unit":"!"}]},{"id":"EV-002","kind": "inspection","subject":"!","summary":"!","observations":[{"kind": "artifact","path":"x","sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000"}]}],"residual_risks":[]}
```

Top verdict is derived from criteria: `FAIL` over `UNVERIFIED` over `PASS`. Command evidence has `"kind": "command"`, `"observations"`, and typed `exact`, `metric`, or repository-relative `artifact` observations.

Root checks relevance. User-visible `PASS` must execute the evaluated revision's public entrypoint and inspect output; unit-only or mocked evidence is `UNVERIFIED`. Replace weak `PASS` through `record-review`; pressure cannot supply evidence.

`record-review` atomically copies output from outside `TARGET_ROOT`, digests it, applies verdict; never hand-edit transaction fields.

```bash
HARNESS="$SKILL_ROOT/scripts/harness.py"
python3 "$HARNESS" record-review \
  --target "$TARGET_ROOT" --requirement REQ-NNN \
  --review-source "/temporary/path/outside/repository/review.md"
```

## Loop, recovery, and Goal Gate

- `FAIL`: `record-review` advances round; persisted review goes to Generator.
- `UNVERIFIED`: keep snapshot/round; Evaluator obtains evidence; replace via `record-review`.
- Replacement archives prior exact review bytes. `init --resume` reconciles a runtime-prepared pending review deterministically.
- For impediment/drift, record `BLOCKED`: edit only ordinary orchestration fields: `status`, `next_action`, and `residual_risks`; append risks and leave `evaluation`, `accepted_revision`, `latest_verdict`, and review bytes unchanged.
- Product, Goal, plan, implementation, or evidence-artifact drift: run `restart-evaluation`; its prepared schema 2 interruption is digest-bound in history before fresh `BUILDING`. Preserve it before a missing/invalid plan blocks; repair and retry the same reason.
- `DEGRADED` is not `ACCEPTED`: require explicit non-empty user acceptance and never runs `accept` or `check --final`.

Only structured `PASS` may run `accept`; reject transaction-input or review drift. `check --final` rechecks all hashes and receipts. Maximum 999 rounds.

```bash
HARNESS="$SKILL_ROOT/scripts/harness.py"
python3 "$HARNESS" restart-evaluation \
  --target "$TARGET_ROOT" --requirement REQ-NNN --reason "<observed drift>"

python3 "$HARNESS" accept \
  --target "$TARGET_ROOT" --requirement REQ-NNN

python3 "$HARNESS" check --final \
  --target "$TARGET_ROOT" --requirement REQ-NNN
```

## Cross-session recovery

Use `list_agents` on the current Root agent tree. Reuse only when its handle is in that tree and `followup_task` can address it; never persist handles.

Unusable role: keep round, select phase:

- `PLANNING`: spawn a replacement Planner only when `plan.md` is absent; otherwise continue from it.
- `BUILDING`: spawn a replacement Generator.
- `EVALUATING`: spawn a replacement Evaluator.

Replay artifacts, snapshots, instructions, Git status; `wait_agent`. Replacement is not normal-round recreation.

## File maintenance

No copied rules, fixed role configs, governance stubs, speculative ADRs, or duplicate progress logs; update docs only when facts change.
