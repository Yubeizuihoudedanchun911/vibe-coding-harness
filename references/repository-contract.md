# Repository and document contract

## Stable control surface

Keep the governance surface small and stack-neutral:

```text
AGENTS.md
.coding-rules/
  core.md
  <selected-language>.md
.codex/agents/
  vibe-planner.toml
  vibe-generator.toml
  vibe-evaluator.toml
.vibe-coding/
  project-profile.json
  runtime/                 # ignored local cursor and transient logs
docs/
  README.md
  onboard/README.md
  architecture/README.md
  decisions/README.md
  iterations/README.md
  iterations/YYYY-MM-DD-topic/
    iteration.md
    product-brief.md
    execution-plan.md
    acceptance-contract.md
    handoffs/
    evaluations/
    evidence/
    delivery-report.md
    manifest.json
  changelog.md
```

Do not force application source into one universal layout. Preserve framework-native conventions. Apply these semantic boundaries instead:

- Keep the project under one explicit Git root. For a greenfield target, initialize Git before implementation so handoffs and evaluations can name a reproducible revision.
- Separate generated output, dependencies, caches, secrets, and source.
- Keep public contracts explicit at process, API, storage, and UI boundaries.
- Keep tests close enough to ownership that affected checks are discoverable.
- Document the canonical install, run, build, lint, typecheck, test, and E2E commands.
- Prefer vertical feature ownership over cross-cutting utility dumping grounds.

## Document lifecycle

- Write process artifacts directly into the active iteration. Do not scatter them and rely on a commit hook to move them later.
- Treat `product-brief.md`, accepted contracts, evaluation rounds, and `delivery-report.md` as durable handoff artifacts.
- Store evaluation rounds immutably as `evaluations/round-NNN.md`.
- Keep large raw logs, caches, and disposable screenshots under ignored runtime storage; commit only evidence needed to reproduce a verdict.
- Update global architecture and onboarding only when facts change.
- Create an ADR only for a durable decision with alternatives, rationale, consequences, and rollback considerations.
- Close an iteration as `ACCEPTED`, `BLOCKED`, `DEGRADED`, or `ABANDONED`; never delete history to make the project look clean.

## Manifest invariants

Each iteration manifest must contain:

```json
{
  "schema_version": 1,
  "iteration": "YYYY-MM-DD-topic",
  "status": "INTAKE",
  "goal": "User-visible outcome",
  "execution_policy": "fast|standard|strict",
  "artifacts": [],
  "evaluation_rounds": [],
  "gates": {},
  "residual_risks": []
}
```

- Every artifact path must be relative to its iteration directory and must exist.
- A terminal status requires `delivery-report.md`.
- `ACCEPTED` requires every hard gate to be `PASS` with non-empty evidence.
- Do not store secrets, full transcripts, or model chain-of-thought.

## Safe updates

- Mark Skill-managed rule and agent files with `Managed by vibe-coding-harness`.
- Replace only files carrying that marker; report unmanaged conflicts.
- Use marked blocks when updating `AGENTS.md` or `.gitignore`.
- Preserve user content outside managed blocks.
- Never stage or commit unrelated work automatically.
