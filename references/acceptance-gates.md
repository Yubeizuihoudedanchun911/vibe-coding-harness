# Acceptance gates

## Verdict policy

Run gates against the exact revision named in the evaluation report. Mark missing or stale evidence `UNVERIFIED`. A hard-gate failure prevents `ACCEPTED`.

## Mandatory gates

| Gate | Minimum proof |
|---|---|
| Scope | Every accepted criterion maps to implementation and evidence |
| Build | Canonical build or compile command succeeds |
| Static quality | Configured lint, formatting check, and typecheck succeed |
| Automated tests | Relevant unit and integration tests pass |
| Core journey | At least one real end-to-end path completes without mocks in the product core |
| Persistence | Required state survives the expected reload, restart, or round trip |
| Error behavior | Invalid input and important failure paths produce intentional behavior |
| Contracts | API, schema, migration, and compatibility promises are verified when affected |
| Security baseline | No committed secrets; auth and permission boundaries are exercised when affected |
| Repository governance | Project profile, rules, iteration manifest, architecture, onboarding, and delivery report validate |

Use `NOT_APPLICABLE` only with a reason. Never convert `UNVERIFIED` to `PASS` because a component or function exists.

## Qualitative gates

Apply when relevant after deterministic checks:

- Usability and clarity of the core workflow.
- Visual coherence and responsive behavior.
- Maintainability, naming, module boundaries, and unnecessary complexity.
- Accessibility and keyboard behavior for user-facing interfaces.
- Product depth: controls must perform their advertised behavior.

## Delivery statuses

- `ACCEPTED`: all hard gates pass and the user-visible outcome works.
- `BLOCKED`: an external dependency, credential, permission, or product decision prevents proof.
- `DEGRADED`: a declared non-core limitation remains and the user explicitly accepts it.
- `ABANDONED`: the user ends the effort without delivery.
