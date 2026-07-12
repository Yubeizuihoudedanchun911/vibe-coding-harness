# Lifecycle and agent protocol

## Contents

1. State machine
2. Orchestrator responsibilities
3. Agent contracts
4. Handoff schema
5. Retry and recovery
6. User interaction boundary

## State machine

Use these durable states in the iteration manifest:

| State | Exit condition |
|---|---|
| `INTAKE` | Outcome, non-goals, constraints, and success evidence are explicit |
| `BOOTSTRAP` | Project profile, rules, agent files, and document roots validate |
| `DISCOVER` | Live execution paths, commands, tests, and risks are mapped |
| `PLAN` | Vertical slices and dependencies are defined |
| `CONTRACT` | Current slice has observable, testable acceptance criteria |
| `IMPLEMENT` | Generator supplies a tested handoff |
| `VERIFY` | Evaluator issues criterion-level evidence and verdict |
| `REPAIR` | A bounded defect set is assigned back to Generator |
| `GOVERN` | Durable docs and decision records match the implementation |
| `RELEASE_GATE` | All mandatory checks have current evidence |
| `ACCEPTED` | Core outcome works and no hard gate fails |
| `BLOCKED` | Progress needs a product decision, credential, permission, or external state change |
| `DEGRADED` | A declared non-core limitation remains and the user explicitly accepts it |
| `ABANDONED` | The user ends the run without delivery |

Do not skip directly from implementation to accepted. Preserve every terminal manifest and delivery report.

## Orchestrator responsibilities

The root agent must:

- Own the state transition and iteration manifest.
- Select internal execution depth from risk rather than user familiarity.
- Spawn roles with bounded tasks and the minimum context needed.
- Persist role outputs before dropping or replacing an agent thread.
- Enforce the single-writer rule for business code.
- Stop plateauing repair loops and replan.
- Run repository governance only after implementation evidence exists.
- Distinguish business completion from code-generation completion.

Use native agent messages for assignment, steering, and concise results. Use files for goals, contracts, handoffs, evaluation evidence, decisions, and recovery. Do not use file polling as a substitute for orchestration.

## Agent contracts

### Planner

- Operate read-only.
- Expand the request into outcome, non-goals, core journey, risks, and vertical slices.
- Reference live repository evidence.
- Avoid prescribing fragile implementation details before discovery.
- Return a proposed contract; never edit business code or declare completion.

### Generator

- Be the only role allowed to edit business source.
- Implement one accepted contract at a time.
- Preserve unrelated dirty work.
- Run focused checks and inspect the real application when possible.
- Never weaken a gate, delete a failing test, or replace core behavior with a stub.
- Return a structured handoff; submit work for evaluation rather than self-accepting it.

### Evaluator

- Operate read-only and independently from Generator.
- Start with fresh task-local context.
- Verify each criterion with commands, interactions, state inspection, or artifacts.
- Treat missing evidence as `UNVERIFIED`, not `PASS`.
- Return `PASS`, `FAIL`, or `BLOCKED` per criterion and an overall verdict.
- Report defects with reproduction steps and expected versus actual behavior; never fix them.

## Handoff schema

Persist Generator handoffs with:

```yaml
slice: <stable id>
revision: <commit or worktree state>
changed_paths: []
commands_run:
  - command: <exact command>
    result: PASS|FAIL|NOT_RUN
known_risks: []
unverified: []
next_verification: <what Evaluator should exercise>
```

Persist Evaluator results with:

```yaml
round: 1
revision: <evaluated state>
overall: PASS|FAIL|BLOCKED
criteria:
  - id: AC-001
    verdict: PASS|FAIL|BLOCKED|UNVERIFIED
    evidence: <command, interaction, state, or artifact>
defects: []
residual_risks: []
```

## Retry and recovery

- First failure: assign the minimal defect set to Generator.
- Second failure of the same symptom: require root-cause analysis and inspect whether the contract or architecture is wrong.
- Third failure: stop automatic retry and transition to `BLOCKED` or replan a materially different approach.
- Record the exact last good revision, current state, next action, and blockers before context reset or session end.
- Resume from artifacts and Git truth, not from a conversational summary alone.
- If a newer manifest or source revision invalidates old evidence, mark that evidence stale and rerun the affected gate.

## User interaction boundary

Do not ask users to select models, agents, rule files, directory templates, or test commands when the repository provides enough evidence. Ask only when:

- Two product interpretations produce materially different outcomes.
- A credential, account, or permission is required.
- An external mutation is irreversible, costly, or affects other people.
- A degraded delivery needs explicit acceptance.
