from __future__ import annotations

import dataclasses
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from vibe.models import (
    AcceptanceCriterion,
    ContractError,
    FrozenRunConfig,
    PlanDocument,
    TaskContract,
)
from vibe.prompt_registry import (
    PromptRegistry,
    parse_single_json_object,
)
from vibe.providers.base import (
    ProviderAdapter,
    ProviderHandle,
)
from vibe.runners import (
    DispatchLedger,
    ReadOnlyAudit,
    RoleInvocation,
    require_operation_id,
)
from vibe.scheduler import Scheduler
from vibe.state_store import canonical_json_bytes


WORKER_TYPES = {
    "implementation",
    "testing",
    "performance",
    "code-quality",
    "documentation",
    "general",
}


class PlannerRunner:
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
        read_only_audit: ReadOnlyAudit,
        scheduler: Scheduler | None = None,
        prior_plans: Sequence[PlanDocument] = (),
    ) -> None:
        self.registry = registry
        self.provider = provider
        self.target_root = target_root.resolve()
        self.run_root = run_root
        self.expected_base = expected_base
        self.config = config
        self.config_sha256 = config_sha256
        self.read_only_audit = read_only_audit
        self.scheduler = scheduler or Scheduler(WORKER_TYPES)
        self.prior_plans = tuple(prior_plans)
        self._audits: dict[
            str,
            tuple[dict[str, object], RoleInvocation],
        ] = {}

    def prepare(
        self,
        *,
        run_id: str,
        operation_id: str,
        attempt_no: int,
        attempt_created_at: str,
        attempt_token: str,
        worktree: Path,
        context: dict[str, object],
        artifact_prefix: str,
        provider_retry_no: int = 0,
        timeout_seconds: int = 900,
    ) -> RoleInvocation:
        del run_id
        require_operation_id(operation_id)
        relative_worktree = _relative_worktree(
            worktree,
            self.target_root,
        )
        enriched = {
            **context,
            "authorized_command_ids": [
                {
                    "id": command.id,
                    "purpose": command.purpose,
                }
                for command in self.config.command_catalog
            ],
            "required_command_ids": list(
                self.config.required_command_ids
            ),
            "config_sha256": self.config_sha256,
        }
        rendered = self.registry.compose_planner(enriched)
        execution = self.provider.execution_identity()
        return RoleInvocation(
            role="planner",
            task_id=None,
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
            expected_base=self.expected_base,
            branch=None,
            worktree=relative_worktree,
            target_root=str(self.target_root),
            run_root=str(self.run_root),
            prompt_body=rendered.body,
            prompt_versions=rendered.prompts,
            schema_body=rendered.schema_path.read_bytes(),
            preflight_body=canonical_json_bytes(
                {
                    "role": "planner",
                    "attempt_created_at": attempt_created_at,
                    "expected_base": self.expected_base,
                    "worktree": relative_worktree,
                }
            ),
            authorized_command_ids=tuple(
                command.id
                for command in self.config.command_catalog
            ),
            required_command_ids=(
                self.config.required_command_ids
            ),
            config_sha256=self.config_sha256,
            codex_version=execution.codex_version,
            execution_policy_sha256=execution.policy_sha256,
            sandbox="read-only",
            artifact_prefix=artifact_prefix,
            timeout_seconds=_positive_int(
                timeout_seconds,
                "timeout_seconds",
            ),
        )

    def start(
        self,
        invocation: RoleInvocation,
        ledger: DispatchLedger,
    ) -> ProviderHandle:
        worktree = Path(invocation.target_root).joinpath(
            *PurePosixPath(invocation.worktree).parts
        )
        before = self.read_only_audit.capture(worktree)
        audited = dataclasses.replace(
            invocation,
            preflight_body=canonical_json_bytes(
                {
                    "role": invocation.role,
                    "attempt_created_at": (
                        invocation.attempt_created_at
                    ),
                    "expected_base": invocation.expected_base,
                    "worktree": invocation.worktree,
                    "audit": before,
                }
            ),
        )
        self._audits[invocation.attempt_token] = (
            before,
            audited,
        )
        return ledger.dispatch(audited, self.provider.start)

    def result(
        self,
        invocation: RoleInvocation,
        handle: ProviderHandle,
        ledger: DispatchLedger,
    ) -> PlanDocument:
        before, audited = self._audits.get(
            invocation.attempt_token,
            ({}, invocation),
        )
        ledger.bind_completion(audited, handle)
        if before:
            worktree = Path(audited.target_root).joinpath(
                *PurePosixPath(audited.worktree).parts
            )
            after = self.read_only_audit.capture(worktree)
            self.read_only_audit.assert_unchanged(
                before,
                after,
            )
        return self.parse_result(
            self.provider.result(handle).body
        )

    def parse_result(self, body: bytes) -> PlanDocument:
        raw = _exact_object(
            parse_single_json_object(body),
            "plan",
            {
                "schema_version",
                "plan_version",
                "summary",
                "acceptance_criteria",
                "global_verification",
                "tasks",
            },
        )
        if (
            type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
        ):
            raise ContractError(
                "plan schema_version must be integer 1"
            )
        plan_version = _positive_int(
            raw["plan_version"],
            "plan_version",
        )
        summary = _nonempty_string(raw["summary"], "summary")
        criteria_values = _list(
            raw["acceptance_criteria"],
            "acceptance_criteria",
        )
        if not criteria_values:
            raise ContractError(
                "plan acceptance_criteria cannot be empty"
            )
        criteria: list[AcceptanceCriterion] = []
        criterion_ids: set[str] = set()
        for value in criteria_values:
            item = _exact_object(
                value,
                "acceptance criterion",
                {"id", "description"},
            )
            criterion_id = _canonical_id(
                item["id"],
                "AC-",
                "criterion ID",
            )
            if criterion_id in criterion_ids:
                raise ContractError(
                    "acceptance criterion IDs must be unique"
                )
            criterion_ids.add(criterion_id)
            criteria.append(
                AcceptanceCriterion(
                    criterion_id,
                    _nonempty_string(
                        item["description"],
                        "criterion description",
                    ),
                )
            )
        global_verification = _command_ids(
            raw["global_verification"],
            self.config,
            "global_verification",
        )
        task_values = _list(raw["tasks"], "tasks")
        if (
            not task_values
            or len(task_values) > self.config.max_plan_tasks
        ):
            raise ContractError(
                "plan task count is outside the frozen limit"
            )
        tasks: list[TaskContract] = []
        task_ids: set[str] = set()
        for value in task_values:
            item = _exact_object(
                value,
                "task",
                {
                    "id",
                    "objective",
                    "worker_type",
                    "covers",
                    "depends_on",
                    "path_scope",
                    "exclusive_resources",
                    "acceptance_checks",
                    "max_attempts",
                },
            )
            task_id = _canonical_id(
                item["id"],
                "TASK-",
                "task ID",
            )
            if task_id in task_ids:
                raise ContractError("task IDs must be unique")
            task_ids.add(task_id)
            worker_type = item["worker_type"]
            if worker_type not in WORKER_TYPES:
                raise ContractError("task worker_type is invalid")
            covers = _unique_strings(
                item["covers"],
                "task covers",
            )
            if not covers or not set(covers).issubset(
                criterion_ids
            ):
                raise ContractError(
                    "task covers unknown acceptance criteria"
                )
            path_scope = _unique_strings(
                item["path_scope"],
                "task path_scope",
            )
            if not path_scope:
                raise ContractError(
                    "task path_scope cannot be empty"
                )
            for path in path_scope:
                _relative_path(path, "task path_scope")
            maximum = _positive_int(
                item["max_attempts"],
                "task max_attempts",
            )
            if maximum > self.config.task_attempts:
                raise ContractError(
                    "task max_attempts exceeds frozen limit"
                )
            tasks.append(
                TaskContract(
                    id=task_id,
                    objective=_nonempty_string(
                        item["objective"],
                        "task objective",
                    ),
                    worker_type=worker_type,
                    covers=covers,
                    depends_on=_unique_strings(
                        item["depends_on"],
                        "task depends_on",
                    ),
                    path_scope=path_scope,
                    exclusive_resources=_unique_strings(
                        item["exclusive_resources"],
                        "task exclusive_resources",
                    ),
                    acceptance_checks=_command_ids(
                        item["acceptance_checks"],
                        self.config,
                        "task acceptance_checks",
                    ),
                    max_attempts=maximum,
                )
            )
        document = PlanDocument(
            schema_version=1,
            plan_version=plan_version,
            summary=summary,
            acceptance_criteria=tuple(criteria),
            global_verification=global_verification,
            tasks=tuple(tasks),
        )
        self.scheduler.validate_plan(
            document,
            self.config,
            self.prior_plans,
        )
        return document


def _exact_object(
    value: object,
    field: str,
    expected: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContractError(f"{field} fields are invalid")
    return value


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ContractError(f"{field} must be an array")
    return value


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be non-empty")
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ContractError(
            f"{field} must be a positive integer"
        )
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ContractError(
            f"{field} must be a non-negative integer"
        )
    return value


def _unique_strings(
    value: object,
    field: str,
) -> tuple[str, ...]:
    items = _list(value, field)
    if (
        not all(isinstance(item, str) for item in items)
        or len(set(items)) != len(items)
    ):
        raise ContractError(
            f"{field} must contain unique strings"
        )
    return tuple(items)


def _command_ids(
    value: object,
    config: FrozenRunConfig,
    field: str,
) -> tuple[str, ...]:
    ids = _unique_strings(value, field)
    catalog = {
        command.id for command in config.command_catalog
    }
    if not set(ids).issubset(catalog):
        raise ContractError(
            f"{field} contains an unknown command ID"
        )
    return ids


def _canonical_id(
    value: object,
    prefix: str,
    field: str,
) -> str:
    raw = _nonempty_string(value, field)
    if (
        len(raw) != len(prefix) + 3
        or not raw.startswith(prefix)
        or not raw[-3:].isdigit()
    ):
        raise ContractError(f"{field} is invalid")
    return raw


def _relative_path(value: str, field: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or "." in path.parts
        or value != path.as_posix()
    ):
        raise ContractError(f"{field} is invalid")
    return value


def _relative_worktree(
    worktree: Path,
    target_root: Path,
) -> str:
    resolved = worktree.resolve(strict=True)
    try:
        relative = resolved.relative_to(target_root)
    except ValueError as error:
        raise ContractError(
            "worktree must stay below target_root"
        ) from error
    return _relative_path(relative.as_posix(), "worktree")
