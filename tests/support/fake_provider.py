from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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


@dataclass
class ProviderScript:
    role: str
    result_body: bytes | Callable[[ProviderRequest], bytes]
    on_start: Callable[[ProviderRequest], None] | None = None
    release: threading.Event | None = None
    failure: ProviderFailure | None = None


class ScenarioProvider(ScriptedProvider):
    def __init__(self, scripts: list[ProviderScript]) -> None:
        super().__init__()
        self.scripts = scripts
        self.started: list[str] = []
        self.requests: list[ProviderRequest] = []
        self.active: set[str] = set()
        self.maximum_active = 0
        self._scenario_lock = threading.Lock()
        self._background_threads: list[threading.Thread] = []

    def start(self, request: ProviderRequest) -> ProviderHandle:
        with self._scenario_lock:
            if not self.scripts:
                raise AssertionError(
                    f"no Provider script remains for role {request.role}"
                )
            script = self.scripts.pop(0)
        if script.role != request.role:
            raise AssertionError(
                f"expected role {script.role}, received {request.role}"
            )
        if script.on_start is not None:
            script.on_start(request)
        handle = super().start(request)
        with self._scenario_lock:
            self.started.append(request.attempt_token)
            self.requests.append(request)
            self.active.add(request.attempt_token)
            self.maximum_active = max(
                self.maximum_active,
                len(self.active),
            )
        if script.failure is not None:
            self.fail(request.attempt_token, script.failure)
            with self._scenario_lock:
                self.active.discard(request.attempt_token)
            return handle
        result_body = (
            script.result_body(request)
            if callable(script.result_body)
            else script.result_body
        )
        Path(request.result_path).write_bytes(result_body)
        if script.release is None or script.release.is_set():
            self.complete(request.attempt_token)
            with self._scenario_lock:
                self.active.discard(request.attempt_token)
        else:
            thread = threading.Thread(
                target=self._complete_when_released,
                args=(request.attempt_token, script.release),
                daemon=True,
            )
            with self._scenario_lock:
                self._background_threads.append(thread)
            thread.start()
        return handle

    def _complete_when_released(
        self,
        attempt_token: str,
        release: threading.Event,
    ) -> None:
        if not release.wait(timeout=10):
            self.record_background_failure(
                AssertionError(
                    f"release timed out for {attempt_token}"
                )
            )
            with self._scenario_lock:
                self.active.discard(attempt_token)
            return
        self.complete(attempt_token)
        with self._scenario_lock:
            self.active.discard(attempt_token)

    def join_background(self, timeout: float = 10) -> None:
        with self._scenario_lock:
            threads = tuple(self._background_threads)
        for thread in threads:
            thread.join(timeout)
            if thread.is_alive():
                self.record_background_failure(
                    AssertionError(
                        f"Provider completion thread leaked: {thread.name}"
                    )
                )
        self.assert_no_background_failures()


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
