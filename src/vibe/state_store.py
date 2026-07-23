from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import json
import os
import stat
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from vibe.models import (
    ArtifactRef,
    ContractError,
    RUN_ID_RE,
    StateConflictError,
    reject_invalid_json_scalars,
    validate_bound_state_semantics,
    validate_run_state,
)


MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_REGULAR_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    raise ContractError(f"non-finite JSON number is forbidden: {value}")


def canonical_json_bytes(value: object) -> bytes:
    try:
        body = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded = body.encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ContractError(f"value is not canonical JSON: {error}") from error
    return encoded + b"\n"


def parse_json_object_bytes(body: bytes) -> dict[str, object]:
    try:
        text = body.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
        reject_invalid_json_scalars(value)
    except ContractError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ContractError(f"cannot parse JSON object: {error}") from error
    if not isinstance(value, dict):
        raise ContractError("JSON root must be an object")
    return value


def load_json_object(path: Path) -> dict[str, object]:
    descriptor = open_absolute_regular_no_follow(path)
    try:
        return parse_json_object_bytes(
            read_bounded(descriptor, max_bytes=MAX_STATE_BYTES)
        )
    finally:
        os.close(descriptor)


def _relative_artifact_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or value != path.as_posix()
        or path.parts[0] in {"state.json", "controller.lock"}
    ):
        raise ContractError(f"invalid artifact path: {value!r}")
    return path


def artifact_ref(relative_path: str, body: bytes) -> ArtifactRef:
    _relative_artifact_path(relative_path)
    return ArtifactRef(
        path=relative_path,
        sha256="sha256:" + hashlib.sha256(body).hexdigest(),
    )


def _validate_absolute_path(path: Path) -> tuple[str, ...]:
    if not path.is_absolute():
        raise ContractError(f"path must be absolute: {path}")
    parts = path.parts
    if not parts or parts[0] != "/" or any(part in {"", ".", ".."} for part in parts[1:]):
        raise ContractError(f"path must be canonical: {path}")
    return parts[1:]


def _translate_open_error(path: object, error: OSError) -> ContractError:
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        return ContractError(
            f"symbolic link or invalid path component is forbidden: {path}"
        )
    return ContractError(f"cannot open {path}: {error}")


def _require_directory(descriptor: int, path: object) -> None:
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise ContractError(f"path is not a directory: {path}")


def _require_regular(descriptor: int, path: object) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise ContractError(f"path is not a regular file: {path}")


def open_absolute_regular_no_follow(path: Path) -> int:
    parts = _validate_absolute_path(path)
    if not parts:
        raise ContractError("root directory is not a regular file")
    parent = open_absolute_directory_no_follow(Path("/").joinpath(*parts[:-1]))
    try:
        try:
            descriptor = os.open(
                parts[-1],
                _REGULAR_READ_FLAGS,
                dir_fd=parent,
            )
        except OSError as error:
            raise _translate_open_error(path, error) from error
        try:
            _require_regular(descriptor, path)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    finally:
        os.close(parent)


def open_absolute_directory_no_follow(path: Path) -> int:
    parts = _validate_absolute_path(path)
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
    except OSError as error:
        raise _translate_open_error(path, error) from error
    try:
        for part in parts:
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as error:
                raise _translate_open_error(path, error) from error
            try:
                _require_directory(child, part)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validate_component(part: str) -> None:
    if not part or "/" in part or part in {".", ".."}:
        raise ContractError(f"invalid path component: {part!r}")


def open_directory_chain(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    create: bool,
) -> int:
    descriptor = os.dup(root_fd)
    try:
        _require_directory(descriptor, "root descriptor")
        for part in parts:
            _validate_component(part)
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise ContractError(f"directory does not exist: {part}")
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise _translate_open_error(part, error) from error
                else:
                    os.fsync(descriptor)
                try:
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except OSError as error:
                    raise _translate_open_error(part, error) from error
            except OSError as error:
                raise _translate_open_error(part, error) from error
            try:
                _require_directory(child, part)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_regular_at(parent_fd: int, leaf: str, *, max_bytes: int) -> bytes:
    _validate_component(leaf)
    try:
        descriptor = os.open(leaf, _REGULAR_READ_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise _translate_open_error(leaf, error) from error
    try:
        _require_regular(descriptor, leaf)
        return read_bounded(descriptor, max_bytes=max_bytes)
    finally:
        os.close(descriptor)


def read_bounded(descriptor: int, *, max_bytes: int) -> bytes:
    if type(max_bytes) is not int or max_bytes < 0:
        raise ContractError("max_bytes must be a non-negative integer")
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes - total + 1))
        except InterruptedError:
            continue
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise ContractError(f"file exceeds byte limit {max_bytes}")
        chunks.append(chunk)


def read_optional_regular_at(
    parent_fd: int,
    leaf: str,
    *,
    max_bytes: int,
) -> bytes | None:
    _validate_component(leaf)
    try:
        descriptor = os.open(leaf, _REGULAR_READ_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _translate_open_error(leaf, error) from error
    try:
        _require_regular(descriptor, leaf)
        return read_bounded(descriptor, max_bytes=max_bytes)
    finally:
        os.close(descriptor)


def _temporary_leaf(leaf: str) -> str:
    return f".{leaf}.tmp-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}"


def _write_all(descriptor: int, body: bytes) -> None:
    remaining = memoryview(body)
    while remaining:
        try:
            amount = os.write(descriptor, remaining)
        except InterruptedError:
            continue
        if amount <= 0:
            raise OSError("write returned no progress")
        remaining = remaining[amount:]


def _create_and_sync_temp(parent_fd: int, leaf: str, body: bytes) -> str:
    temporary = _temporary_leaf(leaf)
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        _require_regular(descriptor, temporary)
        _write_all(descriptor, body)
        os.fsync(descriptor)
        _record_durability_event("artifact-file-fsync")
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)
    return temporary


def replace_mutable_at(parent_fd: int, leaf: str, body: bytes) -> None:
    _validate_component(leaf)
    temporary = _create_and_sync_temp(parent_fd, leaf, body)
    try:
        os.replace(
            temporary,
            leaf,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        if leaf == "state.json":
            _record_durability_event("state-file-fsync")
        os.fsync(parent_fd)
        if leaf == "state.json":
            _record_durability_event("run-dir-fsync")
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def publish_immutable_at(parent_fd: int, leaf: str, body: bytes) -> None:
    _validate_component(leaf)
    temporary = _create_and_sync_temp(parent_fd, leaf, body)
    try:
        try:
            os.link(
                temporary,
                leaf,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            winner = read_regular_at(
                parent_fd,
                leaf,
                max_bytes=max(MAX_ARTIFACT_BYTES, len(body)),
            )
            if winner != body:
                raise StateConflictError(
                    f"immutable artifact already exists with different bytes: {leaf}"
                )
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.fsync(parent_fd)


def append_complete_record_at(
    parent_fd: int,
    leaf: str,
    body: bytes,
    *,
    no_follow: bool,
) -> None:
    _validate_component(leaf)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if no_follow:
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(leaf, flags, 0o600, dir_fd=parent_fd)
    except OSError as error:
        raise _translate_open_error(leaf, error) from error
    try:
        _require_regular(descriptor, leaf)
        _write_all(descriptor, body)
        os.fsync(descriptor)
        os.fsync(parent_fd)
    finally:
        os.close(descriptor)


def _record_durability_event(event: str) -> None:
    del event


def _artifact_refs(value: object) -> Iterator[ArtifactRef]:
    if isinstance(value, dict):
        if set(value) == {"path", "sha256"}:
            path = value["path"]
            digest = value["sha256"]
            if isinstance(path, str) and isinstance(digest, str):
                yield ArtifactRef(path=path, sha256=digest)
            return
        for item in value.values():
            yield from _artifact_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _artifact_refs(item)


def _history_at(
    state: dict[str, object],
    path: tuple[str, ...],
) -> list[object]:
    value: object = state
    for component in path:
        if not isinstance(value, dict) or component not in value:
            raise ContractError(
                f"missing append-only history: {'.'.join(path)}"
            )
        value = value[component]
    if not isinstance(value, list):
        raise ContractError(
            f"append-only history is not a list: {'.'.join(path)}"
        )
    return value


def _history_prefixes(
    state: dict[str, object],
) -> dict[tuple[str, ...], list[object]]:
    paths = [
        ("plans",),
        ("evaluations",),
        ("verifications",),
        ("role_attempts", "planner"),
        ("role_attempts", "evaluator"),
        ("stop_receipts",),
    ]
    paths.extend(
        ("tasks", task_id, "attempts")
        for task_id in sorted(state["tasks"])
    )
    return {
        path: copy.deepcopy(_history_at(state, path))
        for path in paths
    }


def _require_history_prefixes(
    state: dict[str, object],
    prefixes: Mapping[tuple[str, ...], list[object]],
) -> None:
    for path, prefix in prefixes.items():
        current = _history_at(state, path)
        if current[: len(prefix)] != prefix:
            raise ContractError(
                f"append-only history changed: {'.'.join(path)}"
            )


class StateStore:
    def __init__(self, target: Path, run_id: str) -> None:
        if RUN_ID_RE.fullmatch(run_id) is None:
            raise ContractError("run_id must match RUN-YYYYMMDD-NNN")
        self.target = target.resolve()
        self.run_id = run_id
        self.root = self.target / ".vibe-coding" / "runs" / run_id
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / "controller.lock"
        self.log_path = self.root / "logs" / "controller.jsonl"
        self._lock_depth = 0
        self._lock_owner: int | None = None
        self._locked_run_fd: int | None = None
        self._after_run_dir_open_hook: Callable[[], None] | None = None
        self._before_immutable_publish_hook: (
            Callable[[int, str], None] | None
        ) = None

    @classmethod
    def for_run(cls, target: Path, run_id: str) -> StateStore:
        return cls(target, run_id)

    @contextmanager
    def inject_after_run_dir_open(
        self,
        hook: Callable[[], None],
    ) -> Iterator[None]:
        previous = self._after_run_dir_open_hook
        self._after_run_dir_open_hook = hook
        try:
            yield
        finally:
            self._after_run_dir_open_hook = previous

    @contextmanager
    def inject_before_immutable_publish(
        self,
        hook: Callable[[int, str], None],
    ) -> Iterator[None]:
        previous = self._before_immutable_publish_hook
        self._before_immutable_publish_hook = hook
        try:
            yield
        finally:
            self._before_immutable_publish_hook = previous

    def _open_run_directory(self, *, create: bool) -> int:
        target_fd = open_absolute_directory_no_follow(self.target)
        try:
            run_fd = open_directory_chain(
                target_fd,
                (".vibe-coding", "runs", self.run_id),
                create=create,
            )
        finally:
            os.close(target_fd)
        try:
            hook = self._after_run_dir_open_hook
            if hook is not None:
                hook()
                verification_fd = open_absolute_directory_no_follow(self.root)
                try:
                    opened = os.fstat(run_fd)
                    verified = os.fstat(verification_fd)
                    if (opened.st_dev, opened.st_ino) != (
                        verified.st_dev,
                        verified.st_ino,
                    ):
                        raise ContractError(
                            "run directory changed after descriptor open"
                        )
                finally:
                    os.close(verification_fd)
            return run_fd
        except BaseException:
            os.close(run_fd)
            raise

    @contextmanager
    def _snapshot_run_fd(self, *, create: bool = False) -> Iterator[int]:
        if (
            self._lock_depth == 1
            and self._lock_owner == threading.get_ident()
            and self._locked_run_fd is not None
        ):
            descriptor = os.dup(self._locked_run_fd)
        else:
            descriptor = self._open_run_directory(create=create)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def lock(self, *, blocking: bool = True) -> Iterator[None]:
        if self._lock_depth != 0:
            raise StateConflictError("run lock is non-reentrant")
        run_fd = self._open_run_directory(create=True)
        try:
            try:
                descriptor = os.open(
                    "controller.lock",
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=run_fd,
                )
            except OSError as error:
                raise _translate_open_error("controller.lock", error) from error
            try:
                _require_regular(descriptor, "controller.lock")
                operation = fcntl.LOCK_EX
                if not blocking:
                    operation |= fcntl.LOCK_NB
                try:
                    fcntl.flock(descriptor, operation)
                except BlockingIOError as error:
                    raise StateConflictError(
                        "run lock is already held"
                    ) from error
                self._locked_run_fd = run_fd
                self._lock_owner = threading.get_ident()
                self._lock_depth = 1
                try:
                    yield
                finally:
                    self._lock_depth = 0
                    self._lock_owner = None
                    self._locked_run_fd = None
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        finally:
            os.close(run_fd)

    def _require_lock(self) -> None:
        if (
            self._lock_depth != 1
            or self._lock_owner != threading.get_ident()
            or self._locked_run_fd is None
        ):
            raise StateConflictError("state mutation requires the run lock")

    def _open_artifact_parent(
        self,
        relative: str,
        *,
        create: bool,
        run_fd: int | None = None,
    ) -> tuple[int, str]:
        path = _relative_artifact_path(relative)
        if run_fd is not None:
            root_fd = run_fd
        else:
            self._require_lock()
            assert self._locked_run_fd is not None
            root_fd = self._locked_run_fd
        parent = open_directory_chain(
            root_fd,
            tuple(path.parts[:-1]),
            create=create,
        )
        return parent, path.parts[-1]

    def _publish_or_verify_immutable(
        self,
        parent_fd: int,
        leaf: str,
        body: bytes,
        reference: ArtifactRef,
    ) -> None:
        hook = self._before_immutable_publish_hook
        if hook is not None:
            self._before_immutable_publish_hook = None
            hook(parent_fd, leaf)
        try:
            publish_immutable_at(parent_fd, leaf, body)
        except StateConflictError as error:
            raise StateConflictError(
                "immutable artifact already exists with different bytes: "
                f"{reference.path}"
            ) from error

    def _write_immutable(self, relative: str, body: bytes) -> ArtifactRef:
        reference = artifact_ref(relative, body)
        parent_fd, leaf = self._open_artifact_parent(relative, create=True)
        try:
            self._publish_or_verify_immutable(
                parent_fd,
                leaf,
                body,
                reference,
            )
        finally:
            os.close(parent_fd)
        parts = PurePosixPath(relative).parts[:-1]
        if parts:
            _record_durability_event(f"{parts[0]}-dir-fsync")
        if len(parts) > 1:
            name = "task" if parts[0] == "tasks" else parts[-1]
            _record_durability_event(f"{name}-dir-fsync")
        return reference

    def prepare_artifact(self, relative: str, body: bytes) -> ArtifactRef:
        self._require_lock()
        return self._write_immutable(relative, body)

    def _run_leaf_exists(self, leaf: str) -> bool:
        self._require_lock()
        assert self._locked_run_fd is not None
        return (
            read_optional_regular_at(
                self._locked_run_fd,
                leaf,
                max_bytes=MAX_STATE_BYTES,
            )
            is not None
        )

    def create(
        self,
        initial_state: dict[str, object],
        artifacts: Mapping[str, bytes],
    ) -> dict[str, object]:
        self._require_lock()
        if self._run_leaf_exists("state.json"):
            raise StateConflictError("state.json already exists")
        state = validate_run_state(initial_state)
        if state["revision"] != 0:
            raise ContractError("initial revision must be 0")
        self._validate_artifact_bindings(state, artifacts)
        for relative, body in artifacts.items():
            self._write_immutable(relative, body)
        assert self._locked_run_fd is not None
        replace_mutable_at(
            self._locked_run_fd,
            "state.json",
            canonical_json_bytes(state),
        )
        return copy.deepcopy(state)

    def load(self) -> dict[str, object]:
        with self._snapshot_run_fd() as run_fd:
            state = validate_run_state(
                parse_json_object_bytes(
                    read_regular_at(
                        run_fd,
                        "state.json",
                        max_bytes=MAX_STATE_BYTES,
                    )
                )
            )
            bodies = self._verify_and_cache_bound_artifacts(run_fd, state)
            validate_bound_state_semantics(state, bodies.__getitem__)
        return state

    def transact(
        self,
        expected_revision: int,
        artifacts: Mapping[str, bytes],
        mutate: Callable[
            [dict[str, object], Mapping[str, ArtifactRef]],
            None,
        ],
    ) -> dict[str, object]:
        self._require_lock()
        current = self.load()
        actual = current["revision"]
        if actual != expected_revision:
            raise StateConflictError(
                f"expected revision {expected_revision}, found {actual}"
            )
        references = {
            relative: artifact_ref(relative, body)
            for relative, body in artifacts.items()
        }
        updated = copy.deepcopy(current)
        previous_histories = _history_prefixes(current)
        previous_legacy_import = copy.deepcopy(current["legacy_import"])
        indexed = {
            item["path"]: item["sha256"]
            for item in updated["artifact_index"]
        }
        for reference in references.values():
            previous_digest = indexed.get(reference.path)
            if (
                previous_digest is not None
                and previous_digest != reference.sha256
            ):
                raise StateConflictError(
                    "immutable artifact already exists with different bytes: "
                    f"{reference.path}"
                )
            if previous_digest is None:
                updated["artifact_index"].append(reference.as_dict())
                indexed[reference.path] = reference.sha256
        expected_index = copy.deepcopy(updated["artifact_index"])
        mutation_result = mutate(updated, references)
        if mutation_result is not None:
            raise ContractError(
                "state mutator must modify in place and return None"
            )
        if updated["artifact_index"][: len(expected_index)] != expected_index:
            raise ContractError("artifact_index is append-only")
        _require_history_prefixes(updated, previous_histories)
        if (
            previous_legacy_import is not None
            and updated["legacy_import"] != previous_legacy_import
        ):
            raise ContractError("legacy_import is immutable once set")
        updated["revision"] = expected_revision + 1
        validated = validate_run_state(updated)
        self._validate_artifact_bindings(validated, artifacts)
        for relative, body in artifacts.items():
            self._write_immutable(relative, body)
        assert self._locked_run_fd is not None
        replace_mutable_at(
            self._locked_run_fd,
            "state.json",
            canonical_json_bytes(validated),
        )
        return copy.deepcopy(validated)

    def append_log(self, event: Mapping[str, object]) -> None:
        body = canonical_json_bytes(dict(event))
        with self._snapshot_run_fd(create=True) as run_fd:
            logs_fd = open_directory_chain(
                run_fd,
                ("logs",),
                create=True,
            )
            try:
                append_complete_record_at(
                    logs_fd,
                    "controller.jsonl",
                    body,
                    no_follow=True,
                )
            finally:
                os.close(logs_fd)

    def _validate_artifact_bindings(
        self,
        state: dict[str, object],
        artifacts: Mapping[str, bytes],
    ) -> None:
        references: dict[str, ArtifactRef] = {}
        for reference in _artifact_refs(state):
            _relative_artifact_path(reference.path)
            previous = references.get(reference.path)
            if (
                previous is not None
                and previous.sha256 != reference.sha256
            ):
                raise ContractError(
                    f"conflicting artifact digests for {reference.path}"
                )
            references[reference.path] = reference

        for relative, body in artifacts.items():
            expected = artifact_ref(relative, body)
            if references.get(relative) != expected:
                raise ContractError(
                    "transaction artifact is not bound by new state: "
                    f"{relative}"
                )

        for reference in references.values():
            if reference.path in artifacts:
                if (
                    artifact_ref(
                        reference.path,
                        artifacts[reference.path],
                    )
                    != reference
                ):
                    raise ContractError(
                        f"artifact digest mismatch: {reference.path}"
                    )
                continue
            actual = self._sha256_bound_artifact(reference.path)
            if actual != reference.sha256:
                raise ContractError(
                    f"bound artifact digest mismatch: {reference.path}"
                )

    def _sha256_bound_artifact(self, relative: str) -> str:
        parent_fd, leaf = self._open_artifact_parent(
            relative,
            create=False,
        )
        try:
            body = read_regular_at(
                parent_fd,
                leaf,
                max_bytes=MAX_ARTIFACT_BYTES,
            )
        finally:
            os.close(parent_fd)
        return "sha256:" + hashlib.sha256(body).hexdigest()

    def _verify_and_cache_bound_artifacts(
        self,
        run_fd: int,
        state: dict[str, object],
    ) -> dict[str, bytes]:
        references: dict[str, ArtifactRef] = {}
        for reference in _artifact_refs(state):
            _relative_artifact_path(reference.path)
            previous = references.get(reference.path)
            if (
                previous is not None
                and previous.sha256 != reference.sha256
            ):
                raise ContractError(
                    f"conflicting artifact digests for {reference.path}"
                )
            references[reference.path] = reference

        bodies: dict[str, bytes] = {}
        for reference in references.values():
            parent_fd, leaf = self._open_artifact_parent(
                reference.path,
                create=False,
                run_fd=run_fd,
            )
            try:
                body = read_regular_at(
                    parent_fd,
                    leaf,
                    max_bytes=MAX_ARTIFACT_BYTES,
                )
            finally:
                os.close(parent_fd)
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            if digest != reference.sha256:
                raise ContractError(
                    f"bound artifact digest mismatch: {reference.path}"
                )
            bodies[reference.path] = body
        return bodies
