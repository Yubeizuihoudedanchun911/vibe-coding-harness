from __future__ import annotations

import copy
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

from vibe.config import frozen_config_bytes
from vibe.integrator import IntegrationRejected, Integrator
from vibe.models import (
    AcceptanceCriterion,
    ArtifactRef,
    ContractError,
    EvaluationResult,
    EvaluationVerdict,
    FrozenRunConfig,
    PlanDocument,
    PromptRef,
    ProviderStatus,
    RunStatus,
    StateConflictError,
    StopReceipt,
    StopRequest,
    TaskContract,
    goal_gate_satisfied,
)
from vibe.providers.base import (
    ProviderAdapter,
    ProviderHandle,
    ProviderRequest,
    handle_from_pending,
    request_from_pending,
)
from vibe.providers.codex_cli import process_start_identity
from vibe.runners import (
    DispatchLedger,
    RoleInvocation,
    bind_matching_handle,
    role_attempt_prefix,
)
from vibe.runners.evaluator import EvaluatorRunner
from vibe.runners.planner import PlannerRunner
from vibe.runners.worker import WorkerRunner
from vibe.scheduler import (
    Scheduler,
    bind_attempt_preflight,
    effective_global_verification,
    new_task_state,
    start_attempt,
)
from vibe.state_store import (
    MAX_ARTIFACT_BYTES,
    StateStore,
    artifact_ref,
    canonical_json_bytes,
    load_json_object,
    open_absolute_directory_no_follow,
    open_absolute_regular_no_follow,
    parse_json_object_bytes,
    read_bounded,
    read_optional_regular_at,
    replace_mutable_at,
)
from vibe.verification import (
    VerificationEnvironmentError,
    VerificationGate,
)
from vibe.worktrees import (
    AttemptPreflight,
    PreparedSourceCommit,
    ProtectedGitSnapshot,
    SourceAudit,
    SourceCommitMetadata,
    TaskWorktree,
    WorktreeManager,
)


STATIC_RUN_STATUSES = {
    RunStatus.PAUSED,
    RunStatus.STOPPED,
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.IMPORTED_READ_ONLY,
}
RUN_DIR_RE = re.compile(r"RUN-\d{8}-\d{3}\Z")


@dataclass(frozen=True)
class ControllerDependencies:
    store_factory: Callable[[Path, str], StateStore]
    worktrees: WorktreeManager
    scheduler: Scheduler
    planner: PlannerRunner | None
    worker: WorkerRunner | None
    evaluator: EvaluatorRunner | None
    verification: VerificationGate | None
    integrator: Integrator | object | None
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    fault_hook: Callable[[str], None]
    wake_signal: (
        Callable[[dict[str, object]], None] | None
    ) = None


@dataclass(frozen=True)
class TickResult:
    state: dict[str, object]
    progressed: bool
    completed_provider_tokens: tuple[str, ...] = ()


class Controller:
    def __init__(
        self,
        target: Path,
        config: FrozenRunConfig,
        dependencies: ControllerDependencies,
    ) -> None:
        self.target = target.resolve()
        self.config = config
        self.dependencies = dependencies
        self._store: StateStore | None = None
        self._ledger: DispatchLedger | None = None
        self._invocations: dict[str, RoleInvocation] = {}
        self._evaluation_meta: dict[str, tuple[int, int]] = {}

    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def create_run(
        self,
        goal: str,
        config: FrozenRunConfig,
    ) -> str:
        normalized_goal = self._validate_goal(goal)
        config_body = frozen_config_bytes(config)
        baseline = self.dependencies.worktrees.assert_clean_baseline()
        fingerprint = self._creation_fingerprint(
            baseline.identity,
            baseline.base_sha,
            normalized_goal,
            config_body,
        )
        with self._allocation_lock():
            matches = self._matching_creation_runs(fingerprint)
            if len(matches) > 1:
                raise ContractError(
                    "multiple runs have the same incomplete creation fingerprint"
                )
            if matches:
                run_id = matches[0]
                store = self.dependencies.store_factory(
                    self.target,
                    run_id,
                )
                try:
                    state = store.load()
                except (ContractError, FileNotFoundError):
                    state = None
                if state is not None and not self._is_pristine_created(state):
                    run_id = self._allocate_run_id()
            else:
                run_id = self._allocate_run_id()
            return self._finish_creation(
                run_id,
                normalized_goal,
                config,
                config_body,
                baseline,
                fingerprint,
            )

    def execute(
        self,
        run_id: str,
        replan: bool = False,
    ) -> dict[str, object]:
        del replan
        self._require_runtime_dependencies()
        store = self.dependencies.store_factory(self.target, run_id)
        self._set_run_context(store)
        with store.lock():
            state = self._register_controller(store.load())
            return self._drive_locked(state)

    def request_stop(self, run_id: str) -> StopRequest:
        store = self.dependencies.store_factory(
            self.target,
            run_id,
        )
        state = store.load()
        if state["run_id"] != run_id:
            raise ContractError("stop run ID does not match state")
        controller = state.get("controller")
        if not isinstance(controller, dict):
            raise StateConflictError(
                "run has no registered foreground Controller"
            )
        control_fd = self._control_directory(
            store,
            create=True,
        )
        try:
            existing = read_optional_regular_at(
                control_fd,
                "stop.request",
                max_bytes=64 * 1024,
            )
            if existing is not None:
                request = self._parse_stop_request(existing)
                if request.run_id != run_id:
                    raise ContractError(
                        "existing stop request targets another run"
                    )
                return request
            request = StopRequest(
                run_id=run_id,
                observed_revision=state["revision"],
                controller_token=controller[
                    "controller_token"
                ],
                requested_at=self._now(),
                nonce=f"STOP-{uuid.uuid4()}",
            )
            replace_mutable_at(
                control_fd,
                "stop.request",
                canonical_json_bytes(request.as_dict()),
            )
        finally:
            os.close(control_fd)
        if self.dependencies.wake_signal is not None:
            self.dependencies.wake_signal(controller)
        return request

    def recover(
        self,
        run_id: str,
        replan: bool = False,
    ) -> dict[str, object]:
        del replan
        self._require_runtime_dependencies()
        store = self.dependencies.store_factory(
            self.target,
            run_id,
        )
        self._set_run_context(store)
        with store.lock():
            return self._recover_locked(store)

    def resume(
        self,
        run_id: str,
        replan: bool = False,
    ) -> dict[str, object]:
        del replan
        self._require_runtime_dependencies()
        store = self.dependencies.store_factory(
            self.target,
            run_id,
        )
        self._set_run_context(store)
        with store.lock():
            state = self._recover_locked(store)
            status = RunStatus(state["status"])
            if status in {
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
                RunStatus.IMPORTED_READ_ONLY,
            }:
                return state
            if status not in {
                RunStatus.PAUSED,
                RunStatus.STOPPED,
            }:
                raise StateConflictError(
                    "resume requires PAUSED or STOPPED state"
                )
            state = self._restore_and_register_controller(
                state
            )
            self._recover_dispatches(start_unlaunched=True)
            return self._drive_locked(store.load())

    def recover_and_stop(
        self,
        run_id: str,
        request: StopRequest,
    ) -> dict[str, object]:
        self._require_runtime_dependencies()
        if request.run_id != run_id:
            raise ContractError("stop request run ID mismatch")
        store = self.dependencies.store_factory(
            self.target,
            run_id,
        )
        self._set_run_context(store)
        with store.lock():
            state = store.load()
            self._recover_dispatches(start_unlaunched=False)
            result = self._consume_stop_request(
                store.load(),
                expected=request,
            )
            if result is None:
                raise ContractError(
                    "durable stop request is unavailable"
                )
            return result.state

    def _set_run_context(self, store: StateStore) -> None:
        self._store = store
        assert self.dependencies.planner is not None
        self._ledger = DispatchLedger(
            store,
            self.dependencies.planner.provider,
            fault_hook=self.dependencies.fault_hook,
        )

    def _recover_locked(
        self,
        store: StateStore,
    ) -> dict[str, object]:
        state = store.load()
        self._recover_dispatches(start_unlaunched=False)
        state = store.load()
        stop = self._consume_stop_request(state)
        if stop is not None:
            state = stop.state
            if state["status"] == "STOPPED":
                return state
        if state["pending_evaluation"] is not None:
            state = self._accept_pending_evaluation(
                state
            ).state
        if state["pending_source_commit"] is not None:
            state = self._reconcile_source_commit(state).state
        if state["pending_integration"] is not None:
            self._integrator().recover()
            state = store.load()
        status = RunStatus(state["status"])
        if status not in STATIC_RUN_STATUSES:
            previous = status.value

            def pause_after_exit(
                current: dict[str, object],
                refs: Mapping[str, ArtifactRef],
            ) -> None:
                del refs
                if current["status"] != previous:
                    raise StateConflictError(
                        "run status changed during recovery"
                    )
                current["resume_status"] = previous
                current["status"] = "PAUSED"
                current["controller"] = None
                current["last_error"] = {
                    "code": "CONTROLLER_EXIT_RECOVERED",
                    "message": (
                        "the prior foreground Controller exited"
                    ),
                    "retryable": True,
                }
                current["updated_at"] = self._now()

            state = store.transact(
                state["revision"],
                {},
                pause_after_exit,
            )
        return state

    def _restore_and_register_controller(
        self,
        state: dict[str, object],
    ) -> dict[str, object]:
        resume_status = state.get("resume_status")
        if resume_status not in {
            "CREATED",
            "PLANNING",
            "EXECUTING",
            "GLOBAL_VERIFYING",
            "EVALUATING",
            "REPAIRING",
        }:
            raise StateConflictError(
                "paused run has no resumable lifecycle status"
            )
        pid = os.getpid()
        identity = process_start_identity(pid)
        group = os.getpgrp()
        token = f"CONTROLLER-{uuid.uuid4()}"
        store = self._active_store()

        def restore(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            del refs
            if (
                current["status"] not in {"PAUSED", "STOPPED"}
                or current["resume_status"] != resume_status
            ):
                raise StateConflictError(
                    "run changed before resume registration"
                )
            current["status"] = resume_status
            current["resume_status"] = None
            current["controller"] = {
                "pid": pid,
                "process_start_identity": identity,
                "process_group": group,
                "controller_token": token,
            }
            current["last_error"] = None
            current["updated_at"] = self._now()

        return store.transact(
            state["revision"],
            {},
            restore,
        )

    def _recover_dispatches(
        self,
        *,
        start_unlaunched: bool,
    ) -> dict[str, object]:
        store = self._active_store()
        state = store.load()
        for token in tuple(state["pending_dispatches"]):
            state = store.load()
            pending = state["pending_dispatches"].get(token)
            if pending is None:
                continue
            invocation = self._invocation_from_pending(
                state,
                pending,
            )
            self._invocations[token] = invocation
            if pending["provider_handle"] is not None:
                handle_from_pending(
                    pending,
                    store.root,
                    self.target,
                )
                continue
            request = request_from_pending(
                pending,
                store.root,
                self.target,
            )
            launch_body = self._optional_provider_body(
                pending["launch_path"]
            )
            if launch_body is not None:
                handle = self._handle_from_launch(
                    request,
                    launch_body,
                )
                self._bind_recovered_handle(
                    state,
                    pending,
                    handle,
                    launch_body,
                )
                continue
            if not start_unlaunched:
                continue
            handle = self._provider().start(request)
            launch_body = self._required_provider_body(
                pending["launch_path"]
            )
            self._bind_recovered_handle(
                state,
                pending,
                handle,
                launch_body,
            )
        return store.load()

    def _bind_recovered_handle(
        self,
        state: dict[str, object],
        pending: dict[str, object],
        handle: ProviderHandle,
        launch_body: bytes,
    ) -> None:
        launch_path = pending["launch_path"]
        token = pending["attempt_token"]

        def bind(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            bind_matching_handle(
                current,
                token,
                handle,
                refs[launch_path],
            )

        self._active_store().transact(
            state["revision"],
            {launch_path: launch_body},
            bind,
        )

    def _invocation_from_pending(
        self,
        state: dict[str, object],
        pending: dict[str, object],
    ) -> RoleInvocation:
        request = request_from_pending(
            pending,
            self._active_store().root,
            self.target,
        )
        role = pending["role"]
        if role == "worker":
            task_id = pending["task_id"]
            contract = self._task_contracts(state)[task_id]
            authorized = contract.acceptance_checks
            required = tuple(
                command_id
                for command_id
                in self.config.required_command_ids
                if command_id in authorized
            )
        else:
            authorized = tuple(
                command.id
                for command in self.config.command_catalog
            )
            required = self.config.required_command_ids
        provider_prefix = pending["provider_prefix"]
        marker = "/providers/"
        if marker not in provider_prefix:
            raise ContractError(
                "pending Provider prefix is invalid"
            )
        artifact_prefix = provider_prefix.rsplit(
            marker,
            1,
        )[0]

        def body(field: str) -> bytes:
            reference = pending[field]
            return self._read_ref(
                ArtifactRef(
                    reference["path"],
                    reference["sha256"],
                )
            )

        return RoleInvocation(
            role=role,
            task_id=pending["task_id"],
            operation_id=pending["operation_id"],
            attempt_no=pending["attempt_no"],
            attempt_created_at=pending[
                "attempt_created_at"
            ],
            attempt_token=pending["attempt_token"],
            provider_retry_no=pending["provider_retry_no"],
            expected_base=pending["expected_base"],
            branch=pending["branch"],
            worktree=pending["worktree"],
            target_root=str(self.target),
            run_root=str(self._active_store().root),
            prompt_body=body("prompt"),
            prompt_versions=tuple(
                PromptRef(
                    item["id"],
                    item["version"],
                    item["sha256"],
                )
                for item in pending["prompt_versions"]
            ),
            schema_body=body("schema"),
            preflight_body=body("preflight"),
            authorized_command_ids=tuple(authorized),
            required_command_ids=tuple(required),
            config_sha256=self._sha256(
                frozen_config_bytes(self.config)
            ),
            codex_version=request.codex_version,
            execution_policy_sha256=(
                request.execution_policy_sha256
            ),
            sandbox=request.sandbox,
            artifact_prefix=artifact_prefix,
            timeout_seconds=request.timeout_seconds,
        )

    def _handle_from_launch(
        self,
        request: ProviderRequest,
        body: bytes,
    ) -> ProviderHandle:
        raw = parse_json_object_bytes(body)
        identity_fields = {
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
        if set(raw) != identity_fields:
            raise ContractError(
                "Provider launch receipt fields are invalid"
            )
        if (
            raw["attempt_token"] != request.attempt_token
            or raw["codex_version"] != request.codex_version
            or raw["execution_policy_sha256"]
            != request.execution_policy_sha256
        ):
            raise ContractError(
                "Provider launch receipt identity changed"
            )
        return ProviderHandle(
            adapter=raw["adapter"],
            attempt_token=raw["attempt_token"],
            pid=raw["pid"],
            process_start_identity=raw[
                "process_start_identity"
            ],
            process_group=raw["process_group"],
            child_pid=raw["child_pid"],
            child_process_start_identity=raw[
                "child_process_start_identity"
            ],
            child_process_group=raw[
                "child_process_group"
            ],
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

    def _optional_provider_body(
        self,
        relative: str,
    ) -> bytes | None:
        path = self._active_store().root.joinpath(
            *PurePosixPath(relative).parts
        )
        parent = open_absolute_directory_no_follow(path.parent)
        try:
            return read_optional_regular_at(
                parent,
                path.name,
                max_bytes=MAX_ARTIFACT_BYTES,
            )
        finally:
            os.close(parent)

    def _required_provider_body(self, relative: str) -> bytes:
        value = self._optional_provider_body(relative)
        if value is None:
            raise ContractError(
                "Provider launch receipt is missing"
            )
        return value

    def _consume_stop_request(
        self,
        state: dict[str, object],
        *,
        expected: StopRequest | None = None,
    ) -> TickResult | None:
        request = self._read_stop_request()
        if request is None:
            return None
        if expected is not None and request != expected:
            raise StateConflictError(
                "durable stop request changed"
            )
        if request.run_id != state["run_id"]:
            raise ContractError(
                "stop request targets another run"
            )
        if request.observed_revision > state["revision"]:
            raise ContractError(
                "stop request observes a future revision"
            )
        for item in state["stop_receipts"]:
            if item["nonce"] != request.nonce:
                continue
            self._read_ref(
                ArtifactRef(
                    item["receipt"]["path"],
                    item["receipt"]["sha256"],
                )
            )
            self._unlink_stop_request()
            return TickResult(state, True)
        controller = state.get("controller")
        if (
            not isinstance(controller, dict)
            or controller.get("controller_token")
            != request.controller_token
        ):
            updated = self._persist_stop_outcome(
                state,
                request,
                outcome="IGNORED_STALE_CONTROLLER",
                stopped=(),
                forced=(),
            )
            self._unlink_stop_request()
            return TickResult(updated, True)
        self.dependencies.fault_hook(
            "after_stop_request_before_worker_exit"
        )
        stopped: list[str] = []
        forced: list[str] = []
        for pending in state["pending_dispatches"].values():
            if pending["provider_handle"] is None:
                continue
            handle = handle_from_pending(
                pending,
                self._active_store().root,
                self.target,
            )
            result = self._provider().stop(handle, 1.0)
            if result.stopped:
                stopped.append(result.attempt_token)
            if result.forced:
                forced.append(result.attempt_token)
        state = self._active_store().load()
        updated = self._persist_stop_outcome(
            state,
            request,
            outcome="STOPPED",
            stopped=tuple(stopped),
            forced=tuple(forced),
        )
        self._unlink_stop_request()
        return TickResult(updated, True)

    def _persist_stop_outcome(
        self,
        state: dict[str, object],
        request: StopRequest,
        *,
        outcome: str,
        stopped: tuple[str, ...],
        forced: tuple[str, ...],
    ) -> dict[str, object]:
        completed_at = self._now()
        receipt = StopReceipt(
            request=request,
            outcome=outcome,
            stopped_tokens=stopped,
            forced_tokens=forced,
            completed_at=completed_at,
        )
        receipt_path = (
            f"control/receipts/{request.nonce}.json"
        )
        artifacts: dict[str, bytes] = {
            receipt_path: canonical_json_bytes(
                receipt.as_dict()
            )
        }
        attempt_paths: dict[str, str] = {}
        if outcome == "STOPPED":
            for token, pending in state[
                "pending_dispatches"
            ].items():
                if pending["role"] == "worker":
                    task_id = pending["task_id"]
                    task = state["tasks"][task_id]
                    path = (
                        f"tasks/{task_id}/attempts/"
                        f"{task['attempt_no']:03d}/attempt.json"
                    )
                    body = self._worker_attempt_body(
                        state,
                        task_id,
                        status="CANCELLED",
                        source_audit=None,
                        verification=None,
                        last_error={
                            "code": "STOP_REQUESTED",
                            "message": (
                                "attempt cancelled by operator"
                            ),
                            "retryable": True,
                        },
                        pending=pending,
                    )
                else:
                    path = (
                        f"{role_attempt_prefix(pending['role'], pending['operation_id'], pending['attempt_no'])}/"
                        "attempt.json"
                    )
                    body = self._attempt_manifest_body(
                        pending,
                        status="CANCELLED",
                        completed_at=completed_at,
                        last_error={
                            "code": "STOP_REQUESTED",
                            "message": (
                                "attempt cancelled by operator"
                            ),
                            "retryable": True,
                        },
                    )
                artifacts[path] = body
                attempt_paths[token] = path
        store = self._active_store()

        def persist(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            if any(
                item["nonce"] == request.nonce
                for item in current["stop_receipts"]
            ):
                return
            if outcome == "STOPPED":
                previous_status = current["status"]
                for token, path in attempt_paths.items():
                    pending = current[
                        "pending_dispatches"
                    ].get(token)
                    if pending is None:
                        raise StateConflictError(
                            "stop target dispatch changed"
                        )
                    if pending["role"] == "worker":
                        task = current["tasks"][
                            pending["task_id"]
                        ]
                        task["attempts"].append(
                            refs[path].as_dict()
                        )
                        task["active_attempt"] = None
                        task["status"] = "READY"
                        task["result"] = None
                        task["source_commits"] = []
                        task["verification"] = None
                        task["last_error"] = {
                            "code": "STOP_REQUESTED",
                            "message": (
                                "attempt cancelled by operator"
                            ),
                            "retryable": True,
                        }
                    else:
                        role = pending["role"]
                        current["role_attempts"][
                            role
                        ].append(refs[path].as_dict())
                        runtime = current["role_runtime"][role]
                        runtime["active_attempt_token"] = None
                        runtime["last_error"] = {
                            "code": "STOP_REQUESTED",
                            "message": (
                                "attempt cancelled by operator"
                            ),
                            "retryable": True,
                        }
                    del current["pending_dispatches"][token]
                current["resume_status"] = previous_status
                current["status"] = "STOPPED"
                current["controller"] = None
                current["last_error"] = {
                    "code": "STOP_REQUESTED",
                    "message": "run stopped by operator",
                    "retryable": True,
                }
            current["stop_receipts"].append(
                {
                    "nonce": request.nonce,
                    "receipt": refs[
                        receipt_path
                    ].as_dict(),
                }
            )
            current["updated_at"] = completed_at

        return store.transact(
            state["revision"],
            artifacts,
            persist,
        )

    def _read_stop_request(self) -> StopRequest | None:
        control_fd = self._control_directory(
            self._active_store(),
            create=False,
        )
        if control_fd is None:
            return None
        try:
            body = read_optional_regular_at(
                control_fd,
                "stop.request",
                max_bytes=64 * 1024,
            )
        finally:
            os.close(control_fd)
        return (
            None
            if body is None
            else self._parse_stop_request(body)
        )

    @staticmethod
    def _parse_stop_request(body: bytes) -> StopRequest:
        raw = parse_json_object_bytes(body)
        if set(raw) != {
            "run_id",
            "observed_revision",
            "controller_token",
            "requested_at",
            "nonce",
        }:
            raise ContractError(
                "stop request fields are invalid"
            )
        if (
            not isinstance(raw["run_id"], str)
            or type(raw["observed_revision"]) is not int
            or raw["observed_revision"] < 0
            or not isinstance(raw["controller_token"], str)
            or not isinstance(raw["requested_at"], str)
            or not isinstance(raw["nonce"], str)
            or not raw["nonce"].startswith("STOP-")
        ):
            raise ContractError("stop request values are invalid")
        try:
            datetime.fromisoformat(raw["requested_at"])
        except ValueError as error:
            raise ContractError(
                "stop request timestamp is invalid"
            ) from error
        return StopRequest(
            run_id=raw["run_id"],
            observed_revision=raw["observed_revision"],
            controller_token=raw["controller_token"],
            requested_at=raw["requested_at"],
            nonce=raw["nonce"],
        )

    @staticmethod
    def _control_directory(
        store: StateStore,
        *,
        create: bool,
    ) -> int | None:
        root_fd = open_absolute_directory_no_follow(
            store.root
        )
        try:
            try:
                return os.open(
                    "control",
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
            except FileNotFoundError:
                if not create:
                    return None
                os.mkdir("control", 0o700, dir_fd=root_fd)
                os.fsync(root_fd)
                return os.open(
                    "control",
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
        finally:
            os.close(root_fd)

    def _unlink_stop_request(self) -> None:
        control_fd = self._control_directory(
            self._active_store(),
            create=False,
        )
        if control_fd is None:
            return
        try:
            try:
                os.unlink(
                    "stop.request",
                    dir_fd=control_fd,
                )
            except FileNotFoundError:
                pass
            os.fsync(control_fd)
        finally:
            os.close(control_fd)

    def _drive_locked(
        self,
        state: dict[str, object],
    ) -> dict[str, object]:
        while RunStatus(state["status"]) not in STATIC_RUN_STATUSES:
            result = self.tick(state)
            state = result.state
            if not result.progressed:
                self.dependencies.sleep(0.1)
        return state

    def tick(self, state: dict[str, object]) -> TickResult:
        stop = self._consume_stop_request(state)
        if stop is not None:
            return stop
        handlers = {
            RunStatus.CREATED: self._start_initial_planner,
            RunStatus.PLANNING: self._poll_planner,
            RunStatus.EXECUTING: self._execute_tasks,
            RunStatus.GLOBAL_VERIFYING: self._run_global_verification,
            RunStatus.EVALUATING: self._evaluate,
            RunStatus.REPAIRING: self._repair,
        }
        status = RunStatus(state["status"])
        handler = handlers.get(status)
        if handler is None:
            return TickResult(copy.deepcopy(state), False)
        return handler(copy.deepcopy(state))

    def _start_initial_planner(
        self,
        state: dict[str, object],
    ) -> TickResult:
        return self._start_planner_operation(state, repair=False)

    def _repair(self, state: dict[str, object]) -> TickResult:
        if self._pending_for_role(state, "planner") is not None:
            return self._poll_planner(state)
        return self._start_planner_operation(state, repair=True)

    def _start_planner_operation(
        self,
        state: dict[str, object],
        *,
        repair: bool,
    ) -> TickResult:
        planner = self._planner()
        operation_id = (
            ("REPAIR" if repair else "PLAN")
            + f"-{uuid.uuid4()}"
        )
        attempt_no = 1
        attempt_token = f"ATTEMPT-PLANNER-{uuid.uuid4()}"
        created_at = self._now()
        integration_head = state["repository"]["integration_head"]
        worktree = self.dependencies.worktrees.create_disposable_worktree(
            state["run_id"],
            "read-only",
            operation_id,
            integration_head,
        )
        prior_plans = self._load_plans(state)
        planner.expected_base = integration_head
        planner.prior_plans = prior_plans
        context = {
            "goal": state["goal"],
            "repository": state["repository"],
            "prior_plans": [
                self._plan_to_dict(plan) for plan in prior_plans
            ],
            "repair_round": state["repair_round"],
            "last_error": state["last_error"],
        }
        prefix = role_attempt_prefix(
            "planner",
            operation_id,
            attempt_no,
        )
        invocation = planner.prepare(
            run_id=state["run_id"],
            operation_id=operation_id,
            attempt_no=attempt_no,
            attempt_created_at=created_at,
            attempt_token=attempt_token,
            worktree=worktree,
            context=context,
            artifact_prefix=prefix,
        )
        store = self._active_store()

        def allocate(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            del refs
            runtime = current["role_runtime"]["planner"]
            if runtime["active_attempt_token"] is not None:
                raise ContractError("Planner operation is already active")
            runtime["operation_id"] = operation_id
            runtime["attempt_no"] = attempt_no
            runtime["failure_count"] = 0
            runtime["active_attempt_token"] = attempt_token
            runtime["last_error"] = None
            current["status"] = (
                "REPAIRING" if repair else "PLANNING"
            )
            current["updated_at"] = created_at

        allocated = store.transact(state["revision"], {}, allocate)
        self._invocations[attempt_token] = invocation
        planner.start(invocation, self._active_ledger())
        return TickResult(store.load(), True)

    def _poll_planner(
        self,
        state: dict[str, object],
    ) -> TickResult:
        entry = self._pending_for_role(state, "planner")
        if entry is None:
            return TickResult(state, False)
        token, pending = entry
        handle = self._handle_from_pending(pending)
        if handle is None:
            return TickResult(state, False)
        status = self._provider().poll(handle)
        if status is ProviderStatus.RUNNING:
            return TickResult(state, False)
        if status is ProviderStatus.FAILED:
            return self._close_role_failure(
                state,
                "planner",
                token,
                handle,
                "PLANNER_PROVIDER_FAILED",
            )
        invocation = self._require_invocation(token)
        prior = self._load_plans(state)
        planner = self._planner()
        planner.prior_plans = prior
        document = planner.result(
            invocation,
            handle,
            self._active_ledger(),
        )
        raw_body = self._provider().result(handle).body
        return self._accept_plan(
            self._active_store().load(),
            token,
            document,
            raw_body,
        )

    def _accept_plan(
        self,
        state: dict[str, object],
        token: str,
        document: PlanDocument,
        raw_body: bytes,
    ) -> TickResult:
        pending = state["pending_dispatches"][token]
        plan_path = (
            "plan/plan-v001.json"
            if document.plan_version == 1
            else f"plan/repair-v{document.plan_version:03d}.json"
        )
        plan_body = canonical_json_bytes(
            self._plan_to_dict(document)
        )
        attempt_path = (
            f"{role_attempt_prefix('planner', pending['operation_id'], pending['attempt_no'])}/"
            "attempt.json"
        )
        attempt_body = self._attempt_manifest_body(
            pending,
            status="SUCCEEDED",
            completed_at=self._now(),
        )
        artifacts: dict[str, bytes] = {
            plan_path: plan_body,
            attempt_path: attempt_body,
        }
        for task in document.tasks:
            artifacts[f"tasks/{task.id}/task.json"] = (
                canonical_json_bytes(self._task_to_dict(task))
            )
        store = self._active_store()

        def accept(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            current_pending = current["pending_dispatches"].get(token)
            if current_pending != pending:
                raise ContractError("Planner result owner changed")
            if current_pending["result"] is None:
                raise ContractError("Planner result artifact is missing")
            if artifact_ref(
                current_pending["result"]["path"],
                raw_body,
            ).sha256 != current_pending["result"]["sha256"]:
                raise ContractError("Planner raw result digest changed")
            current["plans"].append(refs[plan_path].as_dict())
            current["plan_version"] = document.plan_version
            current["role_attempts"]["planner"].append(
                refs[attempt_path].as_dict()
            )
            runtime = current["role_runtime"]["planner"]
            runtime["active_attempt_token"] = None
            runtime["last_error"] = None
            for task in document.tasks:
                if task.id in current["tasks"]:
                    raise ContractError("Planner attempted to rewrite a task")
                task_state = new_task_state(task)
                task_state["task"] = refs[
                    f"tasks/{task.id}/task.json"
                ].as_dict()
                current["tasks"][task.id] = task_state
            del current["pending_dispatches"][token]
            current["status"] = "EXECUTING"
            current["last_error"] = None
            current["updated_at"] = self._now()

        updated = store.transact(
            state["revision"],
            artifacts,
            accept,
        )
        return TickResult(updated, True, (token,))

    def _execute_tasks(
        self,
        state: dict[str, object],
    ) -> TickResult:
        if state["pending_source_commit"] is not None:
            return self._reconcile_source_commit(state)
        if state["pending_integration"] is not None:
            self._integrator().recover()
            return TickResult(self._active_store().load(), True)
        completed = self._poll_workers(state)
        if completed:
            return TickResult(
                self._active_store().load(),
                True,
                tuple(completed),
            )
        state = self._active_store().load()
        for task_id in sorted(state["tasks"]):
            task = state["tasks"][task_id]
            active = task["active_attempt"]
            if (
                task["status"] == "RUNNING"
                and isinstance(active, dict)
                and active["status"] == "VERIFYING"
            ):
                return self._prepare_source_for_task(
                    state,
                    task_id,
                )
        for task_id in sorted(state["tasks"]):
            if state["tasks"][task_id]["status"] == "READY_TO_INTEGRATE":
                contract = self._task_contracts(state)[task_id]
                audit = self._source_audit_for_active(state, task_id)
                try:
                    self._integrator().integrate(contract, audit)
                except IntegrationRejected as error:
                    return self._close_worker_after_integration_failure(
                        self._active_store().load(),
                        task_id,
                        str(error),
                    )
                return TickResult(self._active_store().load(), True)
        if state["tasks"] and all(
            task["status"] == "COMPLETED"
            for task in state["tasks"].values()
        ):
            return self._transition_status(
                state,
                "GLOBAL_VERIFYING",
            )
        contracts = self._task_contracts(state)
        active_plan = PlanDocument(
            schema_version=1,
            plan_version=state["plan_version"],
            summary="active task set",
            acceptance_criteria=self._load_plans(state)[0].acceptance_criteria,
            global_verification=(),
            tasks=tuple(contracts.values()),
        )
        promoted_probe = copy.deepcopy(state)
        promoted = self.dependencies.scheduler.promote_ready(
            promoted_probe,
            active_plan,
        )
        if promoted:
            store = self._active_store()

            def promote(
                current: dict[str, object],
                refs: Mapping[str, ArtifactRef],
            ) -> None:
                del refs
                for task_id in promoted:
                    if current["tasks"][task_id]["status"] == "PENDING":
                        current["tasks"][task_id]["status"] = "READY"
                current["updated_at"] = self._now()

            updated = store.transact(
                state["revision"],
                {},
                promote,
            )
            return TickResult(updated, True)
        selected = self.dependencies.scheduler.dispatchable(
            state,
            active_plan,
        )
        if selected:
            updated = self._start_workers(
                state,
                contracts,
                selected,
            )
            return TickResult(updated, True)
        return TickResult(state, False)

    def _start_workers(
        self,
        state: dict[str, object],
        contracts: dict[str, TaskContract],
        selected: Sequence[str],
    ) -> dict[str, object]:
        allocated: list[
            tuple[TaskContract, TaskWorktree, str]
        ] = []
        for task_id in selected:
            contract = contracts[task_id]
            task_state = state["tasks"][task_id]
            attempt_no = task_state["attempt_no"] + 1
            base = state["repository"]["integration_head"]
            branch = (
                f"refs/heads/vibe/{state['run_id']}/"
                f"{task_id}-a{attempt_no}"
            )
            path = (
                self.target
                / ".vibe-coding"
                / "worktrees"
                / state["run_id"]
                / f"{task_id}-a{attempt_no}"
            )
            descriptor = TaskWorktree(path, branch, base)
            token = f"ATTEMPT-WORKER-{uuid.uuid4()}"
            store = self._active_store()

            def allocate(
                current: dict[str, object],
                refs: Mapping[str, ArtifactRef],
                *,
                current_contract: TaskContract = contract,
                current_descriptor: TaskWorktree = descriptor,
                current_token: str = token,
            ) -> None:
                del refs
                start_attempt(
                    current["tasks"][current_contract.id],
                    current_descriptor.base_sha,
                    current_descriptor,
                    current_token,
                )
                current["updated_at"] = self._now()

            state = store.transact(
                state["revision"],
                {},
                allocate,
            )
            worktree = (
                self.dependencies.worktrees.create_task_worktree(
                    state["run_id"],
                    task_id,
                    attempt_no,
                    base,
                )
            )
            allocated.append((contract, worktree, token))

        for contract, worktree, token in allocated:
            task = state["tasks"][contract.id]
            active = task["active_attempt"]
            protected = (
                self.dependencies.worktrees.snapshot_protected_git(
                    worktree.branch
                )
            )
            preflight = (
                self.dependencies.worktrees.capture_worker_preflight(
                    worktree,
                    contract.id,
                    active["created_at"],
                    protected,
                )
            )
            preflight_body = canonical_json_bytes(
                preflight.as_dict()
            )
            preflight_path = (
                f"tasks/{contract.id}/attempts/"
                f"{task['attempt_no']:03d}/preflight.json"
            )
            store = self._active_store()

            def bind_preflight(
                current: dict[str, object],
                refs: Mapping[str, ArtifactRef],
                *,
                current_task_id: str = contract.id,
                current_token: str = token,
                current_path: str = preflight_path,
            ) -> None:
                bind_attempt_preflight(
                    current["tasks"][current_task_id],
                    current_token,
                    refs[current_path],
                )
                current["updated_at"] = self._now()

            state = store.transact(
                state["revision"],
                {preflight_path: preflight_body},
                bind_preflight,
            )
            active = state["tasks"][contract.id][
                "active_attempt"
            ]
            invocation = self._worker().prepare(
                run_id=state["run_id"],
                task=contract,
                operation_id=(
                    f"WORK-{contract.id}-"
                    f"A{state['tasks'][contract.id]['attempt_no']}"
                ),
                attempt_no=state["tasks"][contract.id][
                    "attempt_no"
                ],
                attempt_created_at=active["created_at"],
                attempt_token=token,
                worktree=worktree.path,
                task_base_sha=worktree.base_sha,
                previous_failure=state["tasks"][
                    contract.id
                ]["last_error"],
                artifact_prefix=(
                    f"tasks/{contract.id}/attempts/"
                    f"{state['tasks'][contract.id]['attempt_no']:03d}"
                ),
            )
            invocation = dataclasses.replace(
                invocation,
                branch=worktree.branch,
                preflight_body=preflight_body,
            )
            self._invocations[token] = invocation
            self._active_ledger().dispatch(
                invocation,
                self._provider().start,
            )
            state = self._active_store().load()
        return state

    def _start_worker(
        self,
        state: dict[str, object],
        contract: TaskContract,
    ) -> None:
        store = self._active_store()
        task_state = state["tasks"][contract.id]
        attempt_no = task_state["attempt_no"] + 1
        base = state["repository"]["integration_head"]
        branch = (
            f"refs/heads/vibe/{state['run_id']}/"
            f"{contract.id}-a{attempt_no}"
        )
        path = (
            self.target
            / ".vibe-coding"
            / "worktrees"
            / state["run_id"]
            / f"{contract.id}-a{attempt_no}"
        )
        descriptor = TaskWorktree(path, branch, base)
        token = f"ATTEMPT-WORKER-{uuid.uuid4()}"

        def allocate(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            del refs
            start_attempt(
                current["tasks"][contract.id],
                base,
                descriptor,
                token,
            )
            current["updated_at"] = self._now()

        allocated = store.transact(
            state["revision"],
            {},
            allocate,
        )
        worktree = self.dependencies.worktrees.create_task_worktree(
            state["run_id"],
            contract.id,
            attempt_no,
            base,
        )
        active = allocated["tasks"][contract.id]["active_attempt"]
        protected = self.dependencies.worktrees.snapshot_protected_git(
            worktree.branch
        )
        preflight = self.dependencies.worktrees.capture_worker_preflight(
            worktree,
            contract.id,
            active["created_at"],
            protected,
        )
        preflight_body = canonical_json_bytes(preflight.as_dict())
        preflight_path = (
            f"tasks/{contract.id}/attempts/{attempt_no:03d}/"
            "preflight.json"
        )

        def bind_preflight(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            bind_attempt_preflight(
                current["tasks"][contract.id],
                token,
                refs[preflight_path],
            )
            current["updated_at"] = self._now()

        prepared = store.transact(
            allocated["revision"],
            {preflight_path: preflight_body},
            bind_preflight,
        )
        active = prepared["tasks"][contract.id]["active_attempt"]
        worker = self._worker()
        invocation = worker.prepare(
            run_id=state["run_id"],
            task=contract,
            operation_id=f"WORK-{contract.id}-A{attempt_no}",
            attempt_no=attempt_no,
            attempt_created_at=active["created_at"],
            attempt_token=token,
            worktree=worktree.path,
            task_base_sha=base,
            previous_failure=prepared["tasks"][contract.id]["last_error"],
            artifact_prefix=(
                f"tasks/{contract.id}/attempts/{attempt_no:03d}"
            ),
        )
        invocation = dataclasses.replace(
            invocation,
            branch=worktree.branch,
            preflight_body=preflight_body,
        )
        self._invocations[token] = invocation
        self._active_ledger().dispatch(
            invocation,
            self._provider().start,
        )

    def _poll_workers(
        self,
        state: dict[str, object],
    ) -> list[str]:
        completed: list[str] = []
        entries = [
            (token, pending)
            for token, pending in state["pending_dispatches"].items()
            if pending["role"] == "worker"
            and pending["provider_handle"] is not None
        ]
        for token, pending in entries:
            handle = self._handle_from_pending(pending)
            assert handle is not None
            status = self._provider().poll(handle)
            if status is ProviderStatus.RUNNING:
                continue
            invocation = self._require_invocation(token)
            self._active_ledger().bind_completion(
                invocation,
                handle,
            )
            if status is ProviderStatus.FAILED:
                self._freeze_failed_worker(
                    self._active_store().load(),
                    token,
                    "WORKER_PROVIDER_FAILED",
                )
                completed.append(token)
                continue
            body = self._provider().result(handle).body
            result = self._worker().parse_result(
                invocation,
                body,
            )
            if result.status == "BLOCKED":
                self._pause_blocked_worker(
                    self._active_store().load(),
                    token,
                    result.blocker or "Worker blocked",
                )
                completed.append(token)
                continue
            active_result_path = (
                self._active_store()
                .load()["tasks"][result.task_id]["active_attempt"][
                    "result_path"
                ]
            )

            def accept(
                current: dict[str, object],
                current_pending: dict[str, object],
                refs: Mapping[str, ArtifactRef],
            ) -> None:
                task = current["tasks"][result.task_id]
                active = task["active_attempt"]
                if (
                    active is None
                    or active["attempt_token"] != token
                    or current_pending["result"] is None
                    or refs[active_result_path].sha256
                    != current_pending["result"]["sha256"]
                ):
                    raise ContractError(
                        "Worker result identity or digest changed"
                    )
                task["result"] = refs[active_result_path].as_dict()
                active["status"] = "VERIFYING"
                active["provider_handle"] = None
                current["updated_at"] = self._now()

            self._active_ledger().accept_current_result(
                token,
                {active_result_path: body},
                accept,
            )
            completed.append(token)
        return completed

    def _prepare_source_for_task(
        self,
        state: dict[str, object],
        task_id: str,
    ) -> TickResult:
        prepared, worktree = self._prepare_active_source(
            state,
            task_id,
        )
        task = state["tasks"][task_id]
        active = task["active_attempt"]
        source_path = (
            f"tasks/{task_id}/attempts/{task['attempt_no']:03d}/"
            "source-audit.json"
        )
        operation_id = f"SRC-{uuid.uuid4()}"
        marker = {
            "operation_id": operation_id,
            "task_id": task_id,
            "attempt_no": task["attempt_no"],
            "expected_base": worktree.base_sha,
            "task_ref": worktree.branch,
            "tree_oid": prepared.tree_oid,
            "candidate_commit": prepared.candidate_commit,
            "author_name": "Vibe Controller",
            "author_email": "vibe-controller@localhost",
            "timestamp": active["created_at"],
            "message": (
                f"vibe({state['run_id']}): {task_id} "
                f"attempt {task['attempt_no']}"
            ),
            "source_audit": artifact_ref(
                source_path,
                prepared.source_audit_body,
            ).as_dict(),
        }
        store = self._active_store()

        def persist(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            value = copy.deepcopy(marker)
            value["source_audit"] = refs[source_path].as_dict()
            current["pending_source_commit"] = value
            current["updated_at"] = self._now()

        persisted = store.transact(
            state["revision"],
            {source_path: prepared.source_audit_body},
            persist,
        )
        self.dependencies.fault_hook(
            "after_worker_edit_before_controller_commit"
        )
        self.dependencies.worktrees.apply_source_commit_cas(
            worktree,
            prepared,
        )
        self.dependencies.fault_hook(
            "after_controller_commit_before_verification_binding"
        )
        updated = self._complete_source_marker(
            persisted,
            task_id,
            prepared,
        )
        return TickResult(updated, True)

    def _reconcile_source_commit(
        self,
        state: dict[str, object],
    ) -> TickResult:
        marker = state["pending_source_commit"]
        task_id = marker["task_id"]
        task = state["tasks"][task_id]
        active = task["active_attempt"]
        worktree = TaskWorktree(
            self.target.joinpath(
                *PurePosixPath(active["worktree"]).parts
            ).resolve(),
            active["branch"],
            active["task_base_sha"],
        )
        audit_ref = ArtifactRef(
            marker["source_audit"]["path"],
            marker["source_audit"]["sha256"],
        )
        audit_body = self._read_ref(audit_ref)
        audit = self._source_audit_for_active(
            state,
            task_id,
        )
        prepared = PreparedSourceCommit(
            tree_oid=marker["tree_oid"],
            candidate_commit=marker["candidate_commit"],
            source_audit=audit,
            source_audit_body=audit_body,
        )
        if (
            prepared.source_audit.source_head
            != marker["candidate_commit"]
            or prepared.source_audit.task_base_sha
            != marker["expected_base"]
        ):
            return self._pause(
                state,
                "SOURCE_PREPARED_MISMATCH",
                "prepared source commit no longer matches durable marker",
            )
        outcome = self.dependencies.worktrees.classify_source_cas(
            worktree,
            prepared,
        )
        if outcome == "RETRY_CAS":
            self.dependencies.worktrees.apply_source_commit_cas(
                worktree,
                prepared,
            )
        elif outcome == "PAUSE":
            return self._pause(
                state,
                "SOURCE_REF_MOVED",
                "reserved task ref moved outside the Controller",
            )
        updated = self._complete_source_marker(
            self._active_store().load(),
            task_id,
            prepared,
        )
        return TickResult(updated, True)

    def _complete_source_marker(
        self,
        state: dict[str, object],
        task_id: str,
        prepared: PreparedSourceCommit,
    ) -> dict[str, object]:
        marker = state["pending_source_commit"]
        store = self._active_store()

        def complete(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            del refs
            if current["pending_source_commit"] != marker:
                raise ContractError("source marker changed before completion")
            task = current["tasks"][task_id]
            task["source_commits"] = [prepared.candidate_commit]
            task["status"] = "READY_TO_INTEGRATE"
            current["pending_source_commit"] = None
            current["updated_at"] = self._now()

        return store.transact(state["revision"], {}, complete)

    def _prepare_active_source(
        self,
        state: dict[str, object],
        task_id: str,
    ) -> tuple[PreparedSourceCommit, TaskWorktree]:
        task = state["tasks"][task_id]
        active = task["active_attempt"]
        worktree = TaskWorktree(
            self.target.joinpath(
                *PurePosixPath(active["worktree"]).parts
            ).resolve(),
            active["branch"],
            active["task_base_sha"],
        )
        preflight_raw = parse_json_object_bytes(
            self._read_ref(
                ArtifactRef(
                    active["preflight"]["path"],
                    active["preflight"]["sha256"],
                )
            )
        )
        preflight = AttemptPreflight(
            role=preflight_raw["role"],
            task_id=preflight_raw["task_id"],
            attempt_created_at=preflight_raw[
                "attempt_created_at"
            ],
            expected_base=preflight_raw["expected_base"],
            branch=preflight_raw["branch"],
            worktree=preflight_raw["worktree"],
            snapshot=preflight_raw["snapshot"],
        )
        metadata = SourceCommitMetadata(
            state["run_id"],
            task_id,
            task["attempt_no"],
            active["created_at"],
        )
        prepared = self.dependencies.worktrees.prepare_source_commit(
            self._task_contracts(state)[task_id],
            worktree,
            preflight,
            metadata,
        )
        return prepared, worktree

    def _source_audit_for_active(
        self,
        state: dict[str, object],
        task_id: str,
    ) -> SourceAudit:
        task = state["tasks"][task_id]
        reference = self._source_audit_ref(
            state,
            task_id,
            task["attempt_no"],
        )
        raw = parse_json_object_bytes(self._read_ref(reference))
        try:
            return SourceAudit(
                task_base_sha=str(raw["base_sha"]),
                source_head=str(raw["candidate_commit"]),
                source_commits=tuple(
                    str(value) for value in raw["source_commits"]
                ),
                changed_paths=tuple(
                    str(value) for value in raw["changed_paths"]
                ),
                gitlinks_changed=raw["gitlinks_changed"],
                protected_before=self._protected_snapshot(
                    raw["protected_before"]
                ),
                protected_after=self._protected_snapshot(
                    raw["protected_after"]
                ),
            )
        except (KeyError, TypeError) as error:
            raise ContractError(
                "source audit artifact is invalid"
            ) from error

    @staticmethod
    def _protected_snapshot(
        raw: object,
    ) -> ProtectedGitSnapshot:
        if not isinstance(raw, dict):
            raise ContractError(
                "source audit protected snapshot is invalid"
            )
        try:
            return ProtectedGitSnapshot(
                user_head=str(raw["user_head"]),
                index_tree=str(raw["index_tree"]),
                status_digest=str(raw["status_digest"]),
                refs=tuple(
                    (str(item[0]), str(item[1]))
                    for item in raw["refs"]
                ),
                packed_refs_digest=str(
                    raw["packed_refs_digest"]
                ),
                config_digest=str(raw["config_digest"]),
                remote_urls=tuple(
                    (str(item[0]), str(item[1]))
                    for item in raw["remote_urls"]
                ),
            )
        except (KeyError, TypeError, IndexError) as error:
            raise ContractError(
                "source audit protected snapshot is invalid"
            ) from error

    def _close_worker_after_integration_failure(
        self,
        state: dict[str, object],
        task_id: str,
        message: str,
    ) -> TickResult:
        task = state["tasks"][task_id]
        active = task["active_attempt"]
        audit_ref = self._source_audit_ref(
            state,
            task_id,
            task["attempt_no"],
        )
        path = (
            f"tasks/{task_id}/attempts/{task['attempt_no']:03d}/"
            "attempt.json"
        )
        body = self._worker_attempt_body(
            state,
            task_id,
            status="FAILED",
            source_audit=audit_ref,
            verification=None,
            last_error={
                "code": "INTEGRATION_REJECTED",
                "message": message,
                "retryable": True,
            },
        )
        store = self._active_store()

        def close(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            current_task = current["tasks"][task_id]
            if current_task["active_attempt"] != active:
                raise ContractError("Worker Attempt changed before failure close")
            current_task["attempts"].append(refs[path].as_dict())
            current_task["active_attempt"] = None
            current_task["result"] = None
            current_task["verification"] = None
            current_task["source_commits"] = []
            current_task["failure_count"] += 1
            current_task["last_error"] = {
                "code": "INTEGRATION_REJECTED",
                "message": message,
                "retryable": True,
            }
            current_task["status"] = (
                "READY"
                if current_task["failure_count"]
                < current_task["max_attempts"]
                else "FAILED"
            )
            current["updated_at"] = self._now()
            if current_task["status"] == "FAILED":
                current["status"] = "FAILED"
                current["last_error"] = copy.deepcopy(
                    current_task["last_error"]
                )

        updated = store.transact(
            state["revision"],
            {path: body},
            close,
        )
        return TickResult(updated, True)

    def _run_global_verification(
        self,
        state: dict[str, object],
    ) -> TickResult:
        verification = self._verification()
        ref = state["repository"]["integration_ref"]
        head = state["repository"]["integration_head"]
        if self.dependencies.worktrees.resolve_ref(ref) != head:
            return self._pause(
                state,
                "INTEGRATION_REF_MOVED",
                "integration ref moved before global verification",
            )
        operation = f"GLOBAL-{uuid.uuid4()}"
        worktree = self.dependencies.worktrees.create_disposable_worktree(
            state["run_id"],
            "read-only",
            operation,
            head,
        )
        command_ids = effective_global_verification(
            self.config,
            self._load_plans(state),
        )
        try:
            result = verification.run(
                head,
                worktree,
                command_ids,
                f"verification/global/VERIFY-{uuid.uuid4()}",
            )
        except VerificationEnvironmentError as error:
            return self._pause(
                state,
                "VERIFICATION_ENVIRONMENT",
                str(error),
            )
        if self.dependencies.worktrees.resolve_ref(ref) != head:
            return self._pause(
                state,
                "INTEGRATION_REF_MOVED",
                "integration ref moved during global verification",
            )
        if not result.passed:
            return self._open_repair(
                state,
                "GLOBAL_VERIFICATION_FAILED",
                "global verification failed",
                result.manifest_ref,
            )
        store = self._active_store()

        def bind(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            del refs
            current["verifications"].append(
                result.manifest_ref.as_dict()
            )
            current["global_verification"] = {
                "verification": result.manifest_ref.as_dict(),
                "integration_head": head,
                "passed": True,
            }
            current["status"] = "EVALUATING"
            current["updated_at"] = self._now()

        updated = store.transact(state["revision"], {}, bind)
        return TickResult(updated, True)

    def _evaluate(
        self,
        state: dict[str, object],
    ) -> TickResult:
        if state["pending_evaluation"] is not None:
            return self._accept_pending_evaluation(state)
        entry = self._pending_for_role(state, "evaluator")
        if entry is not None:
            return self._poll_evaluator(state, entry)
        evidence_round = (
            state["latest_evaluation"]["evidence_round"] + 1
            if isinstance(state["latest_evaluation"], dict)
            and state["latest_evaluation"]["verdict"] == "UNVERIFIED"
            and state["latest_evaluation"]["integration_head"]
            == state["repository"]["integration_head"]
            else 0
        )
        return self._start_evaluator(state, evidence_round)

    def _start_evaluator(
        self,
        state: dict[str, object],
        evidence_round: int,
    ) -> TickResult:
        evaluator = self._evaluator()
        operation_id = f"EVAL-{uuid.uuid4()}"
        token = f"ATTEMPT-EVALUATOR-{uuid.uuid4()}"
        created_at = self._now()
        head = state["repository"]["integration_head"]
        worktree = self.dependencies.worktrees.create_disposable_worktree(
            state["run_id"],
            "read-only",
            operation_id,
            head,
        )
        criteria = self._load_plans(state)[0].acceptance_criteria
        context = {
            "goal": state["goal"],
            "acceptance_criteria": [
                asdict(item) for item in criteria
            ],
            "plans": state["plans"],
            "tasks": state["tasks"],
            "integration_head": head,
            "evidence_catalog": self._evidence_catalog(state),
            "evidence_round": evidence_round,
        }
        evaluator.expected_base = head
        invocation = evaluator.prepare(
            run_id=state["run_id"],
            operation_id=operation_id,
            attempt_no=1,
            attempt_created_at=created_at,
            attempt_token=token,
            worktree=worktree,
            context=context,
            artifact_prefix=role_attempt_prefix(
                "evaluator",
                operation_id,
                1,
            ),
        )
        store = self._active_store()

        def allocate(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            del refs
            runtime = current["role_runtime"]["evaluator"]
            runtime["operation_id"] = operation_id
            runtime["attempt_no"] = 1
            runtime["failure_count"] = 0
            runtime["active_attempt_token"] = token
            runtime["last_error"] = None
            current["updated_at"] = created_at

        store.transact(state["revision"], {}, allocate)
        self._evaluation_meta[token] = (
            len(state["evaluations"]) + 1,
            evidence_round,
        )
        self._invocations[token] = invocation
        evaluator.start(invocation, self._active_ledger())
        return TickResult(store.load(), True)

    def _poll_evaluator(
        self,
        state: dict[str, object],
        entry: tuple[str, dict[str, object]],
    ) -> TickResult:
        token, pending = entry
        handle = self._handle_from_pending(pending)
        if handle is None:
            return TickResult(state, False)
        status = self._provider().poll(handle)
        if status is ProviderStatus.RUNNING:
            return TickResult(state, False)
        if status is ProviderStatus.FAILED:
            return self._close_role_failure(
                state,
                "evaluator",
                token,
                handle,
                "EVALUATOR_PROVIDER_FAILED",
            )
        invocation = self._require_invocation(token)
        result = self._evaluator().result(
            invocation,
            handle,
            self._active_ledger(),
        )
        raw_body = self._provider().result(handle).body
        criteria = tuple(
            item.id
            for item in self._load_plans(state)[0].acceptance_criteria
        )
        result = self._evaluator().parse_result(
            raw_body,
            expected_criteria=criteria,
        )
        current = self._active_store().load()
        pending = current["pending_dispatches"][token]
        evaluation_round, evidence_round = self._evaluation_meta.get(
            token,
            (len(current["evaluations"]) + 1, 0),
        )
        attempt_path = (
            f"{role_attempt_prefix('evaluator', pending['operation_id'], pending['attempt_no'])}/"
            "attempt.json"
        )
        attempt_body = self._attempt_manifest_body(
            pending,
            status="SUCCEEDED",
            completed_at=self._now(),
        )
        marker = {
            "operation_id": pending["operation_id"],
            "attempt_no": pending["attempt_no"],
            "attempt_token": token,
            "evaluation_round": evaluation_round,
            "evidence_round": evidence_round,
            "integration_head": current["repository"][
                "integration_head"
            ],
            "attempt": artifact_ref(
                attempt_path,
                attempt_body,
            ).as_dict(),
            "raw_result": pending["result"],
        }
        store = self._active_store()

        def prepare(
            active_state: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            active_pending = active_state[
                "pending_dispatches"
            ].get(token)
            if active_pending != pending:
                raise ContractError("Evaluator result owner changed")
            value = copy.deepcopy(marker)
            value["attempt"] = refs[attempt_path].as_dict()
            active_state["role_attempts"]["evaluator"].append(
                refs[attempt_path].as_dict()
            )
            active_state["role_runtime"]["evaluator"][
                "active_attempt_token"
            ] = None
            del active_state["pending_dispatches"][token]
            active_state["pending_evaluation"] = value
            active_state["updated_at"] = self._now()

        updated = store.transact(
            current["revision"],
            {attempt_path: attempt_body},
            prepare,
        )
        del result
        return TickResult(updated, True, (token,))

    def _accept_pending_evaluation(
        self,
        state: dict[str, object],
    ) -> TickResult:
        pending = state["pending_evaluation"]
        raw_ref = ArtifactRef(
            pending["raw_result"]["path"],
            pending["raw_result"]["sha256"],
        )
        criteria = tuple(
            item.id
            for item in self._load_plans(state)[0].acceptance_criteria
        )
        result = self._evaluator().parse_result(
            self._read_ref(raw_ref),
            expected_criteria=criteria,
        )
        catalog = self._evidence_catalog(state)
        self._validate_evaluation_evidence(result, catalog)
        envelope = evaluation_envelope(
            state,
            pending["evaluation_round"],
            pending["evidence_round"],
            raw_ref,
            result,
            self._refs_digest(state["plans"]),
            self._refs_digest(
                [
                    task["result"]
                    for task in state["tasks"].values()
                    if task["result"] is not None
                ]
            ),
            self._refs_digest(state["verifications"]),
            self._prompt_versions(state),
            catalog,
        )
        path = (
            f"evaluations/{pending['evaluation_round']:03d}-"
            f"{pending['evidence_round']:03d}.json"
        )
        body = canonical_json_bytes(envelope)
        store = self._active_store()
        if result.verdict is EvaluationVerdict.PASS:
            return self._accept_pass(
                state,
                pending,
                envelope,
                path,
                body,
            )
        if result.verdict is EvaluationVerdict.NEEDS_REPAIR:
            accepted = self._persist_evaluation(
                state,
                pending,
                result,
                path,
                body,
                "REPAIRING",
            )
            return self._open_repair(
                accepted.state,
                "EVALUATOR_NEEDS_REPAIR",
                "Evaluator requested repair",
                accepted.state["latest_evaluation"]["evaluation"],
                already_incremented=False,
            )
        if result.verdict is EvaluationVerdict.BLOCKED:
            return self._persist_evaluation(
                state,
                pending,
                result,
                path,
                body,
                "PAUSED",
                pause_code="EVALUATOR_BLOCKED",
            )
        accepted = self._persist_evaluation(
            state,
            pending,
            result,
            path,
            body,
            "EVALUATING",
        )
        if pending["evidence_round"] >= self.config.evidence_rounds:
            return self._pause(
                accepted.state,
                "EVIDENCE_EXHAUSTED",
                "supplemental evidence limit exhausted",
            )
        if result.evidence_requests:
            return self._run_supplemental(
                accepted.state,
                result.evidence_requests,
            )
        return accepted

    def _accept_pass(
        self,
        state: dict[str, object],
        pending: dict[str, object],
        envelope: dict[str, object],
        path: str,
        body: bytes,
    ) -> TickResult:
        head = state["repository"]["integration_head"]
        if self.dependencies.worktrees.resolve_ref(
            state["repository"]["integration_ref"]
        ) != head:
            return self._pause(
                state,
                "INTEGRATION_REF_MOVED",
                "integration ref moved before evaluation acceptance",
            )
        reference = artifact_ref(path, body)
        candidate = copy.deepcopy(state)
        candidate["evaluations"].append(reference.as_dict())
        candidate["latest_evaluation"] = {
            "evaluation": reference.as_dict(),
            "verdict": "PASS",
            "evaluation_round": pending["evaluation_round"],
            "evidence_round": pending["evidence_round"],
            "integration_head": head,
        }
        candidate["pending_evaluation"] = None
        if not goal_gate_satisfied(candidate, envelope, head):
            raise ContractError("GOAL_GATE_INVARIANT")
        self.dependencies.worktrees.update_ref(
            state["repository"]["integration_ref"],
            head,
            head,
        )
        store = self._active_store()

        def succeed(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            if current["pending_evaluation"] != pending:
                raise ContractError("pending evaluation changed")
            evaluation_ref = refs[path].as_dict()
            current["evaluations"].append(evaluation_ref)
            current["latest_evaluation"] = {
                "evaluation": evaluation_ref,
                "verdict": "PASS",
                "evaluation_round": pending["evaluation_round"],
                "evidence_round": pending["evidence_round"],
                "integration_head": head,
            }
            current["pending_evaluation"] = None
            current["status"] = "SUCCEEDED"
            current["resume_status"] = None
            current["last_error"] = None
            current["updated_at"] = self._now()

        updated = store.transact(
            state["revision"],
            {path: body},
            succeed,
        )
        return TickResult(updated, True)

    def _persist_evaluation(
        self,
        state: dict[str, object],
        pending: dict[str, object],
        result: EvaluationResult,
        path: str,
        body: bytes,
        next_status: str,
        *,
        pause_code: str | None = None,
    ) -> TickResult:
        store = self._active_store()

        def persist(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            if current["pending_evaluation"] != pending:
                raise ContractError("pending evaluation changed")
            reference = refs[path].as_dict()
            current["evaluations"].append(reference)
            current["latest_evaluation"] = {
                "evaluation": reference,
                "verdict": result.verdict.value,
                "evaluation_round": pending["evaluation_round"],
                "evidence_round": pending["evidence_round"],
                "integration_head": pending["integration_head"],
            }
            current["pending_evaluation"] = None
            if next_status == "PAUSED":
                current["resume_status"] = "EVALUATING"
                current["last_error"] = {
                    "code": pause_code or "EVALUATOR_BLOCKED",
                    "message": "Evaluator blocked the run",
                    "retryable": True,
                    "evidence": reference,
                }
            current["status"] = next_status
            current["updated_at"] = self._now()

        return TickResult(
            store.transact(
                state["revision"],
                {path: body},
                persist,
            ),
            True,
        )

    def _run_supplemental(
        self,
        state: dict[str, object],
        command_ids: tuple[str, ...],
    ) -> TickResult:
        head = state["repository"]["integration_head"]
        integration_ref = state["repository"]["integration_ref"]
        if (
            self.dependencies.worktrees.resolve_ref(
                integration_ref
            )
            != head
        ):
            return self._pause(
                state,
                "INTEGRATION_REF_MOVED",
                "integration ref moved before supplemental verification",
            )
        worktree = self.dependencies.worktrees.create_disposable_worktree(
            state["run_id"],
            "read-only",
            f"SUPPLEMENTAL-{uuid.uuid4()}",
            head,
        )
        try:
            result = self._verification().run(
                head,
                worktree,
                command_ids,
                f"verification/supplemental/VERIFY-{uuid.uuid4()}",
            )
        except VerificationEnvironmentError as error:
            return self._pause(
                state,
                "VERIFICATION_ENVIRONMENT",
                str(error),
            )
        if (
            self.dependencies.worktrees.resolve_ref(
                integration_ref
            )
            != head
        ):
            return self._pause(
                state,
                "INTEGRATION_REF_MOVED",
                "integration ref moved during supplemental verification",
            )
        store = self._active_store()

        def bind(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            del refs
            current["verifications"].append(
                result.manifest_ref.as_dict()
            )
            current["updated_at"] = self._now()

        updated = store.transact(state["revision"], {}, bind)
        return TickResult(updated, True)

    def _open_repair(
        self,
        state: dict[str, object],
        code: str,
        message: str,
        evidence: ArtifactRef | dict[str, object],
        *,
        already_incremented: bool = False,
    ) -> TickResult:
        if state["repair_round"] >= state["max_repair_rounds"]:
            return self._fail(state, "REPAIR_EXHAUSTED", message)
        store = self._active_store()

        def open_repair(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            del refs
            if not already_incremented:
                current["repair_round"] += 1
            current["status"] = "REPAIRING"
            current["last_error"] = {
                "code": code,
                "message": message,
                "retryable": True,
                "evidence": (
                    evidence.as_dict()
                    if isinstance(evidence, ArtifactRef)
                    else evidence
                ),
            }
            current["updated_at"] = self._now()

        return TickResult(
            store.transact(state["revision"], {}, open_repair),
            True,
        )

    def _transition_status(
        self,
        state: dict[str, object],
        status: str,
    ) -> TickResult:
        store = self._active_store()

        def transition(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            del refs
            current["status"] = status
            current["updated_at"] = self._now()

        return TickResult(
            store.transact(state["revision"], {}, transition),
            True,
        )

    def _pause(
        self,
        state: dict[str, object],
        code: str,
        message: str,
    ) -> TickResult:
        store = self._active_store()

        def pause(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            del refs
            current["resume_status"] = current["status"]
            current["status"] = "PAUSED"
            current["last_error"] = {
                "code": code,
                "message": message,
                "retryable": True,
            }
            current["updated_at"] = self._now()

        return TickResult(
            store.transact(state["revision"], {}, pause),
            True,
        )

    def _fail(
        self,
        state: dict[str, object],
        code: str,
        message: str,
    ) -> TickResult:
        store = self._active_store()

        def fail(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            del refs
            current["status"] = "FAILED"
            current["resume_status"] = None
            current["last_error"] = {
                "code": code,
                "message": message,
                "retryable": False,
            }
            current["updated_at"] = self._now()

        return TickResult(
            store.transact(state["revision"], {}, fail),
            True,
        )

    def _close_role_failure(
        self,
        state: dict[str, object],
        role: str,
        token: str,
        handle: ProviderHandle,
        code: str,
    ) -> TickResult:
        invocation = self._require_invocation(token)
        self._active_ledger().bind_completion(invocation, handle)
        current = self._active_store().load()
        pending = current["pending_dispatches"][token]
        path = (
            f"{role_attempt_prefix(role, pending['operation_id'], pending['attempt_no'])}/"
            "attempt.json"
        )
        body = self._attempt_manifest_body(
            pending,
            status="FAILED",
            completed_at=self._now(),
            last_error={
                "code": code,
                "message": f"{role} Provider failed",
                "retryable": True,
            },
        )
        store = self._active_store()

        def close(
            active: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            runtime = active["role_runtime"][role]
            active["role_attempts"][role].append(
                refs[path].as_dict()
            )
            runtime["active_attempt_token"] = None
            runtime["failure_count"] += 1
            del active["pending_dispatches"][token]
            if runtime["failure_count"] >= runtime["max_attempts"]:
                active["status"] = "FAILED"
                active["last_error"] = {
                    "code": code,
                    "message": f"{role} attempts exhausted",
                    "retryable": False,
                }
            active["updated_at"] = self._now()

        return TickResult(
            store.transact(
                current["revision"],
                {path: body},
                close,
            ),
            True,
            (token,),
        )

    def _freeze_failed_worker(
        self,
        state: dict[str, object],
        token: str,
        code: str,
    ) -> None:
        pending = state["pending_dispatches"][token]
        task_id = pending["task_id"]
        task = state["tasks"][task_id]
        path = (
            f"tasks/{task_id}/attempts/{task['attempt_no']:03d}/"
            "attempt.json"
        )
        body = self._worker_attempt_body(
            state,
            task_id,
            status="FAILED",
            source_audit=None,
            verification=None,
            last_error={
                "code": code,
                "message": "Worker Provider failed",
                "retryable": True,
            },
            pending=pending,
        )
        store = self._active_store()

        def close(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            current_task = current["tasks"][task_id]
            current_task["attempts"].append(refs[path].as_dict())
            current_task["active_attempt"] = None
            current_task["failure_count"] += 1
            current_task["last_error"] = {
                "code": code,
                "message": "Worker Provider failed",
                "retryable": True,
            }
            current_task["status"] = (
                "READY"
                if current_task["failure_count"]
                < current_task["max_attempts"]
                else "FAILED"
            )
            del current["pending_dispatches"][token]
            if current_task["status"] == "FAILED":
                current["status"] = "FAILED"
                current["last_error"] = copy.deepcopy(
                    current_task["last_error"]
                )
            current["updated_at"] = self._now()

        store.transact(
            state["revision"],
            {path: body},
            close,
        )

    def _pause_blocked_worker(
        self,
        state: dict[str, object],
        token: str,
        blocker: str,
    ) -> None:
        pending = state["pending_dispatches"][token]
        task_id = pending["task_id"]
        task = state["tasks"][task_id]
        path = (
            f"tasks/{task_id}/attempts/{task['attempt_no']:03d}/"
            "attempt.json"
        )
        body = self._worker_attempt_body(
            state,
            task_id,
            status="FAILED",
            source_audit=None,
            verification=None,
            last_error={
                "code": "WORKER_BLOCKED",
                "message": blocker,
                "retryable": True,
            },
            pending=pending,
        )
        store = self._active_store()

        def pause(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            current_task = current["tasks"][task_id]
            current_task["attempts"].append(refs[path].as_dict())
            current_task["active_attempt"] = None
            current_task["status"] = "FAILED"
            current_task["last_error"] = {
                "code": "WORKER_BLOCKED",
                "message": blocker,
                "retryable": True,
            }
            del current["pending_dispatches"][token]
            current["resume_status"] = "EXECUTING"
            current["status"] = "PAUSED"
            current["last_error"] = copy.deepcopy(
                current_task["last_error"]
            )
            current["updated_at"] = self._now()

        store.transact(
            state["revision"],
            {path: body},
            pause,
        )

    def _worker_attempt_body(
        self,
        state: dict[str, object],
        task_id: str,
        *,
        status: str,
        source_audit: ArtifactRef | None,
        verification: ArtifactRef | None,
        last_error: dict[str, object] | None,
        pending: dict[str, object] | None = None,
    ) -> bytes:
        task = state["tasks"][task_id]
        active = task["active_attempt"]
        source = pending or {}
        return canonical_json_bytes(
            {
                "schema_version": 1,
                "role": "worker",
                "operation_id": (
                    source.get("operation_id")
                    or f"WORK-{task_id}-A{task['attempt_no']}"
                ),
                "task_id": task_id,
                "attempt_no": task["attempt_no"],
                "attempt_token": active["attempt_token"],
                "status": status,
                "created_at": active["created_at"],
                "completed_at": self._now(),
                "expected_base": active["task_base_sha"],
                "branch": active["branch"],
                "worktree": active["worktree"],
                "preflight": active["preflight"],
                "prompt_versions": source.get(
                    "prompt_versions",
                    [],
                ),
                "provider_attempts": source.get(
                    "provider_attempts",
                    [],
                ),
                "request": source.get("request"),
                "launch": source.get("launch"),
                "stdout": source.get("stdout"),
                "stderr": source.get("stderr"),
                "exit": source.get("exit"),
                "result": task["result"],
                "source_audit": (
                    source_audit.as_dict()
                    if source_audit is not None
                    else None
                ),
                "verification": (
                    verification.as_dict()
                    if verification is not None
                    else None
                ),
                "last_error": last_error,
            }
        )

    def _attempt_manifest_body(
        self,
        pending: dict[str, object],
        *,
        status: str,
        completed_at: str,
        last_error: dict[str, object] | None = None,
    ) -> bytes:
        return canonical_json_bytes(
            {
                "schema_version": 1,
                "role": pending["role"],
                "operation_id": pending["operation_id"],
                "task_id": pending["task_id"],
                "attempt_no": pending["attempt_no"],
                "attempt_token": pending["attempt_token"],
                "status": status,
                "created_at": pending["attempt_created_at"],
                "completed_at": completed_at,
                "expected_base": pending["expected_base"],
                "branch": pending["branch"],
                "worktree": pending["worktree"],
                "preflight": pending["preflight"],
                "prompt_versions": pending["prompt_versions"],
                "provider_attempts": pending["provider_attempts"],
                "request": pending["request"],
                "launch": pending["launch"],
                "stdout": pending["stdout"],
                "stderr": pending["stderr"],
                "exit": pending["exit"],
                "result": pending["result"],
                "source_audit": None,
                "verification": None,
                "last_error": last_error,
            }
        )

    def _register_controller(
        self,
        state: dict[str, object],
    ) -> dict[str, object]:
        pid = os.getpid()
        identity = process_start_identity(pid)
        group = os.getpgrp()
        token = f"CONTROLLER-{uuid.uuid4()}"
        store = self._active_store()

        def register(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            del refs
            current["controller"] = {
                "pid": pid,
                "process_start_identity": identity,
                "process_group": group,
                "controller_token": token,
            }
            current["updated_at"] = self._now()

        return store.transact(state["revision"], {}, register)

    def _finish_creation(
        self,
        run_id: str,
        goal: str,
        config: FrozenRunConfig,
        config_body: bytes,
        baseline,
        fingerprint: str,
    ) -> str:
        store = self.dependencies.store_factory(self.target, run_id)
        run_ref = f"refs/heads/vibe/run-{run_id}"
        intent = {
            "schema_version": 1,
            "run_id": run_id,
            "creation_fingerprint": fingerprint,
            "goal_sha256": self._sha256(goal.encode("utf-8")),
            "repository_identity": baseline.identity,
            "base_sha": baseline.base_sha,
            "run_ref": run_ref,
            "config_sha256": self._sha256(config_body),
        }
        intent_body = canonical_json_bytes(intent)
        with store.lock():
            intent_ref = store.prepare_artifact(
                "creation.intent.json",
                intent_body,
            )
            config_ref = store.prepare_artifact(
                "config.json",
                config_body,
            )
            self.dependencies.fault_hook(
                "after_creation_intent_before_ref"
            )
            self.dependencies.worktrees.create_run_ref(
                run_id,
                baseline.base_sha,
            )
            self.dependencies.fault_hook(
                "after_creation_ref_before_state"
            )
            try:
                state = store.load()
            except (ContractError, FileNotFoundError):
                now = self._now()
                initial = self._initial_state(
                    run_id,
                    goal,
                    config,
                    baseline,
                    run_ref,
                    intent_ref,
                    config_ref,
                    now,
                )
                state = store.create(initial, {})
            if not self._creation_state_matches(
                state,
                goal,
                baseline.identity,
                baseline.base_sha,
                run_ref,
                intent_ref,
                config_ref,
            ):
                raise ContractError(
                    "existing creation state does not match the request"
                )
            self.dependencies.fault_hook(
                "after_creation_state_before_receipt"
            )
            if state["creation"]["receipt"] is None:
                receipt_body = canonical_json_bytes(
                    {
                        "schema_version": 1,
                        "run_id": run_id,
                        "creation_fingerprint": fingerprint,
                        "intent": intent_ref.as_dict(),
                        "run_ref": run_ref,
                        "base_sha": baseline.base_sha,
                        "created_at": self._now(),
                    }
                )

                def bind_receipt(
                    current: dict[str, object],
                    refs: Mapping[str, ArtifactRef],
                ) -> None:
                    current["creation"]["receipt"] = refs[
                        "creation.receipt.json"
                    ].as_dict()
                    current["updated_at"] = self._now()

                state = store.transact(
                    state["revision"],
                    {"creation.receipt.json": receipt_body},
                    bind_receipt,
                )
            self.dependencies.fault_hook(
                "after_creation_receipt_before_return"
            )
        return run_id

    def _initial_state(
        self,
        run_id: str,
        goal: str,
        config: FrozenRunConfig,
        baseline,
        run_ref: str,
        intent_ref: ArtifactRef,
        config_ref: ArtifactRef,
        now: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 4,
            "run_id": run_id,
            "revision": 0,
            "goal": goal,
            "repository": {
                "identity": baseline.identity,
                "base_ref": baseline.base_ref,
                "base_sha": baseline.base_sha,
                "integration_ref": run_ref,
                "integration_head": baseline.base_sha,
            },
            "status": "CREATED",
            "resume_status": None,
            "plan_version": 0,
            "repair_round": 0,
            "max_repair_rounds": config.repair_rounds,
            "max_workers": config.max_workers,
            "controller": None,
            "creation": {
                "intent": intent_ref.as_dict(),
                "receipt": None,
            },
            "config": config_ref.as_dict(),
            "artifact_index": [
                intent_ref.as_dict(),
                config_ref.as_dict(),
            ],
            "plans": [],
            "role_attempts": {"planner": [], "evaluator": []},
            "role_runtime": {
                role: {
                    "operation_id": None,
                    "attempt_no": 0,
                    "failure_count": 0,
                    "max_attempts": config.task_attempts,
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
            "created_at": now,
            "updated_at": now,
        }

    def _matching_creation_runs(
        self,
        fingerprint: str,
    ) -> list[str]:
        runs = self.target / ".vibe-coding" / "runs"
        if not runs.is_dir():
            return []
        matches: list[str] = []
        for entry in sorted(runs.iterdir()):
            if (
                not entry.is_dir()
                or entry.is_symlink()
                or RUN_DIR_RE.fullmatch(entry.name) is None
            ):
                continue
            intent_path = entry / "creation.intent.json"
            if not intent_path.is_file() or intent_path.is_symlink():
                continue
            try:
                intent = load_json_object(intent_path)
            except ContractError:
                continue
            if intent.get("creation_fingerprint") == fingerprint:
                matches.append(entry.name)
        return matches

    def _allocate_run_id(self) -> str:
        date = self.dependencies.clock().astimezone(
            timezone.utc
        ).strftime("%Y%m%d")
        runs = self.target / ".vibe-coding" / "runs"
        names = (
            {entry.name for entry in runs.iterdir()}
            if runs.is_dir()
            else set()
        )
        refs = {
            ref
            for ref, _ in self.dependencies.worktrees.snapshot_protected_git().refs
        }
        for sequence in range(1, 1000):
            run_id = f"RUN-{date}-{sequence:03d}"
            if (
                run_id not in names
                and f"refs/heads/vibe/run-{run_id}" not in refs
            ):
                return run_id
        raise ContractError("daily run ID space is exhausted")

    def _allocation_lock(self):
        class _Lock:
            def __init__(inner, controller: Controller) -> None:
                inner.controller = controller
                inner.descriptor: int | None = None

            def __enter__(inner):
                control = (
                    inner.controller.target
                    / ".vibe-coding"
                    / "control"
                )
                inner.controller._ensure_no_symlink_directory(control)
                path = control / "run-allocation.lock"
                inner.descriptor = os.open(
                    path,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_NOFOLLOW,
                    0o600,
                )
                fcntl.flock(inner.descriptor, fcntl.LOCK_EX)
                return None

            def __exit__(inner, exc_type, exc, traceback):
                assert inner.descriptor is not None
                fcntl.flock(inner.descriptor, fcntl.LOCK_UN)
                os.close(inner.descriptor)
                return False

        return _Lock(self)

    def _ensure_no_symlink_directory(self, directory: Path) -> None:
        current = self.target
        for part in directory.relative_to(self.target).parts:
            current = current / part
            if current.is_symlink():
                raise ContractError("Controller control path is a symbolic link")
            if current.exists():
                if not current.is_dir():
                    raise ContractError("Controller control path is not a directory")
            else:
                current.mkdir()

    def _task_contracts(
        self,
        state: dict[str, object],
    ) -> dict[str, TaskContract]:
        contracts: dict[str, TaskContract] = {}
        for plan in self._load_plans(state):
            contracts.update({task.id: task for task in plan.tasks})
        return contracts

    def _load_plans(
        self,
        state: dict[str, object],
    ) -> tuple[PlanDocument, ...]:
        return tuple(
            self._plan_from_dict(
                parse_json_object_bytes(
                    self._read_ref(
                        ArtifactRef(ref["path"], ref["sha256"])
                    )
                )
            )
            for ref in state["plans"]
        )

    @staticmethod
    def _plan_from_dict(value: dict[str, object]) -> PlanDocument:
        return PlanDocument(
            schema_version=value["schema_version"],
            plan_version=value["plan_version"],
            summary=value["summary"],
            acceptance_criteria=tuple(
                AcceptanceCriterion(
                    item["id"],
                    item["description"],
                )
                for item in value["acceptance_criteria"]
            ),
            global_verification=tuple(value["global_verification"]),
            tasks=tuple(
                TaskContract(
                    id=item["id"],
                    objective=item["objective"],
                    worker_type=item["worker_type"],
                    covers=tuple(item["covers"]),
                    depends_on=tuple(item["depends_on"]),
                    path_scope=tuple(item["path_scope"]),
                    exclusive_resources=tuple(
                        item["exclusive_resources"]
                    ),
                    acceptance_checks=tuple(
                        item["acceptance_checks"]
                    ),
                    max_attempts=item["max_attempts"],
                )
                for item in value["tasks"]
            ),
        )

    @staticmethod
    def _plan_to_dict(plan: PlanDocument) -> dict[str, object]:
        return {
            "schema_version": plan.schema_version,
            "plan_version": plan.plan_version,
            "summary": plan.summary,
            "acceptance_criteria": [
                asdict(item) for item in plan.acceptance_criteria
            ],
            "global_verification": list(plan.global_verification),
            "tasks": [
                Controller._task_to_dict(task)
                for task in plan.tasks
            ],
        }

    @staticmethod
    def _task_to_dict(task: TaskContract) -> dict[str, object]:
        value = asdict(task)
        for key in (
            "covers",
            "depends_on",
            "path_scope",
            "exclusive_resources",
            "acceptance_checks",
        ):
            value[key] = list(value[key])
        return value

    def _evidence_catalog(
        self,
        state: dict[str, object],
    ) -> dict[str, object]:
        catalog: dict[str, object] = {}
        criteria = [
            item.id
            for item in self._load_plans(state)[0].acceptance_criteria
        ]
        global_value = state.get("global_verification")
        if isinstance(global_value, dict):
            reference = global_value["verification"]
            manifest = parse_json_object_bytes(
                self._read_ref(
                    ArtifactRef(
                        reference["path"],
                        reference["sha256"],
                    )
                )
            )
            if (
                manifest["commit_sha"]
                == state["repository"]["integration_head"]
            ):
                for command in manifest["commands"]:
                    evidence_id = (
                        f"global:{command['command_id']}"
                    )
                    catalog[evidence_id] = {
                        "kind": "global",
                        "verification": reference,
                        "integration_head": manifest["commit_sha"],
                        "command_id": command["command_id"],
                        "task_id": None,
                        "attempt_no": None,
                        "criterion_ids": criteria,
                    }
        contracts = self._task_contracts(state)
        for task_id, task in state["tasks"].items():
            reference = task.get("verification")
            if reference is None:
                continue
            manifest = parse_json_object_bytes(
                self._read_ref(
                    ArtifactRef(
                        reference["path"],
                        reference["sha256"],
                    )
                )
            )
            if (
                manifest["commit_sha"]
                != state["repository"]["integration_head"]
            ):
                continue
            for command in manifest["commands"]:
                evidence_id = (
                    f"task:{task_id}:{command['command_id']}"
                )
                catalog[evidence_id] = {
                    "kind": "task",
                    "verification": reference,
                    "integration_head": manifest["commit_sha"],
                    "command_id": command["command_id"],
                    "task_id": task_id,
                    "attempt_no": task["attempt_no"],
                    "criterion_ids": list(
                        contracts[task_id].covers
                    ),
                }
        latest = state.get("latest_evaluation")
        if (
            isinstance(latest, dict)
            and latest.get("verdict") == "UNVERIFIED"
            and latest.get("integration_head")
            == state["repository"]["integration_head"]
        ):
            evaluation_ref = latest.get("evaluation")
            if not isinstance(evaluation_ref, dict):
                raise ContractError(
                    "latest evaluation ArtifactRef is invalid"
                )
            envelope = parse_json_object_bytes(
                self._read_ref(
                    ArtifactRef(
                        evaluation_ref["path"],
                        evaluation_ref["sha256"],
                    )
                )
            )
            requested = set(envelope["evidence_requests"])
            permitted_criteria = [
                item["id"]
                for item in envelope["criteria"]
                if item["verdict"] == "UNVERIFIED"
            ]
            for reference in state["verifications"]:
                if not reference["path"].startswith(
                    "verification/supplemental/"
                ):
                    continue
                manifest = parse_json_object_bytes(
                    self._read_ref(
                        ArtifactRef(
                            reference["path"],
                            reference["sha256"],
                        )
                    )
                )
                if (
                    manifest["commit_sha"]
                    != state["repository"]["integration_head"]
                ):
                    continue
                for command in manifest["commands"]:
                    command_id = command["command_id"]
                    if command_id not in requested:
                        continue
                    catalog[f"supplemental:{command_id}"] = {
                        "kind": "supplemental",
                        "verification": reference,
                        "integration_head": manifest["commit_sha"],
                        "command_id": command_id,
                        "task_id": None,
                        "attempt_no": None,
                        "criterion_ids": permitted_criteria,
                    }
        return catalog

    @staticmethod
    def _validate_evaluation_evidence(
        result: EvaluationResult,
        catalog: dict[str, object],
    ) -> None:
        if result.verdict is not EvaluationVerdict.PASS:
            return
        for criterion in result.criteria:
            for evidence_id in criterion.evidence_ids:
                evidence = catalog.get(evidence_id)
                if (
                    not isinstance(evidence, dict)
                    or criterion.id
                    not in evidence.get("criterion_ids", ())
                ):
                    raise ContractError(
                        f"evaluation evidence is not authorized: {evidence_id}"
                    )

    def _prompt_versions(
        self,
        state: dict[str, object],
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "planner": [],
            "worker": {},
            "evaluator": [],
        }
        for role in ("planner", "evaluator"):
            for reference in state["role_attempts"][role]:
                manifest = parse_json_object_bytes(
                    self._read_ref(
                        ArtifactRef(
                            reference["path"],
                            reference["sha256"],
                        )
                    )
                )
                result[role].append(
                    {
                        "operation_id": manifest["operation_id"],
                        "attempt_no": manifest["attempt_no"],
                        "prompts": manifest["prompt_versions"],
                    }
                )
        for task_id, task in state["tasks"].items():
            values = []
            for reference in task["attempts"]:
                manifest = parse_json_object_bytes(
                    self._read_ref(
                        ArtifactRef(
                            reference["path"],
                            reference["sha256"],
                        )
                    )
                )
                values.append(
                    {
                        "attempt_no": manifest["attempt_no"],
                        "prompts": manifest["prompt_versions"],
                    }
                )
            result["worker"][task_id] = values
        return result

    def _source_audit_ref(
        self,
        state: dict[str, object],
        task_id: str,
        attempt_no: int,
    ) -> ArtifactRef:
        path = (
            f"tasks/{task_id}/attempts/{attempt_no:03d}/"
            "source-audit.json"
        )
        for item in state["artifact_index"]:
            if item["path"] == path:
                return ArtifactRef(item["path"], item["sha256"])
        raise ContractError("source audit ArtifactRef is missing")

    def _read_ref(self, reference: ArtifactRef) -> bytes:
        descriptor = open_absolute_regular_no_follow(
            self._active_store().root.joinpath(
                *PurePosixPath(reference.path).parts
            )
        )
        try:
            body = read_bounded(
                descriptor,
                max_bytes=MAX_ARTIFACT_BYTES,
            )
        finally:
            os.close(descriptor)
        if self._sha256(body) != reference.sha256:
            raise ContractError(
                f"artifact digest mismatch: {reference.path}"
            )
        return body

    def _handle_from_pending(
        self,
        pending: dict[str, object],
    ) -> ProviderHandle | None:
        value = pending["provider_handle"]
        if value is None:
            return None
        identity = self._provider().execution_identity()
        root = self._active_store().root

        def absolute(relative: str) -> str:
            return str(
                root.joinpath(*PurePosixPath(relative).parts)
            )

        return ProviderHandle(
            adapter=value["adapter"],
            attempt_token=value["attempt_token"],
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
            codex_version=identity.codex_version,
            execution_policy_sha256=identity.policy_sha256,
            launch_path=absolute(pending["launch_path"]),
            stdout_path=absolute(pending["stdout_path"]),
            stderr_path=absolute(pending["stderr_path"]),
            exit_path=absolute(pending["exit_path"]),
            result_path=absolute(pending["result_path"]),
        )

    def _pending_for_role(
        self,
        state: dict[str, object],
        role: str,
    ) -> tuple[str, dict[str, object]] | None:
        values = [
            (token, pending)
            for token, pending in state["pending_dispatches"].items()
            if pending["role"] == role
        ]
        if len(values) > 1:
            raise ContractError(f"multiple active {role} dispatches")
        return values[0] if values else None

    def _require_invocation(self, token: str) -> RoleInvocation:
        try:
            return self._invocations[token]
        except KeyError as error:
            raise ContractError(
                "active Provider invocation is unavailable for this Controller"
            ) from error

    def _planner(self) -> PlannerRunner:
        value = self.dependencies.planner
        if value is None:
            raise ContractError("Planner dependency is unavailable")
        return value

    def _worker(self) -> WorkerRunner:
        value = self.dependencies.worker
        if value is None:
            raise ContractError("Worker dependency is unavailable")
        return value

    def _evaluator(self) -> EvaluatorRunner:
        value = self.dependencies.evaluator
        if value is None:
            raise ContractError("Evaluator dependency is unavailable")
        return value

    def _verification(self) -> VerificationGate:
        value = self.dependencies.verification
        if value is None:
            raise ContractError("Verification dependency is unavailable")
        return value

    def _integrator(self):
        value = self.dependencies.integrator
        if value is None:
            raise ContractError("Integrator dependency is unavailable")
        return value

    def _provider(self) -> ProviderAdapter:
        return self._planner().provider

    def _active_store(self) -> StateStore:
        if self._store is None:
            raise ContractError("Controller has no active run store")
        return self._store

    def _active_ledger(self) -> DispatchLedger:
        if self._ledger is None:
            raise ContractError("Controller has no active DispatchLedger")
        return self._ledger

    def _require_runtime_dependencies(self) -> None:
        if any(
            value is None
            for value in (
                self.dependencies.planner,
                self.dependencies.worker,
                self.dependencies.evaluator,
                self.dependencies.verification,
                self.dependencies.integrator,
            )
        ):
            raise ContractError(
                "Controller runtime dependencies are incomplete"
            )

    def _now(self) -> str:
        return self.dependencies.clock().astimezone(
            timezone.utc
        ).isoformat(timespec="microseconds")

    @staticmethod
    def _validate_goal(goal: str) -> str:
        if not isinstance(goal, str) or not goal.strip():
            raise ContractError("Goal must be a non-empty string")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in goal):
            raise ContractError("Goal contains an invalid Unicode scalar")
        return goal

    @staticmethod
    def _sha256(body: bytes) -> str:
        return "sha256:" + hashlib.sha256(body).hexdigest()

    @classmethod
    def _creation_fingerprint(
        cls,
        identity: str,
        base_sha: str,
        goal: str,
        config_body: bytes,
    ) -> str:
        return cls._sha256(
            canonical_json_bytes(
                {
                    "repository_identity": identity,
                    "base_sha": base_sha,
                    "goal_sha256": cls._sha256(
                        goal.encode("utf-8")
                    ),
                    "config_sha256": cls._sha256(config_body),
                }
            )
        )

    @staticmethod
    def _is_pristine_created(state: dict[str, object]) -> bool:
        return bool(
            state["status"] == "CREATED"
            and state["controller"] is None
            and not state["plans"]
            and not state["pending_dispatches"]
        )

    @staticmethod
    def _creation_state_matches(
        state: dict[str, object],
        goal: str,
        identity: str,
        base_sha: str,
        run_ref: str,
        intent_ref: ArtifactRef,
        config_ref: ArtifactRef,
    ) -> bool:
        repository = state["repository"]
        return bool(
            state["goal"] == goal
            and repository["identity"] == identity
            and repository["base_sha"] == base_sha
            and repository["integration_ref"] == run_ref
            and repository["integration_head"] == base_sha
            and state["creation"]["intent"] == intent_ref.as_dict()
            and state["config"] == config_ref.as_dict()
        )

    @staticmethod
    def _refs_digest(refs: Sequence[dict[str, object]]) -> str:
        return Controller._sha256(
            canonical_json_bytes(
                sorted(
                    refs,
                    key=lambda item: (
                        item["path"],
                        item["sha256"],
                    ),
                )
            )
        )

    def _head(self, worktree: Path) -> str:
        return self.dependencies.worktrees.runner.run_local(
            worktree,
            "rev-parse",
            "HEAD",
        ).stdout.decode("ascii").strip()


def evaluation_envelope(
    state: dict[str, object],
    evaluation_round: int,
    evidence_round: int,
    raw_result_ref: ArtifactRef,
    result: EvaluationResult,
    plan_manifest_sha256: str,
    task_result_manifest_sha256: str,
    verification_manifest_sha256: str,
    prompt_versions: dict[str, object],
    evidence_catalog: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": state["run_id"],
        "evaluation_round": evaluation_round,
        "evidence_round": evidence_round,
        "integration_head": state["repository"]["integration_head"],
        "goal_sha256": Controller._sha256(
            state["goal"].encode("utf-8")
        ),
        "plan_manifest_sha256": plan_manifest_sha256,
        "task_result_manifest_sha256": (
            task_result_manifest_sha256
        ),
        "verification_manifest_sha256": (
            verification_manifest_sha256
        ),
        "prompt_versions": prompt_versions,
        "evidence_catalog": evidence_catalog,
        "raw_result_sha256": raw_result_ref.sha256,
        "verdict": result.verdict.value,
        "criteria": [
            {
                "id": item.id,
                "verdict": item.verdict,
                "evidence_ids": list(item.evidence_ids),
            }
            for item in result.criteria
        ],
        "findings": [asdict(item) for item in result.findings],
        "evidence_requests": list(result.evidence_requests),
        "residual_risks": list(result.residual_risks),
    }
