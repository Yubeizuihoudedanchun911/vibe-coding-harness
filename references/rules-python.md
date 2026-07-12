# Python Rules

<!-- Managed by vibe-coding-harness -->

- Follow the existing `pyproject.toml` and environment manager; do not introduce a second packaging workflow.
- Use type hints on public and cross-module boundaries; keep internal annotations proportionate to value.
- Prefer `pathlib`, context managers, explicit encodings, and timezone-aware datetimes.
- Keep I/O at boundaries and make core transformations independently testable.
- Use dataclasses or existing validation libraries for structured domain values; avoid untyped nested dictionaries across boundaries.
- Raise specific exceptions, preserve causes, and translate them only at ownership boundaries.
- Avoid mutable default arguments, import-time side effects, global mutable state, and hidden network calls.
- Keep package layout consistent with the existing framework; for new libraries prefer `src/<package>/` plus `tests/`.
- Test behavior with the repository's configured runner and fixtures; do not over-mock code owned by the project.
- Run configured formatting, lint, typecheck, tests, packaging/build, and integration checks that cover the change.
