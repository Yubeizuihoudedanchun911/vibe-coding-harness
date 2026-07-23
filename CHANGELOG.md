# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and releases follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Breaking Schema 4 run state with immutable artifacts, append-only histories,
  prepared operations, and Git-bound recovery.
- Installed external `vibe` CLI for run, resume, status, stop, logs, and
  explicit Schema 3 migration.
- Versioned Planner, base/specialist Worker, and Evaluator Prompts plus strict
  plan, Worker-result, and evaluation Schemas.
- Path/resource-safe parallel specialist Workers with fresh worktree, branch,
  process, and context per Attempt.
- Serial candidate verification, source/ref compare-and-swap, prepared
  dispatch, source-commit, and integration recovery.
- Independent Evaluator verdicts, evidence refresh, bounded repair, and a
  Git/evidence-bound Goal Gate.
- Explicit, read-only, byte-preserving, idempotent Schema 3 migration with
  stable claims, backup trees, reserved Run IDs, and `--replan`.
- Offline fake-Provider and opt-in real Codex CLI tests, package metadata
  checks, wheel/sdist smoke tests, and macOS process-identity coverage.

### Changed

- Replaced the in-agent Root/Generator serial orchestration model with an
  external Controller and parallel finite-DAG execution.
- Replaced requirement-scoped Schema 3 success with native Schema 4 run
  lifecycle semantics; imported terminal history remains
  `IMPORTED_READ_ONLY`.
- Made `FAILED` terminal and foreground stop/resume explicit.
- Frozen project command authorization and Prompt/Schema identities into every
  run.

### Removed

- Skill metadata, Root prompt metadata, the legacy Schema 3 mutation script,
  and their compatibility tests.
- Implicit migration and dirty-baseline execution.

### Security

- Fail closed on symlink/ancestor swaps, malformed strict JSON, stale process
  identity, out-of-scope Worker changes, unsafe Git integrations, stale CAS,
  forged artifacts, and incomplete migration mappings.
- Keep default CI offline and prohibit automated merge, push, pull-request,
  package publication, or Provider network workflows.
