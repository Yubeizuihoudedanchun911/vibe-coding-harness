# JavaScript and TypeScript Rules

<!-- Managed by vibe-coding-harness -->

- Use the package manager selected by the existing lockfile; do not create a second lockfile.
- Prefer TypeScript for new maintained source when the project already supports it.
- Keep strict typechecking enabled; avoid `any`, unchecked casts, and non-null assertions without boundary evidence.
- Validate external JSON, environment variables, request bodies, and persisted data before treating them as typed.
- Keep UI state local unless multiple consumers require shared ownership; do not mirror derivable state.
- Make async cancellation, timeout, loading, empty, and error states explicit.
- Preserve framework routing, server/client, and rendering conventions instead of inventing parallel structure.
- Keep reusable components behavior-focused; avoid universal components with boolean-flag APIs.
- Test observable behavior rather than component implementation details.
- Run configured format, lint, typecheck, unit/integration, build, and E2E scripts that cover the change.
- Do not edit generated bundles, framework caches, coverage output, or dependency directories.
