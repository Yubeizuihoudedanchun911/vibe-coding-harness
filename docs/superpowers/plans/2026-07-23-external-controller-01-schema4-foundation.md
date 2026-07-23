# External Controller Phase 1: Schema 4 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish an installable Python 3.10 package, frozen Schema 4 domain contracts, crash-safe immutable artifacts, revision-CAS state, and effective run configuration.

**Architecture:** Pure domain objects and transition checks live in `models.py`; JSON/config parsing stays in `config.py`; `StateStore` owns all filesystem durability and locking without deciding lifecycle policy. The phase deliberately creates no Provider process and moves no Git ref, so the file transaction boundary can be reviewed independently.

**Tech Stack:** Python 3.10+ standard library, `dataclasses`, `enum`, `fcntl`, `argparse`, `unittest`, setuptools `src` layout.

## Global Constraints

- Keep runtime dependencies empty.
- Do not use Python 3.11-only APIs such as `StrEnum`, `tomllib`, or `TaskGroup`.
- `state.json` is the only mutable authority; every other run artifact is write-once and SHA-256-bound.
- State changes require the run lock and expected revision.
- Write artifacts before state, fsync files and parent directories, then increment revision exactly once.
- A crash-created artifact not referenced by committed state remains an orphan and is never adopted automatically.
- Strict JSON rejects duplicate keys, invalid Unicode scalar values, booleans in integer fields, and unknown object fields.
- Defaults are `max_workers=4`, `task_attempts=3`, `provider_retries=3`, `evidence_rounds=3`, `repair_rounds=3`, and `max_plan_tasks=128`.
- Config commands form the complete explicitly user-authorized catalog. A non-empty repository catalog requires the creation-time `--allow-project-commands` acknowledgement, whose mode and source digest are frozen. Each command has a unique stable ID plus `argv[]`, repo-relative `cwd`, positive timeout, and environment-name allowlist; no shell string is accepted. Agent-authored objects may reference catalog IDs only.
- Provider secret values are never persisted.
- Follow RED-GREEN-REFACTOR and make one commit per task.

---

## File Map

- Create `pyproject.toml`: package metadata, `src` discovery, console script, and root resource installation.
- Modify `.gitignore`: ignore local build/editable-install products.
- Create `src/vibe/__init__.py`: package version.
- Create `src/vibe/__main__.py`: `python -m vibe` entry point.
- Create `src/vibe/cli.py`: temporary parser shell used only to prove packaging; public commands are completed in Phase 5.
- Create `src/vibe/models.py`: enums, immutable contracts, Schema 4 validation, pure transitions, Goal Gate.
- Create `src/vibe/config.py`: strict `vibe.json` parsing, defaults, CLI overrides, command validation.
- Create `src/vibe/state_store.py`: run paths, OS lock, strict JSON, immutable artifact writes, state revision CAS.
- Create `tests/__init__.py`: explicit test package for shared fixture imports.
- Create `tests/test_models.py`: package and state-contract tests.
- Create `tests/test_state_store.py`: durability, immutability, lock, CAS, and orphan tests.
- Create `tests/test_config.py`: effective configuration and command-policy tests.

### Task 1: Package Skeleton and Schema 4 Domain Contracts

**Files:**
- Create: `pyproject.toml`
- Modify: `.gitignore`
- Create: `src/vibe/__init__.py`
- Create: `src/vibe/__main__.py`
- Create: `src/vibe/cli.py`
- Create: `src/vibe/models.py`
- Create: `tests/__init__.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Produces `vibe.__version__: str`.
- Produces `vibe.cli.main(argv: Sequence[str] | None = None) -> int`.
- Produces `RunStatus`, `TaskStatus`, `AttemptStatus`, `ProviderStatus`, and `EvaluationVerdict`.
- Produces immutable `ArtifactRef`, `AttemptManifest`, `CommandSpec`, `FrozenRunConfig`, `AcceptanceCriterion`, `TaskContract`, `PlanDocument`, `WorkerResult`, `EvaluationCriterion`, `EvaluationFinding`, and `EvaluationResult`.
- Produces `validate_run_state(value: object) -> dict[str, object]`.
- Produces `validate_attempt_manifest(value: object) -> AttemptManifest`.
- Produces `validate_bound_state_semantics(state, artifact_loader) -> None`.
- Produces `transition_run(state: dict[str, object], target: RunStatus) -> dict[str, object]`.
- Produces `goal_gate_satisfied(state, evaluation_envelope, actual_integration_head) -> bool`.

- [ ] **Step 1: Write failing package and model tests**

Create an empty `tests/__init__.py`, then create `tests/test_models.py` with these concrete contract cases:

```python
from __future__ import annotations

import copy
import unittest

from vibe.models import (
    AttemptStatus,
    ContractError,
    EvaluationVerdict,
    RunStatus,
    TaskStatus,
    goal_gate_satisfied,
    transition_run,
    validate_run_state,
)


def minimal_state() -> dict[str, object]:
    return {
        "schema_version": 4,
        "run_id": "RUN-20260723-001",
        "revision": 0,
        "goal": "Build the external controller",
        "repository": {
            "identity": "sha256:" + "1" * 64,
            "base_ref": "refs/heads/main",
            "base_sha": "a" * 40,
            "integration_ref": "refs/heads/vibe/run-RUN-20260723-001",
            "integration_head": "a" * 40,
        },
        "status": "CREATED",
        "resume_status": None,
        "plan_version": 0,
        "repair_round": 0,
        "max_repair_rounds": 3,
        "max_workers": 4,
        "controller": None,
        "creation": {
            "intent": {
                "path": "creation.intent.json",
                "sha256": "sha256:" + "6" * 64,
            },
            "receipt": None,
        },
        "config": {
            "path": "config.json",
            "sha256": "sha256:" + "2" * 64,
        },
        "artifact_index": [
            {
                "path": "creation.intent.json",
                "sha256": "sha256:" + "6" * 64,
            },
            {
                "path": "config.json",
                "sha256": "sha256:" + "2" * 64,
            },
        ],
        "plans": [],
        "role_attempts": {"planner": [], "evaluator": []},
        "role_runtime": {
            role: {
                "operation_id": None,
                "attempt_no": 0,
                "failure_count": 0,
                "max_attempts": 3,
                "active_attempt_token": None,
                "last_error": None,
            }
            for role in ("planner", "evaluator")
        },
        "evaluations": [],
        "verifications": [],
        "legacy_import": None,
        "tasks": {},
        "pending_dispatches": {},
        "pending_source_commit": None,
        "pending_integration": None,
        "pending_evaluation": None,
        "latest_evaluation": None,
        "global_verification": None,
        "stop_receipts": [],
        "last_error": None,
        "created_at": "2026-07-23T10:00:00+08:00",
        "updated_at": "2026-07-23T10:00:00+08:00",
    }


class ModelContractTests(unittest.TestCase):
    def test_enums_keep_run_task_attempt_and_verdict_semantics_separate(self) -> None:
        self.assertEqual(RunStatus.PAUSED.value, "PAUSED")
        self.assertEqual(TaskStatus.READY_TO_INTEGRATE.value, "READY_TO_INTEGRATE")
        self.assertEqual(AttemptStatus.ABANDONED.value, "ABANDONED")
        self.assertEqual(EvaluationVerdict.UNVERIFIED.value, "UNVERIFIED")

    def test_validate_run_state_accepts_the_minimum_schema_four_shape(self) -> None:
        state = minimal_state()
        self.assertEqual(validate_run_state(state), state)

    def test_validate_run_state_rejects_boolean_revision_and_unknown_fields(self) -> None:
        state = minimal_state()
        state["revision"] = True
        with self.assertRaisesRegex(ContractError, "revision"):
            validate_run_state(state)

        state = minimal_state()
        state["unexpected"] = "value"
        with self.assertRaisesRegex(ContractError, "unknown run state fields"):
            validate_run_state(state)

    def test_validate_run_state_rejects_nested_unknowns_and_escaping_artifacts(self) -> None:
        state = minimal_state()
        state["controller"] = {
            "pid": 123,
            "process_start_identity": "linux:1234",
            "process_group": 123,
            "unexpected": True,
        }
        with self.assertRaisesRegex(ContractError, "controller fields"):
            validate_run_state(state)

        state = minimal_state()
        state["config"]["path"] = "../../outside.json"
        with self.assertRaisesRegex(ContractError, "artifact path"):
            validate_run_state(state)

    def test_pause_records_the_previous_activity_and_resume_restores_it(self) -> None:
        state = transition_run(minimal_state(), RunStatus.PLANNING)
        paused = transition_run(state, RunStatus.PAUSED)
        self.assertEqual(paused["status"], "PAUSED")
        self.assertEqual(paused["resume_status"], "PLANNING")

        restored = transition_run(paused, RunStatus.PLANNING)
        self.assertEqual(restored["status"], "PLANNING")
        self.assertIsNone(restored["resume_status"])

    def test_only_two_migration_pauses_may_omit_resume_status(self) -> None:
        for code in ("SCHEMA3_REPLAN_REQUIRED", "MIGRATION_INSTALLING"):
            state = minimal_state()
            state["status"] = "PAUSED"
            state["resume_status"] = None
            state["last_error"] = {
                "code": code,
                "message": "migration-owned pause",
                "retryable": code == "SCHEMA3_REPLAN_REQUIRED",
            }
            self.assertEqual(validate_run_state(state), state)

        invalid = minimal_state()
        invalid["status"] = "PAUSED"
        invalid["last_error"] = {
            "code": "ARBITRARY_PAUSE",
            "message": "must have a resume target",
            "retryable": True,
        }
        with self.assertRaisesRegex(ContractError, "resume_status"):
            validate_run_state(invalid)

    def test_invalid_transition_fails_without_mutating_the_input(self) -> None:
        state = minimal_state()
        with self.assertRaisesRegex(ContractError, "CREATED -> SUCCEEDED"):
            transition_run(state, RunStatus.SUCCEEDED)
        self.assertEqual(state["status"], "CREATED")

    def test_goal_gate_requires_bound_pass_and_no_pending_work(self) -> None:
        state = minimal_state()
        state["status"] = "EVALUATING"
        state["plan_version"] = 1
        plan_ref = {
            "path": "plan/plan-v001.json",
            "sha256": "sha256:" + "7" * 64,
        }
        state["plans"] = [plan_ref]
        attempt_ref = {
            "path": "tasks/TASK-001/attempts/001/attempt.json",
            "sha256": "sha256:" + "d" * 64,
        }
        state["tasks"] = {
            "TASK-001": {
                "status": "COMPLETED",
                "task": {
                    "path": "tasks/TASK-001/task.json",
                    "sha256": "sha256:" + "3" * 64,
                },
                "attempt_no": 1,
                "failure_count": 0,
                "max_attempts": 3,
                "active_attempt": None,
                "attempts": [attempt_ref],
                "result": {
                    "path": "tasks/TASK-001/attempts/001/result.json",
                    "sha256": "sha256:" + "8" * 64,
                },
                "verification": {
                    "path": "verification/tasks/TASK-001-a1/manifest.json",
                    "sha256": "sha256:" + "9" * 64,
                },
                "source_commits": ["b" * 40],
                "integrated_commits": ["c" * 40],
                "last_error": None,
            }
        }
        state["latest_evaluation"] = {
            "evaluation": {
                "path": "evaluations/001/evaluation.json",
                "sha256": "sha256:" + "4" * 64,
            },
            "verdict": "PASS",
            "evaluation_round": 1,
            "evidence_round": 0,
            "integration_head": "a" * 40,
        }
        state["global_verification"] = {
            "verification": {
                "path": "verification/global-001.json",
                "sha256": "sha256:" + "5" * 64,
            },
            "integration_head": "a" * 40,
            "passed": True,
        }
        state["evaluations"] = [state["latest_evaluation"]["evaluation"]]
        state["verifications"] = [
            state["tasks"]["TASK-001"]["verification"],
            state["global_verification"]["verification"],
        ]
        state["artifact_index"].extend(
            [
                plan_ref,
                state["tasks"]["TASK-001"]["task"],
                attempt_ref,
                state["tasks"]["TASK-001"]["result"],
                state["tasks"]["TASK-001"]["verification"],
                state["latest_evaluation"]["evaluation"],
                state["global_verification"]["verification"],
            ]
        )
        envelope = {
            "verdict": "PASS",
            "integration_head": "a" * 40,
            "evidence_catalog": {
                "verification:global": {
                    "integration_head": "a" * 40,
                    "verification": state["global_verification"]["verification"],
                    "criterion_ids": ["AC-001"],
                }
            },
            "criteria": [
                {
                    "id": "AC-001",
                    "verdict": "PASS",
                    "evidence_ids": ["verification:global"],
                }
            ],
            "findings": [],
        }

        self.assertTrue(goal_gate_satisfied(state, envelope, "a" * 40))

        unknown = copy.deepcopy(envelope)
        unknown["criteria"][0]["evidence_ids"] = ["unknown"]
        self.assertFalse(goal_gate_satisfied(state, unknown, "a" * 40))

        stale = copy.deepcopy(envelope)
        stale["evidence_catalog"]["verification:global"][
            "integration_head"
        ] = "b" * 40
        self.assertFalse(goal_gate_satisfied(state, stale, "a" * 40))

        wrong_criterion = copy.deepcopy(envelope)
        wrong_criterion["evidence_catalog"]["verification:global"][
            "criterion_ids"
        ] = ["AC-OTHER"]
        self.assertFalse(goal_gate_satisfied(state, wrong_criterion, "a" * 40))

        state["pending_dispatches"] = {"ATTEMPT-stale": {}}
        self.assertFalse(goal_gate_satisfied(state, envelope, "a" * 40))
        state["pending_dispatches"] = {}
        state["verifications"].remove(
            state["global_verification"]["verification"]
        )
        self.assertFalse(goal_gate_satisfied(state, envelope, "a" * 40))

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_models -v
```

Expected: import failure because `src/vibe/models.py` does not exist.

- [ ] **Step 3: Add exact package metadata and the temporary entry point**

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "vibe-coding-harness"
version = "0.1.0.dev0"
description = "Recoverable external controller for parallel specialist coding agents"
readme = "README.md"
requires-python = ">=3.10"
license = { file = "LICENSE" }
authors = [{ name = "jijunling", email = "jijunling@kuaishou.com" }]
dependencies = []

[project.scripts]
vibe = "vibe.cli:main"

[tool.setuptools]
package-dir = { "" = "src" }
include-package-data = true

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.data-files]
"share/vibe/prompts/planner" = ["prompts/planner/*.md"]
"share/vibe/prompts/workers/base" = ["prompts/workers/base/*.md"]
"share/vibe/prompts/workers/implementation" = ["prompts/workers/implementation/*.md"]
"share/vibe/prompts/workers/testing" = ["prompts/workers/testing/*.md"]
"share/vibe/prompts/workers/performance" = ["prompts/workers/performance/*.md"]
"share/vibe/prompts/workers/code-quality" = ["prompts/workers/code-quality/*.md"]
"share/vibe/prompts/workers/documentation" = ["prompts/workers/documentation/*.md"]
"share/vibe/prompts/workers/general" = ["prompts/workers/general/*.md"]
"share/vibe/prompts/evaluator" = ["prompts/evaluator/*.md"]
"share/vibe/schemas" = ["schemas/*.json"]
```

Append these exact ignore rules to `.gitignore`:

```gitignore
.venv/
build/
dist/
*.egg-info/
```

Create `src/vibe/__init__.py`:

```python
"""Vibe Coding Harness external controller."""

__version__ = "0.1.0.dev0"
```

Create `src/vibe/__main__.py`:

```python
from vibe.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
```

Create the Phase 1 `src/vibe/cli.py` shell:

```python
from __future__ import annotations

import argparse
from collections.abc import Sequence


COMMANDS = ("run", "resume", "status", "stop", "logs", "migrate")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vibe")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0.dev0")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        subparsers.add_parser(command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
```

- [ ] **Step 4: Implement the complete Phase 1 model surface**

Create `src/vibe/models.py`. Use `Enum` mixed with `str`, immutable dataclasses, exact field sets, and deep-copy transitions:

```python
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Any


DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
RUN_ID_RE = re.compile(r"RUN-\d{8}-\d{3}\Z")
TASK_ID_RE = re.compile(r"TASK-\d{3}\Z")
ACCEPTANCE_ID_RE = re.compile(r"AC-\d{3}\Z")


class VibeError(ValueError):
    """Base error for expected Vibe contract failures."""


class ContractError(VibeError):
    """Raised when persisted or Agent-provided data violates a contract."""


class StateConflictError(VibeError):
    """Raised when an expected state or Git identity changed."""


class RunStatus(str, Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    GLOBAL_VERIFYING = "GLOBAL_VERIFYING"
    EVALUATING = "EVALUATING"
    REPAIRING = "REPAIRING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    IMPORTED_READ_ONLY = "IMPORTED_READ_ONLY"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    READY_TO_INTEGRATE = "READY_TO_INTEGRATE"
    INTEGRATING = "INTEGRATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AttemptStatus(str, Enum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"


class ProviderStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class EvaluationVerdict(str, Enum):
    PASS = "PASS"
    NEEDS_REPAIR = "NEEDS_REPAIR"
    UNVERIFIED = "UNVERIFIED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    sha256: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class CommandSpec:
    id: str
    purpose: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: int = 900
    env_allowlist: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "purpose": self.purpose,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "env_allowlist": list(self.env_allowlist),
        }


@dataclass(frozen=True)
class CommandAuthorization:
    mode: str
    source_path: str | None
    source_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True)
class FrozenRunConfig:
    provider_name: str
    max_workers: int
    task_attempts: int
    provider_retries: int
    evidence_rounds: int
    repair_rounds: int
    max_plan_tasks: int
    command_catalog: tuple[CommandSpec, ...]
    required_command_ids: tuple[str, ...]
    command_authorization: CommandAuthorization

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": {"name": self.provider_name},
            "scheduler": {"max_workers": self.max_workers},
            "limits": {
                "task_attempts": self.task_attempts,
                "provider_retries": self.provider_retries,
                "evidence_rounds": self.evidence_rounds,
                "repair_rounds": self.repair_rounds,
                "max_plan_tasks": self.max_plan_tasks,
            },
            "verification": {
                "command_catalog": [
                    command.as_dict() for command in self.command_catalog
                ],
                "required_command_ids": list(self.required_command_ids),
                "authorization": self.command_authorization.as_dict(),
            },
        }


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    description: str


@dataclass(frozen=True)
class TaskContract:
    id: str
    objective: str
    worker_type: str
    covers: tuple[str, ...]
    depends_on: tuple[str, ...]
    path_scope: tuple[str, ...]
    exclusive_resources: tuple[str, ...]
    acceptance_checks: tuple[str, ...]
    max_attempts: int


@dataclass(frozen=True)
class PlanDocument:
    schema_version: int
    plan_version: int
    summary: str
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    global_verification: tuple[str, ...]
    tasks: tuple[TaskContract, ...]


@dataclass(frozen=True)
class WorkerCheck:
    command_id: str
    exit_code: int
    summary: str


@dataclass(frozen=True)
class WorkerResult:
    schema_version: int
    task_id: str
    attempt_no: int
    attempt_token: str
    status: str
    task_base_sha: str
    changed_paths: tuple[str, ...]
    checks: tuple[WorkerCheck, ...]
    residual_risks: tuple[str, ...]
    blocker: str | None


@dataclass(frozen=True)
class EvaluationCriterion:
    id: str
    verdict: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationFinding:
    criterion_id: str
    severity: str
    evidence: str
    affected_paths: tuple[str, ...]
    repair_hint: str


@dataclass(frozen=True)
class EvaluationResult:
    schema_version: int
    verdict: EvaluationVerdict
    criteria: tuple[EvaluationCriterion, ...]
    findings: tuple[EvaluationFinding, ...]
    evidence_requests: tuple[str, ...]
    residual_risks: tuple[str, ...]


RUN_FIELDS = {
    "schema_version",
    "run_id",
    "revision",
    "goal",
    "repository",
    "status",
    "resume_status",
    "plan_version",
    "repair_round",
    "max_repair_rounds",
    "max_workers",
    "controller",
    "creation",
    "config",
    "artifact_index",
    "plans",
    "role_attempts",
    "role_runtime",
    "evaluations",
    "verifications",
    "legacy_import",
    "tasks",
    "pending_dispatches",
    "pending_source_commit",
    "pending_integration",
    "pending_evaluation",
    "latest_evaluation",
    "global_verification",
    "stop_receipts",
    "last_error",
    "created_at",
    "updated_at",
}

ALLOWED_RUN_TRANSITIONS = {
    RunStatus.CREATED: {RunStatus.PLANNING, RunStatus.PAUSED, RunStatus.STOPPED},
    RunStatus.PLANNING: {
        RunStatus.EXECUTING,
        RunStatus.PAUSED,
        RunStatus.STOPPED,
        RunStatus.FAILED,
    },
    RunStatus.EXECUTING: {
        RunStatus.GLOBAL_VERIFYING,
        RunStatus.PAUSED,
        RunStatus.STOPPED,
        RunStatus.FAILED,
    },
    RunStatus.GLOBAL_VERIFYING: {
        RunStatus.EVALUATING,
        RunStatus.REPAIRING,
        RunStatus.PAUSED,
        RunStatus.STOPPED,
        RunStatus.FAILED,
    },
    RunStatus.EVALUATING: {
        RunStatus.EVALUATING,
        RunStatus.REPAIRING,
        RunStatus.PAUSED,
        RunStatus.STOPPED,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
    },
    RunStatus.REPAIRING: {
        RunStatus.EXECUTING,
        RunStatus.PAUSED,
        RunStatus.STOPPED,
        RunStatus.FAILED,
    },
    RunStatus.PAUSED: {
        RunStatus.CREATED,
        RunStatus.PLANNING,
        RunStatus.EXECUTING,
        RunStatus.GLOBAL_VERIFYING,
        RunStatus.EVALUATING,
        RunStatus.REPAIRING,
        RunStatus.STOPPED,
    },
    RunStatus.STOPPED: {
        RunStatus.CREATED,
        RunStatus.PLANNING,
        RunStatus.EXECUTING,
        RunStatus.GLOBAL_VERIFYING,
        RunStatus.EVALUATING,
        RunStatus.REPAIRING,
    },
    RunStatus.SUCCEEDED: set(),
    RunStatus.FAILED: set(),
    RunStatus.IMPORTED_READ_ONLY: set(),
}


def _require_plain_int(value: object, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{field} must be an integer >= {minimum}")
    return value


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ContractError(f"{field} contains an invalid Unicode scalar")
    return value


def reject_invalid_json_scalars(value: object, field: str = "$") -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ContractError(f"{field} contains an invalid Unicode scalar")
    elif isinstance(value, dict):
        for key, item in value.items():
            reject_invalid_json_scalars(key, f"{field}.<key>")
            reject_invalid_json_scalars(item, f"{field}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_invalid_json_scalars(item, f"{field}[{index}]")


def _require_artifact(value: object, field: str) -> None:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ContractError(f"{field} must be an ArtifactRef")
    raw_path = _require_string(value["path"], f"{field}.path")
    path = PurePosixPath(raw_path)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or raw_path != path.as_posix()
        or path.parts[0] in {"state.json", "controller.lock"}
    ):
        raise ContractError(f"{field} has an invalid artifact path")
    digest = _require_string(value["sha256"], f"{field}.sha256")
    if DIGEST_RE.fullmatch(digest) is None:
        raise ContractError(f"{field}.sha256 must be canonical")


def validate_run_state(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ContractError("run state must be an object")
    reject_invalid_json_scalars(value)
    unknown = set(value) - RUN_FIELDS
    missing = RUN_FIELDS - set(value)
    if unknown:
        raise ContractError(f"unknown run state fields: {sorted(unknown)}")
    if missing:
        raise ContractError(f"missing run state fields: {sorted(missing)}")
    if value["schema_version"] != 4 or type(value["schema_version"]) is not int:
        raise ContractError("schema_version must be integer 4")
    run_id = _require_string(value["run_id"], "run_id")
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ContractError("run_id must match RUN-YYYYMMDD-NNN")
    _require_plain_int(value["revision"], "revision")
    _require_string(value["goal"], "goal")
    try:
        RunStatus(value["status"])
    except (TypeError, ValueError) as error:
        raise ContractError("status is invalid") from error
    if value["resume_status"] is not None:
        resume = RunStatus(value["resume_status"])
        if resume in {
            RunStatus.PAUSED,
            RunStatus.STOPPED,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.IMPORTED_READ_ONLY,
        }:
            raise ContractError("resume_status must name an active state")
    _require_plain_int(value["plan_version"], "plan_version")
    _require_plain_int(value["repair_round"], "repair_round")
    _require_plain_int(value["max_repair_rounds"], "max_repair_rounds", 1)
    _require_plain_int(value["max_workers"], "max_workers", 1)
    creation = value["creation"]
    if not isinstance(creation, dict) or set(creation) != {"intent", "receipt"}:
        raise ContractError("creation must contain intent and receipt")
    _require_artifact(creation["intent"], "creation.intent")
    if creation["receipt"] is not None:
        _require_artifact(creation["receipt"], "creation.receipt")
    _require_artifact(value["config"], "config")
    for field in ("tasks", "pending_dispatches"):
        if not isinstance(value[field], dict):
            raise ContractError(f"{field} must be an object")
    if not isinstance(value["stop_receipts"], list):
        raise ContractError("stop_receipts must be an array")
    repository = value["repository"]
    if not isinstance(repository, dict):
        raise ContractError("repository must be an object")
    repository_fields = {
        "identity",
        "base_ref",
        "base_sha",
        "integration_ref",
        "integration_head",
    }
    if set(repository) != repository_fields:
        raise ContractError("repository fields are invalid")
    for field in ("base_sha", "integration_head"):
        oid = _require_string(repository[field], f"repository.{field}")
        if OID_RE.fullmatch(oid) is None:
            raise ContractError(f"repository.{field} must be a full commit OID")
    _validate_nested_run_fields(value)
    return copy.deepcopy(value)


def transition_run(
    state: dict[str, object],
    target: RunStatus,
) -> dict[str, object]:
    current = RunStatus(state["status"])
    if target not in ALLOWED_RUN_TRANSITIONS[current]:
        raise ContractError(f"invalid run transition {current.value} -> {target.value}")
    updated = copy.deepcopy(state)
    if target in {RunStatus.PAUSED, RunStatus.STOPPED}:
        if current not in {RunStatus.PAUSED, RunStatus.STOPPED}:
            updated["resume_status"] = current.value
    elif current in {RunStatus.PAUSED, RunStatus.STOPPED}:
        if updated["resume_status"] != target.value:
            raise ContractError(
                f"resume target {target.value} does not match "
                f"{updated['resume_status']}"
            )
        updated["resume_status"] = None
    updated["status"] = target.value
    return updated


def goal_gate_satisfied(
    state: dict[str, object],
    evaluation_envelope: dict[str, object],
    actual_integration_head: str,
) -> bool:
    repository = state["repository"]
    latest = state.get("latest_evaluation")
    verification = state.get("global_verification")
    tasks = state.get("tasks", {})
    evidence_catalog = evaluation_envelope.get("evidence_catalog")
    criteria = evaluation_envelope.get("criteria")
    evidence_is_complete = (
        isinstance(evidence_catalog, dict)
        and isinstance(criteria, list)
        and bool(criteria)
        and all(
            isinstance(item, dict)
            and item.get("verdict") == "PASS"
            and isinstance(item.get("evidence_ids"), list)
            and bool(item["evidence_ids"])
            and all(
                isinstance(evidence_id, str)
                and isinstance(evidence_catalog.get(evidence_id), dict)
                and evidence_catalog[evidence_id].get("integration_head")
                == repository.get("integration_head")
                and item.get("id")
                in evidence_catalog[evidence_id].get("criterion_ids", [])
                and evidence_catalog[evidence_id].get("verification")
                in state.get("verifications", [])
                for evidence_id in item["evidence_ids"]
            )
            for item in criteria
        )
    )
    return bool(
        state.get("plan_version", 0) >= 1
        and state.get("plan_version") == len(state.get("plans", []))
        and bool(tasks)
        and all(
            isinstance(task, dict) and task.get("status") == TaskStatus.COMPLETED.value
            for task in tasks.values()
        )
        and all(
            bool(task.get("attempts"))
            and isinstance(task.get("result"), dict)
            and isinstance(task.get("verification"), dict)
            and task.get("verification") in state.get("verifications", [])
            for task in tasks.values()
        )
        and isinstance(repository, dict)
        and isinstance(latest, dict)
        and isinstance(verification, dict)
        and latest.get("verdict") == EvaluationVerdict.PASS.value
        and bool(state.get("evaluations"))
        and latest.get("evaluation") == state.get("evaluations", [])[-1]
        and latest.get("integration_head") == repository.get("integration_head")
        and evaluation_envelope.get("verdict") == EvaluationVerdict.PASS.value
        and evaluation_envelope.get("integration_head")
        == repository.get("integration_head")
        and evidence_is_complete
        and not evaluation_envelope.get("findings")
        and verification.get("passed") is True
        and verification.get("verification") in state.get("verifications", [])
        and verification.get("integration_head") == repository.get("integration_head")
        and actual_integration_head == repository.get("integration_head")
        and not state.get("pending_dispatches")
        and state.get("pending_source_commit") is None
        and state.get("pending_integration") is None
        and state.get("pending_evaluation") is None
        and state.get("last_error") is None
    )
```

Implement `_validate_nested_run_fields()` with strict unknown-field rejection and this frozen matrix. Every integer uses `type(value) is int` so booleans are rejected; every OID, digest, ID, enum, and Artifact path uses the helpers above.

| State location | Exact shape and invariants |
|---|---|
| `repository` | exact five fields already shown; `identity` is a canonical SHA-256 digest; `base_ref` is `refs/**`, literal `HEAD`, or a full OID; `integration_ref == f"refs/heads/vibe/run-{run_id}"` |
| `controller` | `null` or exactly `{pid, process_start_identity, process_group, controller_token}` with positive integers and non-empty identities |
| `creation` | exactly `{intent, receipt}`; intent is an `ArtifactRef`, receipt is `null` only during creation or an `ArtifactRef` |
| `artifact_index` | append-only list of unique-path ArtifactRefs; duplicate paths require the same digest |
| `plans`, `evaluations`, `verifications` | append-only ArtifactRef lists; `plan_version == len(plans)` |
| `role_attempts` | exactly `{planner, evaluator}`, each an append-only ArtifactRef list |
| `role_runtime[role]` | exactly `{operation_id, attempt_no, failure_count, max_attempts, active_attempt_token, last_error}`; operation/token may be null; `attempt_no` is monotonic inside one operation and resets to `0` only when a new unique operation is opened, `0 <= failure_count <= max_attempts`, and an active token matches exactly one pending dispatch of that role and operation |
| `legacy_import` | `null` for native runs or one ArtifactRef for an imported run |
| `tasks[task_id]` | key matches `TASK_ID_RE`; exactly `{task, status, attempt_no, failure_count, max_attempts, active_attempt, attempts, result, verification, source_commits, integrated_commits, last_error}`; `attempt_no` is a monotonic identity, `0 <= failure_count <= max_attempts`, refs/attempts are bound, and commit arrays contain full OIDs |
| `active_attempt` | `null` or exactly `{attempt_token, status, created_at, task_base_sha, branch, worktree, preflight, provider_handle, result_path}`; status is `AttemptStatus`; `attempt_token` is the current Provider-owner fencing token and may rotate only for a transient Provider retry, while semantic identity is Task plus `attempt_no`; `created_at` is immutable RFC3339 and is the only source-commit timestamp; `preflight` may be null only for a `STARTING` Worker allocation whose ref/worktree has not yet been materialized, and otherwise is an immutable ArtifactRef; `result_path` is the immutable canonical semantic result path; paths are canonical run/target-relative paths |
| `pending_dispatches[token]` | exact roadmap wire shape; key equals `attempt_token`; `operation_id` and `attempt_created_at` match the owning runtime/active Attempt; role is `planner`, `worker`, or `evaluator`; Worker alone requires non-null task/branch; prompt/schema/preflight/request are ArtifactRefs and preflight equals the owning active Attempt; `provider_attempts` is an append-only ArtifactRef list carried across Provider retries; handle is `null` or exactly `{adapter, attempt_token, pid, process_start_identity, process_group, child_pid, child_process_start_identity, child_process_group}` |
| `pending_source_commit` | `null` or exactly the roadmap wire `{operation_id, task_id, attempt_no, expected_base, task_ref, tree_oid, candidate_commit, author_name, author_email, timestamp, message, source_audit}`; it matches one `RUNNING` Task whose active Attempt is `VERIFYING`, whose result is bound, and whose `source_commits` is still empty |
| `pending_integration` | `null` or exactly `{operation_id, task_id, attempt_no, expected_head, candidate_head, source_base, source_head, verification}` with full OIDs and an ArtifactRef |
| `pending_evaluation` | `null` or the exact roadmap wire `{operation_id, attempt_no, attempt_token, evaluation_round, evidence_round, integration_head, attempt, raw_result}`; it matches a frozen terminal Evaluator Attempt in `role_attempts.evaluator`, no active Evaluator token, and the current integration head |
| `latest_evaluation` | `null` or exactly `{evaluation, verdict, evaluation_round, evidence_round, integration_head}`; evaluation is an ArtifactRef |
| `global_verification` | `null` or exactly `{verification, integration_head, passed}`; verification is an `ArtifactRef` |
| `stop_receipts[]` | exactly `{nonce, receipt}` where receipt is an `ArtifactRef` |
| `last_error` | `null` or an object requiring non-empty `code` and `message`, boolean `retryable`, and allowing only optional ArtifactRef `evidence` in addition |
| `created_at`, `updated_at` | timezone-aware RFC3339 strings accepted by `datetime.fromisoformat`; `updated_at >= created_at` |

Also require lifecycle coherence:

- `resume_status` is non-null exactly for `PAUSED` and `STOPPED`, except a freshly migrated `PAUSED/SCHEMA3_REPLAN_REQUIRED` run and an incomplete `PAUSED/MIGRATION_INSTALLING` run, which have it null. `MIGRATION_INSTALLING` is never resumable/replannable and may transition only through the migration finalizer after its bound completed manifest/index claim are verified;
- `pending_source_commit` implies exactly one matching active Worker Attempt; `pending_integration` implies exactly one matching Task is `INTEGRATING`; `pending_evaluation` implies an `EVALUATING` run with a closed matching Evaluator Attempt. All three prepared markers are mutually exclusive;
- Task fields obey the complete status truth table below; successful Provider result acceptance leaves the Attempt `VERIFYING`, and only successful candidate verification freezes it;
- every `pending_dispatches` Worker token equals its matching Task active token; the converse is required once Provider dispatch preparation begins, while a `STARTING` Worker allocation may have no pending dispatch until its recorded ref/worktree is materialized and its immutable preflight is attached;
- a Worker pending handle is null exactly while the matching active `provider_handle` is null; binding a matching launch/handle atomically writes the identical handle to both views and changes active `STARTING` to `RUNNING`;
- `SUCCEEDED` requires state-local final pointers and no pending work; Controller separately calls `goal_gate_satisfied(state, envelope, actual_head)` because persisted-state validation alone cannot read Git truth;
- `FAILED`, `SUCCEEDED`, and `IMPORTED_READ_ONLY` have no pending dispatch, source commit, integration, or evaluation marker.

| Task status | Required field relationship |
|---|---|
| `PENDING`, `READY` | `active_attempt`, `result`, and `verification` are null; prior terminal `attempts` may exist after a retry/cancel; the next dispatch uses `attempt_no + 1` |
| `RUNNING` | `active_attempt` is non-null. A newly allocated `STARTING` Attempt has immutable semantic number/time/base/branch/worktree/result-path identity, a first owner token, null preflight/handle/result, and no pending dispatch; after idempotent ref/worktree creation, it remains `STARTING` while the Controller captures and atomically attaches the immutable preflight before preparing dispatch. Handle binding atomically changes it to `RUNNING`; thereafter the current owner token is pending-dispatch matched until Provider acceptance. Transient rotation changes only that owner token and atomically moves both handle views through null before binding the replacement. `result` is non-null only when active status is `VERIFYING`, where it is the stable semantic result ref published byte-identically from the final raw Provider result, pending dispatch is absent, and active `provider_handle` is null; `verification` is null |
| `READY_TO_INTEGRATE` | `active_attempt` is non-null with status `VERIFYING`; `result` and exactly one Controller-created `source_commit` are non-null/non-empty; `verification` is null; `pending_source_commit` is null |
| `INTEGRATING` | active is null; the final `attempts` entry is terminal `SUCCEEDED`; `result` and `verification` are non-null, and verification equals `pending_integration.verification` |
| `COMPLETED` | active is null; successful Attempt, `result`, and `verification` are non-null; `source_commits` and `integrated_commits` are non-empty |
| `FAILED`, `CANCELLED` | active is null; final Attempt is terminal with the matching status; `last_error` is non-null for `FAILED`; accepted result/verification pointers are optional only when failure occurred after they were frozen |

`role_attempts` is a terminal-manifest history, `role_runtime.attempt_no` is the operation-local identity sequence, and `role_runtime.failure_count` is the operation-local semantic failure-budget counter. Do not equate history length with either counter: a canceled attempt is frozen without incrementing failure count, Provider retries remain inside one semantic Attempt, and later operations restart at Attempt 1. One exact builder emits `roles/<planner|evaluator>/operations/<operation_id>/attempts/<NNN>/...`; manifest uniqueness is `(role, operation_id, attempt_no)`. Attempt manifests use the roadmap's strict Schema 1, and the active runtime token may not also appear in a terminal manifest.

`validate_run_state()` checks only state-local shape and pointer relationships; it cannot infer the contents of an `ArtifactRef`. `StateStore.load()` must therefore call `validate_bound_state_semantics()` after every referenced artifact has passed path/type/digest validation. That validator strictly parses every Attempt manifest and proves:

- the history owner equals manifest role/task and `(role, operation_id, attempt_no)` is unique;
- role attempt numbers are strictly increasing within each operation, while Task numbers are strictly increasing per Task;
- terminal token/status/time fields are coherent and no terminal token is active or pending;
- every non-null active/pending/terminal preflight view of one semantic Attempt binds the same immutable preflight, whose `created_at`, base, role/task, worktree, and protected/read-only snapshot agree with state; missing preflight is allowed only for the exact active `STARTING` Worker allocation above, or its `CANCELLED/CANCELLED_BEFORE_MATERIALIZATION` terminal manifest with every Provider/result/source/verification field null;
- every accepted Worker `Task.result` path equals that Attempt's canonical `active.result_path`, its digest equals the final raw Provider result digest, and the terminal Worker manifest binds the same stable ref while the raw ref remains in `artifact_index`;
- `INTEGRATING`/`COMPLETED` ends in a `SUCCEEDED` Worker manifest whose result/source-audit/verification refs equal state;
- `FAILED`/`CANCELLED` terminal state agrees with the final manifest;
- `pending_evaluation.attempt/raw_result` match the frozen Evaluator manifest.
- `global_verification.verification` parses as a passing global-kind
  verification manifest for the exact integration head. It must be present in
  `verifications`, but need not be its final entry because supplemental
  evidence manifests are append-only and may follow it.

Add a strict parser with exact fields/types and bounded ArtifactRef lists. A test must construct an Attempt file whose digest is correctly bound in state but whose role, status, token, or verification pointer disagrees with state, and require `StateStore.load()` to reject it. Digest validity alone is not semantic validity.

Add one table-driven test per row, changing a valid populated state by one wrong type, extra key, mismatched token, invalid OID/path, or incoherent lifecycle relation and asserting `ContractError`. Do not postpone nested validation to Controller code: `StateStore.load()` must reject malformed persisted state before recovery uses any path or process identity.

- [ ] **Step 5: Run package and model tests and verify GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_models -v
python3 -m pip install --no-deps -e .
vibe --help
```

Expected: model tests end in `OK`; editable install succeeds; help lists `run`, `resume`, `status`, `stop`, `logs`, and `migrate`.

- [ ] **Step 6: Commit Task 1**

```bash
git add pyproject.toml .gitignore src/vibe/__init__.py \
  src/vibe/__main__.py src/vibe/cli.py src/vibe/models.py \
  tests/__init__.py tests/test_models.py
git diff --cached --check
git commit -m "feat: add schema 4 package and domain contracts"
```

### Task 2: Crash-Safe StateStore and Immutable Artifacts

**Files:**
- Create: `src/vibe/state_store.py`
- Create: `tests/test_state_store.py`

**Interfaces:**
- Consumes `ArtifactRef`, `ContractError`, `StateConflictError`, and `validate_run_state`.
- Produces `canonical_json_bytes(value: object) -> bytes`.
- Produces `load_json_object(path: Path) -> dict[str, object]`.
- Produces `artifact_ref(relative_path: str, body: bytes) -> ArtifactRef`.
- Produces `StateStore.for_run(target: Path, run_id: str) -> StateStore`.
- Produces `StateStore.lock(blocking: bool = True)`.
- Produces `StateStore.prepare_artifact(relative_path, body)`.
- Produces `StateStore.create(initial_state, artifacts)`.
- Produces `StateStore.load()`.
- Produces `StateStore.transact(expected_revision, artifacts, mutate)`.
- Produces `StateStore.append_log(event)`.

- [ ] **Step 1: Write failing durability and transaction tests**

Create `tests/test_state_store.py` with a temporary run root, the `minimal_state()` helper from `test_models`, and these cases:

```python
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from tests.test_models import minimal_state
from vibe.models import ContractError, StateConflictError
from vibe.state_store import StateStore, artifact_ref, load_json_object


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name)
        self.store = StateStore.for_run(self.target, "RUN-20260723-001")

    def _create(self) -> dict[str, object]:
        config_body = b'{"frozen":true}\n'
        intent_body = b'{"run_id":"RUN-20260723-001"}\n'
        state = minimal_state()
        config_ref = artifact_ref("config.json", config_body)
        intent_ref = artifact_ref("creation.intent.json", intent_body)
        state["config"] = config_ref.as_dict()
        state["creation"] = {
            "intent": intent_ref.as_dict(),
            "receipt": None,
        }
        state["artifact_index"] = [
            intent_ref.as_dict(),
            config_ref.as_dict(),
        ]
        with self.store.lock():
            return self.store.create(
                state,
                {
                    "config.json": config_body,
                    "creation.intent.json": intent_body,
                },
            )

    def test_create_writes_artifact_before_bound_state(self) -> None:
        state = self._create()
        config = self.store.root / "config.json"
        self.assertEqual(config.read_bytes(), b'{"frozen":true}\n')
        self.assertEqual(self.store.load(), state)

    def test_artifact_is_write_once_but_identical_retry_is_idempotent(self) -> None:
        self._create()
        with self.store.lock():
            state = self.store.transact(
                0,
                {"config.json": b'{"frozen":true}\n'},
                lambda current, refs: current.update({"last_error": None}),
            )
        self.assertEqual(state["revision"], 1)

        with self.store.lock(), self.assertRaisesRegex(
            StateConflictError, "immutable artifact"
        ):
            self.store.transact(
                1,
                {"config.json": b'{"frozen":false}\n'},
                lambda current, refs: current.update({"last_error": None}),
            )

    def test_mutator_cannot_remove_a_newly_auto_indexed_artifact(self) -> None:
        self._create()

        def remove_new_index_entry(
            current: dict[str, object],
            refs: object,
        ) -> None:
            current["artifact_index"].pop()

        with self.store.lock(), self.assertRaisesRegex(
            ContractError, "artifact_index is append-only"
        ):
            self.store.transact(
                0,
                {"diagnostics/new.json": b"{}\n"},
                remove_new_index_entry,
            )
        self.assertFalse((self.store.root / "diagnostics/new.json").exists())

    def test_semantic_histories_cannot_be_removed_reordered_or_rewritten(self) -> None:
        self._create()
        plan_bodies = {
            "plan/plan-v001.json": b'{"plan_version":1}\n',
            "plan/plan-v002.json": b'{"plan_version":2}\n',
        }

        def append_plans(current: dict[str, object], refs: object) -> None:
            current["plans"].extend(
                refs[path].as_dict() for path in sorted(plan_bodies)
            )
            current["plan_version"] = 2

        with self.store.lock():
            committed = self.store.transact(
                0,
                plan_bodies,
                append_plans,
            )
        for mutation in (
            lambda state: state["plans"].clear(),
            lambda state: state["plans"].reverse(),
            lambda state: state["plans"][0].update(
                {"sha256": "sha256:" + "f" * 64}
            ),
        ):
            with self.subTest(mutation=mutation), self.store.lock():
                with self.assertRaisesRegex(ContractError, "append-only history"):
                    self.store.transact(
                        committed["revision"],
                        {},
                        lambda current, refs: mutation(current),
                    )

    def test_revision_conflict_does_not_write_artifacts(self) -> None:
        self._create()
        with self.store.lock(), self.assertRaisesRegex(
            StateConflictError, "expected revision 8"
        ):
            self.store.transact(
                8,
                {"tasks/TASK-001/task.json": b"{}\n"},
                lambda current, refs: None,
            )
        self.assertFalse((self.store.root / "tasks/TASK-001/task.json").exists())

    def test_orphan_artifact_is_not_adopted_by_load(self) -> None:
        self._create()
        orphan = self.store.root / "tasks/TASK-999/task.json"
        orphan.parent.mkdir(parents=True)
        orphan.write_text("{}\n", encoding="utf-8")
        self.assertNotIn("TASK-999", self.store.load()["tasks"])

    def test_tampered_bound_artifact_is_rejected_on_load(self) -> None:
        self._create()
        (self.store.root / "config.json").write_text(
            '{"frozen":false}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ContractError, "artifact digest"):
            self.store.load()

    def test_lock_rejects_a_second_nonblocking_writer(self) -> None:
        with self.store.lock():
            second = StateStore.for_run(self.target, "RUN-20260723-001")
            with self.assertRaisesRegex(StateConflictError, "run lock"):
                with second.lock(blocking=False):
                    self.fail("second writer acquired the lock")

    def test_same_store_lock_is_explicitly_non_reentrant(self) -> None:
        with self.store.lock(), self.assertRaisesRegex(
            StateConflictError, "non-reentrant"
        ):
            with self.store.lock():
                self.fail("nested lock unexpectedly succeeded")

    def test_same_store_mutation_requires_the_owning_thread(self) -> None:
        self._create()
        failures: list[BaseException] = []

        def mutate_from_other_thread() -> None:
            try:
                self.store.transact(0, {}, lambda state, refs: None)
            except BaseException as error:
                failures.append(error)

        with self.store.lock():
            thread = threading.Thread(target=mutate_from_other_thread)
            thread.start()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], StateConflictError)

    def test_strict_loader_rejects_duplicate_keys_and_symlink_state(self) -> None:
        self.store.root.mkdir(parents=True)
        self.store.state_path.write_text(
            '{"schema_version":4,"schema_version":4}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ContractError, "duplicate JSON key"):
            load_json_object(self.store.state_path)

        self.store.state_path.unlink()
        target = self.store.root / "elsewhere.json"
        target.write_text("{}\n", encoding="utf-8")
        self.store.state_path.symlink_to(target)
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            load_json_object(self.store.state_path)

    def test_strict_loader_rejects_non_finite_numbers(self) -> None:
        self.store.root.mkdir(parents=True)
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                self.store.state_path.write_text(
                    f'{{"value":{literal}}}\n',
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ContractError, "non-finite"):
                    load_json_object(self.store.state_path)

    def test_load_and_log_reject_a_symlinked_control_ancestor(self) -> None:
        self._create()
        control = self.target / ".vibe-coding"
        external = self.target / "relocated-control"
        control.rename(external)
        control.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            self.store.load()
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            self.store.append_log({"event": "must-not-escape"})

    def test_append_log_rejects_a_symlink_leaf(self) -> None:
        self._create()
        external = self.target / "external.log"
        external.write_text("", encoding="utf-8")
        self.store.log_path.parent.mkdir(parents=True)
        self.store.log_path.symlink_to(external)
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            self.store.append_log({"event": "must-not-escape"})

    def test_parent_swap_after_open_never_reads_or_writes_external_tree(self) -> None:
        self._create()
        external = self.target / "external"
        external.mkdir()
        sentinel = external / "state.json"
        sentinel.write_text("external\n", encoding="utf-8")
        with self.store.inject_after_run_dir_open(
            lambda: self.swap_run_directory_for_symlink(external)
        ):
            with self.assertRaises(ContractError):
                self.store.load()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "external\n")

    def test_racing_immutable_leaf_is_never_replaced(self) -> None:
        self._create()
        with self.store.inject_before_immutable_publish(
            lambda parent_fd, leaf: self.create_at(parent_fd, leaf, b"racer\n")
        ), self.store.lock(), self.assertRaisesRegex(
            StateConflictError,
            "immutable artifact",
        ):
            self.store.transact(
                0,
                {"diagnostics/race.json": b"controller\n"},
                lambda state, refs: None,
            )
        self.assertEqual(
            (self.store.root / "diagnostics/race.json").read_bytes(),
            b"racer\n",
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run StateStore tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_state_store -v
```

Expected: import failure because `vibe.state_store` does not exist.

- [ ] **Step 3: Implement strict JSON, descriptor-rooted writes, and the OS run lock**

Create `src/vibe/state_store.py` with these concrete primitives. The docstring-only low-level functions below are implementation contracts, not acceptable stubs: fill them with descriptor-relative POSIX operations. `StateStore` also implements `_open_run_directory`, `_snapshot_run_fd`, `_open_artifact_parent`, `_publish_or_verify_immutable`, `_verify_and_cache_bound_artifacts`, and `_sha256_bound_artifact` on top of those primitives.

```python
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from vibe.models import (
    ArtifactRef,
    ContractError,
    RUN_ID_RE,
    StateConflictError,
    reject_invalid_json_scalars,
    validate_bound_state_semantics,
    validate_run_state,
)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    raise ContractError(f"non-finite JSON number is forbidden: {value}")


def canonical_json_bytes(value: object) -> bytes:
    try:
        body = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded = body.encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ContractError(f"value is not canonical JSON: {error}") from error
    return encoded + b"\n"


def parse_json_object_bytes(body: bytes) -> dict[str, object]:
    try:
        text = body.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
        reject_invalid_json_scalars(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot parse JSON object: {error}") from error
    if not isinstance(value, dict):
        raise ContractError("JSON root must be an object")
    return value


def load_json_object(path: Path) -> dict[str, object]:
    descriptor = open_absolute_regular_no_follow(path)
    try:
        return parse_json_object_bytes(
            read_bounded(descriptor, max_bytes=16 * 1024 * 1024)
        )
    finally:
        os.close(descriptor)


def _relative_artifact_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or value != path.as_posix()
        or path.parts[0] in {"state.json", "controller.lock"}
    ):
        raise ContractError(f"invalid artifact path: {value!r}")
    return path


def artifact_ref(relative_path: str, body: bytes) -> ArtifactRef:
    _relative_artifact_path(relative_path)
    return ArtifactRef(
        path=relative_path,
        sha256="sha256:" + hashlib.sha256(body).hexdigest(),
    )


def open_absolute_regular_no_follow(path: Path) -> int:
    """Walk a canonical absolute path from `/` with dir_fd and O_NOFOLLOW."""


def open_absolute_directory_no_follow(path: Path) -> int:
    """Walk a canonical absolute directory from `/` with dir_fd and O_NOFOLLOW."""


def open_directory_chain(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    create: bool,
) -> int:
    """Open each component with O_DIRECTORY|O_NOFOLLOW; mkdirat+fsync if allowed."""


def read_regular_at(parent_fd: int, leaf: str, *, max_bytes: int) -> bytes:
    """Open with O_NOFOLLOW, fstat regular, then read at most max_bytes."""


def read_bounded(descriptor: int, *, max_bytes: int) -> bytes:
    """Loop over os.read and reject, rather than truncate, byte max_bytes + 1."""


def read_optional_regular_at(
    parent_fd: int,
    leaf: str,
    *,
    max_bytes: int,
) -> bytes | None:
    """Return None only for ENOENT; otherwise enforce a bounded regular leaf."""


def replace_mutable_at(parent_fd: int, leaf: str, body: bytes) -> None:
    """Write/fsync an O_EXCL temp, replaceat in parent_fd, then fsync parent_fd."""


def publish_immutable_at(parent_fd: int, leaf: str, body: bytes) -> None:
    """Write/fsync an O_EXCL temp, linkat create-if-absent, unlink, fsync parent."""


def append_complete_record_at(
    parent_fd: int,
    leaf: str,
    body: bytes,
    *,
    no_follow: bool,
) -> None:
    """Append every byte, fsync the file, then fsync the pinned parent."""


def _record_durability_event(event: str) -> None:
    """No-op production observer patched only by durability-order tests."""


class StateStore:
    def __init__(self, target: Path, run_id: str) -> None:
        if RUN_ID_RE.fullmatch(run_id) is None:
            raise ContractError("run_id must match RUN-YYYYMMDD-NNN")
        self.target = target.resolve()
        self.run_id = run_id
        self.root = self.target / ".vibe-coding" / "runs" / run_id
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / "controller.lock"
        self.log_path = self.root / "logs" / "controller.jsonl"
        self._lock_depth = 0
        self._lock_owner: int | None = None

    @classmethod
    def for_run(cls, target: Path, run_id: str) -> "StateStore":
        return cls(target, run_id)

    @contextmanager
    def lock(self, *, blocking: bool = True) -> Iterator[None]:
        if self._lock_depth != 0:
            raise StateConflictError("run lock is non-reentrant")
        self._locked_run_fd = self._open_run_directory(create=True)
        descriptor = os.open(
            "controller.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=self._locked_run_fd,
        )
        operation = fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            try:
                fcntl.flock(descriptor, operation)
            except BlockingIOError as error:
                raise StateConflictError("run lock is already held") from error
            self._lock_owner = threading.get_ident()
            self._lock_depth += 1
            yield
        finally:
            if self._lock_depth:
                self._lock_depth -= 1
            self._lock_owner = None
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            os.close(self._locked_run_fd)
            self._locked_run_fd = None

    def _require_lock(self) -> None:
        if (
            self._lock_depth != 1
            or self._lock_owner != threading.get_ident()
        ):
            raise StateConflictError("state mutation requires the run lock")

    def _write_immutable(self, relative: str, body: bytes) -> ArtifactRef:
        reference = artifact_ref(relative, body)
        parent_fd, leaf = self._open_artifact_parent(relative, create=True)
        try:
            self._publish_or_verify_immutable(parent_fd, leaf, body, reference)
        finally:
            os.close(parent_fd)
        return reference

    def prepare_artifact(self, relative: str, body: bytes) -> ArtifactRef:
        self._require_lock()
        return self._write_immutable(relative, body)

    def create(
        self,
        initial_state: dict[str, object],
        artifacts: Mapping[str, bytes],
    ) -> dict[str, object]:
        self._require_lock()
        if self._run_leaf_exists("state.json"):
            raise StateConflictError("state.json already exists")
        state = validate_run_state(initial_state)
        if state["revision"] != 0:
            raise ContractError("initial revision must be 0")
        self._validate_artifact_bindings(state, artifacts)
        for relative, body in artifacts.items():
            self._write_immutable(relative, body)
        replace_mutable_at(
            self._locked_run_fd,
            "state.json",
            canonical_json_bytes(state),
        )
        return copy.deepcopy(state)

    def load(self) -> dict[str, object]:
        with self._snapshot_run_fd() as run_fd:
            state = validate_run_state(
                parse_json_object_bytes(
                    read_regular_at(
                        run_fd,
                        "state.json",
                        max_bytes=16 * 1024 * 1024,
                    )
                )
            )
            bodies = self._verify_and_cache_bound_artifacts(run_fd, state)
            validate_bound_state_semantics(state, bodies.__getitem__)
        return state

    def transact(
        self,
        expected_revision: int,
        artifacts: Mapping[str, bytes],
        mutate: Callable[
            [dict[str, object], Mapping[str, ArtifactRef]],
            None,
        ],
    ) -> dict[str, object]:
        self._require_lock()
        current = self.load()
        actual = current["revision"]
        if actual != expected_revision:
            raise StateConflictError(
                f"expected revision {expected_revision}, found {actual}"
            )
        references = {
            relative: artifact_ref(relative, body)
            for relative, body in artifacts.items()
        }
        updated = copy.deepcopy(current)
        previous_histories = _history_prefixes(current)
        previous_legacy_import = copy.deepcopy(current["legacy_import"])
        indexed = {
            item["path"]: item["sha256"]
            for item in updated["artifact_index"]
        }
        for reference in references.values():
            previous_digest = indexed.get(reference.path)
            if previous_digest is not None and previous_digest != reference.sha256:
                raise StateConflictError(
                    "immutable artifact already exists with different bytes: "
                    f"{reference.path}"
                )
            if previous_digest is None:
                updated["artifact_index"].append(reference.as_dict())
                indexed[reference.path] = reference.sha256
        expected_index = copy.deepcopy(updated["artifact_index"])
        mutation_result = mutate(updated, references)
        if mutation_result is not None:
            raise ContractError("state mutator must modify in place and return None")
        if updated["artifact_index"][: len(expected_index)] != expected_index:
            raise ContractError("artifact_index is append-only")
        _require_history_prefixes(updated, previous_histories)
        if (
            previous_legacy_import is not None
            and updated["legacy_import"] != previous_legacy_import
        ):
            raise ContractError("legacy_import is immutable once set")
        updated["revision"] = expected_revision + 1
        validated = validate_run_state(updated)
        self._validate_artifact_bindings(validated, artifacts)
        for relative, body in artifacts.items():
            self._write_immutable(relative, body)
        replace_mutable_at(
            self._locked_run_fd,
            "state.json",
            canonical_json_bytes(validated),
        )
        return copy.deepcopy(validated)

    def append_log(self, event: Mapping[str, object]) -> None:
        body = canonical_json_bytes(dict(event))
        with self._snapshot_run_fd(create=True) as run_fd:
            logs_fd = open_directory_chain(run_fd, ("logs",), create=True)
            try:
                append_complete_record_at(
                    logs_fd,
                    "controller.jsonl",
                    body,
                    no_follow=True,
                )
            finally:
                os.close(logs_fd)
```

Add a private recursive collector:

```python
def _artifact_refs(value: object) -> Iterator[ArtifactRef]:
    if isinstance(value, dict):
        if set(value) == {"path", "sha256"}:
            path = value["path"]
            digest = value["sha256"]
            if isinstance(path, str) and isinstance(digest, str):
                yield ArtifactRef(path=path, sha256=digest)
            return
        for item in value.values():
            yield from _artifact_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _artifact_refs(item)


def _history_prefixes(
    state: dict[str, object],
) -> dict[tuple[str, ...], list[object]]:
    paths = [
        ("plans",),
        ("evaluations",),
        ("verifications",),
        ("role_attempts", "planner"),
        ("role_attempts", "evaluator"),
        ("stop_receipts",),
    ]
    paths.extend(
        ("tasks", task_id, "attempts")
        for task_id in sorted(state["tasks"])
    )
    return {
        path: copy.deepcopy(_history_at(state, path))
        for path in paths
    }


def _history_at(
    state: dict[str, object],
    path: tuple[str, ...],
) -> list[object]:
    value: object = state
    for component in path:
        if not isinstance(value, dict) or component not in value:
            raise ContractError(
                f"missing append-only history: {'.'.join(path)}"
            )
        value = value[component]
    if not isinstance(value, list):
        raise ContractError(
            f"append-only history is not a list: {'.'.join(path)}"
        )
    return value


def _require_history_prefixes(
    state: dict[str, object],
    prefixes: Mapping[tuple[str, ...], list[object]],
) -> None:
    for path, prefix in prefixes.items():
        current = _history_at(state, path)
        if current[: len(prefix)] != prefix:
            raise ContractError(
                f"append-only history changed: {'.'.join(path)}"
            )
```

Implement the method used by both `create()` and `transact()`:

```python
def _validate_artifact_bindings(
    self,
    state: dict[str, object],
    artifacts: Mapping[str, bytes],
) -> None:
    references: dict[str, ArtifactRef] = {}
    for reference in _artifact_refs(state):
        _relative_artifact_path(reference.path)
        previous = references.get(reference.path)
        if previous is not None and previous.sha256 != reference.sha256:
            raise ContractError(
                f"conflicting artifact digests for {reference.path}"
            )
        references[reference.path] = reference

    for relative, body in artifacts.items():
        expected = artifact_ref(relative, body)
        if references.get(relative) != expected:
            raise ContractError(
                f"transaction artifact is not bound by new state: {relative}"
            )

    for reference in references.values():
        if reference.path in artifacts:
            if artifact_ref(reference.path, artifacts[reference.path]) != reference:
                raise ContractError(
                    f"artifact digest mismatch: {reference.path}"
                )
            continue
        actual = self._sha256_bound_artifact(reference.path)
        if actual != reference.sha256:
            raise ContractError(
                f"bound artifact digest mismatch: {reference.path}"
            )
```

Implement every low-level contract with `os.open`, `os.mkdir`, `os.replace`,
`os.link`, and `os.unlink` using `dir_fd`; every directory component is opened
with `O_DIRECTORY|O_NOFOLLOW` and every regular leaf with `O_NOFOLLOW` followed
by `fstat`. Never use `Path.exists()`, `Path.is_symlink()`, `Path.read_bytes()`,
path-based `tempfile`, or a path-based replace for a security decision. A newly
created directory is fsynced and its already-pinned parent is fsynced before
descending. Mutable publication orders temp-file fsync, `replaceat`, then parent
fsync. Immutable publication writes and fsyncs a same-directory temp, uses
hard-link create-if-absent semantics, fsyncs the parent, and on `EEXIST` reads
and hashes the winning regular file without replacing it.

`read_bounded()` repeatedly calls `os.read()` until EOF, handling short reads
and `InterruptedError`; it requests at most the remaining allowance plus one
byte and raises `ContractError` if that extra byte exists. It never returns a
truncated prefix. `read_regular_at()` and `read_optional_regular_at()` delegate
to this one primitive after `fstat` proves a regular file.

`lock()` pins the run-directory descriptor for the whole critical section.
When the current thread owns that lock, `_snapshot_run_fd()` must `dup()` the
already-pinned `_locked_run_fd`; it must not resolve/open the run path again.
Outside a transaction, `load()` pins one fresh snapshot descriptor for the
whole parse/hash/semantic-validation operation. If an attacker swaps any
ancestor after it is opened, the operation
must either continue against the originally opened directory or fail closed,
never follow the replacement. `_verify_and_cache_bound_artifacts()` reads every
bound Artifact once through the pinned run descriptor, enforces the per-kind
size bound, streams its digest, and returns the verified bytes required by
`validate_bound_state_semantics()`. `append_complete_record_at()` retries short
writes and refuses an existing symlink/non-regular log.

This permits `prepare_artifact()` followed by `create(..., {})`, but a successful `create()` or `transact()` cannot introduce unbound new bytes. `load()` repeats validation for every bound ArtifactRef. An orphan is therefore possible only when `prepare_artifact()` is intentionally used before an external effect, or when a crash happens after Artifact rename and before state rename.

- [ ] **Step 4: Add explicit parent-directory fsync and crash-window assertions**

Patch `tests/test_state_store.py` to inject a durability-event recorder into
the descriptor primitives and to fault the descriptor-relative replace:

```python
from unittest import mock


def test_transaction_orders_artifact_and_state_durability(self) -> None:
    self._create()
    events: list[str] = []
    with mock.patch(
        "vibe.state_store._record_durability_event",
        side_effect=events.append,
    ):
        with self.store.lock():
            self.store.transact(
                0,
                {"tasks/TASK-001/task.json": b'{"id":"TASK-001"}\n'},
                lambda state, refs: state["tasks"].update(
                    {
                        "TASK-001": {
                            "status": "PENDING",
                            "task": refs["tasks/TASK-001/task.json"].as_dict(),
                            "attempt_no": 0,
                            "failure_count": 0,
                            "max_attempts": 3,
                            "active_attempt": None,
                            "attempts": [],
                            "result": None,
                            "verification": None,
                            "source_commits": [],
                            "integrated_commits": [],
                            "last_error": None,
                        }
                    }
                ),
            )
    self.assertLess(events.index("artifact-file-fsync"), events.index("tasks-dir-fsync"))
    self.assertLess(events.index("tasks-dir-fsync"), events.index("task-dir-fsync"))
    self.assertLess(events.index("task-dir-fsync"), events.index("state-file-fsync"))
    self.assertLess(events.index("state-file-fsync"), events.index("run-dir-fsync"))


def test_append_log_retries_partial_writes_and_fsyncs_new_parent(self) -> None:
    self._create()
    real_write = os.write

    def partial_write(descriptor: int, body: object) -> int:
        view = memoryview(body)
        amount = max(1, len(view) // 2)
        return real_write(descriptor, view[:amount])

    with mock.patch(
        "vibe.state_store.os.write",
        side_effect=partial_write,
    ) as write, mock.patch("vibe.state_store.os.fsync") as fsync:
        self.store.append_log({"event": "complete"})

    self.assertGreater(write.call_count, 1)
    self.assertEqual(
        json.loads(self.store.log_path.read_text(encoding="utf-8")),
        {"event": "complete"},
    )
    self.assertGreaterEqual(fsync.call_count, 2)


def test_state_replace_failure_leaves_only_an_unreferenced_orphan(self) -> None:
    self._create()
    original = self.store.state_path.read_bytes()
    with mock.patch(
        "vibe.state_store.replace_mutable_at",
        side_effect=OSError("injected state crash"),
    ):
        with self.store.lock(), self.assertRaisesRegex(OSError, "injected"):
            self.store.transact(
                0,
                {"tasks/TASK-001/task.json": b'{"id":"TASK-001"}\n'},
                lambda state, refs: state["tasks"].update(
                    {
                        "TASK-001": {
                            "status": "PENDING",
                            "task": refs["tasks/TASK-001/task.json"].as_dict(),
                            "attempt_no": 0,
                            "failure_count": 0,
                            "max_attempts": 3,
                            "active_attempt": None,
                            "attempts": [],
                            "result": None,
                            "verification": None,
                            "source_commits": [],
                            "integrated_commits": [],
                            "last_error": None,
                        }
                    }
                ),
            )

    self.assertEqual(self.store.state_path.read_bytes(), original)
    self.assertTrue((self.store.root / "tasks/TASK-001/task.json").is_file())
```

Add `import os` and `from unittest import mock` to the test file. Expected behavior is a committed old state plus an orphan artifact, never a half-written state. The log test proves a short `write(2)` cannot truncate an audit record and the first log creation is made directory-durable.

Add adversarial tests that swap `.vibe-coding`, `runs`, the run leaf, and an
artifact parent after their descriptors have been opened. Each test must prove
the operation either fails or affects only the originally pinned inode. Add a
barrier race in which two writers publish different bytes to one immutable
leaf: exactly one wins, the loser verifies the winning digest and raises, and
the winner is never replaced. Finally, bind a correctly hashed Attempt manifest
whose role, status, attempt token, or verification pointer disagrees with state;
`StateStore.load()` must reject each semantic mismatch.

- [ ] **Step 5: Run StateStore and model tests and verify GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_models tests.test_state_store -v
```

Expected: all tests end in `OK`; lock contention, duplicate keys, revision conflict, immutable overwrite, and injected state-replace failure all fail closed.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/vibe/state_store.py tests/test_state_store.py tests/test_models.py
git diff --cached --check
git commit -m "feat: add crash-safe schema 4 state store"
```

### Task 3: Frozen Run Configuration and Structured Commands

**Files:**
- Create: `src/vibe/config.py`
- Create: `tests/test_config.py`
- Modify: `src/vibe/models.py`

**Interfaces:**
- Consumes `CommandAuthorization`, `CommandSpec`, `ContractError`, and `FrozenRunConfig`.
- Produces `DEFAULT_CONFIG: FrozenRunConfig`.
- Produces `load_run_config(target: Path, overrides: Mapping[str, int | str | bool | None]) -> FrozenRunConfig`.
- Produces `parse_project_config(value: object, authorization: CommandAuthorization) -> FrozenRunConfig`; repository input cannot author its own authorization.
- Produces `parse_frozen_config(value: object) -> FrozenRunConfig`.
- Produces `frozen_config_bytes(config: FrozenRunConfig) -> bytes`.
- Produces `resolve_command_ids(config: FrozenRunConfig, ids: Sequence[str]) -> tuple[CommandSpec, ...]`; rejects unknown or duplicate Agent-supplied IDs before any process is spawned.
- Produces `effective_command_ids(config, additions) -> tuple[str, ...]`; prepends all frozen required IDs and permits Agent output to append catalog IDs only.

- [ ] **Step 1: Write failing config-policy tests**

Create `tests/test_config.py`:

```python
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vibe.config import frozen_config_bytes, load_run_config, parse_frozen_config
from vibe.models import ContractError


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name)

    def test_defaults_are_finite_and_codex_cli_is_the_only_v1_provider(self) -> None:
        config = load_run_config(self.target, {})
        self.assertEqual(config.provider_name, "codex-cli")
        self.assertEqual(config.max_workers, 4)
        self.assertEqual(config.task_attempts, 3)
        self.assertEqual(config.provider_retries, 3)
        self.assertEqual(config.evidence_rounds, 3)
        self.assertEqual(config.repair_rounds, 3)
        self.assertEqual(config.max_plan_tasks, 128)
        self.assertEqual(config.command_authorization.mode, "EMPTY")

    def test_cli_override_wins_and_the_result_can_be_frozen(self) -> None:
        (self.target / "vibe.json").write_text(
            json.dumps({"scheduler": {"max_workers": 2}}),
            encoding="utf-8",
        )
        config = load_run_config(self.target, {"max_workers": 6})
        frozen = frozen_config_bytes(config)
        (self.target / "vibe.json").write_text("{}", encoding="utf-8")

        self.assertEqual(parse_frozen_config(json.loads(frozen)), config)
        self.assertEqual(config.max_workers, 6)

    def test_project_commands_require_explicit_creation_authorization(self) -> None:
        body = json.dumps(
            {
                "verification": {
                    "command_catalog": [
                        {
                            "id": "unit",
                            "purpose": "Run the unit-test suite",
                            "argv": ["python3", "-m", "unittest"],
                        }
                    ],
                    "required_command_ids": ["unit"],
                }
            },
            sort_keys=True,
        )
        (self.target / "vibe.json").write_text(body, encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "--allow-project-commands"):
            load_run_config(self.target, {})

        config = load_run_config(
            self.target,
            {"allow_project_commands": True},
        )
        frozen = frozen_config_bytes(config)
        self.assertEqual(
            config.command_authorization.mode,
            "EXPLICIT_PROJECT_FILE",
        )
        self.assertEqual(config.command_authorization.source_path, "vibe.json")
        self.assertTrue(config.command_authorization.source_sha256.startswith("sha256:"))

        (self.target / "vibe.json").write_text("{}", encoding="utf-8")
        self.assertEqual(parse_frozen_config(json.loads(frozen)), config)

    def test_command_rejects_shell_strings_absolute_cwd_and_bad_env_names(self) -> None:
        invalid_values = (
            {
                "verification": {
                    "command_catalog": [
                        {
                            "id": "unit",
                            "purpose": "Run unit tests",
                            "argv": "python -m unittest",
                        }
                    ]
                }
            },
            {
                "verification": {
                    "command_catalog": [
                        {
                            "id": "unit",
                            "purpose": "Run unit tests",
                            "argv": ["python3"],
                            "cwd": "/tmp",
                        }
                    ]
                }
            },
            {
                "verification": {
                    "command_catalog": [
                        {
                            "id": "unit",
                            "purpose": "Run unit tests",
                            "argv": ["python3"],
                            "env_allowlist": ["TOKEN=value"],
                        }
                    ]
                }
            },
        )
        for value in invalid_values:
            (self.target / "vibe.json").write_text(
                json.dumps(value),
                encoding="utf-8",
            )
            with self.subTest(value=value), self.assertRaises(ContractError):
                load_run_config(self.target, {})

    def test_unknown_provider_and_unknown_fields_fail_closed(self) -> None:
        for value in (
            {"provider": {"name": "other"}},
            {"scheduler": {"max_workers": 4, "queue": "remote"}},
        ):
            (self.target / "vibe.json").write_text(
                json.dumps(value),
                encoding="utf-8",
            )
            with self.assertRaises(ContractError):
                load_run_config(self.target, {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run config tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_config -v
```

Expected: import failure because `vibe.config` does not exist.

- [ ] **Step 3: Implement strict merge, overrides, and frozen parsing**

Create `src/vibe/config.py`:

```python
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from vibe.models import (
    CommandAuthorization,
    CommandSpec,
    ContractError,
    FrozenRunConfig,
)
from vibe.state_store import (
    canonical_json_bytes,
    open_absolute_directory_no_follow,
    parse_json_object_bytes,
    read_optional_regular_at,
)


ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
COMMAND_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
PROJECT_TOP_FIELDS = {"provider", "scheduler", "limits", "verification"}
FROZEN_TOP_FIELDS = PROJECT_TOP_FIELDS
LIMIT_FIELDS = {
    "task_attempts",
    "provider_retries",
    "evidence_rounds",
    "repair_rounds",
    "max_plan_tasks",
}

EMPTY_COMMAND_AUTHORIZATION = CommandAuthorization(
    mode="EMPTY",
    source_path=None,
    source_sha256=(
        "sha256:"
        "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"
    ),
)

DEFAULT_CONFIG = FrozenRunConfig(
    provider_name="codex-cli",
    max_workers=4,
    task_attempts=3,
    provider_retries=3,
    evidence_rounds=3,
    repair_rounds=3,
    max_plan_tasks=128,
    command_catalog=(),
    required_command_ids=(),
    command_authorization=EMPTY_COMMAND_AUTHORIZATION,
)


def read_optional_project_source(
    target: Path,
    leaf: str,
    *,
    max_bytes: int,
) -> bytes | None:
    if leaf != "vibe.json":
        raise ContractError("unsupported project config leaf")
    root_fd = open_absolute_directory_no_follow(target)
    try:
        return read_optional_regular_at(root_fd, leaf, max_bytes=max_bytes)
    finally:
        os.close(root_fd)


def _parse_command_authorization(value: object) -> CommandAuthorization:
    raw = _object(
        value,
        "verification.authorization",
        {"mode", "source_path", "source_sha256"},
    )
    mode = raw.get("mode")
    source_path = raw.get("source_path")
    digest = raw.get("source_sha256")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise ContractError("authorization source_sha256 is invalid")
    if mode == "EMPTY":
        if source_path is not None or digest != EMPTY_COMMAND_AUTHORIZATION.source_sha256:
            raise ContractError("EMPTY authorization fields are invalid")
    elif mode == "EXPLICIT_PROJECT_FILE":
        if source_path != "vibe.json":
            raise ContractError("project authorization source must be vibe.json")
    else:
        raise ContractError("command authorization mode is invalid")
    return CommandAuthorization(mode=mode, source_path=source_path, source_sha256=digest)


def command_authorization_for_project_source(
    *,
    path: Path | None,
    exact_source_bytes: bytes,
    commands_present: bool,
) -> CommandAuthorization:
    if not commands_present:
        return EMPTY_COMMAND_AUTHORIZATION
    if path is None or path.name != "vibe.json":
        raise ContractError("authorized commands require a vibe.json source")
    return CommandAuthorization(
        mode="EXPLICIT_PROJECT_FILE",
        source_path="vibe.json",
        source_sha256=(
            "sha256:" + hashlib.sha256(exact_source_bytes).hexdigest()
        ),
    )


def _plain_positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ContractError(f"{field} must be a positive integer")
    return value


def _object(value: object, field: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ContractError(f"unknown {field} fields: {sorted(unknown)}")
    return value


def _command(value: object) -> CommandSpec:
    raw = _object(
        value,
        "command",
        {"id", "purpose", "argv", "cwd", "timeout_seconds", "env_allowlist"},
    )
    command_id = raw.get("id")
    if (
        not isinstance(command_id, str)
        or COMMAND_ID_RE.fullmatch(command_id) is None
    ):
        raise ContractError("command.id must be a canonical stable ID")
    purpose = raw.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        raise ContractError("command.purpose must be a non-empty string")
    argv = raw.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise ContractError("command.argv must be a non-empty string array")
    cwd = raw.get("cwd", ".")
    if not isinstance(cwd, str):
        raise ContractError("command.cwd must be a string")
    path = PurePosixPath(cwd)
    if (
        path.is_absolute()
        or ".." in path.parts
        or cwd != path.as_posix()
        or cwd.startswith(".vibe-coding")
    ):
        raise ContractError("command.cwd must stay inside the product repository")
    timeout = _plain_positive_int(
        raw.get("timeout_seconds", 900),
        "command.timeout_seconds",
    )
    env = raw.get("env_allowlist", [])
    if (
        not isinstance(env, list)
        or not all(
            isinstance(name, str) and ENV_NAME_RE.fullmatch(name)
            for name in env
        )
        or len(set(env)) != len(env)
    ):
        raise ContractError("command.env_allowlist contains an invalid name")
    return CommandSpec(
        id=command_id,
        purpose=purpose,
        argv=tuple(argv),
        cwd=cwd,
        timeout_seconds=timeout,
        env_allowlist=tuple(env),
    )


def _parse_config(
    value: object,
    *,
    frozen: bool,
    authorization: CommandAuthorization | None,
) -> FrozenRunConfig:
    root = _object(value, "config", FROZEN_TOP_FIELDS if frozen else PROJECT_TOP_FIELDS)
    provider = _object(root.get("provider", {}), "provider", {"name"})
    scheduler = _object(
        root.get("scheduler", {}),
        "scheduler",
        {"max_workers"},
    )
    limits = _object(root.get("limits", {}), "limits", LIMIT_FIELDS)
    verification = _object(
        root.get("verification", {}),
        "verification",
        (
            {"command_catalog", "required_command_ids", "authorization"}
            if frozen
            else {"command_catalog", "required_command_ids"}
        ),
    )
    provider_name = provider.get("name", DEFAULT_CONFIG.provider_name)
    if provider_name != "codex-cli":
        raise ContractError("V1 provider.name must be codex-cli")
    commands = verification.get("command_catalog", [])
    if not isinstance(commands, list):
        raise ContractError("verification.command_catalog must be an array")
    catalog = tuple(_command(item) for item in commands)
    if len({command.id for command in catalog}) != len(catalog):
        raise ContractError("verification.command_catalog IDs must be unique")
    required_ids = verification.get("required_command_ids", [])
    if (
        not isinstance(required_ids, list)
        or not all(isinstance(item, str) for item in required_ids)
        or len(set(required_ids)) != len(required_ids)
    ):
        raise ContractError("verification.required_command_ids must be unique IDs")
    catalog_ids = {command.id for command in catalog}
    if not set(required_ids).issubset(catalog_ids):
        raise ContractError("every required command ID must exist in the catalog")
    if frozen:
        authorization = _parse_command_authorization(
            verification.get("authorization")
        )
    if authorization is None:
        raise ContractError("command authorization is required")
    if frozen:
        expected_mode = (
            "EXPLICIT_PROJECT_FILE" if catalog or required_ids else "EMPTY"
        )
        if authorization.mode != expected_mode:
            raise ContractError(
                "command catalog and authorization mode are inconsistent"
            )
    return FrozenRunConfig(
        provider_name=provider_name,
        max_workers=_plain_positive_int(
            scheduler.get("max_workers", DEFAULT_CONFIG.max_workers),
            "scheduler.max_workers",
        ),
        task_attempts=_plain_positive_int(
            limits.get("task_attempts", DEFAULT_CONFIG.task_attempts),
            "limits.task_attempts",
        ),
        provider_retries=_plain_positive_int(
            limits.get("provider_retries", DEFAULT_CONFIG.provider_retries),
            "limits.provider_retries",
        ),
        evidence_rounds=_plain_positive_int(
            limits.get("evidence_rounds", DEFAULT_CONFIG.evidence_rounds),
            "limits.evidence_rounds",
        ),
        repair_rounds=_plain_positive_int(
            limits.get("repair_rounds", DEFAULT_CONFIG.repair_rounds),
            "limits.repair_rounds",
        ),
        max_plan_tasks=_plain_positive_int(
            limits.get("max_plan_tasks", DEFAULT_CONFIG.max_plan_tasks),
            "limits.max_plan_tasks",
        ),
        command_catalog=catalog,
        required_command_ids=tuple(required_ids),
        command_authorization=authorization,
    )


def parse_project_config(
    value: object,
    authorization: CommandAuthorization,
) -> FrozenRunConfig:
    return _parse_config(value, frozen=False, authorization=authorization)


def parse_frozen_config(value: object) -> FrozenRunConfig:
    return _parse_config(value, frozen=True, authorization=None)


def load_run_config(
    target: Path,
    overrides: Mapping[str, int | str | bool | None],
) -> FrozenRunConfig:
    source = read_optional_project_source(
        target,
        "vibe.json",
        max_bytes=4 * 1024 * 1024,
    )
    if source is not None:
        source_path: Path | None = target / "vibe.json"
        source_bytes = source
        value = parse_json_object_bytes(source_bytes)
    else:
        source_path = None
        source_bytes = b"{}\n"
        value = {}
    if not isinstance(value, dict):
        raise ContractError("vibe.json root must be an object")
    provisional = parse_project_config(
        value,
        DEFAULT_CONFIG.command_authorization,
    )
    commands_present = bool(
        provisional.command_catalog or provisional.required_command_ids
    )
    if commands_present and overrides.get("allow_project_commands") is not True:
        raise ContractError(
            "non-empty project commands require --allow-project-commands"
        )
    authorization = command_authorization_for_project_source(
        path=source_path,
        exact_source_bytes=source_bytes,
        commands_present=commands_present,
    )
    config = parse_project_config(value, authorization)
    replaced = config.as_dict()
    override_map = {
        "max_workers": ("scheduler", "max_workers"),
        "task_attempts": ("limits", "task_attempts"),
        "provider_retries": ("limits", "provider_retries"),
        "evidence_rounds": ("limits", "evidence_rounds"),
        "repair_rounds": ("limits", "repair_rounds"),
    }
    for name, (section, field) in override_map.items():
        override = overrides.get(name)
        if override is not None:
            replaced[section][field] = override
    return parse_frozen_config(replaced)


def frozen_config_bytes(config: FrozenRunConfig) -> bytes:
    return canonical_json_bytes(config.as_dict())
```

Do not persist arbitrary Provider configuration or environment values. V1 stores only `provider.name=codex-cli`.

`_parse_command_authorization()` rejects unknown fields and accepts exactly
`{mode, source_path, source_sha256}`. `EMPTY` requires `source_path=null` and
the canonical empty-command digest; `EXPLICIT_PROJECT_FILE` requires
`source_path="vibe.json"` and a canonical SHA-256 digest. Project parsing has no
`authorization` field, so repository content cannot self-authorize. Read the
project file once through the descriptor-safe bounded reader, hash those exact
bytes, parse those same bytes, and use the resulting immutable authorization in
the frozen config. `--allow-project-commands` is a creation/migration
acknowledgement only; it is never read from `vibe.json` and resume reads only
the frozen artifact. Test that malformed commands still fail with the flag,
non-empty commands fail without it before any Provider or command process is
started, and later changes to `vibe.json` cannot change a frozen run. Add a
barrier test that swaps `vibe.json` while loading: authorization digest and
parsed catalog must always come from the same descriptor-read byte string.

- [ ] **Step 4: Prove Agent output can only select frozen authorized commands**

Add this helper and test to `tests/test_config.py`:

```python
import dataclasses

from vibe.config import (
    DEFAULT_CONFIG,
    effective_command_ids,
    resolve_command_ids,
)
from vibe.models import CommandAuthorization, CommandSpec


def test_agent_command_ids_resolve_only_the_frozen_catalog(self) -> None:
    catalog = (
        CommandSpec(
            id="unit",
            purpose="Run the unit-test suite",
            argv=("python3", "-m", "unittest"),
            cwd=".",
            timeout_seconds=900,
            env_allowlist=(),
        ),
        CommandSpec(
            id="models",
            purpose="Run model-contract tests",
            argv=("python3", "-m", "unittest", "tests.test_models"),
            cwd=".",
            timeout_seconds=120,
            env_allowlist=(),
        ),
    )
    config = dataclasses.replace(
        DEFAULT_CONFIG,
        command_catalog=catalog,
        required_command_ids=("unit",),
        command_authorization=CommandAuthorization(
            mode="EXPLICIT_PROJECT_FILE",
            source_path="vibe.json",
            source_sha256="sha256:" + "a" * 64,
        ),
    )
    self.assertEqual(
        effective_command_ids(config, ("models",)),
        ("unit", "models"),
    )
    self.assertEqual(effective_command_ids(config, ()), ("unit",))
    with self.assertRaises(ContractError):
        effective_command_ids(config, ("models", "models"))
    self.assertEqual(resolve_command_ids(config, ("unit", "models")), catalog)
    for ids in (("unknown",), ("unit", "unit")):
        with self.subTest(ids=ids), self.assertRaises(ContractError):
            resolve_command_ids(config, ids)
```

Add the exact deterministic implementation. It performs no executable lookup and returns only objects already present in the frozen config:

```python
def resolve_command_ids(
    config: FrozenRunConfig,
    ids: Sequence[str],
) -> tuple[CommandSpec, ...]:
    if len(set(ids)) != len(ids):
        raise ContractError("command IDs must be unique")
    catalog = {command.id: command for command in config.command_catalog}
    try:
        return tuple(catalog[command_id] for command_id in ids)
    except KeyError as error:
        raise ContractError(f"unknown command ID: {error.args[0]}") from error


def effective_command_ids(
    config: FrozenRunConfig,
    additions: Sequence[str],
) -> tuple[str, ...]:
    requested = tuple(additions)
    resolve_command_ids(config, requested)
    combined = tuple(dict.fromkeys(config.required_command_ids + requested))
    resolve_command_ids(config, combined)
    return combined
```

- [ ] **Step 5: Run all Phase 1 tests and verify GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest \
  tests.test_models tests.test_state_store tests.test_config -v
python3 -m compileall -q src/vibe
```

Expected: all Phase 1 tests end in `OK`; compileall produces no output and exits `0`.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/vibe/config.py src/vibe/models.py tests/test_config.py
git diff --cached --check
git commit -m "feat: freeze schema 4 run configuration"
```

## Phase 1 Completion Gate

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q src/vibe
python3 -m pip install --no-deps -e .
vibe --version
vibe --help
```

Expected:

- New Phase 1 tests pass.
- Existing Schema 3 tests still pass because no legacy file has been deleted or modified.
- `vibe --version` prints `vibe 0.1.0.dev0`.
- `vibe --help` lists all six future public commands.
- No Provider process starts and no Git ref moves during any Phase 1 test.
