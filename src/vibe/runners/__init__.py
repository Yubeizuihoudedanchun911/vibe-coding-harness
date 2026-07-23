from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from vibe.models import (
    ArtifactRef,
    ContractError,
    StateConflictError,
)
from vibe.prompt_registry import PromptRef
from vibe.providers.base import (
    ProviderAdapter,
    ProviderHandle,
    ProviderRequest,
    parse_exit_receipt,
)
from vibe.state_store import (
    StateStore,
    canonical_json_bytes,
    open_absolute_directory_no_follow,
    read_bounded,
)


OPERATION_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z"
)
ATTEMPT_TOKEN_RE = re.compile(
    r"ATTEMPT-[A-Za-z0-9-]+\Z"
)
OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def require_operation_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or OPERATION_ID_RE.fullmatch(value) is None
    ):
        raise ContractError(
            "operation_id must be a canonical path-safe ID"
        )
    return value


@dataclass(frozen=True)
class RoleInvocation:
    role: str
    task_id: str | None
    operation_id: str
    attempt_no: int
    attempt_created_at: str
    attempt_token: str
    provider_retry_no: int
    expected_base: str
    branch: str | None
    worktree: str
    target_root: str
    run_root: str
    prompt_body: bytes
    prompt_versions: tuple[PromptRef, ...]
    schema_body: bytes
    preflight_body: bytes
    authorized_command_ids: tuple[str, ...]
    required_command_ids: tuple[str, ...]
    config_sha256: str
    codex_version: str
    execution_policy_sha256: str
    sandbox: str
    artifact_prefix: str
    timeout_seconds: int

    @property
    def provider_prefix(self) -> str:
        return (
            f"{self.artifact_prefix}/providers/"
            f"{self.provider_retry_no + 1:03d}-"
            f"{self.attempt_token}"
        )


class ReadOnlyAudit(Protocol):
    def capture(self, worktree: Path) -> dict[str, object]:
        raise NotImplementedError

    def assert_unchanged(
        self,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        raise NotImplementedError


def role_attempt_prefix(
    role: str,
    operation_id: str,
    attempt_no: int,
) -> str:
    if role not in {"planner", "evaluator"}:
        raise ContractError(
            "role_attempt_prefix is for read-only roles"
        )
    require_operation_id(operation_id)
    if type(attempt_no) is not int or attempt_no < 1:
        raise ContractError(
            "attempt_no must be a positive integer"
        )
    return (
        f"roles/{role}/operations/{operation_id}/"
        f"attempts/{attempt_no:03d}"
    )


def provider_request_for(
    invocation: RoleInvocation,
    prompt_path: str,
    schema_path: str,
    request_path: str,
) -> ProviderRequest:
    run_root = Path(invocation.run_root)
    prefix = PurePosixPath(invocation.provider_prefix)
    return ProviderRequest(
        attempt_token=invocation.attempt_token,
        role=invocation.role,
        request_path=str(run_root.joinpath(*PurePosixPath(request_path).parts)),
        prompt_path=str(run_root.joinpath(*PurePosixPath(prompt_path).parts)),
        schema_path=str(run_root.joinpath(*PurePosixPath(schema_path).parts)),
        cwd=str(
            Path(invocation.target_root).joinpath(
                *PurePosixPath(invocation.worktree).parts
            )
        ),
        sandbox=invocation.sandbox,
        launch_path=str(
            run_root.joinpath(*prefix.parts, "launch.json")
        ),
        stdout_path=str(
            run_root.joinpath(*prefix.parts, "stdout.log")
        ),
        stderr_path=str(
            run_root.joinpath(*prefix.parts, "stderr.log")
        ),
        exit_path=str(
            run_root.joinpath(*prefix.parts, "exit.json")
        ),
        result_path=str(
            run_root.joinpath(*prefix.parts, "result.json")
        ),
        timeout_seconds=invocation.timeout_seconds,
        codex_version=invocation.codex_version,
        execution_policy_sha256=(
            invocation.execution_policy_sha256
        ),
    )


def add_pending_dispatch(
    current: dict[str, object],
    invocation: RoleInvocation,
    prompt_ref: ArtifactRef,
    schema_ref: ArtifactRef,
    preflight_ref: ArtifactRef,
    request_ref: ArtifactRef,
    request: ProviderRequest,
) -> None:
    del request
    pending = current["pending_dispatches"]
    if invocation.attempt_token in pending:
        raise ContractError("attempt token already prepared")
    pending[invocation.attempt_token] = {
        "attempt_token": invocation.attempt_token,
        "role": invocation.role,
        "task_id": invocation.task_id,
        "operation_id": invocation.operation_id,
        "attempt_no": invocation.attempt_no,
        "attempt_created_at": invocation.attempt_created_at,
        "provider_retry_no": invocation.provider_retry_no,
        "expected_base": invocation.expected_base,
        "branch": invocation.branch,
        "worktree": invocation.worktree,
        "provider_prefix": invocation.provider_prefix,
        "prompt": prompt_ref.as_dict(),
        "schema": schema_ref.as_dict(),
        "preflight": preflight_ref.as_dict(),
        "prompt_versions": [
            reference.as_dict()
            for reference in invocation.prompt_versions
        ],
        "request": request_ref.as_dict(),
        "launch_path": (
            f"{invocation.provider_prefix}/launch.json"
        ),
        "stdout_path": (
            f"{invocation.provider_prefix}/stdout.log"
        ),
        "stderr_path": (
            f"{invocation.provider_prefix}/stderr.log"
        ),
        "exit_path": f"{invocation.provider_prefix}/exit.json",
        "result_path": (
            f"{invocation.provider_prefix}/result.json"
        ),
        "launch": None,
        "stdout": None,
        "stderr": None,
        "exit": None,
        "result": None,
        "provider_attempts": [],
        "provider_handle": None,
        "prepared_revision": current["revision"] + 1,
    }


def bind_matching_handle(
    current: dict[str, object],
    attempt_token: str,
    handle: ProviderHandle,
    launch_ref: ArtifactRef,
) -> None:
    pending = current["pending_dispatches"].get(attempt_token)
    if (
        pending is None
        or handle.attempt_token != attempt_token
    ):
        raise StateConflictError(
            "dispatch was superseded before handle binding"
        )
    handle_state = handle.as_state_dict()
    launch_state = launch_ref.as_dict()
    if (
        pending["launch"] not in (None, launch_state)
        or pending["provider_handle"]
        not in (None, handle_state)
    ):
        raise StateConflictError(
            "dispatch was superseded before handle binding"
        )
    role = pending["role"]
    if role == "worker":
        task_id = pending["task_id"]
        task = current["tasks"].get(task_id)
        active = None if task is None else task["active_attempt"]
        if (
            active is None
            or active["attempt_token"] != attempt_token
            or active["status"] not in {"STARTING", "RUNNING"}
            or active["preflight"] != pending["preflight"]
            or active["provider_handle"]
            not in (None, handle_state)
        ):
            raise StateConflictError(
                "dispatch was superseded before handle binding"
            )
        active["status"] = "RUNNING"
        active["provider_handle"] = handle_state
    else:
        runtime = current["role_runtime"][role]
        if (
            runtime["active_attempt_token"] != attempt_token
            or runtime["operation_id"]
            != pending["operation_id"]
        ):
            raise StateConflictError(
                "dispatch was superseded before handle binding"
            )
    pending["launch"] = launch_state
    pending["provider_handle"] = handle_state


def lexical_relative_path(
    path: Path,
    trusted_root: Path,
) -> PurePosixPath:
    if not path.is_absolute() or not trusted_root.is_absolute():
        raise ContractError(
            "provider artifact paths must be absolute"
        )
    try:
        relative = path.relative_to(trusted_root)
    except ValueError as error:
        raise ContractError(
            "provider artifact escapes the trusted run root"
        ) from error
    pure = PurePosixPath(relative.as_posix())
    if (
        not pure.parts
        or pure.is_absolute()
        or ".." in pure.parts
        or "." in pure.parts
        or relative.as_posix() != pure.as_posix()
    ):
        raise ContractError(
            "provider artifact path is not canonical"
        )
    return pure


def read_regular_bytes(
    path: Path,
    trusted_root: Path,
    *,
    max_bytes: int,
) -> bytes:
    relative = lexical_relative_path(path, trusted_root)
    descriptor = open_absolute_directory_no_follow(
        trusted_root
    )
    try:
        for component in relative.parts[:-1]:
            try:
                child = os.open(
                    component,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise ContractError(
                    "unsafe provider artifact ancestor"
                ) from error
            os.close(descriptor)
            descriptor = child
        try:
            leaf = os.open(
                relative.parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
        except OSError as error:
            raise ContractError(
                "unsafe provider artifact leaf"
            ) from error
        try:
            metadata = os.fstat(leaf)
            if not stat.S_ISREG(metadata.st_mode):
                raise ContractError(
                    "provider artifact is not a regular file"
                )
            return read_bounded(leaf, max_bytes=max_bytes)
        finally:
            os.close(leaf)
    finally:
        os.close(descriptor)


def _read_optional_regular_bytes(
    path: Path,
    trusted_root: Path,
    *,
    max_bytes: int,
) -> bytes | None:
    try:
        return read_regular_bytes(
            path,
            trusted_root,
            max_bytes=max_bytes,
        )
    except ContractError as error:
        relative = lexical_relative_path(path, trusted_root)
        parent = open_absolute_directory_no_follow(
            trusted_root
        )
        try:
            for component in relative.parts[:-1]:
                try:
                    child = os.open(
                        component,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_NOFOLLOW,
                        dir_fd=parent,
                    )
                except FileNotFoundError:
                    return None
                except OSError:
                    raise error
                os.close(parent)
                parent = child
            try:
                os.stat(
                    relative.parts[-1],
                    dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return None
            raise error
        finally:
            os.close(parent)


class DispatchLedger:
    def __init__(
        self,
        store: StateStore,
        provider: ProviderAdapter,
        fault_hook: Callable[[str], None] = lambda event: None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.fault_hook = fault_hook

    def dispatch(
        self,
        invocation: RoleInvocation,
        start: Callable[[ProviderRequest], ProviderHandle],
    ) -> ProviderHandle:
        prompt_path = (
            f"{invocation.artifact_prefix}/prompt.md"
        )
        schema_path = (
            f"{invocation.artifact_prefix}/output.schema.json"
        )
        preflight_path = (
            f"{invocation.artifact_prefix}/preflight.json"
        )
        request_path = (
            f"{invocation.provider_prefix}/request.json"
        )
        request = provider_request_for(
            invocation,
            prompt_path,
            schema_path,
            request_path,
        )
        request_body = canonical_json_bytes(asdict(request))
        expected_revision = self.store.load()["revision"]
        state = self.store.transact(
            expected_revision,
            {
                prompt_path: invocation.prompt_body,
                schema_path: invocation.schema_body,
                preflight_path: invocation.preflight_body,
                request_path: request_body,
            },
            lambda current, refs: add_pending_dispatch(
                current,
                invocation,
                refs[prompt_path],
                refs[schema_path],
                refs[preflight_path],
                refs[request_path],
                request,
            ),
        )
        self.fault_hook(
            "after_dispatch_intent_before_provider_start"
        )
        handle = start(request)
        self.fault_hook(
            "after_provider_start_before_handle_binding"
        )
        launch_path = (
            f"{invocation.provider_prefix}/launch.json"
        )
        launch_body = read_regular_bytes(
            Path(request.launch_path),
            Path(invocation.run_root),
            max_bytes=4 * 1024 * 1024,
        )
        self.store.transact(
            state["revision"],
            {launch_path: launch_body},
            lambda current, refs: bind_matching_handle(
                current,
                invocation.attempt_token,
                handle,
                refs[launch_path],
            ),
        )
        return handle

    def bind_completion(
        self,
        invocation: RoleInvocation,
        handle: ProviderHandle,
    ) -> dict[str, object]:
        state = self.store.load()
        pending = state["pending_dispatches"].get(
            invocation.attempt_token
        )
        if (
            pending is None
            or pending["provider_handle"]
            != handle.as_state_dict()
        ):
            raise StateConflictError(
                "dispatch was superseded before completion"
            )
        run_root = Path(invocation.run_root)
        stdout = read_regular_bytes(
            Path(handle.stdout_path),
            run_root,
            max_bytes=16 * 1024 * 1024,
        )
        stderr = read_regular_bytes(
            Path(handle.stderr_path),
            run_root,
            max_bytes=16 * 1024 * 1024,
        )
        exit_body = read_regular_bytes(
            Path(handle.exit_path),
            run_root,
            max_bytes=4 * 1024 * 1024,
        )
        parsed = parse_exit_receipt(
            exit_body,
            handle,
            stderr_body=stderr,
        )
        if self.provider.completion(handle) != parsed:
            raise ContractError(
                "Provider completion disagrees with durable receipt"
            )
        result = _read_optional_regular_bytes(
            Path(handle.result_path),
            run_root,
            max_bytes=4 * 1024 * 1024,
        )
        if parsed.result_published and result is None:
            raise ContractError(
                "published Provider result is missing"
            )
        if not parsed.result_published and result is not None:
            raise ContractError(
                "failed Provider unexpectedly published a result"
            )
        artifacts = {
            pending["stdout_path"]: stdout,
            pending["stderr_path"]: stderr,
            pending["exit_path"]: exit_body,
        }
        if result is not None:
            artifacts[pending["result_path"]] = result

        def bind(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            current_pending = current[
                "pending_dispatches"
            ].get(invocation.attempt_token)
            if (
                current_pending is None
                or current_pending["provider_handle"]
                != handle.as_state_dict()
            ):
                raise StateConflictError(
                    "dispatch was superseded before completion"
                )
            for field, path in (
                ("stdout", current_pending["stdout_path"]),
                ("stderr", current_pending["stderr_path"]),
                ("exit", current_pending["exit_path"]),
            ):
                reference = refs[path].as_dict()
                if current_pending[field] not in (
                    None,
                    reference,
                ):
                    raise StateConflictError(
                        "completion receipt changed during replay"
                    )
                current_pending[field] = reference
            if result is not None:
                path = current_pending["result_path"]
                reference = refs[path].as_dict()
                if current_pending["result"] not in (
                    None,
                    reference,
                ):
                    raise StateConflictError(
                        "Provider result changed during replay"
                    )
                current_pending["result"] = reference

        return self.store.transact(
            state["revision"],
            artifacts,
            bind,
        )

    def retry_transient(
        self,
        invocation: RoleInvocation,
        handle: ProviderHandle,
        next_invocation: RoleInvocation,
        start: Callable[[ProviderRequest], ProviderHandle],
    ) -> ProviderHandle:
        self._validate_retry(invocation, next_invocation)
        current = self.store.load()
        old_pending = current["pending_dispatches"].get(
            invocation.attempt_token
        )
        if (
            old_pending is None
            or old_pending["provider_handle"]
            != handle.as_state_dict()
            or old_pending["exit"] is None
        ):
            raise StateConflictError(
                "Provider retry source is not current and terminal"
            )
        prompt_path = (
            f"{next_invocation.provider_prefix}/prompt.md"
        )
        schema_path = (
            f"{next_invocation.provider_prefix}/"
            "output.schema.json"
        )
        preflight_path = (
            f"{next_invocation.artifact_prefix}/preflight.json"
        )
        request_path = (
            f"{next_invocation.provider_prefix}/request.json"
        )
        summary_path = (
            f"{invocation.provider_prefix}/provider-attempt.json"
        )
        request = provider_request_for(
            next_invocation,
            prompt_path,
            schema_path,
            request_path,
        )
        summary_body = canonical_json_bytes(
            {
                "attempt_token": invocation.attempt_token,
                "provider_retry_no": invocation.provider_retry_no,
                "launch": old_pending["launch"],
                "stdout": old_pending["stdout"],
                "stderr": old_pending["stderr"],
                "exit": old_pending["exit"],
                "result": old_pending["result"],
            }
        )
        artifacts = {
            prompt_path: next_invocation.prompt_body,
            schema_path: next_invocation.schema_body,
            preflight_path: next_invocation.preflight_body,
            request_path: canonical_json_bytes(
                request.as_dict()
            ),
            summary_path: summary_body,
        }

        def rotate(
            state: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            old = state["pending_dispatches"].get(
                invocation.attempt_token
            )
            if (
                old is None
                or old["provider_handle"]
                != handle.as_state_dict()
            ):
                raise StateConflictError(
                    "Provider retry was superseded"
                )
            if invocation.role == "worker":
                task = state["tasks"][invocation.task_id]
                active = task["active_attempt"]
                if (
                    active is None
                    or active["attempt_token"]
                    != invocation.attempt_token
                    or active["provider_handle"]
                    != handle.as_state_dict()
                ):
                    raise StateConflictError(
                        "Worker retry owner was superseded"
                    )
                active["attempt_token"] = (
                    next_invocation.attempt_token
                )
                active["status"] = "RUNNING"
                active["provider_handle"] = None
            else:
                runtime = state["role_runtime"][
                    invocation.role
                ]
                if (
                    runtime["active_attempt_token"]
                    != invocation.attempt_token
                ):
                    raise StateConflictError(
                        "role retry owner was superseded"
                    )
                runtime["active_attempt_token"] = (
                    next_invocation.attempt_token
                )
            previous_attempts = list(
                old["provider_attempts"]
            )
            del state["pending_dispatches"][
                invocation.attempt_token
            ]
            add_pending_dispatch(
                state,
                next_invocation,
                refs[prompt_path],
                refs[schema_path],
                refs[preflight_path],
                refs[request_path],
                request,
            )
            state["pending_dispatches"][
                next_invocation.attempt_token
            ]["provider_attempts"] = (
                previous_attempts
                + [refs[summary_path].as_dict()]
            )

        rotated = self.store.transact(
            current["revision"],
            artifacts,
            rotate,
        )
        self.fault_hook(
            "after_dispatch_intent_before_provider_start"
        )
        new_handle = start(request)
        self.fault_hook(
            "after_provider_start_before_handle_binding"
        )
        launch_path = (
            f"{next_invocation.provider_prefix}/launch.json"
        )
        launch_body = read_regular_bytes(
            Path(request.launch_path),
            Path(next_invocation.run_root),
            max_bytes=4 * 1024 * 1024,
        )
        self.store.transact(
            rotated["revision"],
            {launch_path: launch_body},
            lambda state, refs: bind_matching_handle(
                state,
                next_invocation.attempt_token,
                new_handle,
                refs[launch_path],
            ),
        )
        return new_handle

    @staticmethod
    def _validate_retry(
        current: RoleInvocation,
        following: RoleInvocation,
    ) -> None:
        stable_fields = (
            "role",
            "task_id",
            "operation_id",
            "attempt_no",
            "attempt_created_at",
            "expected_base",
            "branch",
            "worktree",
            "target_root",
            "run_root",
            "artifact_prefix",
            "preflight_body",
        )
        if any(
            getattr(current, field)
            != getattr(following, field)
            for field in stable_fields
        ):
            raise ContractError(
                "Provider retry changed semantic Attempt identity"
            )
        if (
            following.provider_retry_no
            != current.provider_retry_no + 1
            or following.attempt_token
            == current.attempt_token
        ):
            raise ContractError(
                "Provider retry counter or token is invalid"
            )

    def accept_current_result(
        self,
        attempt_token: str,
        artifacts: Mapping[str, bytes],
        accept: Callable[
            [
                dict[str, object],
                dict[str, object],
                Mapping[str, ArtifactRef],
            ],
            None,
        ],
    ) -> bool:
        state = self.store.load()
        if attempt_token not in state["pending_dispatches"]:
            self.store.append_log(
                {
                    "event": "stale-result-rejected",
                    "attempt_token": attempt_token,
                }
            )
            return False

        def close(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            pending = current["pending_dispatches"].get(
                attempt_token
            )
            if pending is None:
                raise StateConflictError(
                    "result owner was superseded"
                )
            if accept(current, pending, refs) is not None:
                raise ContractError(
                    "result accept callback must return None"
                )
            del current["pending_dispatches"][attempt_token]

        self.store.transact(
            state["revision"],
            artifacts,
            close,
        )
        return True

    def abandon(
        self,
        attempt_token: str,
        artifacts: Mapping[str, bytes],
        close: Callable[
            [
                dict[str, object],
                dict[str, object],
                Mapping[str, ArtifactRef],
            ],
            None,
        ],
    ) -> dict[str, object]:
        state = self.store.load()
        if attempt_token not in state["pending_dispatches"]:
            raise StateConflictError(
                "abandon owner was superseded"
            )

        def abandon_current(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            pending = current["pending_dispatches"].get(
                attempt_token
            )
            if pending is None:
                raise StateConflictError(
                    "abandon owner was superseded"
                )
            if close(current, pending, refs) is not None:
                raise ContractError(
                    "abandon callback must return None"
                )
            del current["pending_dispatches"][attempt_token]

        return self.store.transact(
            state["revision"],
            artifacts,
            abandon_current,
        )
