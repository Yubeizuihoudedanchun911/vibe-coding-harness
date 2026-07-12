# Java Rules

<!-- Managed by vibe-coding-harness -->

- Follow the existing Maven or Gradle module layout and dependency-management source.
- Keep package boundaries aligned with domain ownership; avoid catch-all `util`, `common`, `manager`, or `handler` packages.
- Prefer immutable value objects, constructor injection, and explicit interfaces at replaceable boundaries.
- Validate transport input before mapping it into trusted domain types.
- Preserve exception causes, distinguish domain from infrastructure failures, and avoid broad catch blocks.
- Keep transactions explicit and short; do not hide remote calls inside transactional work.
- Avoid nullable contracts when an existing project convention provides a clearer representation.
- Write focused unit tests for domain behavior and integration tests for framework, persistence, and serialization boundaries.
- Preserve the repository's established file-header and author convention; never identify an AI tool as the source author.
- Run configured formatting/static analysis, affected tests, the module build, and integration checks.
