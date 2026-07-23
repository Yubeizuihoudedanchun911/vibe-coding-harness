from __future__ import annotations

import heapq
import re
from pathlib import PurePosixPath
from typing import Iterable, Sequence

from vibe.config import resolve_command_ids
from vibe.models import (
    ACCEPTANCE_ID_RE,
    TASK_ID_RE,
    ContractError,
    FrozenRunConfig,
    PlanDocument,
    TaskContract,
)


DEFAULT_WORKER_TYPES = frozenset(
    {
        "implementation",
        "testing",
        "performance",
        "code-quality",
        "documentation",
        "general",
    }
)
RESOURCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


def normalize_scope(value: str) -> str:
    if value == ".":
        return value
    if not value or value.startswith("/") or "\\" in value:
        raise ContractError(f"invalid path scope: {value!r}")
    directory = value.endswith("/")
    raw = value[:-1] if directory else value
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or raw != path.as_posix()
        or path.parts[0] == ".vibe-coding"
        or path.parts[0] == ".git"
    ):
        raise ContractError(f"invalid path scope: {value!r}")
    return path.as_posix() + ("/" if directory else "")


def path_matches_scope(path: str, scope: str) -> bool:
    if scope == ".":
        return True
    if scope.endswith("/"):
        return path.startswith(scope)
    return path == scope


def effective_global_verification(
    config: FrozenRunConfig,
    plans: Sequence[PlanDocument],
) -> tuple[str, ...]:
    command_ids = config.required_command_ids
    for plan in sorted(plans, key=lambda item: item.plan_version):
        resolve_command_ids(config, plan.global_verification)
        command_ids = tuple(
            dict.fromkeys(command_ids + plan.global_verification)
        )
    resolve_command_ids(config, command_ids)
    return command_ids


class Scheduler:
    def __init__(
        self,
        registered_worker_types: Iterable[str] = DEFAULT_WORKER_TYPES,
    ) -> None:
        self.registered_worker_types = frozenset(registered_worker_types)
        if not self.registered_worker_types:
            raise ContractError("registered Worker types cannot be empty")

    def validate_plan(
        self,
        document: PlanDocument,
        config: FrozenRunConfig,
        prior_plans: Sequence[PlanDocument],
    ) -> None:
        plans = tuple(sorted(prior_plans, key=lambda item: item.plan_version))
        self._validate_versions(document, plans)
        self._validate_acceptance(document, plans)
        prior_tasks = tuple(
            task for plan in plans for task in plan.tasks
        )
        if len(prior_tasks) + len(document.tasks) > config.max_plan_tasks:
            raise ContractError("global Planner task limit exceeded")
        prior_ids = {task.id for task in prior_tasks}
        if len(prior_ids) != len(prior_tasks):
            raise ContractError("prior Plan task IDs are not globally unique")
        candidate_ids = [task.id for task in document.tasks]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ContractError("task IDs must be unique")
        overlap = prior_ids.intersection(candidate_ids)
        if overlap:
            raise ContractError(
                "repair Plan cannot rewrite a completed or prior task: "
                + ", ".join(sorted(overlap))
            )
        all_tasks = prior_tasks + document.tasks
        all_ids = {task.id for task in all_tasks}
        criteria_ids = {
            criterion.id for criterion in document.acceptance_criteria
        }
        for task in document.tasks:
            self._validate_task(task, config, criteria_ids, all_ids)
        self._topological_order(all_tasks)
        covered = {
            criterion
            for task in all_tasks
            for criterion in task.covers
        }
        missing = criteria_ids - covered
        if missing:
            raise ContractError(
                "acceptance criteria are not fully covered: "
                + ", ".join(sorted(missing))
            )
        resolve_command_ids(config, document.global_verification)
        effective_global_verification(config, plans + (document,))

    def topological_order(
        self,
        document: PlanDocument,
    ) -> tuple[str, ...]:
        return self._topological_order(document.tasks)

    def _validate_versions(
        self,
        document: PlanDocument,
        prior_plans: tuple[PlanDocument, ...],
    ) -> None:
        if document.schema_version != 1:
            raise ContractError("plan schema_version must be 1")
        for index, plan in enumerate(prior_plans, start=1):
            if plan.schema_version != 1 or plan.plan_version != index:
                raise ContractError("prior Plan versions are not contiguous")
        expected = len(prior_plans) + 1
        if document.plan_version != expected:
            raise ContractError(
                f"plan_version must equal prior maximum plus one: {expected}"
            )

    @staticmethod
    def _validate_acceptance(
        document: PlanDocument,
        prior_plans: tuple[PlanDocument, ...],
    ) -> None:
        criteria = document.acceptance_criteria
        if not criteria:
            raise ContractError("acceptance criteria cannot be empty")
        ids = [criterion.id for criterion in criteria]
        if len(set(ids)) != len(ids):
            raise ContractError("acceptance criterion IDs must be unique")
        for criterion in criteria:
            if ACCEPTANCE_ID_RE.fullmatch(criterion.id) is None:
                raise ContractError("acceptance criterion ID is invalid")
            if not criterion.description.strip():
                raise ContractError("acceptance criterion description is empty")
        if prior_plans:
            original = prior_plans[0].acceptance_criteria
            if criteria != original:
                raise ContractError(
                    "repair Plan acceptance criteria must equal the original"
                )
            for plan in prior_plans[1:]:
                if plan.acceptance_criteria != original:
                    raise ContractError(
                        "prior repair acceptance criteria changed"
                    )

    def _validate_task(
        self,
        task: TaskContract,
        config: FrozenRunConfig,
        criteria_ids: set[str],
        all_ids: set[str],
    ) -> None:
        if TASK_ID_RE.fullmatch(task.id) is None:
            raise ContractError(f"invalid task ID: {task.id}")
        if not task.objective.strip():
            raise ContractError(f"task {task.id} objective is empty")
        if task.worker_type not in self.registered_worker_types:
            raise ContractError(f"task {task.id} Worker type is not registered")
        if (
            not task.covers
            or len(set(task.covers)) != len(task.covers)
            or not set(task.covers).issubset(criteria_ids)
        ):
            raise ContractError(f"task {task.id} has invalid acceptance coverage")
        if (
            len(set(task.depends_on)) != len(task.depends_on)
            or task.id in task.depends_on
            or not set(task.depends_on).issubset(all_ids)
        ):
            raise ContractError(f"task {task.id} has an invalid dependency")
        if not task.path_scope:
            raise ContractError(f"task {task.id} path scope is empty")
        scopes = tuple(normalize_scope(scope) for scope in task.path_scope)
        if len(set(scopes)) != len(scopes):
            raise ContractError(f"task {task.id} path scopes are duplicated")
        if "." in scopes and len(scopes) != 1:
            raise ContractError(f"task {task.id} whole-repository scope is not exclusive")
        if len(set(task.exclusive_resources)) != len(task.exclusive_resources):
            raise ContractError(f"task {task.id} resources are duplicated")
        for resource in task.exclusive_resources:
            if RESOURCE_RE.fullmatch(resource) is None:
                raise ContractError(
                    f"task {task.id} has an invalid exclusive resource"
                )
        if not 1 <= task.max_attempts <= config.task_attempts:
            raise ContractError(f"task {task.id} max_attempts is invalid")
        resolve_command_ids(config, task.acceptance_checks)

    @staticmethod
    def _topological_order(
        tasks: Sequence[TaskContract],
    ) -> tuple[str, ...]:
        by_id = {task.id: task for task in tasks}
        if len(by_id) != len(tasks):
            raise ContractError("task IDs must be unique")
        indegree = {task.id: 0 for task in tasks}
        followers: dict[str, list[str]] = {
            task.id: [] for task in tasks
        }
        for task in tasks:
            for dependency in task.depends_on:
                if dependency not in by_id:
                    continue
                indegree[task.id] += 1
                followers[dependency].append(task.id)
        ready = [task_id for task_id, count in indegree.items() if count == 0]
        heapq.heapify(ready)
        ordered: list[str] = []
        while ready:
            task_id = heapq.heappop(ready)
            ordered.append(task_id)
            for follower in sorted(followers[task_id]):
                indegree[follower] -= 1
                if indegree[follower] == 0:
                    heapq.heappush(ready, follower)
        if len(ordered) != len(tasks):
            raise ContractError("Planner task graph contains a cycle")
        return tuple(ordered)
