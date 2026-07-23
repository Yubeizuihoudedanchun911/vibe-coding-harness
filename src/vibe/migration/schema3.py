from __future__ import annotations

import copy
import fcntl
import hashlib
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping

from vibe.config import frozen_config_bytes, load_run_config
from vibe.models import ContractError, FrozenRunConfig, StateConflictError
from vibe.state_store import (
    StateStore,
    canonical_json_bytes,
    load_json_object,
    parse_json_object_bytes,
)
from vibe.worktrees import WorktreeManager, repository_snapshot


SCHEMA_VERSION = 3
EVALUATION_RECORD_VERSION = 2
INTERRUPTION_RECORD_VERSION = 2
REQUIREMENT_PATTERN = re.compile(r"REQ-(\d+)\Z")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
OID_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
RUN_STATUSES = {"ACTIVE", "BLOCKED", "DEGRADED", "ACCEPTED"}
PHASES = {"PLANNING", "BUILDING", "EVALUATING"}
VERDICTS = {None, "PASS", "FAIL", "UNVERIFIED"}
STATE_FIELDS = {
    "schema_version",
    "requirement_id",
    "goal",
    "status",
    "phase",
    "active_round",
    "next_action",
    "accepted_revision",
    "evaluation",
    "latest_verdict",
    "residual_risks",
    "failed_evaluations",
    "review_attempts",
    "interruption_history",
    "pending_evaluation",
    "pending_review",
    "pending_interruption",
}
OPTIONAL_STATE_FIELDS = {"degradation_acceptance"}


@dataclass(frozen=True)
class Schema3Requirement:
    requirement_id: str
    root: Path


@dataclass(frozen=True)
class LegacyValidation:
    requirement_id: str
    status: str
    goal: str
    source_identity: str
    workspace_snapshot: dict[str, str]
    validated_state: dict[str, object]


@dataclass(frozen=True)
class MigrationEntry:
    requirement_id: str
    source_status: str
    source_identity: str
    base_sha: str
    run_id: str
    migration_id: str
    mapped_status: str
    backup_path: str

    def as_dict(self) -> dict[str, str]:
        return {
            "requirement_id": self.requirement_id,
            "source_status": self.source_status,
            "source_identity": self.source_identity,
            "base_sha": self.base_sha,
            "run_id": self.run_id,
            "migration_id": self.migration_id,
            "mapped_status": self.mapped_status,
            "backup_path": self.backup_path,
        }


@dataclass(frozen=True)
class MigrationManifest:
    schema_version: int
    migration_id: str
    created_at: str
    target_repository_identity: str
    base_sha: str
    entries: tuple[MigrationEntry, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "migration_id": self.migration_id,
            "created_at": self.created_at,
            "target_repository_identity": self.target_repository_identity,
            "base_sha": self.base_sha,
            "entries": [item.as_dict() for item in self.entries],
        }


def _requirement_root(target: Path, requirement_id: str) -> Path:
    if REQUIREMENT_PATTERN.fullmatch(requirement_id) is None:
        raise ContractError("requirement must match REQ-NNN")
    return (
        target
        / ".vibe-coding"
        / "requirements"
        / requirement_id
    )


def discover_requirements(target: Path) -> tuple[str, ...]:
    root = target.resolve() / ".vibe-coding" / "requirements"
    if root.is_symlink():
        raise ContractError("Schema 3 requirements root is a symbolic link")
    if not root.exists():
        return ()
    if not root.is_dir():
        raise ContractError("Schema 3 requirements root is not a directory")
    result: list[tuple[int, str]] = []
    with os.scandir(root) as entries:
        for entry in entries:
            match = REQUIREMENT_PATTERN.fullmatch(entry.name)
            if match is None:
                continue
            if entry.is_symlink():
                raise ContractError(
                    f"Schema 3 requirement is a symbolic link: {entry.name}"
                )
            if not entry.is_dir(follow_symlinks=False):
                raise ContractError(
                    f"Schema 3 requirement is not a directory: {entry.name}"
                )
            result.append((int(match.group(1)), entry.name))
    return tuple(name for _, name in sorted(result))


def _tree_entries(root: Path) -> list[tuple[str, int, int, bytes]]:
    if root.is_symlink() or not root.is_dir():
        raise ContractError(f"Schema 3 requirement is not a safe directory: {root}")
    entries: list[tuple[str, int, int, bytes]] = []

    def walk(directory: Path) -> None:
        with os.scandir(directory) as children:
            ordered = sorted(children, key=lambda item: os.fsencode(item.name))
        for child in ordered:
            path = Path(child.path)
            metadata = child.stat(follow_symlinks=False)
            relative = path.relative_to(root).as_posix()
            kind = stat.S_IFMT(metadata.st_mode)
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                body = os.fsencode(os.readlink(path))
            elif stat.S_ISREG(metadata.st_mode):
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    chunks: list[bytes] = []
                    while True:
                        chunk = os.read(descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    body = b"".join(chunks)
                finally:
                    os.close(descriptor)
            elif stat.S_ISDIR(metadata.st_mode):
                body = b""
            else:
                body = b""
            entries.append((relative, kind, mode, body))
            if stat.S_ISDIR(metadata.st_mode):
                walk(path)

    walk(root)
    return entries


def hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for relative, kind, mode, body in _tree_entries(root):
        for value in (
            relative.encode("utf-8"),
            str(kind).encode("ascii"),
            f"{mode:o}".encode("ascii"),
            body,
        ):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return "sha256:" + digest.hexdigest()


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"Schema 3 validation failed: {field} is invalid")
    return value


def _validate_history_records(
    requirement_root: Path,
    state: Mapping[str, object],
) -> None:
    for field in (
        "residual_risks",
        "failed_evaluations",
        "review_attempts",
        "interruption_history",
    ):
        if not isinstance(state.get(field), list):
            raise ContractError(
                f"Schema 3 validation failed: {field} must be a list"
            )
    for index, item in enumerate(state["failed_evaluations"]):
        if not isinstance(item, dict):
            raise ContractError(
                "Schema 3 validation failed: failed evaluation is invalid"
            )
        evaluation = item.get("evaluation")
        if isinstance(evaluation, dict):
            version = evaluation.get("schema_version")
            if version is not None and version != EVALUATION_RECORD_VERSION:
                raise ContractError(
                    "Schema 3 validation failed: evaluation schema_version "
                    f"must be {EVALUATION_RECORD_VERSION}"
                )
    for path in sorted(requirement_root.rglob("*.json")):
        if path.name == "state.json":
            continue
        value = parse_json_object_bytes(path.read_bytes())
        if path.name == "evaluation.json":
            expected = EVALUATION_RECORD_VERSION
        elif path.name == "interruption.json":
            expected = INTERRUPTION_RECORD_VERSION
        else:
            continue
        if value.get("schema_version") != expected:
            raise ContractError(
                "Schema 3 validation failed: "
                f"{path.name} schema_version must be {expected}"
            )


def validate_schema3_requirement(
    target: Path,
    requirement_id: str,
) -> LegacyValidation:
    target = target.resolve()
    root = _requirement_root(target, requirement_id)
    entries = _tree_entries(root)
    for relative, kind, _, _ in entries:
        if stat.S_ISLNK(kind):
            raise ContractError(
                "Schema 3 validation failed: symbolic links are forbidden: "
                f"{relative}"
            )
        if not (stat.S_ISREG(kind) or stat.S_ISDIR(kind)):
            raise ContractError(
                "Schema 3 validation failed: non-regular entry is forbidden: "
                f"{relative}"
            )
    state_path = root / "state.json"
    if not state_path.is_file() or state_path.is_symlink():
        raise ContractError(
            "Schema 3 validation failed: state.json is missing or unsafe"
        )
    try:
        state = load_json_object(state_path)
    except (ContractError, OSError) as error:
        raise ContractError(
            f"Schema 3 validation failed: {error}"
        ) from error
    actual_fields = set(state)
    unknown = actual_fields - STATE_FIELDS - OPTIONAL_STATE_FIELDS
    missing = STATE_FIELDS - actual_fields
    if unknown or missing:
        raise ContractError(
            "Schema 3 validation failed: state fields are invalid "
            f"(missing={sorted(missing)}, unknown={sorted(unknown)})"
        )
    if type(state["schema_version"]) is not int or state["schema_version"] != 3:
        raise ContractError(
            "Schema 3 validation failed: schema_version must be 3"
        )
    if state["requirement_id"] != requirement_id:
        raise ContractError(
            "Schema 3 validation failed: requirement_id does not match directory"
        )
    goal = _require_string(state["goal"], "goal")
    status = state["status"]
    if status not in RUN_STATUSES:
        raise ContractError("Schema 3 validation failed: status is invalid")
    if state["phase"] not in PHASES:
        raise ContractError("Schema 3 validation failed: phase is invalid")
    if state["latest_verdict"] not in VERDICTS:
        raise ContractError(
            "Schema 3 validation failed: latest_verdict is invalid"
        )
    round_number = state["active_round"]
    if type(round_number) is not int or not 1 <= round_number <= 999:
        raise ContractError(
            "Schema 3 validation failed: active_round is invalid"
        )
    _require_string(state["next_action"], "next_action")
    accepted = state["accepted_revision"]
    if not isinstance(accepted, str) or (
        accepted and OID_PATTERN.fullmatch(accepted) is None
    ):
        raise ContractError(
            "Schema 3 validation failed: accepted_revision is invalid"
        )
    if status == "ACCEPTED" and not accepted:
        raise ContractError(
            "Schema 3 validation failed: ACCEPTED requires accepted_revision"
        )
    if status != "ACCEPTED" and accepted:
        raise ContractError(
            "Schema 3 validation failed: non-accepted state has accepted_revision"
        )
    if status == "DEGRADED":
        _require_string(
            state.get("degradation_acceptance"),
            "degradation_acceptance",
        )
    for field in (
        "pending_evaluation",
        "pending_review",
        "pending_interruption",
    ):
        if state[field] is not None and not isinstance(state[field], dict):
            raise ContractError(
                f"Schema 3 validation failed: {field} is invalid"
            )
    goal_file = root / "goal.md"
    if not goal_file.is_file() or goal_file.is_symlink():
        raise ContractError(
            "Schema 3 validation failed: goal.md is missing or unsafe"
        )
    try:
        if goal_file.read_text(encoding="utf-8").strip() != goal:
            raise ContractError(
                "Schema 3 validation failed: goal.md does not bind state.goal"
            )
    except UnicodeError as error:
        raise ContractError(
            "Schema 3 validation failed: goal.md is not UTF-8"
        ) from error
    _validate_history_records(root, state)
    source_identity = hash_tree(root)
    snapshot = repository_snapshot(target)
    return LegacyValidation(
        requirement_id=requirement_id,
        status=status,
        goal=goal,
        source_identity=source_identity,
        workspace_snapshot=snapshot.as_dict(),
        validated_state=copy.deepcopy(state),
    )


def _safe_directory(target: Path, directory: Path) -> None:
    target = target.resolve()
    current = target
    for component in directory.relative_to(target).parts:
        current = current / component
        if current.is_symlink():
            raise ContractError(f"migration path is a symbolic link: {current}")
        if current.exists():
            if not current.is_dir():
                raise ContractError(
                    f"migration path is not a directory: {current}"
                )
        else:
            current.mkdir(mode=0o700)


@contextmanager
def _file_lock(target: Path, relative: str) -> Iterator[None]:
    path = target / relative
    _safe_directory(target, path.parent)
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _publish_immutable(path: Path, body: bytes) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise StateConflictError(f"immutable migration target is unsafe: {path}")
        if path.read_bytes() != body:
            raise StateConflictError(
                f"immutable migration target has different bytes: {path}"
            )
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        offset = 0
        while offset < len(body):
            offset += os.write(descriptor, body[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _migration_id(
    repository_identity: str,
    base_sha: str,
    validations: list[LegacyValidation],
) -> str:
    body = canonical_json_bytes(
        {
            "repository_identity": repository_identity,
            "base_sha": base_sha,
            "requirements": [
                {
                    "requirement_id": item.requirement_id,
                    "source_identity": item.source_identity,
                }
                for item in validations
            ],
        }
    )
    return "MIG-" + hashlib.sha256(body).hexdigest()[:20]


def _load_claim(path: Path) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise StateConflictError(f"migration claim is unsafe: {path}")
    return load_json_object(path)


def _mapped_status(source_status: str) -> str:
    if source_status in {"ACCEPTED", "DEGRADED"}:
        return "IMPORTED_READ_ONLY"
    return "PAUSED"


def _entry_from_claim(claim: Mapping[str, object]) -> MigrationEntry:
    return MigrationEntry(
        requirement_id=str(claim["requirement_id"]),
        source_status=str(claim["source_status"]),
        source_identity=str(claim["source_identity"]),
        base_sha=str(claim["base_sha"]),
        run_id=str(claim["run_id"]),
        migration_id=str(claim["migration_id"]),
        mapped_status=str(claim["mapped_status"]),
        backup_path=str(claim["backup_path"]),
    )


def _verify_completed_entry(
    target: Path,
    entry: MigrationEntry,
) -> None:
    backup = target / entry.backup_path
    if hash_tree(backup) != entry.source_identity:
        raise StateConflictError(
            f"migration backup changed: {entry.requirement_id}"
        )
    state = StateStore.for_run(target, entry.run_id).load()
    if (
        state["status"] != entry.mapped_status
        or state["repository"]["base_sha"] != entry.base_sha
        or state["legacy_import"] is None
    ):
        raise StateConflictError(
            f"migration run mapping changed: {entry.requirement_id}"
        )
    actual_ref = WorktreeManager(target).resolve_ref(
        state["repository"]["integration_ref"]
    )
    if actual_ref != entry.base_sha:
        raise StateConflictError(
            f"migration run ref changed: {entry.requirement_id}"
        )


def _reserved_run_ids(target: Path) -> set[str]:
    result: set[str] = set()
    root = target / ".vibe-coding" / "migrations" / "reservations"
    if not root.exists():
        return result
    if root.is_symlink() or not root.is_dir():
        raise ContractError("migration reservations path is unsafe")
    for path in sorted(root.glob("*.json")):
        value = load_json_object(path)
        requirements = value.get("requirements")
        if isinstance(requirements, list):
            for item in requirements:
                if isinstance(item, dict) and isinstance(item.get("run_id"), str):
                    result.add(item["run_id"])
    return result


def _allocate_run_ids(
    target: Path,
    count: int,
) -> tuple[str, ...]:
    runs_root = target / ".vibe-coding" / "runs"
    occupied = (
        {path.name for path in runs_root.iterdir()}
        if runs_root.is_dir()
        else set()
    )
    occupied.update(_reserved_run_ids(target))
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    allocated: list[str] = []
    for sequence in range(1, 1000):
        run_id = f"RUN-{date}-{sequence:03d}"
        if run_id not in occupied:
            allocated.append(run_id)
            occupied.add(run_id)
            if len(allocated) == count:
                return tuple(allocated)
    raise ContractError("daily run ID space is exhausted")


def _copy_tree_exact(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise StateConflictError(f"migration staging target exists: {destination}")
    destination.mkdir(mode=stat.S_IMODE(source.lstat().st_mode))
    for relative, kind, mode, body in _tree_entries(source):
        target = destination / relative
        if stat.S_ISDIR(kind):
            target.mkdir(mode=mode)
        elif stat.S_ISREG(kind):
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                mode,
            )
            try:
                offset = 0
                while offset < len(body):
                    offset += os.write(descriptor, body[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(target, mode, follow_symlinks=False)
        else:
            raise ContractError(
                f"Schema 3 validation failed: unsafe source entry {relative}"
            )


def _empty_role_runtime(config: FrozenRunConfig) -> dict[str, object]:
    return {
        role: {
            "operation_id": None,
            "attempt_no": 0,
            "failure_count": 0,
            "max_attempts": config.task_attempts,
            "active_attempt_token": None,
            "last_error": None,
        }
        for role in ("planner", "evaluator")
    }


def _create_imported_run(
    *,
    target: Path,
    worktrees: WorktreeManager,
    validation: LegacyValidation,
    entry: MigrationEntry,
    config: FrozenRunConfig,
    prepared_manifest_path: str,
    prepared_manifest_sha256: str,
    created_at: str,
) -> None:
    store = StateStore.for_run(target, entry.run_id)
    run_ref = f"refs/heads/vibe/run-{entry.run_id}"
    config_body = frozen_config_bytes(config)
    intent_body = canonical_json_bytes(
        {
            "schema_version": 1,
            "run_id": entry.run_id,
            "creation_fingerprint": hashlib.sha256(
                canonical_json_bytes(entry.as_dict())
            ).hexdigest(),
            "goal_sha256": (
                "sha256:"
                + hashlib.sha256(validation.goal.encode("utf-8")).hexdigest()
            ),
            "repository_identity": worktrees.identity,
            "base_sha": entry.base_sha,
            "run_ref": run_ref,
            "config_sha256": (
                "sha256:" + hashlib.sha256(config_body).hexdigest()
            ),
        }
    )
    legacy_body = canonical_json_bytes(
        {
            "schema_version": 1,
            "migration_id": entry.migration_id,
            "requirement_id": entry.requirement_id,
            "source_status": entry.source_status,
            "source_identity": entry.source_identity,
            "base_sha": entry.base_sha,
            "backup_path": entry.backup_path,
            "prepared_manifest": {
                "path": prepared_manifest_path,
                "sha256": prepared_manifest_sha256,
            },
            "workspace_snapshot": validation.workspace_snapshot,
            "legacy_state": validation.validated_state,
        }
    )
    with store.lock():
        try:
            existing = store.load()
        except (ContractError, FileNotFoundError):
            existing = None
        if existing is None:
            worktrees.create_run_ref(entry.run_id, entry.base_sha)
            intent_ref = store.prepare_artifact(
                "creation.intent.json",
                intent_body,
            )
            config_ref = store.prepare_artifact("config.json", config_body)
            legacy_ref = store.prepare_artifact(
                "legacy-import.json",
                legacy_body,
            )
            initial = {
                "schema_version": 4,
                "run_id": entry.run_id,
                "revision": 0,
                "goal": validation.goal,
                "repository": {
                    "identity": worktrees.identity,
                    "base_ref": entry.base_sha,
                    "base_sha": entry.base_sha,
                    "integration_ref": run_ref,
                    "integration_head": entry.base_sha,
                },
                "status": "PAUSED",
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
                    legacy_ref.as_dict(),
                ],
                "plans": [],
                "role_attempts": {"planner": [], "evaluator": []},
                "role_runtime": _empty_role_runtime(config),
                "evaluations": [],
                "verifications": [],
                "legacy_import": legacy_ref.as_dict(),
                "tasks": {},
                "pending_dispatches": {},
                "pending_source_commit": None,
                "pending_integration": None,
                "pending_evaluation": None,
                "latest_evaluation": None,
                "global_verification": None,
                "stop_receipts": [],
                "last_error": {
                    "code": "MIGRATION_INSTALLING",
                    "message": "Schema 3 import is not yet fully installed",
                    "retryable": False,
                },
                "created_at": created_at,
                "updated_at": created_at,
            }
            existing = store.create(initial, {})
        if existing["creation"]["receipt"] is None:
            receipt_body = canonical_json_bytes(
                {
                    "schema_version": 1,
                    "run_id": entry.run_id,
                    "migration_id": entry.migration_id,
                    "intent": existing["creation"]["intent"],
                    "run_ref": run_ref,
                    "base_sha": entry.base_sha,
                    "created_at": created_at,
                }
            )

            def bind_receipt(
                current: dict[str, object],
                references: Mapping[str, object],
            ) -> None:
                current["creation"]["receipt"] = references[
                    "creation.receipt.json"
                ].as_dict()
                current["updated_at"] = created_at

            store.transact(
                existing["revision"],
                {"creation.receipt.json": receipt_body},
                bind_receipt,
            )


def _finalize_imported_run(
    target: Path,
    entry: MigrationEntry,
    claim_body: bytes,
    completed_body: bytes,
    now: str,
) -> None:
    store = StateStore.for_run(target, entry.run_id)
    with store.lock():
        state = store.load()
        if state["status"] == entry.mapped_status and (
            state["last_error"] is None
            or state["last_error"].get("code") == "SCHEMA3_REPLAN_REQUIRED"
        ):
            return
        if (
            state["status"] != "PAUSED"
            or state["last_error"] is None
            or state["last_error"].get("code") != "MIGRATION_INSTALLING"
        ):
            raise StateConflictError(
                f"migration run cannot be finalized: {entry.run_id}"
            )
        completion_body = canonical_json_bytes(
            {
                "schema_version": 1,
                "migration_id": entry.migration_id,
                "requirement_id": entry.requirement_id,
                "claim_sha256": (
                    "sha256:" + hashlib.sha256(claim_body).hexdigest()
                ),
                "completed_sha256": (
                    "sha256:" + hashlib.sha256(completed_body).hexdigest()
                ),
                "completed_at": now,
            }
        )

        def finalize(
            current: dict[str, object],
            references: Mapping[str, object],
        ) -> None:
            del references
            current["status"] = entry.mapped_status
            current["resume_status"] = None
            if entry.mapped_status == "PAUSED":
                current["last_error"] = {
                    "code": "SCHEMA3_REPLAN_REQUIRED",
                    "message": (
                        f"Schema 3 {entry.source_status} requires "
                        "a new Schema 4 plan"
                    ),
                    "retryable": True,
                }
            else:
                current["last_error"] = None
            current["updated_at"] = now

        store.transact(
            state["revision"],
            {"migration-completion.json": completion_body},
            finalize,
        )


def migrate_schema3(
    target: Path,
    requirement_id: str | None,
    migrate_all: bool,
    base: str,
    allow_project_commands: bool = False,
    fault_hook: Callable[[str], None] | None = None,
) -> tuple[MigrationEntry, ...]:
    if (requirement_id is None) == (migrate_all is False):
        raise ContractError(
            "select exactly one of --requirement or --all"
        )
    target = target.resolve()
    worktrees = WorktreeManager(target)
    base_sha = worktrees.resolve_ref(base)
    config = load_run_config(
        target,
        {"allow_project_commands": allow_project_commands},
    )
    selected = (
        discover_requirements(target)
        if migrate_all
        else (str(requirement_id),)
    )
    if not selected:
        raise ContractError("no Schema 3 requirements were found")
    inject = fault_hook or (lambda point: None)

    with _file_lock(
        target,
        ".vibe-coding/control/migration.lock",
    ):
        structural = {
            item: hash_tree(_requirement_root(target, item))
            for item in selected
        }
        replay: list[MigrationEntry] = []
        replay_complete = True
        for item in selected:
            claim_path = (
                target
                / ".vibe-coding"
                / "migrations"
                / "index"
                / f"{item}.json"
            )
            claim = _load_claim(claim_path)
            if claim is None:
                replay_complete = False
                continue
            if claim.get("source_identity") != structural[item]:
                raise StateConflictError(
                    f"Schema 3 source changed for {item}"
                )
            if claim.get("base_sha") != base_sha:
                raise StateConflictError(
                    f"Schema 3 migration uses a different base for {item}"
                )
            replay.append(_entry_from_claim(claim))
        if replay and not replay_complete:
            pass
        if replay_complete:
            try:
                for entry in replay:
                    _verify_completed_entry(target, entry)
            except StateConflictError:
                pass
            else:
                return tuple(replay)

        validations = [
            validate_schema3_requirement(target, item)
            for item in selected
        ]
        inject("after_validation")
        for validation in validations:
            if (
                validation.source_identity
                != structural[validation.requirement_id]
                or hash_tree(
                    _requirement_root(
                        target,
                        validation.requirement_id,
                    )
                )
                != validation.source_identity
            ):
                raise ContractError(
                    "Schema 3 source changed during validation"
                )
        migration_id = _migration_id(
            worktrees.identity,
            base_sha,
            validations,
        )
        migration_root = (
            target / ".vibe-coding" / "migrations" / migration_id
        )
        reservations_root = (
            target / ".vibe-coding" / "migrations" / "reservations"
        )
        index_root = target / ".vibe-coding" / "migrations" / "index"
        backups_root = (
            target / ".vibe-coding" / "schema3-backups" / migration_id
        )
        for directory in (
            migration_root,
            reservations_root,
            index_root,
            backups_root.parent,
        ):
            _safe_directory(target, directory)
        prepared_path = migration_root / "prepared.json"
        if prepared_path.exists():
            existing_prepared = load_json_object(prepared_path)
            created_at = str(existing_prepared.get("created_at"))
            if (
                existing_prepared.get("migration_id") != migration_id
                or existing_prepared.get("base_sha") != base_sha
                or not created_at
            ):
                raise StateConflictError(
                    "prepared migration manifest conflicts"
                )
        else:
            created_at = datetime.now(timezone.utc).isoformat(
                timespec="microseconds"
            )
        if replay and any(
            item.migration_id != migration_id
            for item in replay
        ):
            raise StateConflictError(
                "migration selection mixes an existing requirement claim "
                "with a different batch"
            )

        with _file_lock(
            target,
            ".vibe-coding/control/run-allocation.lock",
        ):
            reservation_path = reservations_root / f"{migration_id}.json"
            existing_reservation = (
                _load_claim(reservation_path)
                if reservation_path.exists()
                else None
            )
            if existing_reservation is None:
                run_ids = _allocate_run_ids(target, len(validations))
                reservation = {
                    "schema_version": 1,
                    "state": "PREPARED",
                    "migration_id": migration_id,
                    "repository_identity": worktrees.identity,
                    "base_sha": base_sha,
                    "requirements": [
                        {
                            "requirement_id": validation.requirement_id,
                            "source_identity": validation.source_identity,
                            "run_id": run_id,
                            "run_ref": (
                                f"refs/heads/vibe/run-{run_id}"
                            ),
                        }
                        for validation, run_id in zip(
                            validations,
                            run_ids,
                        )
                    ],
                }
                _publish_immutable(
                    reservation_path,
                    canonical_json_bytes(reservation),
                )
                inject("after_reservation")
            else:
                reservation = existing_reservation
                requirements = reservation.get("requirements")
                if (
                    reservation.get("repository_identity") != worktrees.identity
                    or reservation.get("base_sha") != base_sha
                    or not isinstance(requirements, list)
                    or [
                        (
                            item.get("requirement_id"),
                            item.get("source_identity"),
                        )
                        for item in requirements
                        if isinstance(item, dict)
                    ]
                    != [
                        (
                            item.requirement_id,
                            item.source_identity,
                        )
                        for item in validations
                    ]
                ):
                    raise StateConflictError(
                        "prepared migration reservation conflicts"
                    )
                run_ids = tuple(
                    str(item["run_id"])
                    for item in requirements
                    if isinstance(item, dict)
                )

            entries = tuple(
                MigrationEntry(
                    requirement_id=validation.requirement_id,
                    source_status=validation.status,
                    source_identity=validation.source_identity,
                    base_sha=base_sha,
                    run_id=run_id,
                    migration_id=migration_id,
                    mapped_status=_mapped_status(validation.status),
                    backup_path=(
                        ".vibe-coding/schema3-backups/"
                        f"{migration_id}/{validation.requirement_id}"
                    ),
                )
                for validation, run_id in zip(validations, run_ids)
            )
            staging_root = migration_root / "staging"
            _safe_directory(target, staging_root)
            for validation in validations:
                source = _requirement_root(
                    target,
                    validation.requirement_id,
                )
                if hash_tree(source) != validation.source_identity:
                    raise ContractError(
                        "Schema 3 source changed before staging"
                    )
                staged = staging_root / validation.requirement_id
                if not staged.exists():
                    _copy_tree_exact(source, staged)
                if hash_tree(staged) != validation.source_identity:
                    raise ContractError(
                        "Schema 3 staged backup changed"
                    )
                inject("after_staged_requirement")

            manifest = MigrationManifest(
                schema_version=1,
                migration_id=migration_id,
                created_at=created_at,
                target_repository_identity=worktrees.identity,
                base_sha=base_sha,
                entries=entries,
            )
            prepared_body = canonical_json_bytes(
                {
                    **manifest.as_dict(),
                    "state": "PREPARED",
                    "workspace_snapshots": {
                        item.requirement_id: item.workspace_snapshot
                        for item in validations
                    },
                }
            )
            _publish_immutable(prepared_path, prepared_body)
            inject("after_prepared_manifest")
            prepared_sha = (
                "sha256:" + hashlib.sha256(prepared_body).hexdigest()
            )
            for validation, entry in zip(validations, entries):
                source = _requirement_root(
                    target,
                    validation.requirement_id,
                )
                if hash_tree(source) != validation.source_identity:
                    raise ContractError(
                        "Schema 3 source changed before install"
                    )
                staged = staging_root / validation.requirement_id
                backup = target / entry.backup_path
                _safe_directory(target, backup.parent)
                if backup.exists():
                    if hash_tree(backup) != validation.source_identity:
                        raise StateConflictError(
                            "installed Schema 3 backup has changed"
                        )
                else:
                    os.replace(staged, backup)
                inject("after_backup_install")
                _create_imported_run(
                    target=target,
                    worktrees=worktrees,
                    validation=validation,
                    entry=entry,
                    config=config,
                    prepared_manifest_path=(
                        f".vibe-coding/migrations/{migration_id}/prepared.json"
                    ),
                    prepared_manifest_sha256=prepared_sha,
                    created_at=created_at,
                )
                inject("after_imported_run")

            claim_bodies: dict[str, bytes] = {}
            for entry in entries:
                claim_body = canonical_json_bytes(
                    {
                        "schema_version": 1,
                        "state": "COMPLETED",
                        **entry.as_dict(),
                        "repository_identity": worktrees.identity,
                    }
                )
                claim_bodies[entry.requirement_id] = claim_body
                _publish_immutable(
                    index_root / f"{entry.requirement_id}.json",
                    claim_body,
                )
                inject("after_index_claim")
            completed_body = canonical_json_bytes(
                {
                    **manifest.as_dict(),
                    "state": "COMPLETED",
                }
            )
            _publish_immutable(
                migration_root / "completed.json",
                completed_body,
            )
            inject("after_completed_manifest")
            finalized_at = datetime.now(timezone.utc).isoformat(
                timespec="microseconds"
            )
            for entry in entries:
                _finalize_imported_run(
                    target,
                    entry,
                    claim_bodies[entry.requirement_id],
                    completed_body,
                    finalized_at,
                )
                inject("after_run_finalization")
            for entry in entries:
                _verify_completed_entry(target, entry)
            return entries
