from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import os
from pathlib import Path, PurePosixPath
from typing import Protocol

from vibe.models import ContractError, ProviderStatus
from vibe.prompt_registry import parse_single_json_object
from vibe.state_store import (
    canonical_json_bytes,
    open_absolute_regular_no_follow,
    read_bounded,
)


class ProviderConfigurationError(ContractError):
    """The configured Provider cannot be launched safely."""


class ProviderIdentityError(ContractError):
    """A persisted process handle no longer identifies the same process."""


class ProviderFailureKind(str, Enum):
    TRANSIENT = "TRANSIENT"
    AUTH = "AUTH"
    CONFIGURATION = "CONFIGURATION"
    TIMEOUT = "TIMEOUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    PROCESS = "PROCESS"


@dataclass(frozen=True)
class ProviderFailure:
    kind: ProviderFailureKind
    message: str


@dataclass(frozen=True)
class ProviderExecutionIdentity:
    codex_version: str
    policy_sha256: str


@dataclass(frozen=True)
class ProviderRequest:
    attempt_token: str
    role: str
    request_path: str
    prompt_path: str
    schema_path: str
    cwd: str
    sandbox: str
    launch_path: str
    stdout_path: str
    stderr_path: str
    exit_path: str
    result_path: str
    timeout_seconds: int
    codex_version: str
    execution_policy_sha256: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def for_test(
        cls,
        root: Path,
        attempt_token: str,
        role: str,
        result_body: bytes,
    ) -> ProviderRequest:
        root.mkdir(parents=True, exist_ok=True)
        prompt = root / "prompt.md"
        schema = root / "schema.json"
        prompt.write_text("test\n", encoding="utf-8")
        schema.write_text(
            '{"type":"object"}\n',
            encoding="utf-8",
        )
        result = root / "result.json"
        result.write_bytes(result_body)
        return cls(
            attempt_token=attempt_token,
            role=role,
            request_path=str(root / "request.json"),
            prompt_path=str(prompt),
            schema_path=str(schema),
            cwd=str(root),
            sandbox=(
                "read-only"
                if role != "worker"
                else "workspace-write"
            ),
            launch_path=str(root / "launch.json"),
            stdout_path=str(root / "stdout.log"),
            stderr_path=str(root / "stderr.log"),
            exit_path=str(root / "exit.json"),
            result_path=str(result),
            timeout_seconds=30,
            codex_version="codex-cli-test",
            execution_policy_sha256="sha256:" + "0" * 64,
        )


@dataclass(frozen=True)
class ProviderHandle:
    adapter: str
    attempt_token: str
    pid: int
    process_start_identity: str
    process_group: int
    child_pid: int
    child_process_start_identity: str
    child_process_group: int
    codex_version: str
    execution_policy_sha256: str
    launch_path: str
    stdout_path: str
    stderr_path: str
    exit_path: str
    result_path: str

    def as_state_dict(self) -> dict[str, object]:
        return {
            "adapter": self.adapter,
            "attempt_token": self.attempt_token,
            "pid": self.pid,
            "process_start_identity": self.process_start_identity,
            "process_group": self.process_group,
            "child_pid": self.child_pid,
            "child_process_start_identity": (
                self.child_process_start_identity
            ),
            "child_process_group": self.child_process_group,
        }


@dataclass(frozen=True)
class ProviderCompletion:
    adapter: str
    attempt_token: str
    pid: int
    process_start_identity: str
    process_group: int
    child_pid: int
    child_process_start_identity: str
    child_process_group: int
    codex_version: str
    execution_policy_sha256: str
    exit_code: int
    timed_out: bool
    result_published: bool
    stop_requested: bool
    stop_forced: bool
    stderr_body: bytes


@dataclass(frozen=True)
class ProviderResult:
    attempt_token: str
    body: bytes
    exit_code: int


@dataclass(frozen=True)
class StopResult:
    attempt_token: str
    stopped: bool
    forced: bool


EXIT_FIELDS = {
    "adapter",
    "attempt_token",
    "pid",
    "process_start_identity",
    "process_group",
    "child_pid",
    "child_process_start_identity",
    "child_process_group",
    "codex_version",
    "execution_policy_sha256",
    "exit_code",
    "timed_out",
    "result_published",
    "stop_requested",
    "stop_forced",
}


def parse_exit_receipt(
    raw: bytes,
    expected: ProviderHandle,
    *,
    stderr_body: bytes = b"",
) -> ProviderCompletion:
    value = parse_single_json_object(raw)
    if set(value) != EXIT_FIELDS:
        raise ContractError("exit receipt fields are invalid")
    expected_identity = expected.as_state_dict()
    for field, expected_value in expected_identity.items():
        if value.get(field) != expected_value:
            raise ProviderIdentityError(
                f"exit receipt {field} does not match the handle"
            )
    for field in ("codex_version", "execution_policy_sha256"):
        expected_value = getattr(expected, field)
        if value.get(field) != expected_value:
            raise ProviderIdentityError(
                f"exit receipt {field} does not match the request"
            )
    if type(value["exit_code"]) is not int:
        raise ContractError("exit_code must be an integer")
    for field in (
        "timed_out",
        "result_published",
        "stop_requested",
        "stop_forced",
    ):
        if type(value[field]) is not bool:
            raise ContractError(f"{field} must be a boolean")
    if value["stop_forced"] and not value["stop_requested"]:
        raise ContractError("forced stop requires a stop request")
    if value["timed_out"] and value["exit_code"] != 124:
        raise ContractError(
            "timed-out receipt must use exit code 124"
        )
    if value["stop_requested"] and (
        value["timed_out"] or value["exit_code"] == 0
    ):
        raise ContractError(
            "stop receipt has impossible terminal cause"
        )
    expected_published = (
        value["exit_code"] == 0
        and not value["timed_out"]
        and not value["stop_requested"]
    )
    if value["result_published"] is not expected_published:
        raise ContractError(
            "result publication disagrees with terminal cause"
        )
    return ProviderCompletion(
        **expected_identity,
        codex_version=expected.codex_version,
        execution_policy_sha256=(
            expected.execution_policy_sha256
        ),
        exit_code=value["exit_code"],
        timed_out=value["timed_out"],
        result_published=value["result_published"],
        stop_requested=value["stop_requested"],
        stop_forced=value["stop_forced"],
        stderr_body=stderr_body,
    )


class ProviderAdapter(Protocol):
    def execution_identity(self) -> ProviderExecutionIdentity:
        raise NotImplementedError

    def start(self, request: ProviderRequest) -> ProviderHandle:
        raise NotImplementedError

    def poll(self, handle: ProviderHandle) -> ProviderStatus:
        raise NotImplementedError

    def stop(
        self,
        handle: ProviderHandle,
        grace_period: float,
    ) -> StopResult:
        raise NotImplementedError

    def completion(
        self,
        handle: ProviderHandle,
    ) -> ProviderCompletion:
        raise NotImplementedError

    def result(self, handle: ProviderHandle) -> ProviderResult:
        raise NotImplementedError


def request_from_pending(
    pending: object,
    trusted_run_root: Path,
    trusted_target_root: Path,
) -> ProviderRequest:
    if not isinstance(pending, dict):
        raise ContractError("pending dispatch must be an object")
    run_root = trusted_run_root.resolve()
    target_root = trusted_target_root.resolve()

    def relative(field: str) -> PurePosixPath:
        value = pending.get(field)
        if not isinstance(value, str):
            raise ContractError(
                f"pending dispatch {field} is invalid"
            )
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or "." in path.parts
            or ".." in path.parts
            or value != path.as_posix()
        ):
            raise ContractError(
                f"pending dispatch {field} is unsafe"
            )
        return path

    def artifact_path(field: str) -> Path:
        value = pending.get(field)
        if (
            not isinstance(value, dict)
            or set(value) != {"path", "sha256"}
            or not isinstance(value.get("path"), str)
        ):
            raise ContractError(
                f"pending dispatch {field} ArtifactRef is invalid"
            )
        path = PurePosixPath(value["path"])
        if (
            path.is_absolute()
            or not path.parts
            or "." in path.parts
            or ".." in path.parts
        ):
            raise ContractError(
                f"pending dispatch {field} path is unsafe"
            )
        return run_root.joinpath(*path.parts)

    request_path = artifact_path("request")
    descriptor = open_absolute_regular_no_follow(request_path)
    try:
        body = read_bounded(
            descriptor,
            max_bytes=4 * 1024 * 1024,
        )
    finally:
        os.close(descriptor)
    raw = parse_single_json_object(body)
    expected_fields = set(ProviderRequest.__dataclass_fields__)
    if set(raw) != expected_fields:
        raise ContractError(
            "persisted Provider request fields are invalid"
        )
    try:
        request = ProviderRequest(**raw)
    except TypeError as error:
        raise ContractError(
            "persisted Provider request is invalid"
        ) from error
    expected_paths = {
        "request_path": request_path,
        "prompt_path": artifact_path("prompt"),
        "schema_path": artifact_path("schema"),
        "cwd": target_root.joinpath(
            *relative("worktree").parts
        ),
        "launch_path": run_root.joinpath(
            *relative("launch_path").parts
        ),
        "stdout_path": run_root.joinpath(
            *relative("stdout_path").parts
        ),
        "stderr_path": run_root.joinpath(
            *relative("stderr_path").parts
        ),
        "exit_path": run_root.joinpath(
            *relative("exit_path").parts
        ),
        "result_path": run_root.joinpath(
            *relative("result_path").parts
        ),
    }
    for field, expected in expected_paths.items():
        if getattr(request, field) != str(expected):
            raise ContractError(
                f"persisted Provider request {field} is not trusted"
            )
    scalar_fields = {
        "attempt_token": pending.get("attempt_token"),
        "role": pending.get("role"),
    }
    for field, expected in scalar_fields.items():
        if getattr(request, field) != expected:
            raise ContractError(
                f"persisted Provider request {field} changed"
            )
    if body != canonical_json_bytes(request.as_dict()):
        raise ContractError(
            "persisted Provider request is not canonical"
        )
    return request


def handle_from_pending(
    pending: object,
    trusted_run_root: Path,
    trusted_target_root: Path,
) -> ProviderHandle:
    request = request_from_pending(
        pending,
        trusted_run_root,
        trusted_target_root,
    )
    assert isinstance(pending, dict)
    identity = pending.get("provider_handle")
    if not isinstance(identity, dict):
        raise ContractError(
            "pending dispatch has no Provider handle"
        )
    expected_identity_fields = {
        "adapter",
        "attempt_token",
        "pid",
        "process_start_identity",
        "process_group",
        "child_pid",
        "child_process_start_identity",
        "child_process_group",
    }
    if set(identity) != expected_identity_fields:
        raise ContractError(
            "pending Provider handle fields are invalid"
        )
    try:
        return ProviderHandle(
            **identity,
            codex_version=request.codex_version,
            execution_policy_sha256=(
                request.execution_policy_sha256
            ),
            launch_path=request.launch_path,
            stdout_path=request.stdout_path,
            stderr_path=request.stderr_path,
            exit_path=request.exit_path,
            result_path=request.result_path,
        )
    except TypeError as error:
        raise ContractError(
            "pending Provider handle is invalid"
        ) from error


def classify_provider_failure(
    exit_code: int,
    stderr: str,
) -> ProviderFailure:
    lowered = stderr.lower()
    if exit_code == 124 or "timed out" in lowered:
        kind = ProviderFailureKind.TIMEOUT
    elif (
        "rate limit" in lowered
        or "temporar" in lowered
        or "network" in lowered
    ):
        kind = ProviderFailureKind.TRANSIENT
    elif "auth" in lowered or "login" in lowered:
        kind = ProviderFailureKind.AUTH
    elif "schema" in lowered or "invalid output" in lowered:
        kind = ProviderFailureKind.INVALID_OUTPUT
    elif (
        "configuration" in lowered
        or "not found" in lowered
    ):
        kind = ProviderFailureKind.CONFIGURATION
    else:
        kind = ProviderFailureKind.PROCESS
    return ProviderFailure(
        kind=kind,
        message=stderr.strip() or f"exit {exit_code}",
    )
