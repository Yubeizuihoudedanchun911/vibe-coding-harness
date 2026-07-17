# Contributing

Thank you for helping improve Vibe Coding Harness. Contributions should preserve the Skill's role isolation, recovery semantics, and evidence integrity.

## Before you start

- Search existing issues and pull requests before opening a duplicate.
- Use a focused issue for behavior changes that need design discussion.
- Never include credentials, private repository contents, or user data in examples or tests.
- Read `SKILL.md` and the tests that cover the behavior you intend to change.

## Development setup

The runtime uses only Python's standard library.

```bash
git clone https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness.git
cd vibe-coding-harness
PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Python 3.10 or newer is required.

## Workflow

1. Fork the repository or create a feature branch.
2. Keep one user-visible concern per branch.
3. Add or update tests before changing lifecycle semantics.
4. Preserve unrelated work and stage explicit paths.
5. Run the complete test suite.
6. Open a pull request against the canonical branch and complete the template.
7. Address review and CI failures without weakening acceptance gates.

Use concise conventional commit subjects:

```text
feat: add requirement selection behavior
fix: reject stale evaluation evidence
docs: clarify recovery workflow
test: cover symlink boundary
chore: update repository automation
```

## Architectural invariants

Changes must preserve these rules unless the pull request explicitly proposes and justifies a contract revision:

- Root never writes business code.
- Planner and Evaluator are read-only.
- Generator is the only business-code writer.
- Role execution is serial.
- Each user goal has requirement-scoped state.
- Acceptance is bound to substantive evidence and the exact evaluated Git revision.
- Cross-session recovery uses repository artifacts as truth.
- Missing multi-agent support produces `BLOCKED`, not silent single-agent fallback.

## Skill authoring

- Keep `SKILL.md` concise and imperative.
- Keep YAML frontmatter limited to `name` and `description`.
- Update `agents/openai.yaml` if the Skill's user-facing purpose changes.
- Avoid fixed role configuration files, copied coding rules, and empty templates.
- Add deterministic scripts only when they reduce repeated or fragile work.

## Pull request checklist

- The change has a clear user or maintainer outcome.
- Tests cover new behavior and failure paths.
- The full test suite passes.
- Documentation matches implemented behavior.
- No secrets, generated caches, or local agent state are committed.
- The change remains compatible with Python 3.10+.

AI-assisted contributions are welcome, but contributors remain responsible for understanding the change, verifying its behavior, and reporting the actual commands and results used for validation.

By submitting a contribution, you agree that it may be distributed under the repository's license.
