# Vibe Coding Harness

[![CI](https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)

[简体中文](README.zh-CN.md)

Vibe Coding Harness is a Codex Skill for software goals that span multiple sessions. It separates planning, implementation, and evaluation into isolated agent roles while keeping durable, requirement-scoped recovery evidence in the target Git repository.

## Why it exists

Long-running coding tasks fail when intent, implementation state, or evaluation evidence exists only in chat history. This Skill keeps the user-facing workflow simple while enforcing:

- one read-only Planner per requirement;
- one business-code-writing Generator;
- one independent read-only Evaluator;
- serial role execution and bounded repair loops;
- Git-bound acceptance evidence;
- durable recovery across context and session boundaries.

## How it works

```text
User goal
   |
   v
Root orchestrator
   |
   +--> Planner (read-only, once)
   |
   +--> Generator (only business-code writer)
   |          ^
   |          |
   +--> Evaluator (read-only) -- FAIL --> repair round
                    |
                    +-- PASS --> Goal Gate --> ACCEPTED
```

Each goal owns a durable record:

```text
.vibe-coding/requirements/REQ-NNN/
├── state.json
├── plan.md
└── rounds/
    └── NNN/
        ├── implementation.md
        └── review.md
```

The repository artifacts are the recovery contract. Agent chat is a control channel, not the source of truth.

## Requirements

- Codex with multi-agent tools: `spawn_agent`, `followup_task`, and `wait_agent`
- Git
- Python 3.10 or newer

If multi-agent tools are unavailable, the Skill records the requirement as blocked instead of silently letting the Root agent implement business code.

## Install

Clone the repository into the Codex skills directory:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
git clone --branch master --single-branch \
  https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/vibe-coding-harness"
```

To update an existing installation:

```bash
git -C "${CODEX_HOME:-$HOME/.codex}/skills/vibe-coding-harness" pull --ff-only
```

## Use

Start a Codex task with a user-visible goal:

```text
Use $vibe-coding-harness to build and verify an MVP expense tracker.
```

Codex handles role selection and the implementation/evaluation loop. The runtime CLI is primarily for deterministic state initialization, recovery, and validation:

```bash
python scripts/harness.py init \
  --target /path/to/git/repository \
  --goal "Build and verify an MVP expense tracker"

python scripts/harness.py init \
  --resume \
  --target /path/to/git/repository \
  --requirement REQ-001

python scripts/harness.py check \
  --target /path/to/git/repository \
  --requirement REQ-001
```

## Safety invariants

- Root orchestrates and persists evidence but never writes business code.
- Planner and Evaluator remain read-only.
- Generator is the only business-code writer.
- Roles run serially.
- A PASS review must contain substantive evidence for the exact Git revision.
- Historical requirement and evaluation artifacts remain intact.
- Unrelated user changes are never staged automatically.

See [SKILL.md](SKILL.md) for the complete agent protocol.

## Development

Run the complete test suite:

```bash
PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The project uses only the Python standard library at runtime.

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Report vulnerabilities privately according to [SECURITY.md](SECURITY.md). Community participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
