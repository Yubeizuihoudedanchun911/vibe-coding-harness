from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from vibe.models import (
    ContractError,
    FrozenRunConfig,
    TaskContract,
    WorkerCheck,
    WorkerResult,
)
from vibe.prompt_registry import (
    PromptRegistry,
    collect_repository_instructions,
    parse_single_json_object,
)
from vibe.providers.base import ProviderAdapter
from vibe.runners import (
    RoleInvocation,
    require_operation_id,
)
from vibe.runners.planner import (
    _exact_object,
    _list,
    _nonempty_string,
    _nonnegative_int,
    _positive_int,
    _relative_path,
    _relative_worktree,
    _unique_strings,
)
from vibe.state_store import canonical_json_bytes


class WorkerRunner:
    def __init__(
        self,
        *,
        registry: PromptRegistry,
        provider: ProviderAdapter,
        target_root: Path,
        run_root: Path,
        expected_base: str,
        config: FrozenRunConfig,
        config_sha256: str,
    ) -> None:
        self.registry = registry
        self.provider = provider
        self.target_root = target_root.resolve()
        self.run_root = run_root
        self.expected_base = expected_base
        self.config = config
        self.config_sha256 = config_sha256

    def prepare(
        self,
        *,
        run_id: str,
        task: TaskContract,
        operation_id: str,
        attempt_no: int,
        attempt_created_at: str,
        attempt_token: str,
        worktree: Path,
        task_base_sha: str,
        previous_failure: object,
        artifact_prefix: str,
        provider_retry_no: int = 0,
        timeout_seconds: int = 1800,
    ) -> RoleInvocation:
        require_operation_id(operation_id)
        relative_worktree = _relative_worktree(
            worktree,
            self.target_root,
        )
        repository_instructions = (
            collect_repository_instructions(
                worktree,
                task.path_scope,
            )
        )
        task_contract = {
            "id": task.id,
            "objective": task.objective,
            "worker_type": task.worker_type,
            "covers": list(task.covers),
            "depends_on": list(task.depends_on),
            "path_scope": list(task.path_scope),
            "exclusive_resources": list(
                task.exclusive_resources
            ),
            "acceptance_checks": list(
                task.acceptance_checks
            ),
            "max_attempts": task.max_attempts,
        }
        branch = (
            f"refs/heads/vibe/{run_id}/"
            f"{task.id}-a{attempt_no}"
        )
        context = {
            "repository_instructions": repository_instructions,
            "task_contract": task_contract,
            "execution": {
                "operation_id": operation_id,
                "attempt_no": attempt_no,
                "attempt_token": attempt_token,
                "task_base_sha": task_base_sha,
                "branch": branch,
                "worktree": relative_worktree,
                "authorized_command_ids": list(
                    task.acceptance_checks
                ),
                "config_sha256": self.config_sha256,
            },
            "previous_failure": previous_failure,
        }
        rendered = self.registry.compose_worker(
            task.worker_type,
            context,
        )
        execution = self.provider.execution_identity()
        return RoleInvocation(
            role="worker",
            task_id=task.id,
            operation_id=operation_id,
            attempt_no=_positive_int(
                attempt_no,
                "attempt_no",
            ),
            attempt_created_at=attempt_created_at,
            attempt_token=attempt_token,
            provider_retry_no=_nonnegative_int(
                provider_retry_no,
                "provider_retry_no",
            ),
            expected_base=task_base_sha,
            branch=branch,
            worktree=relative_worktree,
            target_root=str(self.target_root),
            run_root=str(self.run_root),
            prompt_body=rendered.body,
            prompt_versions=rendered.prompts,
            schema_body=rendered.schema_path.read_bytes(),
            preflight_body=canonical_json_bytes(
                {
                    "role": "worker",
                    "task_id": task.id,
                    "operation_id": operation_id,
                    "attempt_no": attempt_no,
                    "attempt_created_at": attempt_created_at,
                    "task_base_sha": task_base_sha,
                    "branch": branch,
                    "worktree": relative_worktree,
                }
            ),
            authorized_command_ids=task.acceptance_checks,
            required_command_ids=tuple(
                command_id
                for command_id
                in self.config.required_command_ids
                if command_id in task.acceptance_checks
            ),
            config_sha256=self.config_sha256,
            codex_version=execution.codex_version,
            execution_policy_sha256=execution.policy_sha256,
            sandbox="workspace-write",
            artifact_prefix=artifact_prefix,
            timeout_seconds=_positive_int(
                timeout_seconds,
                "timeout_seconds",
            ),
        )

    def parse_result(
        self,
        invocation: RoleInvocation,
        body: bytes,
    ) -> WorkerResult:
        raw = _exact_object(
            parse_single_json_object(body),
            "Worker result",
            {
                "schema_version",
                "task_id",
                "attempt_no",
                "attempt_token",
                "status",
                "task_base_sha",
                "changed_paths",
                "checks",
                "residual_risks",
                "blocker",
            },
        )
        if (
            type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
        ):
            raise ContractError(
                "Worker result schema_version is invalid"
            )
        if (
            raw["task_id"] != invocation.task_id
            or raw["attempt_no"] != invocation.attempt_no
            or raw["attempt_token"]
            != invocation.attempt_token
            or raw["task_base_sha"]
            != invocation.expected_base
        ):
            raise ContractError(
                "Worker result task identity does not match assignment"
            )
        status = raw["status"]
        if status not in {"COMPLETED", "BLOCKED"}:
            raise ContractError("Worker result status is invalid")
        changed_paths = _unique_strings(
            raw["changed_paths"],
            "changed_paths",
        )
        for path in changed_paths:
            _relative_path(path, "changed path")
        checks: list[WorkerCheck] = []
        command_ids: set[str] = set()
        for value in _list(raw["checks"], "checks"):
            item = _exact_object(
                value,
                "Worker check",
                {"command_id", "exit_code", "summary"},
            )
            command_id = _nonempty_string(
                item["command_id"],
                "command_id",
            )
            if (
                command_id
                not in invocation.authorized_command_ids
                or command_id in command_ids
            ):
                raise ContractError(
                    "Worker check command ID is unauthorized or duplicate"
                )
            command_ids.add(command_id)
            if type(item["exit_code"]) is not int:
                raise ContractError(
                    "Worker check exit_code must be an integer"
                )
            checks.append(
                WorkerCheck(
                    command_id=command_id,
                    exit_code=item["exit_code"],
                    summary=_nonempty_string(
                        item["summary"],
                        "check summary",
                    ),
                )
            )
        residual = _unique_strings(
            raw["residual_risks"],
            "residual_risks",
        )
        blocker = raw["blocker"]
        if status == "BLOCKED":
            blocker = _nonempty_string(blocker, "blocker")
        elif blocker is not None:
            raise ContractError(
                "completed Worker result cannot contain a blocker"
            )
        return WorkerResult(
            schema_version=1,
            task_id=invocation.task_id or "",
            attempt_no=invocation.attempt_no,
            attempt_token=invocation.attempt_token,
            status=status,
            task_base_sha=invocation.expected_base,
            changed_paths=changed_paths,
            checks=tuple(checks),
            residual_risks=residual,
            blocker=blocker,
        )
