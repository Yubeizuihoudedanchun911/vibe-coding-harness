from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from vibe.models import (
    CommandAuthorization,
    CommandSpec,
    ContractError,
    FrozenRunConfig,
)
from vibe.state_store import (
    canonical_json_bytes,
    open_absolute_directory_no_follow,
    parse_json_object_bytes,
    read_optional_regular_at,
)


ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
COMMAND_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
PROJECT_TOP_FIELDS = {"provider", "scheduler", "limits", "verification"}
FROZEN_TOP_FIELDS = PROJECT_TOP_FIELDS
LIMIT_FIELDS = {
    "task_attempts",
    "provider_retries",
    "evidence_rounds",
    "repair_rounds",
    "max_plan_tasks",
}

EMPTY_COMMAND_AUTHORIZATION = CommandAuthorization(
    mode="EMPTY",
    source_path=None,
    source_sha256=(
        "sha256:"
        "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"
    ),
)

DEFAULT_CONFIG = FrozenRunConfig(
    provider_name="codex-cli",
    max_workers=4,
    task_attempts=3,
    provider_retries=3,
    evidence_rounds=3,
    repair_rounds=3,
    max_plan_tasks=128,
    command_catalog=(),
    required_command_ids=(),
    command_authorization=EMPTY_COMMAND_AUTHORIZATION,
)


def read_optional_project_source(
    target: Path,
    leaf: str,
    *,
    max_bytes: int,
) -> bytes | None:
    if leaf != "vibe.json":
        raise ContractError("unsupported project config leaf")
    root_fd = open_absolute_directory_no_follow(target.resolve())
    try:
        return read_optional_regular_at(
            root_fd,
            leaf,
            max_bytes=max_bytes,
        )
    finally:
        os.close(root_fd)


def _parse_command_authorization(value: object) -> CommandAuthorization:
    raw = _object(
        value,
        "verification.authorization",
        {"mode", "source_path", "source_sha256"},
    )
    mode = raw.get("mode")
    source_path = raw.get("source_path")
    digest = raw.get("source_sha256")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise ContractError("authorization source_sha256 is invalid")
    if mode == "EMPTY":
        if (
            source_path is not None
            or digest != EMPTY_COMMAND_AUTHORIZATION.source_sha256
        ):
            raise ContractError("EMPTY authorization fields are invalid")
    elif mode == "EXPLICIT_PROJECT_FILE":
        if source_path != "vibe.json":
            raise ContractError(
                "project authorization source must be vibe.json"
            )
    else:
        raise ContractError("command authorization mode is invalid")
    return CommandAuthorization(
        mode=mode,
        source_path=source_path,
        source_sha256=digest,
    )


def command_authorization_for_project_source(
    *,
    path: Path | None,
    exact_source_bytes: bytes,
    commands_present: bool,
) -> CommandAuthorization:
    if not commands_present:
        return EMPTY_COMMAND_AUTHORIZATION
    if path is None or path.name != "vibe.json":
        raise ContractError("authorized commands require a vibe.json source")
    return CommandAuthorization(
        mode="EXPLICIT_PROJECT_FILE",
        source_path="vibe.json",
        source_sha256=(
            "sha256:" + hashlib.sha256(exact_source_bytes).hexdigest()
        ),
    )


def _plain_positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ContractError(f"{field} must be a positive integer")
    return value


def _object(
    value: object,
    field: str,
    allowed: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ContractError(f"unknown {field} fields: {sorted(unknown)}")
    return value


def _command(value: object) -> CommandSpec:
    raw = _object(
        value,
        "command",
        {
            "id",
            "purpose",
            "argv",
            "cwd",
            "timeout_seconds",
            "env_allowlist",
        },
    )
    command_id = raw.get("id")
    if (
        not isinstance(command_id, str)
        or COMMAND_ID_RE.fullmatch(command_id) is None
    ):
        raise ContractError("command.id must be a canonical stable ID")
    purpose = raw.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        raise ContractError("command.purpose must be a non-empty string")
    argv = raw.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise ContractError(
            "command.argv must be a non-empty string array"
        )
    cwd = raw.get("cwd", ".")
    if not isinstance(cwd, str):
        raise ContractError("command.cwd must be a string")
    path = PurePosixPath(cwd)
    if (
        path.is_absolute()
        or ".." in path.parts
        or cwd != path.as_posix()
        or cwd.startswith(".vibe-coding")
    ):
        raise ContractError(
            "command.cwd must stay inside the product repository"
        )
    timeout = _plain_positive_int(
        raw.get("timeout_seconds", 900),
        "command.timeout_seconds",
    )
    env = raw.get("env_allowlist", [])
    if (
        not isinstance(env, list)
        or not all(
            isinstance(name, str) and ENV_NAME_RE.fullmatch(name)
            for name in env
        )
        or len(set(env)) != len(env)
    ):
        raise ContractError(
            "command.env_allowlist contains an invalid name"
        )
    return CommandSpec(
        id=command_id,
        purpose=purpose,
        argv=tuple(argv),
        cwd=cwd,
        timeout_seconds=timeout,
        env_allowlist=tuple(env),
    )


def _parse_config(
    value: object,
    *,
    frozen: bool,
    authorization: CommandAuthorization | None,
) -> FrozenRunConfig:
    root = _object(
        value,
        "config",
        FROZEN_TOP_FIELDS if frozen else PROJECT_TOP_FIELDS,
    )
    provider = _object(root.get("provider", {}), "provider", {"name"})
    scheduler = _object(
        root.get("scheduler", {}),
        "scheduler",
        {"max_workers"},
    )
    limits = _object(
        root.get("limits", {}),
        "limits",
        LIMIT_FIELDS,
    )
    verification = _object(
        root.get("verification", {}),
        "verification",
        (
            {
                "command_catalog",
                "required_command_ids",
                "authorization",
            }
            if frozen
            else {"command_catalog", "required_command_ids"}
        ),
    )
    provider_name = provider.get("name", DEFAULT_CONFIG.provider_name)
    if provider_name != "codex-cli":
        raise ContractError("V1 provider.name must be codex-cli")
    commands = verification.get("command_catalog", [])
    if not isinstance(commands, list):
        raise ContractError(
            "verification.command_catalog must be an array"
        )
    catalog = tuple(_command(item) for item in commands)
    if len({command.id for command in catalog}) != len(catalog):
        raise ContractError(
            "verification.command_catalog IDs must be unique"
        )
    required_ids = verification.get("required_command_ids", [])
    if (
        not isinstance(required_ids, list)
        or not all(isinstance(item, str) for item in required_ids)
        or len(set(required_ids)) != len(required_ids)
    ):
        raise ContractError(
            "verification.required_command_ids must be unique IDs"
        )
    catalog_ids = {command.id for command in catalog}
    if not set(required_ids).issubset(catalog_ids):
        raise ContractError(
            "every required command ID must exist in the catalog"
        )
    if frozen:
        authorization = _parse_command_authorization(
            verification.get("authorization")
        )
    if authorization is None:
        raise ContractError("command authorization is required")
    if frozen:
        expected_mode = (
            "EXPLICIT_PROJECT_FILE" if catalog or required_ids else "EMPTY"
        )
        if authorization.mode != expected_mode:
            raise ContractError(
                "command catalog and authorization mode are inconsistent"
            )
    return FrozenRunConfig(
        provider_name=provider_name,
        max_workers=_plain_positive_int(
            scheduler.get("max_workers", DEFAULT_CONFIG.max_workers),
            "scheduler.max_workers",
        ),
        task_attempts=_plain_positive_int(
            limits.get("task_attempts", DEFAULT_CONFIG.task_attempts),
            "limits.task_attempts",
        ),
        provider_retries=_plain_positive_int(
            limits.get(
                "provider_retries",
                DEFAULT_CONFIG.provider_retries,
            ),
            "limits.provider_retries",
        ),
        evidence_rounds=_plain_positive_int(
            limits.get("evidence_rounds", DEFAULT_CONFIG.evidence_rounds),
            "limits.evidence_rounds",
        ),
        repair_rounds=_plain_positive_int(
            limits.get("repair_rounds", DEFAULT_CONFIG.repair_rounds),
            "limits.repair_rounds",
        ),
        max_plan_tasks=_plain_positive_int(
            limits.get("max_plan_tasks", DEFAULT_CONFIG.max_plan_tasks),
            "limits.max_plan_tasks",
        ),
        command_catalog=catalog,
        required_command_ids=tuple(required_ids),
        command_authorization=authorization,
    )


def parse_project_config(
    value: object,
    authorization: CommandAuthorization,
) -> FrozenRunConfig:
    return _parse_config(
        value,
        frozen=False,
        authorization=authorization,
    )


def parse_frozen_config(value: object) -> FrozenRunConfig:
    return _parse_config(
        value,
        frozen=True,
        authorization=None,
    )


def load_run_config(
    target: Path,
    overrides: Mapping[str, int | str | bool | None],
) -> FrozenRunConfig:
    source = read_optional_project_source(
        target,
        "vibe.json",
        max_bytes=4 * 1024 * 1024,
    )
    if source is not None:
        source_path: Path | None = target / "vibe.json"
        source_bytes = source
        value = parse_json_object_bytes(source_bytes)
    else:
        source_path = None
        source_bytes = b"{}\n"
        value = {}
    provisional = parse_project_config(
        value,
        DEFAULT_CONFIG.command_authorization,
    )
    commands_present = bool(
        provisional.command_catalog or provisional.required_command_ids
    )
    if (
        commands_present
        and overrides.get("allow_project_commands") is not True
    ):
        raise ContractError(
            "non-empty project commands require "
            "--allow-project-commands"
        )
    authorization = command_authorization_for_project_source(
        path=source_path,
        exact_source_bytes=source_bytes,
        commands_present=commands_present,
    )
    config = parse_project_config(value, authorization)
    replaced = config.as_dict()
    override_map = {
        "max_workers": ("scheduler", "max_workers"),
        "task_attempts": ("limits", "task_attempts"),
        "provider_retries": ("limits", "provider_retries"),
        "evidence_rounds": ("limits", "evidence_rounds"),
        "repair_rounds": ("limits", "repair_rounds"),
        "max_plan_tasks": ("limits", "max_plan_tasks"),
    }
    unknown = set(overrides) - set(override_map) - {
        "allow_project_commands"
    }
    if unknown:
        raise ContractError(f"unknown config overrides: {sorted(unknown)}")
    for name, (section, field) in override_map.items():
        override = overrides.get(name)
        if override is not None:
            replaced[section][field] = override
    return parse_frozen_config(replaced)


def frozen_config_bytes(config: FrozenRunConfig) -> bytes:
    return canonical_json_bytes(config.as_dict())


def resolve_command_ids(
    config: FrozenRunConfig,
    ids: Sequence[str],
) -> tuple[CommandSpec, ...]:
    if len(set(ids)) != len(ids):
        raise ContractError("command IDs must be unique")
    catalog = {
        command.id: command
        for command in config.command_catalog
    }
    try:
        return tuple(catalog[command_id] for command_id in ids)
    except KeyError as error:
        raise ContractError(
            f"unknown command ID: {error.args[0]}"
        ) from error


def effective_command_ids(
    config: FrozenRunConfig,
    additions: Sequence[str],
) -> tuple[str, ...]:
    requested = tuple(additions)
    resolve_command_ids(config, requested)
    combined = tuple(
        dict.fromkeys(config.required_command_ids + requested)
    )
    resolve_command_ids(config, combined)
    return combined
