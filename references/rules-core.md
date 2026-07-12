# Core Coding Rules

<!-- Managed by vibe-coding-harness -->

- Preserve unrelated user changes and use scoped writes, tests, and staging.
- Build user-visible vertical slices; avoid infrastructure that has no accepted consumer.
- Keep the core journey free of stubs, fake persistence, placeholder controls, and silent fallbacks.
- Prefer explicit contracts and domain vocabulary over generic containers and implicit shapes.
- Keep modules cohesive, side effects visible, and dependency direction intentional.
- Validate untrusted input at system boundaries; do not duplicate framework guarantees internally.
- Keep secrets out of source, logs, fixtures, screenshots, and committed configuration.
- Add dependencies only when their lifecycle cost is justified and record material choices.
- Change behavior with focused tests and run the narrowest relevant checks before the full gate.
- Treat missing evidence as unverified. Never delete or weaken a test merely to obtain green output.
- Update onboarding, architecture, decisions, and iteration records only when their facts change.
- Use repository-native commands and conventions when they satisfy these invariants.
- Avoid speculative abstractions, generic utility dumping grounds, and compatibility layers without a current caller.
- Make failures actionable: include context, preserve the cause, and avoid swallowing errors.
- Keep generated files, caches, vendored dependencies, build output, and runtime state outside hand-edited source.
