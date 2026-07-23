from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

from vibe.models import ProviderStatus
from vibe.providers.base import (
    ProviderCompletion,
    ProviderExecutionIdentity,
    ProviderFailure,
    ProviderFailureKind,
    ProviderHandle,
    ProviderRequest,
    ProviderResult,
    StopResult,
    parse_exit_receipt,
)


@dataclass
class _FakeRun:
    request: ProviderRequest
    complete: bool = False
    stopped: bool = False
    failure: ProviderFailure | None = None


class ScriptedProvider:
    def __init__(self) -> None:
        self._runs: dict[str, _FakeRun] = {}
        self._lock = threading.Lock()
        self.failures: list[BaseException] = []

    def execution_identity(self) -> ProviderExecutionIdentity:
        return ProviderExecutionIdentity(
            codex_version="codex-cli-test",
            policy_sha256="sha256:" + "0" * 64,
        )

    def start(self, request: ProviderRequest) -> ProviderHandle:
        with self._lock:
            if request.attempt_token in self._runs:
                raise AssertionError(
                    "fake attempt token started twice"
                )
            run = _FakeRun(request=request)
            self._runs[request.attempt_token] = run
        launch = _identity_fields(request)
        Path(request.stdout_path).write_bytes(b"")
        Path(request.stderr_path).write_bytes(b"")
        _publish_json(Path(request.launch_path), launch)
        return ProviderHandle(
            adapter="fake",
            attempt_token=request.attempt_token,
            pid=1,
            process_start_identity="fake:1",
            process_group=1,
            child_pid=2,
            child_process_start_identity="fake:2",
            child_process_group=2,
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

    def complete(self, attempt_token: str) -> None:
        with self._lock:
            run = self._runs[attempt_token]
            if (
                run.stopped
                or run.failure is not None
                or run.complete
            ):
                return
            run.complete = True
            _publish_json(
                Path(run.request.exit_path),
                {
                    **_identity_fields(run.request),
                    "exit_code": 0,
                    "timed_out": False,
                    "result_published": True,
                    "stop_requested": False,
                    "stop_forced": False,
                },
            )

    def fail(
        self,
        attempt_token: str,
        failure: ProviderFailure,
    ) -> None:
        with self._lock:
            run = self._runs[attempt_token]
            if run.stopped or run.complete:
                return
            run.complete = True
            run.failure = failure
            exit_code = (
                124
                if failure.kind is ProviderFailureKind.TIMEOUT
                else 1
            )
            Path(run.request.stderr_path).write_text(
                failure.message + "\n",
                encoding="utf-8",
            )
            result_path = Path(run.request.result_path)
            if (
                result_path.exists()
                and not result_path.is_symlink()
            ):
                result_path.unlink()
            _publish_json(
                Path(run.request.exit_path),
                {
                    **_identity_fields(run.request),
                    "exit_code": exit_code,
                    "timed_out": (
                        failure.kind
                        is ProviderFailureKind.TIMEOUT
                    ),
                    "result_published": False,
                    "stop_requested": False,
                    "stop_forced": False,
                },
            )

    def assert_no_background_failures(self) -> None:
        with self._lock:
            failures = tuple(self.failures)
        if failures:
            raise AssertionError(failures)

    def record_background_failure(
        self,
        error: BaseException,
    ) -> None:
        with self._lock:
            self.failures.append(error)

    def poll(self, handle: ProviderHandle) -> ProviderStatus:
        with self._lock:
            run = self._runs[handle.attempt_token]
            if run.stopped or run.failure is not None:
                return ProviderStatus.FAILED
            return (
                ProviderStatus.SUCCEEDED
                if run.complete
                else ProviderStatus.RUNNING
            )

    def stop(
        self,
        handle: ProviderHandle,
        grace_period: float,
    ) -> StopResult:
        del grace_period
        with self._lock:
            run = self._runs[handle.attempt_token]
            if run.stopped:
                return StopResult(
                    handle.attempt_token,
                    stopped=True,
                    forced=False,
                )
            if run.complete:
                return StopResult(
                    handle.attempt_token,
                    stopped=False,
                    forced=False,
                )
            run.stopped = True
            _publish_json(
                Path(run.request.exit_path),
                {
                    **_identity_fields(run.request),
                    "exit_code": 143,
                    "timed_out": False,
                    "result_published": False,
                    "stop_requested": True,
                    "stop_forced": False,
                },
            )
            return StopResult(
                handle.attempt_token,
                stopped=True,
                forced=False,
            )

    def completion(
        self,
        handle: ProviderHandle,
    ) -> ProviderCompletion:
        with self._lock:
            stderr = (
                Path(handle.stderr_path).read_bytes()
                if Path(handle.stderr_path).is_file()
                else b""
            )
            return parse_exit_receipt(
                Path(handle.exit_path).read_bytes(),
                handle,
                stderr_body=stderr,
            )

    def result(self, handle: ProviderHandle) -> ProviderResult:
        with self._lock:
            run = self._runs[handle.attempt_token]
            if (
                not run.complete
                or run.failure is not None
                or run.stopped
            ):
                raise AssertionError(
                    "fake result requested before success"
                )
            return ProviderResult(
                attempt_token=handle.attempt_token,
                body=Path(handle.result_path).read_bytes(),
                exit_code=0,
            )


def _publish_json(
    path: Path,
    value: dict[str, object],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _identity_fields(
    request: ProviderRequest,
) -> dict[str, object]:
    return {
        "adapter": "fake",
        "attempt_token": request.attempt_token,
        "pid": 1,
        "process_start_identity": "fake:1",
        "process_group": 1,
        "child_pid": 2,
        "child_process_start_identity": "fake:2",
        "child_process_group": 2,
        "codex_version": request.codex_version,
        "execution_policy_sha256": (
            request.execution_policy_sha256
        ),
    }
