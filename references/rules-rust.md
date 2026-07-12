# Rust Rules

<!-- Managed by vibe-coding-harness -->

- Follow the existing Cargo workspace and crate boundaries; add a crate only for a real ownership boundary.
- Model invalid states out of core types when the complexity cost is justified.
- Prefer borrowing and clear ownership over cloning to satisfy the compiler without analysis.
- Avoid `unwrap` and `expect` in production paths unless an invariant is documented and locally proven.
- Use explicit error types at library boundaries and add context at application boundaries.
- Keep `unsafe` isolated, documented with invariants, and covered by focused tests.
- Make async task ownership, cancellation, and shutdown explicit.
- Preserve feature-flag compatibility and do not enable expensive default features casually.
- Run `cargo fmt --check`, configured Clippy checks, affected tests, workspace tests, and builds for affected feature sets.
