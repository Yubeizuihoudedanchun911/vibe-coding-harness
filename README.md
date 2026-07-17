# Vibe Coding Harness

[![CI](https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)

[简体中文](README.zh-CN.md)

Vibe Coding Harness is a Codex Skill for software goals that span multiple sessions. It separates planning, implementation, and evaluation into isolated agent roles while keeping durable, requirement-scoped recovery evidence in the target Git repository.

## Why it exists

Long-running coding tasks fail when intent, implementation state, or evaluation evidence exists only in chat history. This Skill keeps the user-facing workflow simple while enforcing:

- one audited, instruction-level read-only Planner per requirement;
- one business-code-writing Generator;
- one independently contextualized, audited Evaluator;
- serial role execution and bounded repair loops;
- snapshot-bound acceptance evidence for Git-visible workspace bytes;
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
        ├── evaluation-inputs/
        │   ├── plan.md
        │   └── implementation.md
        ├── attempts/
        │   └── NNN.md
        ├── implementation.md
        ├── review.md
        └── interruption.json
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

Codex handles role selection and the implementation/evaluation loop. Schema 3 uses explicit evaluation transactions:

```bash
python3 scripts/harness.py init --target /path/to/repo \
  --goal "Build and verify an MVP expense tracker"
python3 scripts/harness.py snapshot --target /path/to/repo
python3 scripts/harness.py begin-evaluation --target /path/to/repo \
  --requirement REQ-001
python3 scripts/harness.py record-review --target /path/to/repo \
  --requirement REQ-001 \
  --review-source /tmp/review.md
# Run only when the evaluated snapshot has drifted:
python3 scripts/harness.py restart-evaluation --target /path/to/repo \
  --requirement REQ-001 --reason "Describe the observed drift"
python3 scripts/harness.py accept --target /path/to/repo \
  --requirement REQ-001
python3 scripts/harness.py check --final --target /path/to/repo \
  --requirement REQ-001
```

`begin-evaluation` freezes the exact goal, plan, implementation handoff, Git revision, and product workspace bytes as one transaction. It stores `pending_evaluation` before archiving the plan and implementation under `evaluation-inputs/`, then returns the transaction identity and hashes that the Evaluator must repeat. If this write is interrupted, rerun `begin-evaluation`; it completes matching prepared inputs or safely reprepares current ones. `init --resume` deliberately reports this marker instead of reconciling it. The `record-review` source must be a regular file outside the target repository.

Review persistence is a two-phase transaction. A prepared marker is stored before `review.md` changes, so `init --resume` can deterministically finish an interrupted write. Replacing a PASS or UNVERIFIED review archives the exact prior bytes under `attempts/`; a FAIL creates a hash-bound historical receipt before advancing the round. `restart-evaluation` similarly prepares and validates `interruption.json`, records its digest in history, and starts a fresh build round only for real transaction-input or evidence-artifact drift.

Schema 3 is a breaking change: schema 2 state is intentionally unsupported and is not migrated. Evaluation records and interruption records use their own schema 2 contracts; their schema 1 forms are also unsupported.

## Safety invariants

- Root orchestrates and persists evidence but never writes business code.
- Planner and Evaluator are instruction-level read-only roles audited with before/after workspace snapshots.
- Generator is the only business-code writer.
- Roles run serially.
- A PASS review binds schema 2 typed observations to the requirement ID, round, exact goal/plan/implementation hashes, revision, complete workspace fingerprint, acceptance IDs, and review bytes. Free-text summaries are explanatory only; evidence must carry exact values, finite metrics, or repository-relative artifacts whose SHA-256 matches current bytes.
- Existing dirty files are allowed, but raw tracked bytes, staged, unstaged, non-ignored untracked, and recursively inspected submodule content becomes part of the evaluated fingerprint. Clean filters and `assume-unchanged` cannot mask tracked changes.
- Snapshotting disables external diff and text-conversion helpers.
- Archived transaction inputs, replaced review attempts, failed-evaluation receipts, and interruption receipts are rehashed during validation; forged or mutated history is rejected.
- Lifecycle writes use prepared state markers and atomic file replacement so retries either complete the recorded transaction or fail closed. Orphan lifecycle files are never trusted as committed transaction authority.
- Evaluation is bounded at 999 rounds, and filesystem/Unicode failures are returned as structured CLI errors without tracebacks.
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
