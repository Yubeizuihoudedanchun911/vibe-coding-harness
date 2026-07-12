---
name: vibe-coding-harness
description: Build and evolve maintainable software from a natural-language idea or change request through planning, implementation, independent evaluation, repository governance, and evidence-backed acceptance. Use for vibe coding, greenfield product creation, MVP delivery, end-to-end feature work, long-running autonomous coding, inheriting or regularizing an existing repository, installing coding rules, or continuing a multi-session development effort that must remain structured and verifiable.
---

# Vibe Coding Harness

Turn a user's product intent into an accepted delivery while keeping the internal planning, agent roles, coding rules, documentation lifecycle, and recovery state out of the user's way.

## Operating contract

- Treat the root agent as the lifecycle orchestrator.
- Ask the user only for unresolved product choices, credentials or permissions, and irreversible external actions.
- Keep Planner and Evaluator read-only. Keep Generator as the only business-code writer.
- Use agent messages for control and repository artifacts for durable handoff.
- Preserve unrelated user changes and use scoped writes and staging.
- Never report completion while a hard acceptance gate is failing or unverified.
- Mark an incomplete run `BLOCKED` or `DEGRADED`; require explicit user acceptance before treating degradation as delivery.

## Start or resume a run

1. Read applicable `AGENTS.md` files and inspect repository status before changing files.
2. Resolve this Skill directory as `SKILL_ROOT` and the target project as `TARGET_ROOT`. Reuse its Git root when present. For a greenfield target that belongs to this request, initialize Git before implementation; never nest a repository inside another repository.
3. Run `scripts/detect_project_profile.py --target TARGET_ROOT` without writes.
4. Read [rule-selection.md](references/rule-selection.md), then read [rules-core.md](references/rules-core.md) plus every language rule selected by the profile. Do not load unrelated language rules.
5. Read [repository-contract.md](references/repository-contract.md) and [lifecycle.md](references/lifecycle.md).
6. If the infrastructure is missing or stale, run `scripts/bootstrap_harness.py` with the target, project name, project goal, and iteration topic. Review its summary and conflicts before continuing.
7. Run `scripts/validate_harness.py --target TARGET_ROOT`. Repair structural errors before business implementation.
8. Resume the open iteration when it matches the request. Otherwise create a new iteration; never mix unrelated goals in one iteration.

## Select execution depth internally

Do not make the user choose a harness mode unless cost or latency materially changes their decision.

| Policy | Use when | Required loop |
|---|---|---|
| Fast | Small, reversible, low-risk change with deterministic checks | Orchestrator plan, Generator, fresh final Evaluator |
| Standard | Normal feature or product slice | Planner, contract, Generator, Evaluator per slice |
| Strict | Auth, payments, migrations, permissions, destructive data work, or hard-to-reverse architecture | Planner/Evaluator contract review, bounded slices, fresh Evaluator each round, explicit release gate |

Escalate depth when ambiguity, blast radius, external dependencies, or failed verification increases. De-escalate only when evidence shows the extra scaffold is not load-bearing.

## Execute the lifecycle

Follow the state machine in [lifecycle.md](references/lifecycle.md):

`INTAKE -> BOOTSTRAP -> DISCOVER -> PLAN -> CONTRACT -> IMPLEMENT -> VERIFY -> GOVERN -> RELEASE_GATE -> ACCEPTED`

On verification failure, enter `REPAIR` and return a bounded defect report to Generator. After the same defect fails twice, require root-cause analysis or replanning. After three failed rounds, stop the loop and report a blocker instead of repeating the same approach.

### Intake and discovery

- Derive the user-visible outcome, non-goals, core journey, constraints, and proof of success.
- Inspect live code, manifests, commands, tests, and runtime behavior. Prefer repository truth over old documentation.
- Detect existing conventions and extend them unless they conflict with a hard invariant.

### Plan and contract

- Split work into user-visible vertical slices, not infrastructure-only layers.
- Define observable behavior, error behavior, data effects, and verification for every slice.
- Put the brief, plan, and contracts directly in the current iteration paths defined by [repository-contract.md](references/repository-contract.md).
- Keep the core journey free of placeholders, fake persistence, display-only controls, and unimplemented stubs.

### Implement

- Give Generator one accepted contract and its relevant files at a time.
- Require focused tests and a concise handoff containing changed paths, commands run, results, known risks, and the next verification target.
- Do not let parallel agents edit overlapping code. Use separate worktrees for genuinely independent write lanes.

### Verify and repair

- Start Evaluator with fresh task-local context: goal, contract, diff, run commands, and raw artifacts only.
- Require deterministic checks first, then real UI/API/database interaction, then qualitative judgment when needed.
- Persist each round as `evaluations/round-NNN.md`; never overwrite earlier evidence.
- Read [acceptance-gates.md](references/acceptance-gates.md) before issuing a verdict.

### Govern and deliver

- Update the iteration manifest, architecture, onboarding, decisions, and changelog only when their underlying facts changed.
- Create ADRs only for durable decisions with documented rationale and consequences.
- Run the full release gate and `scripts/validate_harness.py` again.
- Write `delivery-report.md` with scope, gate results, evidence, residual risks, and one of `ACCEPTED`, `BLOCKED`, or `DEGRADED`.
- Report the outcome to the user in product language; expose internal details only when they explain a decision, risk, or failure.

## Dynamic Coding Rule routing

- Always apply `.coding-rules/core.md`.
- Read `.vibe-coding/project-profile.json` before editing source.
- Apply every rule listed for the touched path or language; primary language does not suppress secondary languages.
- Re-run detection when manifests, lockfiles, module roots, or previously unmapped source types change.
- Preserve user-authored rule content outside managed blocks. Never silently replace an unmanaged file.

Language references:

- JavaScript/TypeScript: [rules-javascript-typescript.md](references/rules-javascript-typescript.md)
- Python: [rules-python.md](references/rules-python.md)
- Go: [rules-go.md](references/rules-go.md)
- Java: [rules-java.md](references/rules-java.md)
- Rust: [rules-rust.md](references/rules-rust.md)

## Infrastructure scripts

- `scripts/detect_project_profile.py`: detect languages, module roots, framework evidence, and standard commands without depending on file counts alone.
- `scripts/bootstrap_harness.py`: install or refresh managed governance files safely and create an iteration without overwriting user documents.
- `scripts/validate_harness.py`: validate the project profile, selected rules, iteration manifests, artifact references, and lifecycle status.
