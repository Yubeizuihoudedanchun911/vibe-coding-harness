# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Requirement-scoped state under `.vibe-coding/requirements/REQ-NNN/`.
- Planner-once, Generator/Evaluator-loop orchestration.
- Git-bound Goal Gate validation and evidence requirements.
- Cross-session role recovery from durable artifacts.
- Python standard-library CLI and automated test coverage.
- Apache-2.0 licensing and open-source community governance files.

### Changed

- Replaced the original global progress-file harness and fixed role configurations with a minimal Skill-driven runtime.
- Replaced schema 2 with schema 3 evaluation transactions; schema 2 state is intentionally unsupported.
- Replaced manual evaluation state edits with explicit `snapshot`, `begin-evaluation`, `record-review`, `restart-evaluation`, and `accept` commands.
- Freeze exact transaction inputs (plan and implementation) for each evaluation, and bind reviews to requirement, round, goal, input hashes, revision, and product snapshot.
- Preserve replaced reviews, failed evaluations, and interruptions through ordered hash-bound history receipts.
- Changed Planner and Evaluator claims from sandboxed read-only to audited instruction-level read-only boundaries.

### Security

- Reject symbolic-link escapes, invalid state transitions, stale revisions, and empty or malformed review evidence.
- Bind PASS evidence to the requirement ID, round, exact goal/plan/implementation inputs, evaluated revision, complete workspace fingerprint, acceptance IDs, and exact review bytes.
- Disable external diff/text-conversion helpers, hash raw tracked bytes independently of clean filters and `assume-unchanged`, and recursively fingerprint initialized submodules.
- Replace free-text evidence-result inference with evaluation-record schema 2 typed observations: exact values, finite metrics, and repository-relative artifacts verified by SHA-256.
- Reject case-folded or nested Git/control artifact paths and normalize oversized JSON-number or invalid Unicode-scalar failures without tracebacks.
- Reconcile evaluation-input, review, and interruption writes with state-first prepared markers and atomic file replacement; reject orphan lifecycle files rather than trusting them.
- Archive exact evaluation inputs and replaced review bytes, and rehash all historical attempts, FAIL receipts, and interruption receipts during validation.
- Preserve workspace, goal, plan, implementation, and hash-bound artifact drift in schema 2 `interruption.json` before entering a fresh build round; missing/invalid plans block only after evidence is durable, and schema 1 interruptions are unsupported.
- Bound evaluation at 999 rounds before lifecycle writes and return expected filesystem, numeric JSON, and Unicode failures as structured errors without tracebacks.
