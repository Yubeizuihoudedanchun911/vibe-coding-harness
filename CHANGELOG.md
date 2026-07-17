# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Requirement-scoped schema 2 state under `.vibe-coding/requirements/REQ-NNN/`.
- Planner-once, Generator/Evaluator-loop orchestration.
- Git-bound Goal Gate validation and evidence requirements.
- Cross-session role recovery from durable artifacts.
- Python standard-library CLI and automated test coverage.
- Apache-2.0 licensing and open-source community governance files.

### Changed

- Replaced the original global progress-file harness and fixed role configurations with a minimal Skill-driven runtime.

### Security

- Reject symbolic-link escapes, invalid state transitions, stale revisions, and empty or malformed review evidence.
