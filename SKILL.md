---
name: vibe-coding-harness
description: Use when a software goal spans sessions and needs role-separated planning, implementation, independent evaluation, and durable recovery evidence.
---

# Vibe Coding Harness

Run long software goals through requirement-scoped artifacts and three isolated role agents.

## Contract

- Treat Git, live behavior, and applicable `AGENTS.md` files as truth.
- Root only orchestrates agents, persists harness artifacts, validates the Goal Gate, and reports status. Root never writes business code.
- Planner and Evaluator are read-only. Generator is the only business-code writer.
- After every role dispatch, Root uses `wait_agent` until it completes; roles run strictly serially. Preserve unrelated user changes and use scoped commits.
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

Root writes the handoff to `rounds/NNN/implementation.md`, changes `phase` to `EVALUATING`, sets `latest_verdict=null`, confirms the current `review.md` does not exist yet, and dispatches Evaluator. Reuse the requirement's Generator with `followup_task` after a failed review; do not create another Planner for normal repair.

## Evaluator: QA rounds

Use `spawn_agent` once to create an independent read-only Evaluator. Provide `state.json`, `plan.md`, the current `implementation.md`, evaluated revision or diff, canonical commands, and raw evidence. Require criterion-level `PASS`, `FAIL`, or `UNVERIFIED`, reproduction details, and residual risks. Evaluator never edits or relaxes criteria.

Require the exact single-line heading `## Overall verdict`. Its next non-empty line must be the review's only plain-text verdict: `PASS`, `FAIL`, or `UNVERIFIED`. A `PASS` review must include the exact `## Evidence` section with substantive evidence. Do not decorate these machine-readable headings.

Root writes `rounds/NNN/review.md`. Reuse the requirement's Evaluator with `followup_task` for later QA rounds or missing evidence. Persistent files remain authoritative over role-chat history.

## Loop and recovery

- `PASS`: Root checks Goal alignment and evidence, sets `ACCEPTED`, then runs `check --final`.
- `FAIL`: Root increments `active_round`, sets `phase=BUILDING` and `latest_verdict=FAIL`, and sends the review to Generator.
- `UNVERIFIED`: keep the round and `phase=EVALUATING`; append the next evaluation attempt to the same review file.
- Agent interruption: keep phase and round; redispatch the same role. Create a replacement only if that role is unusable.
- External impediment: record `BLOCKED`, reason, and actionable `next_action`.
- `DEGRADED`: require non-empty `degradation_acceptance` from the user.

Never infer completion from file existence. `ACCEPTED` requires the current round's evidenced PASS and the evaluated current Git revision.

## File maintenance

Do not create copied rules, fixed role configs, empty governance files, speculative ADRs, or duplicate progress logs. Update existing project documentation only when implementation facts changed. Keep historical requirement directories and round evidence intact.
