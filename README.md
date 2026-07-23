# Vibe Coding Harness

[![CI](https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)

[简体中文](README.zh-CN.md)

## What it is

Vibe Coding Harness is an external, recoverable Controller for running a
Planner, parallel specialist Workers, and an independent Evaluator against a
Git repository. The installed `vibe` CLI owns orchestration and durable Schema
4 state; chat history is never the recovery source of truth.

## Architecture

```text
goal -> Planner -> finite dependency DAG
                    |
                    +-> specialist Workers in isolated attempts
                    |      (parallel only when paths/resources are safe)
                    v
              serial candidate verification + Git CAS
                    |
                    v
              independent Evaluator
                    |
        PASS / NEEDS_REPAIR / UNVERIFIED / BLOCKED
```

The Controller is the sole state writer. Every Worker Attempt receives a fresh
worktree, branch, provider process, and prompt context. Candidate commits are
verified serially and update the run ref only through compare-and-swap. A
bounded repair loop sends findings back to a fresh Planner operation.

## Requirements

- Git
- Python 3.10 or newer
- Codex CLI available to the Provider adapter

The product repository must start from a clean committed baseline. There is no
`--allow-dirty` mode.

## Install

```bash
git clone \
  https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness.git
cd vibe-coding-harness
python -m pip install .
vibe --help
```

## Configuration

Optional repository-local `vibe.json` configures concurrency, limits, and an
explicit verification command catalog:

```json
{
  "scheduler": {"max_workers": 4},
  "limits": {
    "task_attempts": 3,
    "provider_retries": 3,
    "evidence_rounds": 3,
    "repair_rounds": 3,
    "max_plan_tasks": 128
  },
  "verification": {
    "command_catalog": [
      {
        "id": "unit",
        "purpose": "Run the offline unit suite",
        "argv": ["python", "-m", "unittest", "discover", "-s", "tests"],
        "cwd": ".",
        "timeout_seconds": 900,
        "env_allowlist": []
      }
    ],
    "required_command_ids": ["unit"]
  }
}
```

Non-empty project commands require the explicit
`--allow-project-commands` flag. Their exact source digest and authorization
mode are frozen into the run.

## Run

```bash
vibe run --target /path/to/repo \
  --goal "Build and verify an MVP expense tracker"
```

Use `--goal-file` instead of `--goal` for a file-backed goal. A foreground run
creates `.vibe-coding/runs/RUN-YYYYMMDD-NNN/` and a private
`refs/heads/vibe/run-*` integration ref.

## Resume

```bash
vibe resume --target /path/to/repo RUN-20260723-001
```

Foreground interruption and `vibe stop` are recoverable. `FAILED` is terminal
and cannot be resumed: inspect the failure, check out the desired clean commit,
then start an explicit new `vibe run`, which records that new baseline.

An imported active or blocked Schema 3 requirement needs an explicit clean
baseline replan:

```bash
vibe resume --target /path/to/repo RUN-20260723-002 --replan
```

## Status

```bash
vibe status --target /path/to/repo RUN-20260723-001
vibe status --target /path/to/repo RUN-20260723-001 --json
```

## Stop

```bash
vibe stop --target /path/to/repo RUN-20260723-001
```

The stop request is durable and bound to the registered Controller process
identity. Resuming uses fresh Attempts rather than reusing cancelled context.

## Logs

```bash
vibe logs --target /path/to/repo RUN-20260723-001
vibe logs --target /path/to/repo RUN-20260723-001 --task TASK-001
```

## Schema 3 migration

Schema 3 is never loaded or migrated implicitly. Select one requirement or all
requirements and bind the import to an explicit Git base:

```bash
vibe migrate --target /path/to/repo \
  --requirement REQ-001 --base HEAD
vibe migrate --target /path/to/repo --all --base refs/heads/main
```

Migration validates every selected legacy tree before producing a mapping,
preserves exact bytes and modes under
`.vibe-coding/schema3-backups/MIG-*/`, and creates immutable claims. `ACCEPTED`
and `DEGRADED` become `IMPORTED_READ_ONLY`; `ACTIVE` and `BLOCKED` become
`PAUSED` with `SCHEMA3_REPLAN_REQUIRED`. Changed source bytes or a different
base conflict with the stable claim. Dirty product bytes may be archived as
historical context, but never become the Schema 4 base.

## Safety invariants

- The Controller alone mutates run state and protected Vibe refs.
- Planner and Evaluator operations are read-only and audited.
- Workers may change only their declared path scope; exclusive resources
  prevent unsafe parallel overlap.
- Provider output is untrusted until strict JSON/schema and identity checks
  pass.
- Attempt, source-commit, integration, evaluation, stop, and migration
  protocols are retryable across recorded crash windows.
- Goal success requires completed tasks, current verification evidence, an
  independent PASS, and the exact integration head.
- The tool never automatically merges, pushes, opens a pull request, or
  publishes a package.

## Development

```bash
python -m pip install --no-deps -e .
PYTHONDONTWRITEBYTECODE=1 \
  python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q src/vibe
```

The offline suite is the default gate. A real Codex CLI smoke test is opt-in
through its documented environment flag; CI does not contact a Provider by
default.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request, use
[SUPPORT.md](SUPPORT.md) for public help, and report vulnerabilities privately
according to [SECURITY.md](SECURITY.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
