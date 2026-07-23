from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Callable


DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
RUN_ID_RE = re.compile(r"RUN-\d{8}-\d{3}\Z")
TASK_ID_RE = re.compile(r"TASK-\d{3}\Z")
ACCEPTANCE_ID_RE = re.compile(r"AC-\d{3}\Z")
OPERATION_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z"
)
TOKEN_RE = re.compile(r"[A-Z][A-Z0-9_-]{0,191}\Z")


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
class PromptRef:
    id: str
    version: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "version": self.version,
            "sha256": self.sha256,
        }


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


@dataclass(frozen=True)
class AttemptManifest:
    schema_version: int
    role: str
    operation_id: str
    task_id: str | None
    attempt_no: int
    attempt_token: str
    status: AttemptStatus
    created_at: str
    completed_at: str
    expected_base: str
    branch: str | None
    worktree: str
    preflight: ArtifactRef | None
    prompt_versions: tuple[PromptRef, ...]
    provider_attempts: tuple[ArtifactRef, ...]
    request: ArtifactRef | None
    launch: ArtifactRef | None
    stdout: ArtifactRef | None
    stderr: ArtifactRef | None
    exit: ArtifactRef | None
    result: ArtifactRef | None
    source_audit: ArtifactRef | None
    verification: ArtifactRef | None
    last_error: dict[str, object] | None


@dataclass(frozen=True)
class PendingIntegration:
    operation_id: str
    task_id: str
    attempt_no: int
    expected_head: str
    candidate_head: str
    source_base: str
    source_head: str
    verification: ArtifactRef

    def as_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "task_id": self.task_id,
            "attempt_no": self.attempt_no,
            "expected_head": self.expected_head,
            "candidate_head": self.candidate_head,
            "source_base": self.source_base,
            "source_head": self.source_head,
            "verification": self.verification.as_dict(),
        }


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

TASK_FIELDS = {
    "task",
    "status",
    "attempt_no",
    "failure_count",
    "max_attempts",
    "active_attempt",
    "attempts",
    "result",
    "verification",
    "source_commits",
    "integrated_commits",
    "last_error",
}

ACTIVE_ATTEMPT_FIELDS = {
    "attempt_token",
    "status",
    "created_at",
    "task_base_sha",
    "branch",
    "worktree",
    "preflight",
    "provider_handle",
    "result_path",
}

PROVIDER_HANDLE_FIELDS = {
    "adapter",
    "attempt_token",
    "pid",
    "process_start_identity",
    "process_group",
    "child_pid",
    "child_process_start_identity",
    "child_process_group",
}

PENDING_DISPATCH_FIELDS = {
    "attempt_token",
    "role",
    "operation_id",
    "task_id",
    "attempt_no",
    "attempt_created_at",
    "provider_retry_no",
    "expected_base",
    "branch",
    "worktree",
    "provider_prefix",
    "prompt",
    "schema",
    "preflight",
    "prompt_versions",
    "request",
    "launch_path",
    "stdout_path",
    "stderr_path",
    "exit_path",
    "result_path",
    "launch",
    "stdout",
    "stderr",
    "exit",
    "result",
    "provider_attempts",
    "provider_handle",
    "prepared_revision",
}

ATTEMPT_MANIFEST_FIELDS = {
    "schema_version",
    "role",
    "operation_id",
    "task_id",
    "attempt_no",
    "attempt_token",
    "status",
    "created_at",
    "completed_at",
    "expected_base",
    "branch",
    "worktree",
    "preflight",
    "prompt_versions",
    "provider_attempts",
    "request",
    "launch",
    "stdout",
    "stderr",
    "exit",
    "result",
    "source_audit",
    "verification",
    "last_error",
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


def _require_plain_int(
    value: object,
    field: str,
    minimum: int = 0,
) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{field} must be an integer >= {minimum}")
    return value


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ContractError(f"{field} contains an invalid Unicode scalar")
    return value


def _require_exact_fields(
    value: object,
    field: str,
    expected: set[str],
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    actual = set(value)
    if actual != expected:
        raise ContractError(
            f"{field} fields are invalid: expected {sorted(expected)}, "
            f"found {sorted(actual)}"
        )
    return value


def _require_rfc3339(value: object, field: str) -> str:
    text = _require_string(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ContractError(f"{field} must be RFC3339") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{field} must include a timezone")
    return text


def _require_oid(value: object, field: str) -> str:
    text = _require_string(value, field)
    if OID_RE.fullmatch(text) is None:
        raise ContractError(f"{field} must be a full commit OID")
    return text


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


def _artifact(value: object, field: str) -> ArtifactRef:
    raw = _require_exact_fields(value, field, {"path", "sha256"})
    raw_path = _require_string(raw["path"], f"{field}.path")
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
    digest = _require_string(raw["sha256"], f"{field}.sha256")
    if DIGEST_RE.fullmatch(digest) is None:
        raise ContractError(f"{field}.sha256 must be canonical")
    return ArtifactRef(path=raw_path, sha256=digest)


def _optional_artifact(value: object, field: str) -> ArtifactRef | None:
    if value is None:
        return None
    return _artifact(value, field)


def _artifact_list(value: object, field: str) -> list[ArtifactRef]:
    if not isinstance(value, list):
        raise ContractError(f"{field} must be an array")
    references = [
        _artifact(item, f"{field}[{index}]")
        for index, item in enumerate(value)
    ]
    paths: dict[str, str] = {}
    for reference in references:
        previous = paths.get(reference.path)
        if previous is not None and previous != reference.sha256:
            raise ContractError(f"{field} contains conflicting artifact digests")
        paths[reference.path] = reference.sha256
    return references


def _optional_error(value: object, field: str) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be null or an object")
    allowed = {"code", "message", "retryable", "evidence"}
    if not {"code", "message", "retryable"}.issubset(value) or set(value) - allowed:
        raise ContractError(f"{field} fields are invalid")
    _require_string(value["code"], f"{field}.code")
    _require_string(value["message"], f"{field}.message")
    if type(value["retryable"]) is not bool:
        raise ContractError(f"{field}.retryable must be a boolean")
    if "evidence" in value:
        _artifact(value["evidence"], f"{field}.evidence")
    return copy.deepcopy(value)


def _validate_repository(value: object, run_id: str) -> None:
    repository = _require_exact_fields(
        value,
        "repository",
        {
            "identity",
            "base_ref",
            "base_sha",
            "integration_ref",
            "integration_head",
        },
    )
    identity = _require_string(repository["identity"], "repository.identity")
    if DIGEST_RE.fullmatch(identity) is None:
        raise ContractError("repository.identity must be canonical")
    base_ref = _require_string(repository["base_ref"], "repository.base_ref")
    if not (
        base_ref == "HEAD"
        or base_ref.startswith("refs/")
        or OID_RE.fullmatch(base_ref)
    ):
        raise ContractError("repository.base_ref is invalid")
    _require_oid(repository["base_sha"], "repository.base_sha")
    _require_oid(repository["integration_head"], "repository.integration_head")
    expected_ref = f"refs/heads/vibe/run-{run_id}"
    if repository["integration_ref"] != expected_ref:
        raise ContractError(f"repository.integration_ref must equal {expected_ref}")


def _validate_controller(value: object) -> None:
    if value is None:
        return
    controller = _require_exact_fields(
        value,
        "controller",
        {
            "pid",
            "process_start_identity",
            "process_group",
            "controller_token",
        },
    )
    _require_plain_int(controller["pid"], "controller.pid", 1)
    _require_plain_int(
        controller["process_group"],
        "controller.process_group",
        1,
    )
    _require_string(
        controller["process_start_identity"],
        "controller.process_start_identity",
    )
    _require_string(controller["controller_token"], "controller.controller_token")


def _validate_provider_handle(
    value: object,
    field: str,
) -> dict[str, object] | None:
    if value is None:
        return None
    handle = _require_exact_fields(value, field, PROVIDER_HANDLE_FIELDS)
    _require_string(handle["adapter"], f"{field}.adapter")
    _require_string(handle["attempt_token"], f"{field}.attempt_token")
    for name in (
        "pid",
        "process_group",
        "child_pid",
        "child_process_group",
    ):
        _require_plain_int(handle[name], f"{field}.{name}", 1)
    for name in (
        "process_start_identity",
        "child_process_start_identity",
    ):
        _require_string(handle[name], f"{field}.{name}")
    return handle


def _validate_role_runtime(value: object) -> None:
    roles = _require_exact_fields(
        value,
        "role_runtime",
        {"planner", "evaluator"},
    )
    expected = {
        "operation_id",
        "attempt_no",
        "failure_count",
        "max_attempts",
        "active_attempt_token",
        "last_error",
    }
    for role in ("planner", "evaluator"):
        runtime = _require_exact_fields(
            roles[role],
            f"role_runtime.{role}",
            expected,
        )
        operation_id = runtime["operation_id"]
        if operation_id is not None:
            operation = _require_string(
                operation_id,
                f"role_runtime.{role}.operation_id",
            )
            if OPERATION_ID_RE.fullmatch(operation) is None:
                raise ContractError(f"role_runtime.{role}.operation_id is invalid")
        attempt_no = _require_plain_int(
            runtime["attempt_no"],
            f"role_runtime.{role}.attempt_no",
        )
        failures = _require_plain_int(
            runtime["failure_count"],
            f"role_runtime.{role}.failure_count",
        )
        maximum = _require_plain_int(
            runtime["max_attempts"],
            f"role_runtime.{role}.max_attempts",
            1,
        )
        if failures > maximum:
            raise ContractError(f"role_runtime.{role}.failure_count exceeds limit")
        token = runtime["active_attempt_token"]
        if token is not None:
            _require_string(token, f"role_runtime.{role}.active_attempt_token")
            if operation_id is None or attempt_no < 1:
                raise ContractError(
                    f"role_runtime.{role} active token lacks operation identity"
                )
        _optional_error(runtime["last_error"], f"role_runtime.{role}.last_error")


def _validate_active_attempt(
    value: object,
    field: str,
) -> dict[str, object] | None:
    if value is None:
        return None
    active = _require_exact_fields(value, field, ACTIVE_ATTEMPT_FIELDS)
    _require_string(active["attempt_token"], f"{field}.attempt_token")
    try:
        status = AttemptStatus(active["status"])
    except (TypeError, ValueError) as error:
        raise ContractError(f"{field}.status is invalid") from error
    _require_rfc3339(active["created_at"], f"{field}.created_at")
    _require_oid(active["task_base_sha"], f"{field}.task_base_sha")
    branch = _require_string(active["branch"], f"{field}.branch")
    if not branch.startswith("refs/heads/vibe/"):
        raise ContractError(f"{field}.branch must be a vibe ref")
    _require_relative_path(active["worktree"], f"{field}.worktree")
    _require_relative_path(active["result_path"], f"{field}.result_path")
    preflight = _optional_artifact(active["preflight"], f"{field}.preflight")
    handle = _validate_provider_handle(
        active["provider_handle"],
        f"{field}.provider_handle",
    )
    if preflight is None and status is not AttemptStatus.STARTING:
        raise ContractError(f"{field}.preflight may be null only while STARTING")
    if handle is not None and handle["attempt_token"] != active["attempt_token"]:
        raise ContractError(f"{field}.provider_handle token mismatch")
    return active


def _require_relative_path(value: object, field: str) -> str:
    raw = _require_string(value, field)
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or not path.parts
        or "." in path.parts
        or ".." in path.parts
        or raw != path.as_posix()
    ):
        raise ContractError(f"{field} must be a canonical relative path")
    return raw


def _validate_task(task_id: str, value: object) -> None:
    if TASK_ID_RE.fullmatch(task_id) is None:
        raise ContractError(f"invalid task ID: {task_id}")
    task = _require_exact_fields(value, f"tasks.{task_id}", TASK_FIELDS)
    _artifact(task["task"], f"tasks.{task_id}.task")
    try:
        status = TaskStatus(task["status"])
    except (TypeError, ValueError) as error:
        raise ContractError(f"tasks.{task_id}.status is invalid") from error
    attempt_no = _require_plain_int(
        task["attempt_no"],
        f"tasks.{task_id}.attempt_no",
    )
    failures = _require_plain_int(
        task["failure_count"],
        f"tasks.{task_id}.failure_count",
    )
    maximum = _require_plain_int(
        task["max_attempts"],
        f"tasks.{task_id}.max_attempts",
        1,
    )
    if failures > maximum:
        raise ContractError(f"tasks.{task_id}.failure_count exceeds limit")
    active = _validate_active_attempt(
        task["active_attempt"],
        f"tasks.{task_id}.active_attempt",
    )
    attempts = _artifact_list(task["attempts"], f"tasks.{task_id}.attempts")
    result = _optional_artifact(task["result"], f"tasks.{task_id}.result")
    verification = _optional_artifact(
        task["verification"],
        f"tasks.{task_id}.verification",
    )
    for name in ("source_commits", "integrated_commits"):
        values = task[name]
        if not isinstance(values, list):
            raise ContractError(f"tasks.{task_id}.{name} must be an array")
        for index, oid in enumerate(values):
            _require_oid(oid, f"tasks.{task_id}.{name}[{index}]")
    _optional_error(task["last_error"], f"tasks.{task_id}.last_error")
    if status in {TaskStatus.PENDING, TaskStatus.READY}:
        if active is not None or result is not None or verification is not None:
            raise ContractError(f"tasks.{task_id} idle fields are incoherent")
    elif status is TaskStatus.RUNNING:
        if active is None or verification is not None:
            raise ContractError(f"tasks.{task_id} RUNNING fields are incoherent")
        if active["status"] == AttemptStatus.VERIFYING.value:
            if result is None or active["provider_handle"] is not None:
                raise ContractError(
                    f"tasks.{task_id} VERIFYING fields are incoherent"
                )
        elif result is not None:
            raise ContractError(
                f"tasks.{task_id}.result requires VERIFYING active status"
            )
    elif status is TaskStatus.READY_TO_INTEGRATE:
        if (
            active is None
            or active["status"] != AttemptStatus.VERIFYING.value
            or result is None
            or verification is not None
            or len(task["source_commits"]) != 1
        ):
            raise ContractError(
                f"tasks.{task_id} READY_TO_INTEGRATE fields are incoherent"
            )
    elif status is TaskStatus.INTEGRATING:
        if active is not None or not attempts or result is None or verification is None:
            raise ContractError(f"tasks.{task_id} INTEGRATING fields are incoherent")
    elif status is TaskStatus.COMPLETED:
        if (
            active is not None
            or not attempts
            or result is None
            or verification is None
            or not task["source_commits"]
            or not task["integrated_commits"]
        ):
            raise ContractError(f"tasks.{task_id} COMPLETED fields are incoherent")
    elif status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
        if active is not None or not attempts:
            raise ContractError(f"tasks.{task_id} terminal fields are incoherent")
    if active is not None and attempt_no < 1:
        raise ContractError(f"tasks.{task_id} active Attempt requires attempt_no")


def _validate_pending_dispatch(
    token: str,
    value: object,
    state: dict[str, object],
) -> None:
    pending = _require_exact_fields(
        value,
        f"pending_dispatches.{token}",
        PENDING_DISPATCH_FIELDS,
    )
    if pending["attempt_token"] != token:
        raise ContractError("pending dispatch key/token mismatch")
    role = pending["role"]
    if role not in {"planner", "worker", "evaluator"}:
        raise ContractError("pending dispatch role is invalid")
    _require_string(pending["operation_id"], "pending operation_id")
    _require_plain_int(pending["attempt_no"], "pending attempt_no", 1)
    _require_plain_int(
        pending["provider_retry_no"],
        "pending provider_retry_no",
    )
    _require_plain_int(
        pending["prepared_revision"],
        "pending prepared_revision",
        1,
    )
    _require_rfc3339(
        pending["attempt_created_at"],
        "pending attempt_created_at",
    )
    _require_oid(pending["expected_base"], "pending expected_base")
    _require_relative_path(pending["worktree"], "pending worktree")
    _require_relative_path(pending["provider_prefix"], "pending provider_prefix")
    for name in (
        "launch_path",
        "stdout_path",
        "stderr_path",
        "exit_path",
        "result_path",
    ):
        _require_relative_path(pending[name], f"pending {name}")
    for name in ("prompt", "schema", "preflight", "request"):
        _artifact(pending[name], f"pending {name}")
    for name in ("launch", "stdout", "stderr", "exit", "result"):
        _optional_artifact(pending[name], f"pending {name}")
    _artifact_list(pending["provider_attempts"], "pending provider_attempts")
    if not isinstance(pending["prompt_versions"], list):
        raise ContractError("pending prompt_versions must be an array")
    handle = _validate_provider_handle(
        pending["provider_handle"],
        "pending provider_handle",
    )
    if handle is not None and handle["attempt_token"] != token:
        raise ContractError("pending provider handle token mismatch")
    if role == "worker":
        task_id = pending["task_id"]
        if not isinstance(task_id, str) or task_id not in state["tasks"]:
            raise ContractError("Worker pending dispatch has invalid task")
        branch = pending["branch"]
        if not isinstance(branch, str):
            raise ContractError("Worker pending dispatch requires branch")
        active = state["tasks"][task_id]["active_attempt"]
        if active is None or active["attempt_token"] != token:
            raise ContractError("Worker pending dispatch is not active")
        if active["preflight"] != pending["preflight"]:
            raise ContractError("Worker pending preflight mismatch")
        if active["provider_handle"] != pending["provider_handle"]:
            raise ContractError("Worker provider handle views disagree")
    else:
        if pending["task_id"] is not None or pending["branch"] is not None:
            raise ContractError("read-only pending identity is invalid")
        runtime = state["role_runtime"][role]
        if (
            runtime["active_attempt_token"] != token
            or runtime["operation_id"] != pending["operation_id"]
        ):
            raise ContractError("read-only pending dispatch is not active")


def _validate_prepared_markers(state: dict[str, object]) -> None:
    source = state["pending_source_commit"]
    integration = state["pending_integration"]
    evaluation = state["pending_evaluation"]
    if sum(item is not None for item in (source, integration, evaluation)) > 1:
        raise ContractError("prepared markers are mutually exclusive")
    if source is not None:
        raw = _require_exact_fields(
            source,
            "pending_source_commit",
            {
                "operation_id",
                "task_id",
                "attempt_no",
                "expected_base",
                "task_ref",
                "tree_oid",
                "candidate_commit",
                "author_name",
                "author_email",
                "timestamp",
                "message",
                "source_audit",
            },
        )
        task_id = _require_string(raw["task_id"], "pending_source_commit.task_id")
        if task_id not in state["tasks"]:
            raise ContractError("pending_source_commit task is missing")
        _require_plain_int(
            raw["attempt_no"],
            "pending_source_commit.attempt_no",
            1,
        )
        for name in ("expected_base", "tree_oid", "candidate_commit"):
            _require_oid(raw[name], f"pending_source_commit.{name}")
        _require_rfc3339(raw["timestamp"], "pending_source_commit.timestamp")
        _artifact(raw["source_audit"], "pending_source_commit.source_audit")
    if integration is not None:
        raw = _require_exact_fields(
            integration,
            "pending_integration",
            {
                "operation_id",
                "task_id",
                "attempt_no",
                "expected_head",
                "candidate_head",
                "source_base",
                "source_head",
                "verification",
            },
        )
        task_id = _require_string(raw["task_id"], "pending_integration.task_id")
        if task_id not in state["tasks"]:
            raise ContractError("pending_integration task is missing")
        _require_plain_int(raw["attempt_no"], "pending_integration.attempt_no", 1)
        for name in (
            "expected_head",
            "candidate_head",
            "source_base",
            "source_head",
        ):
            _require_oid(raw[name], f"pending_integration.{name}")
        _artifact(raw["verification"], "pending_integration.verification")
    if evaluation is not None:
        raw = _require_exact_fields(
            evaluation,
            "pending_evaluation",
            {
                "operation_id",
                "attempt_no",
                "attempt_token",
                "evaluation_round",
                "evidence_round",
                "integration_head",
                "attempt",
                "raw_result",
            },
        )
        for name in ("attempt_no", "evaluation_round"):
            _require_plain_int(raw[name], f"pending_evaluation.{name}", 1)
        _require_plain_int(
            raw["evidence_round"],
            "pending_evaluation.evidence_round",
        )
        _require_oid(raw["integration_head"], "pending_evaluation.integration_head")
        _artifact(raw["attempt"], "pending_evaluation.attempt")
        _artifact(raw["raw_result"], "pending_evaluation.raw_result")


def validate_run_state(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ContractError("run state must be an object")
    unknown = set(value) - RUN_FIELDS
    missing = RUN_FIELDS - set(value)
    if unknown:
        raise ContractError(f"unknown run state fields: {sorted(unknown)}")
    if missing:
        raise ContractError(f"missing run state fields: {sorted(missing)}")
    state = value
    reject_invalid_json_scalars(state)
    if type(state["schema_version"]) is not int or state["schema_version"] != 4:
        raise ContractError("schema_version must be integer 4")
    run_id = _require_string(state["run_id"], "run_id")
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ContractError("run_id must match RUN-YYYYMMDD-NNN")
    _require_plain_int(state["revision"], "revision")
    _require_string(state["goal"], "goal")
    _validate_repository(state["repository"], run_id)
    try:
        status = RunStatus(state["status"])
    except (TypeError, ValueError) as error:
        raise ContractError("status is invalid") from error
    resume_value = state["resume_status"]
    if resume_value is not None:
        try:
            resume = RunStatus(resume_value)
        except (TypeError, ValueError) as error:
            raise ContractError("resume_status is invalid") from error
        if resume in {
            RunStatus.PAUSED,
            RunStatus.STOPPED,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.IMPORTED_READ_ONLY,
        }:
            raise ContractError("resume_status must name an active state")
    _require_plain_int(state["plan_version"], "plan_version")
    _require_plain_int(state["repair_round"], "repair_round")
    _require_plain_int(state["max_repair_rounds"], "max_repair_rounds", 1)
    _require_plain_int(state["max_workers"], "max_workers", 1)
    _validate_controller(state["controller"])
    creation = _require_exact_fields(
        state["creation"],
        "creation",
        {"intent", "receipt"},
    )
    _artifact(creation["intent"], "creation.intent")
    _optional_artifact(creation["receipt"], "creation.receipt")
    _artifact(state["config"], "config")
    artifact_index = _artifact_list(state["artifact_index"], "artifact_index")
    index_paths: dict[str, str] = {}
    for reference in artifact_index:
        previous = index_paths.get(reference.path)
        if previous is not None and previous != reference.sha256:
            raise ContractError("artifact_index contains conflicting digests")
        index_paths[reference.path] = reference.sha256
    plans = _artifact_list(state["plans"], "plans")
    if state["plan_version"] != len(plans):
        raise ContractError("plan_version must equal len(plans)")
    role_attempts = _require_exact_fields(
        state["role_attempts"],
        "role_attempts",
        {"planner", "evaluator"},
    )
    for role in ("planner", "evaluator"):
        _artifact_list(role_attempts[role], f"role_attempts.{role}")
    _validate_role_runtime(state["role_runtime"])
    _artifact_list(state["evaluations"], "evaluations")
    _artifact_list(state["verifications"], "verifications")
    _optional_artifact(state["legacy_import"], "legacy_import")
    if not isinstance(state["tasks"], dict):
        raise ContractError("tasks must be an object")
    for task_id, task in state["tasks"].items():
        _validate_task(task_id, task)
    if not isinstance(state["pending_dispatches"], dict):
        raise ContractError("pending_dispatches must be an object")
    for token, pending in state["pending_dispatches"].items():
        _require_string(token, "pending dispatch key")
        _validate_pending_dispatch(token, pending, state)
    _validate_prepared_markers(state)
    latest = state["latest_evaluation"]
    if latest is not None:
        raw = _require_exact_fields(
            latest,
            "latest_evaluation",
            {
                "evaluation",
                "verdict",
                "evaluation_round",
                "evidence_round",
                "integration_head",
            },
        )
        _artifact(raw["evaluation"], "latest_evaluation.evaluation")
        try:
            EvaluationVerdict(raw["verdict"])
        except (TypeError, ValueError) as error:
            raise ContractError("latest_evaluation.verdict is invalid") from error
        _require_plain_int(
            raw["evaluation_round"],
            "latest_evaluation.evaluation_round",
            1,
        )
        _require_plain_int(
            raw["evidence_round"],
            "latest_evaluation.evidence_round",
        )
        _require_oid(
            raw["integration_head"],
            "latest_evaluation.integration_head",
        )
    global_verification = state["global_verification"]
    if global_verification is not None:
        raw = _require_exact_fields(
            global_verification,
            "global_verification",
            {"verification", "integration_head", "passed"},
        )
        _artifact(raw["verification"], "global_verification.verification")
        _require_oid(
            raw["integration_head"],
            "global_verification.integration_head",
        )
        if type(raw["passed"]) is not bool:
            raise ContractError("global_verification.passed must be a boolean")
    if not isinstance(state["stop_receipts"], list):
        raise ContractError("stop_receipts must be an array")
    for index, item in enumerate(state["stop_receipts"]):
        raw = _require_exact_fields(
            item,
            f"stop_receipts[{index}]",
            {"nonce", "receipt"},
        )
        _require_string(raw["nonce"], f"stop_receipts[{index}].nonce")
        _artifact(raw["receipt"], f"stop_receipts[{index}].receipt")
    last_error = _optional_error(state["last_error"], "last_error")
    created_at = _require_rfc3339(state["created_at"], "created_at")
    updated_at = _require_rfc3339(state["updated_at"], "updated_at")
    if datetime.fromisoformat(updated_at) < datetime.fromisoformat(created_at):
        raise ContractError("updated_at must not precede created_at")
    migration_pause = (
        status is RunStatus.PAUSED
        and isinstance(last_error, dict)
        and last_error.get("code")
        in {"SCHEMA3_REPLAN_REQUIRED", "MIGRATION_INSTALLING"}
    )
    if status in {RunStatus.PAUSED, RunStatus.STOPPED}:
        if resume_value is None and not migration_pause:
            raise ContractError("resume_status is required while paused/stopped")
    elif resume_value is not None:
        raise ContractError("resume_status is only valid while paused/stopped")
    if status in {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.IMPORTED_READ_ONLY,
    } and (
        state["pending_dispatches"]
        or state["pending_source_commit"] is not None
        or state["pending_integration"] is not None
        or state["pending_evaluation"] is not None
    ):
        raise ContractError("terminal runs cannot retain pending work")
    return copy.deepcopy(state)


def transition_run(
    state: dict[str, object],
    target: RunStatus,
) -> dict[str, object]:
    current = RunStatus(state["status"])
    if target not in ALLOWED_RUN_TRANSITIONS[current]:
        raise ContractError(
            f"invalid run transition {current.value} -> {target.value}"
        )
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


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = item
    return result


def _parse_artifact_bytes(body: bytes, field: str) -> dict[str, object]:
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda literal: (_ for _ in ()).throw(
                ContractError(f"non-finite JSON number: {literal}")
            ),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ContractError(f"{field} is not strict JSON: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be a JSON object")
    reject_invalid_json_scalars(value)
    return value


def _parse_prompt_ref(value: object, field: str) -> PromptRef:
    raw = _require_exact_fields(value, field, {"id", "version", "sha256"})
    prompt_id = _require_string(raw["id"], f"{field}.id")
    version = _require_plain_int(raw["version"], f"{field}.version", 1)
    digest = _require_string(raw["sha256"], f"{field}.sha256")
    if DIGEST_RE.fullmatch(digest) is None:
        raise ContractError(f"{field}.sha256 must be canonical")
    return PromptRef(id=prompt_id, version=version, sha256=digest)


def validate_attempt_manifest(value: object) -> AttemptManifest:
    raw = _require_exact_fields(
        value,
        "attempt manifest",
        ATTEMPT_MANIFEST_FIELDS,
    )
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ContractError("attempt manifest schema_version must be integer 1")
    role = _require_string(raw["role"], "attempt manifest role")
    if role not in {"planner", "worker", "evaluator"}:
        raise ContractError("attempt manifest role is invalid")
    operation_id = _require_string(
        raw["operation_id"],
        "attempt manifest operation_id",
    )
    if OPERATION_ID_RE.fullmatch(operation_id) is None:
        raise ContractError("attempt manifest operation_id is invalid")
    task_id_value = raw["task_id"]
    if role == "worker":
        task_id = _require_string(task_id_value, "attempt manifest task_id")
        if TASK_ID_RE.fullmatch(task_id) is None:
            raise ContractError("attempt manifest task_id is invalid")
        branch = _require_string(raw["branch"], "attempt manifest branch")
        if not branch.startswith("refs/heads/vibe/"):
            raise ContractError("attempt manifest branch is invalid")
    else:
        if task_id_value is not None or raw["branch"] is not None:
            raise ContractError("read-only attempt identity is invalid")
        task_id = None
        branch = None
    attempt_no = _require_plain_int(
        raw["attempt_no"],
        "attempt manifest attempt_no",
        1,
    )
    attempt_token = _require_string(
        raw["attempt_token"],
        "attempt manifest attempt_token",
    )
    try:
        status = AttemptStatus(raw["status"])
    except (TypeError, ValueError) as error:
        raise ContractError("attempt manifest status is invalid") from error
    if status not in {
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.CANCELLED,
        AttemptStatus.ABANDONED,
    }:
        raise ContractError("attempt manifest status must be terminal")
    created_at = _require_rfc3339(
        raw["created_at"],
        "attempt manifest created_at",
    )
    completed_at = _require_rfc3339(
        raw["completed_at"],
        "attempt manifest completed_at",
    )
    if datetime.fromisoformat(completed_at) < datetime.fromisoformat(created_at):
        raise ContractError("attempt manifest completion precedes creation")
    expected_base = _require_oid(
        raw["expected_base"],
        "attempt manifest expected_base",
    )
    worktree = _require_relative_path(
        raw["worktree"],
        "attempt manifest worktree",
    )
    preflight = _optional_artifact(
        raw["preflight"],
        "attempt manifest preflight",
    )
    prompt_values = raw["prompt_versions"]
    if not isinstance(prompt_values, list):
        raise ContractError("attempt manifest prompt_versions must be an array")
    prompt_versions = tuple(
        _parse_prompt_ref(item, f"prompt_versions[{index}]")
        for index, item in enumerate(prompt_values)
    )
    provider_attempts = tuple(
        _artifact_list(raw["provider_attempts"], "provider_attempts")
    )
    artifact_names = (
        "request",
        "launch",
        "stdout",
        "stderr",
        "exit",
        "result",
        "source_audit",
        "verification",
    )
    artifacts = {
        name: _optional_artifact(raw[name], f"attempt manifest {name}")
        for name in artifact_names
    }
    last_error = _optional_error(raw["last_error"], "attempt manifest last_error")
    null_preflight_allowed = (
        role == "worker"
        and status is AttemptStatus.CANCELLED
        and isinstance(last_error, dict)
        and last_error.get("code") == "CANCELLED_BEFORE_MATERIALIZATION"
        and all(reference is None for reference in artifacts.values())
    )
    if preflight is None and not null_preflight_allowed:
        raise ContractError("attempt manifest preflight is required")
    if status is AttemptStatus.SUCCEEDED and role == "worker":
        for name in ("result", "source_audit", "verification"):
            if artifacts[name] is None:
                raise ContractError(
                    f"successful Worker attempt requires {name}"
                )
    return AttemptManifest(
        schema_version=1,
        role=role,
        operation_id=operation_id,
        task_id=task_id,
        attempt_no=attempt_no,
        attempt_token=attempt_token,
        status=status,
        created_at=created_at,
        completed_at=completed_at,
        expected_base=expected_base,
        branch=branch,
        worktree=worktree,
        preflight=preflight,
        prompt_versions=prompt_versions,
        provider_attempts=provider_attempts,
        request=artifacts["request"],
        launch=artifacts["launch"],
        stdout=artifacts["stdout"],
        stderr=artifacts["stderr"],
        exit=artifacts["exit"],
        result=artifacts["result"],
        source_audit=artifacts["source_audit"],
        verification=artifacts["verification"],
        last_error=last_error,
    )


def validate_bound_state_semantics(
    state: dict[str, object],
    artifact_loader: Callable[[str], bytes],
) -> None:
    active_tokens: set[str] = set(state["pending_dispatches"])
    for task in state["tasks"].values():
        active = task["active_attempt"]
        if active is not None:
            active_tokens.add(active["attempt_token"])
    seen_role_attempts: set[tuple[str, str, int]] = set()
    for role in ("planner", "evaluator"):
        previous_by_operation: dict[str, int] = {}
        for reference in state["role_attempts"][role]:
            manifest = validate_attempt_manifest(
                _parse_artifact_bytes(
                    artifact_loader(reference["path"]),
                    reference["path"],
                )
            )
            if manifest.role != role or manifest.task_id is not None:
                raise ContractError("role attempt history owner mismatch")
            identity = (role, manifest.operation_id, manifest.attempt_no)
            if identity in seen_role_attempts:
                raise ContractError("duplicate role attempt identity")
            seen_role_attempts.add(identity)
            previous = previous_by_operation.get(manifest.operation_id, 0)
            if manifest.attempt_no <= previous:
                raise ContractError("role attempt numbers are not increasing")
            previous_by_operation[manifest.operation_id] = manifest.attempt_no
            if manifest.attempt_token in active_tokens:
                raise ContractError("terminal attempt token remains active")
    for task_id, task in state["tasks"].items():
        previous = 0
        parsed: list[AttemptManifest] = []
        for reference in task["attempts"]:
            manifest = validate_attempt_manifest(
                _parse_artifact_bytes(
                    artifact_loader(reference["path"]),
                    reference["path"],
                )
            )
            if manifest.role != "worker" or manifest.task_id != task_id:
                raise ContractError("Worker attempt history owner mismatch")
            if manifest.attempt_no <= previous:
                raise ContractError("Worker attempt numbers are not increasing")
            previous = manifest.attempt_no
            if manifest.attempt_token in active_tokens:
                raise ContractError("terminal Worker token remains active")
            parsed.append(manifest)
        if task["status"] in {
            TaskStatus.INTEGRATING.value,
            TaskStatus.COMPLETED.value,
        }:
            if not parsed or parsed[-1].status is not AttemptStatus.SUCCEEDED:
                raise ContractError("integrated Task lacks successful Attempt")
            final = parsed[-1]
            if (
                final.result is None
                or final.result.as_dict() != task["result"]
                or final.verification is None
                or final.verification.as_dict() != task["verification"]
            ):
                raise ContractError("successful Attempt pointers disagree")
    pending_evaluation = state["pending_evaluation"]
    if pending_evaluation is not None:
        attempt_ref = pending_evaluation["attempt"]
        manifest = validate_attempt_manifest(
            _parse_artifact_bytes(
                artifact_loader(attempt_ref["path"]),
                attempt_ref["path"],
            )
        )
        if (
            manifest.role != "evaluator"
            or manifest.operation_id != pending_evaluation["operation_id"]
            or manifest.attempt_no != pending_evaluation["attempt_no"]
            or manifest.attempt_token != pending_evaluation["attempt_token"]
            or manifest.result is None
            or manifest.result.as_dict() != pending_evaluation["raw_result"]
        ):
            raise ContractError("pending_evaluation does not match Attempt")


def goal_gate_satisfied(
    state: dict[str, object],
    evaluation_envelope: dict[str, object],
    actual_integration_head: str,
) -> bool:
    repository = state.get("repository")
    latest = state.get("latest_evaluation")
    verification = state.get("global_verification")
    tasks = state.get("tasks", {})
    if not isinstance(repository, dict):
        return False
    integration_head = repository.get("integration_head")
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
                == integration_head
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
        and isinstance(tasks, dict)
        and bool(tasks)
        and all(
            isinstance(task, dict)
            and task.get("status") == TaskStatus.COMPLETED.value
            and bool(task.get("attempts"))
            and isinstance(task.get("result"), dict)
            and isinstance(task.get("verification"), dict)
            and task.get("verification") in state.get("verifications", [])
            for task in tasks.values()
        )
        and isinstance(latest, dict)
        and isinstance(verification, dict)
        and latest.get("verdict") == EvaluationVerdict.PASS.value
        and bool(state.get("evaluations"))
        and latest.get("evaluation") == state.get("evaluations", [])[-1]
        and latest.get("integration_head") == integration_head
        and evaluation_envelope.get("verdict") == EvaluationVerdict.PASS.value
        and evaluation_envelope.get("integration_head") == integration_head
        and evidence_is_complete
        and not evaluation_envelope.get("findings")
        and verification.get("passed") is True
        and verification.get("verification") in state.get("verifications", [])
        and verification.get("integration_head") == integration_head
        and actual_integration_head == integration_head
        and not state.get("pending_dispatches")
        and state.get("pending_source_commit") is None
        and state.get("pending_integration") is None
        and state.get("pending_evaluation") is None
        and state.get("last_error") is None
    )
