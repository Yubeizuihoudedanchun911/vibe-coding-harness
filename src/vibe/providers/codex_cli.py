from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from typing import Any

from vibe.models import ContractError, ProviderStatus
from vibe.prompt_registry import parse_single_json_object
from vibe.providers.base import (
    ProviderCompletion,
    ProviderConfigurationError,
    ProviderExecutionIdentity,
    ProviderHandle,
    ProviderIdentityError,
    ProviderRequest,
    ProviderResult,
    StopResult,
    parse_exit_receipt,
)
from vibe.state_store import (
    canonical_json_bytes,
    open_absolute_directory_no_follow,
    open_absolute_regular_no_follow,
    publish_immutable_at,
    read_bounded,
    read_optional_regular_at,
    read_regular_at,
)


MAX_PROVIDER_FILE_BYTES = 64 * 1024 * 1024
ATTEMPT_TOKEN_RE = re.compile(r"ATTEMPT-[A-Za-z0-9-]+\Z")
LAUNCH_FIELDS = {
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
}
DISABLED_CAPABILITIES = (
    "apps",
    "browser",
    "computer_use",
    "hooks",
    "image_generation",
    "multi_agent",
    "plugins",
    "remote_plugins",
    "mcp",
    "network_proxy",
)
CHILD_ENVIRONMENT_NAMES = (
    "HOME",
    "CODEX_HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "TMPDIR",
)
AGENT_SHELL_ENVIRONMENT_NAMES = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TMPDIR",
)


@dataclass(frozen=True)
class ProviderExecutionPolicy:
    policy_id: str
    codex_args: tuple[str, ...]
    allowed_child_environment_names: tuple[str, ...]
    agent_shell_environment_names: tuple[str, ...]
    disabled_capabilities: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "codex_args": list(self.codex_args),
            "allowed_child_environment_names": list(
                self.allowed_child_environment_names
            ),
            "agent_shell_environment_names": list(
                self.agent_shell_environment_names
            ),
            "disabled_capabilities": list(
                self.disabled_capabilities
            ),
        }


def _execution_policy() -> ProviderExecutionPolicy:
    arguments = (
        "-c",
        "mcp_servers={}",
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-c",
        'shell_environment_policy.inherit="none"',
        "-c",
        (
            "shell_environment_policy.include_only="
            '["PATH","LANG","LC_ALL","TMPDIR"]'
        ),
    )
    disabled = tuple(
        item
        for feature in DISABLED_CAPABILITIES
        for item in ("--disable", feature)
    )
    return ProviderExecutionPolicy(
        policy_id="codex-cli-v1-hermetic",
        codex_args=arguments + disabled,
        allowed_child_environment_names=CHILD_ENVIRONMENT_NAMES,
        agent_shell_environment_names=(
            AGENT_SHELL_ENVIRONMENT_NAMES
        ),
        disabled_capabilities=DISABLED_CAPABILITIES,
    )


EXECUTION_POLICY = _execution_policy()
EXECUTION_POLICY_SHA256 = (
    "sha256:"
    + hashlib.sha256(
        canonical_json_bytes(EXECUTION_POLICY.as_dict())
    ).hexdigest()
)


class ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


PROC_PIDTBSDINFO = 3


def process_start_identity(pid: int) -> str:
    if type(pid) is not int or pid <= 0:
        raise ProviderConfigurationError(
            "process PID must be a positive integer"
        )
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(
                encoding="utf-8"
            )
        except OSError as error:
            raise ProviderConfigurationError(
                f"cannot read Linux process identity: {pid}"
            ) from error
        closing_parenthesis = raw.rfind(")")
        if closing_parenthesis < 0:
            raise ProviderConfigurationError(
                "invalid Linux process stat"
            )
        fields_after_comm = raw[closing_parenthesis + 1 :].split()
        if len(fields_after_comm) < 20:
            raise ProviderConfigurationError(
                "invalid Linux process stat"
            )
        return f"linux:{fields_after_comm[19]}"
    if sys.platform == "darwin":
        try:
            libproc = ctypes.CDLL(
                "/usr/lib/libproc.dylib",
                use_errno=True,
            )
        except OSError as error:
            raise ProviderConfigurationError(
                "cannot load macOS libproc"
            ) from error
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libproc.proc_pidinfo.restype = ctypes.c_int
        info = ProcBsdInfo()
        size = libproc.proc_pidinfo(
            pid,
            PROC_PIDTBSDINFO,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if size != ctypes.sizeof(info):
            raise ProviderConfigurationError(
                "cannot read macOS process start time"
            )
        return (
            f"darwin:{info.pbi_start_tvsec}:"
            f"{info.pbi_start_tvusec}"
        )
    raise ProviderConfigurationError(
        f"unsupported process identity platform: {sys.platform}"
    )


class ProcessInspection(str, Enum):
    MATCHING_LIVE = "MATCHING_LIVE"
    ABSENT = "ABSENT"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"


def inspect_process(
    pid: int,
    start_identity: str,
    process_group: int,
) -> ProcessInspection:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessInspection.ABSENT
    except PermissionError:
        pass
    try:
        actual_group = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return ProcessInspection.ABSENT
    try:
        actual_identity = process_start_identity(pid)
    except ProviderConfigurationError:
        return ProcessInspection.IDENTITY_MISMATCH
    if (
        actual_identity != start_identity
        or actual_group != process_group
    ):
        return ProcessInspection.IDENTITY_MISMATCH
    return ProcessInspection.MATCHING_LIVE


def parse_launch_receipt(
    raw: bytes,
    expected_request: ProviderRequest,
) -> ProviderHandle:
    value = parse_single_json_object(raw)
    if set(value) != LAUNCH_FIELDS:
        raise ContractError("launch receipt fields are invalid")
    expected = {
        "adapter": "codex-cli",
        "attempt_token": expected_request.attempt_token,
        "codex_version": expected_request.codex_version,
        "execution_policy_sha256": (
            expected_request.execution_policy_sha256
        ),
    }
    for field, expected_value in expected.items():
        if value.get(field) != expected_value:
            raise ProviderIdentityError(
                f"launch receipt {field} does not match the request"
            )
    for field in (
        "pid",
        "process_group",
        "child_pid",
        "child_process_group",
    ):
        if type(value[field]) is not int or value[field] <= 0:
            raise ContractError(
                f"launch receipt {field} must be positive"
            )
    for field in (
        "process_start_identity",
        "child_process_start_identity",
    ):
        if not isinstance(value[field], str) or not value[field]:
            raise ContractError(
                f"launch receipt {field} must be non-empty"
            )
    return ProviderHandle(
        adapter="codex-cli",
        attempt_token=expected_request.attempt_token,
        pid=value["pid"],
        process_start_identity=value[
            "process_start_identity"
        ],
        process_group=value["process_group"],
        child_pid=value["child_pid"],
        child_process_start_identity=value[
            "child_process_start_identity"
        ],
        child_process_group=value["child_process_group"],
        codex_version=expected_request.codex_version,
        execution_policy_sha256=(
            expected_request.execution_policy_sha256
        ),
        launch_path=expected_request.launch_path,
        stdout_path=expected_request.stdout_path,
        stderr_path=expected_request.stderr_path,
        exit_path=expected_request.exit_path,
        result_path=expected_request.result_path,
    )


class CodexCLIAdapter:
    def __init__(
        self,
        codex_bin: str | Path | None = None,
    ) -> None:
        selected = (
            Path(codex_bin)
            if codex_bin is not None
            else Path(shutil.which("codex") or "codex")
        )
        try:
            self.codex_bin = selected.resolve(strict=True)
        except OSError as error:
            raise ProviderConfigurationError(
                f"Codex executable not found: {selected}"
            ) from error
        if not self.codex_bin.is_file() or not os.access(
            self.codex_bin,
            os.X_OK,
        ):
            raise ProviderConfigurationError(
                f"Codex executable is not runnable: {self.codex_bin}"
            )
        self._execution_identity: (
            ProviderExecutionIdentity | None
        ) = None
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._process_lock = threading.Lock()

    def execution_identity(self) -> ProviderExecutionIdentity:
        if self._execution_identity is not None:
            return self._execution_identity
        try:
            version = subprocess.run(
                [str(self.codex_bin), "--version"],
                check=True,
                capture_output=True,
                timeout=5,
            ).stdout.decode("utf-8").strip()
            help_text = subprocess.run(
                [str(self.codex_bin), "exec", "--help"],
                check=True,
                capture_output=True,
                timeout=5,
            ).stdout.decode("utf-8")
        except (
            OSError,
            subprocess.SubprocessError,
            UnicodeError,
        ) as error:
            raise ProviderConfigurationError(
                "cannot probe Codex CLI"
            ) from error
        if not version:
            raise ProviderConfigurationError(
                "Codex CLI version output is empty"
            )
        required = (
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--sandbox",
            "--output-schema",
            "--output-last-message",
            "-c",
            "--disable",
        )
        missing = [flag for flag in required if flag not in help_text]
        if missing:
            raise ProviderConfigurationError(
                f"Codex CLI lacks required features: {missing}"
            )
        self._execution_identity = ProviderExecutionIdentity(
            codex_version=version,
            policy_sha256=EXECUTION_POLICY_SHA256,
        )
        return self._execution_identity

    def start(self, request: ProviderRequest) -> ProviderHandle:
        self._validate_request(request)
        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, True)
        process: subprocess.Popen[bytes] | None = None
        handle: ProviderHandle | None = None
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "vibe.providers.codex_cli",
                    "_wrapper",
                    request.request_path,
                    str(read_fd),
                    str(self.codex_bin),
                ],
                pass_fds=(read_fd,),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.close(read_fd)
            read_fd = -1
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                raw = _read_optional_path(
                    Path(request.launch_path),
                    max_bytes=1024 * 1024,
                )
                if raw is not None:
                    handle = parse_launch_receipt(raw, request)
                    break
                if process.poll() is not None:
                    raise ProviderConfigurationError(
                        "Codex wrapper exited before launch receipt"
                    )
                time.sleep(0.01)
            if handle is None:
                raise ProviderConfigurationError(
                    "timed out waiting for Codex launch receipt"
                )
            states = _inspect_handle(handle)
            _reject_identity_mismatch(states)
            if any(
                state is not ProcessInspection.MATCHING_LIVE
                for state in states
            ):
                raise ProviderConfigurationError(
                    "Codex launch identity is not live"
                )
            if os.write(write_fd, b"1") != 1:
                raise ProviderConfigurationError(
                    "cannot activate Codex child"
                )
            os.close(write_fd)
            write_fd = -1
            with self._process_lock:
                self._processes[handle.attempt_token] = process
            return handle
        except BaseException:
            if write_fd >= 0:
                os.close(write_fd)
                write_fd = -1
            if read_fd >= 0:
                os.close(read_fd)
            if handle is not None:
                try:
                    _terminate_handle_processes(handle, grace=0.2)
                finally:
                    if process is not None:
                        _terminate_local_process(
                            process,
                            grace=0.2,
                        )
            elif process is not None:
                _terminate_local_process(process, grace=0.2)
            raise

    def poll(self, handle: ProviderHandle) -> ProviderStatus:
        completion = self._optional_completion(handle)
        if completion is not None:
            self._reap_local(handle.attempt_token)
            return (
                ProviderStatus.SUCCEEDED
                if completion.exit_code == 0
                else ProviderStatus.FAILED
            )
        wrapper, child = _inspect_handle(handle)
        if (
            wrapper is ProcessInspection.IDENTITY_MISMATCH
            or child is ProcessInspection.IDENTITY_MISMATCH
        ):
            return ProviderStatus.FAILED
        if wrapper is ProcessInspection.MATCHING_LIVE:
            return ProviderStatus.RUNNING
        return ProviderStatus.FAILED

    def stop(
        self,
        handle: ProviderHandle,
        grace_period: float,
    ) -> StopResult:
        completion = self._optional_completion(handle)
        if completion is not None:
            self._reap_local(handle.attempt_token)
            return StopResult(
                handle.attempt_token,
                stopped=completion.stop_requested,
                forced=completion.stop_forced,
            )
        states = _inspect_handle(handle)
        _reject_identity_mismatch(states)
        wrapper, child = states
        if wrapper is ProcessInspection.MATCHING_LIVE:
            os.killpg(handle.process_group, signal.SIGTERM)
        deadline = time.monotonic() + max(0.0, grace_period)
        while time.monotonic() < deadline:
            completion = self._optional_completion(handle)
            if completion is not None:
                self._reap_local(handle.attempt_token)
                return StopResult(
                    handle.attempt_token,
                    stopped=completion.stop_requested,
                    forced=completion.stop_forced,
                )
            time.sleep(0.01)

        forced = False
        for identity in (
            (
                handle.child_pid,
                handle.child_process_start_identity,
                handle.child_process_group,
            ),
            (
                handle.pid,
                handle.process_start_identity,
                handle.process_group,
            ),
        ):
            if _terminate_identity(*identity, grace=0.2):
                forced = True
        self._reap_local(handle.attempt_token)
        states = _inspect_handle(handle)
        _reject_identity_mismatch(states)
        if any(
            state is not ProcessInspection.ABSENT
            for state in states
        ):
            raise ProviderConfigurationError(
                "Provider groups remain live after stop"
            )
        receipt = _exit_value(
            handle,
            exit_code=137,
            timed_out=False,
            result_published=False,
            stop_requested=True,
            stop_forced=True,
        )
        try:
            _publish_immutable_path(
                Path(handle.exit_path),
                canonical_json_bytes(receipt),
            )
        except ContractError:
            completion = self._optional_completion(handle)
            if completion is None:
                raise
            return StopResult(
                handle.attempt_token,
                stopped=completion.stop_requested,
                forced=completion.stop_forced,
            )
        return StopResult(
            handle.attempt_token,
            stopped=True,
            forced=True or forced,
        )

    def completion(
        self,
        handle: ProviderHandle,
    ) -> ProviderCompletion:
        completion = self._optional_completion(handle)
        if completion is None:
            raise ContractError(
                "Provider has no terminal exit receipt"
            )
        self._reap_local(handle.attempt_token)
        return completion

    def result(self, handle: ProviderHandle) -> ProviderResult:
        completion = self.completion(handle)
        if completion.exit_code != 0 or not completion.result_published:
            raise ContractError(
                "Provider result is unavailable after failure"
            )
        body = _read_path(
            Path(handle.result_path),
            max_bytes=MAX_PROVIDER_FILE_BYTES,
        )
        return ProviderResult(
            attempt_token=handle.attempt_token,
            body=body,
            exit_code=completion.exit_code,
        )

    def wait(
        self,
        handle: ProviderHandle,
        timeout_seconds: float,
    ) -> ProviderStatus:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status = self.poll(handle)
            if status is not ProviderStatus.RUNNING:
                return status
            time.sleep(0.02)
        raise TimeoutError(
            f"Provider wait timed out: {handle.attempt_token}"
        )

    def _optional_completion(
        self,
        handle: ProviderHandle,
    ) -> ProviderCompletion | None:
        raw = _read_optional_path(
            Path(handle.exit_path),
            max_bytes=1024 * 1024,
        )
        if raw is None:
            return None
        stderr = _read_optional_path(
            Path(handle.stderr_path),
            max_bytes=MAX_PROVIDER_FILE_BYTES,
        )
        return parse_exit_receipt(
            raw,
            handle,
            stderr_body=stderr or b"",
        )

    def _reap_local(self, attempt_token: str) -> None:
        with self._process_lock:
            process = self._processes.get(attempt_token)
            if process is None:
                return
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    return
            else:
                process.wait()
            del self._processes[attempt_token]

    def _validate_request(self, request: ProviderRequest) -> None:
        if (
            not isinstance(request.attempt_token, str)
            or ATTEMPT_TOKEN_RE.fullmatch(request.attempt_token)
            is None
        ):
            raise ContractError("invalid Provider attempt token")
        sandboxes = {
            "planner": "read-only",
            "evaluator": "read-only",
            "worker": "workspace-write",
        }
        if sandboxes.get(request.role) != request.sandbox:
            raise ContractError(
                "Provider role and sandbox are inconsistent"
            )
        if (
            type(request.timeout_seconds) is not int
            or request.timeout_seconds < 1
        ):
            raise ContractError(
                "Provider timeout must be a positive integer"
            )
        execution = self.execution_identity()
        if (
            request.codex_version != execution.codex_version
            or request.execution_policy_sha256
            != execution.policy_sha256
        ):
            raise ProviderConfigurationError(
                "Provider execution identity changed"
            )
        path_fields = (
            "request_path",
            "prompt_path",
            "schema_path",
            "cwd",
            "launch_path",
            "stdout_path",
            "stderr_path",
            "exit_path",
            "result_path",
        )
        paths = {
            name: _canonical_absolute_path(
                Path(getattr(request, name)),
                name,
            )
            for name in path_fields
        }
        output_names = (
            "launch_path",
            "stdout_path",
            "stderr_path",
            "exit_path",
            "result_path",
        )
        if len({str(paths[name]) for name in output_names}) != len(
            output_names
        ):
            raise ContractError("Provider output paths must be unique")
        cwd_fd = open_absolute_directory_no_follow(paths["cwd"])
        os.close(cwd_fd)
        for name in (
            "request_path",
            "prompt_path",
            "schema_path",
        ):
            descriptor = open_absolute_regular_no_follow(paths[name])
            os.close(descriptor)
        for name in output_names:
            parent = open_absolute_directory_no_follow(
                paths[name].parent
            )
            try:
                existing = read_optional_regular_at(
                    parent,
                    paths[name].name,
                    max_bytes=MAX_PROVIDER_FILE_BYTES,
                )
                if existing is not None:
                    raise ContractError(
                        f"Provider output already exists: {name}"
                    )
            finally:
                os.close(parent)
        actual = _read_path(
            paths["request_path"],
            max_bytes=4 * 1024 * 1024,
        )
        expected = canonical_json_bytes(request.as_dict())
        if actual != expected:
            raise ContractError(
                "Provider request bytes do not match the supplied request"
            )


def _canonical_absolute_path(path: Path, field: str) -> Path:
    if (
        not path.is_absolute()
        or str(path) != os.path.abspath(path)
        or path.name in {"", ".", ".."}
    ):
        raise ContractError(
            f"Provider {field} must be a canonical absolute path"
        )
    return path


def _read_path(path: Path, *, max_bytes: int) -> bytes:
    descriptor = open_absolute_regular_no_follow(path)
    try:
        return read_bounded(descriptor, max_bytes=max_bytes)
    finally:
        os.close(descriptor)


def _read_optional_path(
    path: Path,
    *,
    max_bytes: int,
) -> bytes | None:
    parent = open_absolute_directory_no_follow(path.parent)
    try:
        return read_optional_regular_at(
            parent,
            path.name,
            max_bytes=max_bytes,
        )
    finally:
        os.close(parent)


def _publish_immutable_path(path: Path, body: bytes) -> None:
    parent = open_absolute_directory_no_follow(path.parent)
    try:
        publish_immutable_at(parent, path.name, body)
    finally:
        os.close(parent)


def _exit_value(
    handle: ProviderHandle,
    *,
    exit_code: int,
    timed_out: bool,
    result_published: bool,
    stop_requested: bool,
    stop_forced: bool,
) -> dict[str, object]:
    return {
        **handle.as_state_dict(),
        "codex_version": handle.codex_version,
        "execution_policy_sha256": (
            handle.execution_policy_sha256
        ),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "result_published": result_published,
        "stop_requested": stop_requested,
        "stop_forced": stop_forced,
    }


def _inspect_handle(
    handle: ProviderHandle,
) -> tuple[ProcessInspection, ProcessInspection]:
    return (
        inspect_process(
            handle.pid,
            handle.process_start_identity,
            handle.process_group,
        ),
        inspect_process(
            handle.child_pid,
            handle.child_process_start_identity,
            handle.child_process_group,
        ),
    )


def _reject_identity_mismatch(
    states: tuple[ProcessInspection, ProcessInspection],
) -> None:
    if ProcessInspection.IDENTITY_MISMATCH in states:
        raise ProviderIdentityError(
            "Provider process identity mismatch"
        )


def _terminate_local_process(
    process: subprocess.Popen[bytes],
    *,
    grace: float,
) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def _terminate_identity(
    pid: int,
    start_identity: str,
    process_group: int,
    *,
    grace: float,
) -> bool:
    state = inspect_process(pid, start_identity, process_group)
    if state is ProcessInspection.IDENTITY_MISMATCH:
        raise ProviderIdentityError(
            "Provider process identity mismatch"
        )
    if state is ProcessInspection.ABSENT:
        return False
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        state = inspect_process(
            pid,
            start_identity,
            process_group,
        )
        if state is ProcessInspection.ABSENT:
            return False
        if state is ProcessInspection.IDENTITY_MISMATCH:
            return False
        time.sleep(0.01)
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        state = inspect_process(
            pid,
            start_identity,
            process_group,
        )
        if state is ProcessInspection.ABSENT:
            return True
        if state is ProcessInspection.IDENTITY_MISMATCH:
            return True
        time.sleep(0.01)
    return True


def _terminate_handle_processes(
    handle: ProviderHandle,
    *,
    grace: float,
) -> None:
    for identity in (
        (
            handle.child_pid,
            handle.child_process_start_identity,
            handle.child_process_group,
        ),
        (
            handle.pid,
            handle.process_start_identity,
            handle.process_group,
        ),
    ):
        _terminate_identity(*identity, grace=grace)


def _wait_for_process_identity(pid: int, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            return process_start_identity(pid)
        except ProviderConfigurationError as error:
            last_error = error
            time.sleep(0.005)
    raise ProviderConfigurationError(
        f"cannot bind process identity for PID {pid}"
    ) from last_error


def _request_from_path(path: Path) -> ProviderRequest:
    body = _read_path(path, max_bytes=4 * 1024 * 1024)
    value = parse_single_json_object(body)
    expected_fields = {
        field.name for field in fields(ProviderRequest)
    }
    if set(value) != expected_fields:
        raise ContractError("Provider request fields are invalid")
    try:
        request = ProviderRequest(**value)
    except TypeError as error:
        raise ContractError(
            f"Provider request types are invalid: {error}"
        ) from error
    if body != canonical_json_bytes(request.as_dict()):
        raise ContractError(
            "Provider request is not canonical JSON"
        )
    return request


def _ensure_regular_output(path: Path) -> None:
    parent = open_absolute_directory_no_follow(path.parent)
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
        except FileExistsError:
            descriptor = os.open(
                path.name,
                os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW,
                dir_fd=parent,
            )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ContractError(
                    f"Provider output is not regular: {path}"
                )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent)
    finally:
        os.close(parent)


def _open_output_exclusive(path: Path) -> int:
    parent = open_absolute_directory_no_follow(path.parent)
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ContractError(
                    f"symbolic link Provider output is forbidden: {path}"
                ) from error
            raise
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ContractError(
                f"Provider output is not regular: {path}"
            )
        return descriptor
    finally:
        os.close(parent)


def _fsync_regular_if_present(path: Path) -> None:
    parent = open_absolute_directory_no_follow(path.parent)
    try:
        body = read_optional_regular_at(
            parent,
            path.name,
            max_bytes=MAX_PROVIDER_FILE_BYTES,
        )
        if body is None:
            _ensure_regular_output(path)
            return
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent)
    finally:
        os.close(parent)


def _unlink_if_regular(path: Path) -> None:
    parent = open_absolute_directory_no_follow(path.parent)
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent,
            )
        except FileNotFoundError:
            return
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ContractError(
                    f"refusing to unlink non-regular output: {path}"
                )
        finally:
            os.close(descriptor)
        os.unlink(path.name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def _result_temporary_path(request: ProviderRequest) -> Path:
    result = Path(request.result_path)
    return result.with_name(
        f".{result.name}.{request.attempt_token}.tmp"
    )


def _result_is_regular(path: Path) -> bool:
    try:
        descriptor = open_absolute_regular_no_follow(path)
    except ContractError:
        return False
    else:
        os.close(descriptor)
        return True


def _atomic_publish_result(
    temporary: Path,
    result: Path,
) -> None:
    if temporary.parent != result.parent:
        raise ContractError(
            "Provider result temp must share the result directory"
        )
    parent = open_absolute_directory_no_follow(result.parent)
    try:
        descriptor = os.open(
            temporary.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ContractError(
                    "Provider temporary result is not regular"
                )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.stat(
                result.name,
                dir_fd=parent,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise ContractError(
                "Provider result path already exists"
            )
        os.replace(
            temporary.name,
            result.name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        os.fsync(parent)
    finally:
        os.close(parent)


def build_child_env(
    *,
    home: Path,
    codex_home: Path,
    tmpdir: Path,
) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "PATH": os.defpath,
        "LANG": "C",
        "LC_ALL": "C",
        "TMPDIR": str(tmpdir),
    }
    if set(environment) != set(CHILD_ENVIRONMENT_NAMES):
        raise ProviderConfigurationError(
            "Codex child environment surface changed"
        )
    return environment


def _prepare_child_environment() -> tuple[dict[str, str], Path]:
    invocation = Path(
        tempfile.mkdtemp(prefix="vibe-codex-invocation-")
    ).resolve()
    invocation.chmod(0o700)
    home = invocation / "home"
    codex_home = invocation / "codex-home"
    scratch = invocation / "tmp"
    for directory in (home, codex_home, scratch):
        directory.mkdir(mode=0o700)
    inherited_home = os.environ.get("HOME")
    if inherited_home:
        auth = Path(inherited_home) / ".codex" / "auth.json"
        if auth.is_file() and not auth.is_symlink():
            destination = codex_home / "auth.json"
            shutil.copyfile(auth, destination)
            destination.chmod(0o600)
    environment = build_child_env(
        home=home,
        codex_home=codex_home,
        tmpdir=scratch,
    )
    return environment, invocation


def _supervisor(
    request_path: Path,
    activation_fd: int,
    codex_bin: Path,
) -> int:
    request = _request_from_path(request_path)
    activation = os.read(activation_fd, 1)
    os.close(activation_fd)
    if activation != b"1":
        return 125
    stdout_path = Path(request.stdout_path)
    stderr_path = Path(request.stderr_path)
    prompt = _read_path(
        Path(request.prompt_path),
        max_bytes=MAX_PROVIDER_FILE_BYTES,
    )
    result_temporary = _result_temporary_path(request)
    _unlink_if_regular(result_temporary)
    stdout_fd = _open_output_exclusive(stdout_path)
    stderr_fd = _open_output_exclusive(stderr_path)
    invocation: Path | None = None
    try:
        environment, invocation = _prepare_child_environment()
        command = [
            str(codex_bin),
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            *EXECUTION_POLICY.codex_args,
            "--color",
            "never",
            "--json",
            "--sandbox",
            request.sandbox,
            "--cd",
            request.cwd,
            "--output-schema",
            request.schema_path,
            "--output-last-message",
            str(result_temporary),
            "-",
        ]
        child = subprocess.Popen(
            command,
            cwd=request.cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=stdout_fd,
            stderr=stderr_fd,
            start_new_session=False,
        )
        child.communicate(prompt)
        exit_code = child.returncode
        os.fsync(stdout_fd)
        os.fsync(stderr_fd)
        if exit_code == 0:
            _atomic_publish_result(
                result_temporary,
                Path(request.result_path),
            )
        else:
            _unlink_if_regular(result_temporary)
        return exit_code
    except BaseException as error:
        try:
            os.write(
                stderr_fd,
                (
                    f"provider supervisor error: {error}\n"
                ).encode("utf-8", "replace"),
            )
            os.fsync(stderr_fd)
        except OSError:
            pass
        return 70
    finally:
        os.close(stdout_fd)
        os.close(stderr_fd)
        if invocation is not None:
            shutil.rmtree(invocation, ignore_errors=True)


def _wrapper(
    request_path: Path,
    activation_fd: int,
    codex_bin: Path,
) -> int:
    request = _request_from_path(request_path)
    wrapper_pid = os.getpid()
    wrapper_start = process_start_identity(wrapper_pid)
    wrapper_group = os.getpgrp()
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "vibe.providers.codex_cli",
            "_supervisor",
            str(request_path),
            str(activation_fd),
            str(codex_bin),
        ],
        pass_fds=(activation_fd,),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(activation_fd)
    child_start = _wait_for_process_identity(child.pid, 1)
    handle = ProviderHandle(
        adapter="codex-cli",
        attempt_token=request.attempt_token,
        pid=wrapper_pid,
        process_start_identity=wrapper_start,
        process_group=wrapper_group,
        child_pid=child.pid,
        child_process_start_identity=child_start,
        child_process_group=child.pid,
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
    _publish_immutable_path(
        Path(request.launch_path),
        canonical_json_bytes(
            {
                **handle.as_state_dict(),
                "codex_version": handle.codex_version,
                "execution_policy_sha256": (
                    handle.execution_policy_sha256
                ),
            }
        ),
    )

    stop_requested = False

    def stop_handler(
        signum: int,
        frame: Any,
    ) -> None:
        del signum, frame
        nonlocal stop_requested
        stop_requested = True
        if (
            inspect_process(
                handle.child_pid,
                handle.child_process_start_identity,
                handle.child_process_group,
            )
            is ProcessInspection.MATCHING_LIVE
        ):
            os.killpg(
                handle.child_process_group,
                signal.SIGTERM,
            )

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    deadline = time.monotonic() + request.timeout_seconds
    timed_out = False
    while child.poll() is None:
        if stop_requested:
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _terminate_identity(
                    handle.child_pid,
                    handle.child_process_start_identity,
                    handle.child_process_group,
                    grace=0.2,
                )
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _terminate_identity(
                handle.child_pid,
                handle.child_process_start_identity,
                handle.child_process_group,
                grace=0.5,
            )
            break
        time.sleep(0.01)
    try:
        child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _terminate_identity(
            handle.child_pid,
            handle.child_process_start_identity,
            handle.child_process_group,
            grace=0.2,
        )
        child.wait(timeout=1)

    if timed_out:
        exit_code = 124
    elif stop_requested:
        exit_code = 143
    else:
        raw_code = child.returncode
        exit_code = (
            raw_code
            if raw_code is not None and raw_code >= 0
            else 128 + abs(raw_code or -1)
        )
    if timed_out or stop_requested or exit_code != 0:
        _unlink_if_regular(_result_temporary_path(request))
        if _result_is_regular(Path(request.result_path)):
            _unlink_if_regular(Path(request.result_path))
    result_published = (
        exit_code == 0
        and not timed_out
        and not stop_requested
        and _result_is_regular(Path(request.result_path))
    )
    if exit_code == 0 and not result_published:
        exit_code = 70
    _fsync_regular_if_present(Path(request.stdout_path))
    _fsync_regular_if_present(Path(request.stderr_path))
    receipt = _exit_value(
        handle,
        exit_code=exit_code,
        timed_out=timed_out,
        result_published=result_published,
        stop_requested=stop_requested,
        stop_forced=False,
    )
    try:
        _publish_immutable_path(
            Path(request.exit_path),
            canonical_json_bytes(receipt),
        )
    except ContractError:
        existing = _read_optional_path(
            Path(request.exit_path),
            max_bytes=1024 * 1024,
        )
        if existing is None:
            raise
        parse_exit_receipt(existing, handle)
    return exit_code


def _main(arguments: list[str]) -> int:
    if len(arguments) != 4:
        raise SystemExit(
            "usage: codex_cli.py (_wrapper|_supervisor) "
            "REQUEST ACTIVATION_FD CODEX_BIN"
        )
    mode, request, descriptor, codex_bin = arguments
    if mode == "_wrapper":
        return _wrapper(
            Path(request),
            int(descriptor),
            Path(codex_bin),
        )
    if mode == "_supervisor":
        return _supervisor(
            Path(request),
            int(descriptor),
            Path(codex_bin),
        )
    raise SystemExit(f"unknown provider mode: {mode}")


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
