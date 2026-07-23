#!/usr/bin/env python3
"""Manage durable requirement state and exact evaluation transactions."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 3
EVALUATION_RECORD_VERSION = 2
INTERRUPTION_RECORD_VERSION = 2
MAX_ROUNDS = 999
MAX_JSON_INTEGER_DIGITS = 4300
RUN_STATUSES = {"ACTIVE", "BLOCKED", "DEGRADED", "ACCEPTED"}
TERMINAL_STATUSES = ("DEGRADED", "ACCEPTED")
PHASES = {"PLANNING", "BUILDING", "EVALUATING"}
VERDICTS = (None, "PASS", "FAIL", "UNVERIFIED")
REQUIREMENT_PATTERN = re.compile(r"REQ-(\d+)\Z")
CRITERION_PATTERN = re.compile(r"AC-\d{3}\Z")
OBSERVATION_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
OBJECT_ID_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
OBSERVED_FILE_STATES = {"missing", "symlink", "non_regular", "unreadable"}
WORKSPACE_PATHSPEC = (
    "--",
    ".",
    ":(exclude).vibe-coding",
    ":(exclude).vibe-coding/**",
)


@dataclass(frozen=True)
class RoundPaths:
    root: Path
    inputs: Path
    plan: Path
    implementation: Path
    implementation_snapshot: Path
    review: Path
    interruption: Path
    attempts: Path


@dataclass(frozen=True)
class RequirementPaths:
    root: Path
    state: Path
    plan: Path
    rounds: Path

    def round(self, number: int) -> RoundPaths:
        root = self.rounds / f"{number:03d}"
        inputs = root / "evaluation-inputs"
        return RoundPaths(
            root=root,
            inputs=inputs,
            plan=inputs / "plan.md",
            implementation=root / "implementation.md",
            implementation_snapshot=inputs / "implementation.md",
            review=root / "review.md",
            interruption=root / "interruption.json",
            attempts=root / "attempts",
        )


@dataclass(frozen=True)
class ArtifactSnapshot:
    exists: bool
    body: str | None
    error: str | None


@dataclass(frozen=True)
class RepositorySnapshot:
    revision: str
    workspace_fingerprint: str

    def as_dict(self) -> dict[str, str]:
        return {
            "revision": self.revision,
            "workspace_fingerprint": self.workspace_fingerprint,
        }


@dataclass(frozen=True)
class MarkdownHeading:
    title: str
    exact: bool
    start: int
    content_start: int


class HarnessError(ValueError):
    """Raised when harness state cannot be changed or resumed safely."""


def _emit(value: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    safe_rendered = rendered.encode("utf-8", errors="backslashreplace").decode(
        "utf-8"
    )
    print(safe_rendered, file=stream)


def _non_empty_string(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(
            0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    )


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise HarnessError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _parse_json_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(
            "integer string conversion exceeds application digit limit"
        )
    return int(value)


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise HarnessError(f"{label} must not be a symbolic link")


def _target_root(value: str) -> Path:
    target = Path(value).resolve()
    if not target.is_dir():
        raise HarnessError(f"target is not a directory: {target}")
    result = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise HarnessError("target must be inside a Git repository")
    git_root = Path(result.stdout.strip()).resolve()
    if git_root != target:
        raise HarnessError(f"target must be the Git root: {git_root}")
    return target


def _git_bytes(target: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(target), *arguments],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        command = " ".join(arguments)
        raise HarnessError(f"git {command} failed: {detail or 'unknown error'}")
    return result.stdout


def _revision(target: Path) -> str:
    revision = _git_bytes(target, "rev-parse", "HEAD").decode("ascii").strip()
    if not OBJECT_ID_PATTERN.fullmatch(revision):
        raise HarnessError("HEAD must resolve to a canonical full commit OID")
    return revision


def _resolve_revision(target: Path, revision: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "rev-parse",
            "--verify",
            f"{revision}^{{commit}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _hash_part(digest: Any, label: bytes, value: bytes) -> None:
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _untracked_fingerprint_stream(target: Path) -> bytes:
    output = _git_bytes(
        target,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        *WORKSPACE_PATHSPEC,
    )
    paths = sorted(path for path in output.split(b"\0") if path)
    digest = hashlib.sha256()
    for raw_path in paths:
        path = target / os.fsdecode(raw_path)
        try:
            metadata = path.lstat()
        except OSError as error:
            raise HarnessError(
                f"cannot inspect untracked path {os.fsdecode(raw_path)!r}: {error}"
            ) from error
        mode = f"{stat.S_IFMT(metadata.st_mode):o}:{metadata.st_mode & 0o111:o}".encode(
            "ascii"
        )
        if stat.S_ISLNK(metadata.st_mode):
            content = os.fsencode(os.readlink(path))
            kind = b"symlink"
        elif stat.S_ISREG(metadata.st_mode):
            try:
                content = path.read_bytes()
            except OSError as error:
                raise HarnessError(
                    f"cannot read untracked path {os.fsdecode(raw_path)!r}: {error}"
                ) from error
            kind = b"file"
        else:
            content = b""
            kind = b"special"
        _hash_part(digest, b"path", raw_path)
        _hash_part(digest, b"kind", kind)
        _hash_part(digest, b"mode", mode)
        _hash_part(digest, b"content", content)
    return digest.digest()


def _index_modes(target: Path) -> dict[bytes, set[bytes]]:
    output = _git_bytes(
        target,
        "ls-files",
        "--stage",
        "-z",
        *WORKSPACE_PATHSPEC,
    )
    modes: dict[bytes, set[bytes]] = {}
    for entry in output.split(b"\0"):
        if not entry:
            continue
        header, separator, raw_path = entry.partition(b"\t")
        fields = header.split()
        if separator and len(fields) == 3:
            modes.setdefault(raw_path, set()).add(fields[0])
    return modes


def _tracked_worktree_fingerprint_stream(
    target: Path,
    index_modes: dict[bytes, set[bytes]],
) -> bytes:
    digest = hashlib.sha256()
    for raw_path in sorted(index_modes):
        if b"160000" in index_modes[raw_path]:
            continue
        display_path = os.fsdecode(raw_path)
        path = target / display_path
        _hash_part(digest, b"path", raw_path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            _hash_part(digest, b"state", b"absent")
            continue
        except OSError as error:
            raise HarnessError(
                f"cannot inspect tracked path {display_path!r}: {error}"
            ) from error

        mode = (
            f"{stat.S_IFMT(metadata.st_mode):o}:"
            f"{stat.S_IMODE(metadata.st_mode):o}"
        ).encode("ascii")
        if stat.S_ISLNK(metadata.st_mode):
            content = os.fsencode(os.readlink(path))
            kind = b"symlink"
        elif stat.S_ISREG(metadata.st_mode):
            try:
                content = path.read_bytes()
            except OSError as error:
                raise HarnessError(
                    f"cannot read tracked path {display_path!r}: {error}"
                ) from error
            kind = b"file"
        elif stat.S_ISDIR(metadata.st_mode):
            content = b""
            kind = b"directory"
        else:
            content = b""
            kind = b"special"
        _hash_part(digest, b"state", b"present")
        _hash_part(digest, b"kind", kind)
        _hash_part(digest, b"mode", mode)
        _hash_part(digest, b"content", content)
    return digest.digest()


def _submodule_fingerprint_stream(
    target: Path,
    ancestry: frozenset[Path],
    index_modes: dict[bytes, set[bytes]],
) -> bytes:
    submodules = [
        raw_path
        for raw_path, modes in index_modes.items()
        if b"160000" in modes
    ]

    digest = hashlib.sha256()
    for raw_path in sorted(submodules):
        display_path = os.fsdecode(raw_path)
        path = target / display_path
        _hash_part(digest, b"path", raw_path)
        if path.is_symlink():
            raise HarnessError(
                f"submodule worktree must not be a symbolic link: {display_path}"
            )
        if not path.exists():
            _hash_part(digest, b"state", b"absent")
            continue
        if not path.is_dir():
            raise HarnessError(
                f"submodule worktree must be a directory: {display_path}"
            )

        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
        resolved_path = path.resolve()
        resolved_root = (
            Path(result.stdout.strip()).resolve()
            if result.returncode == 0 and result.stdout.strip()
            else None
        )
        if resolved_root != resolved_path:
            try:
                has_content = any(path.iterdir())
            except OSError as error:
                raise HarnessError(
                    f"cannot inspect submodule worktree {display_path}: {error}"
                ) from error
            if has_content:
                raise HarnessError(
                    "uninitialized submodule worktree must be empty: "
                    f"{display_path}"
                )
            _hash_part(digest, b"state", b"uninitialized")
            continue

        nested = _repository_snapshot(resolved_path, ancestry)
        _hash_part(digest, b"state", b"initialized")
        _hash_part(digest, b"revision", nested.revision.encode("ascii"))
        _hash_part(
            digest,
            b"workspace-fingerprint",
            nested.workspace_fingerprint.encode("ascii"),
        )
    return digest.digest()


def _repository_snapshot(
    target: Path,
    ancestry: frozenset[Path] | None = None,
) -> RepositorySnapshot:
    target = target.resolve()
    ancestors = ancestry or frozenset()
    if target in ancestors:
        raise HarnessError(f"recursive submodule worktree detected: {target}")
    descendants = ancestors | {target}
    status = _git_bytes(
        target,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
        *WORKSPACE_PATHSPEC,
    )
    staged = _git_bytes(
        target,
        "diff",
        "--cached",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--ignore-submodules=none",
        *WORKSPACE_PATHSPEC,
    )
    unstaged = _git_bytes(
        target,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--ignore-submodules=none",
        *WORKSPACE_PATHSPEC,
    )
    untracked = _untracked_fingerprint_stream(target)
    index_modes = _index_modes(target)
    tracked_worktree = _tracked_worktree_fingerprint_stream(
        target,
        index_modes,
    )
    submodules = _submodule_fingerprint_stream(
        target,
        descendants,
        index_modes,
    )
    digest = hashlib.sha256()
    _hash_part(digest, b"status-v2", status)
    _hash_part(digest, b"staged-diff", staged)
    _hash_part(digest, b"unstaged-diff", unstaged)
    _hash_part(digest, b"untracked", untracked)
    _hash_part(digest, b"tracked-worktree", tracked_worktree)
    _hash_part(digest, b"submodules", submodules)
    return RepositorySnapshot(
        revision=_revision(target),
        workspace_fingerprint="sha256:" + digest.hexdigest(),
    )


def snapshot(target: Path) -> dict[str, str]:
    return _repository_snapshot(target).as_dict()


def _review_sha256(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _observed_file_digest(path: Path) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink"
    if not stat.S_ISREG(metadata.st_mode):
        return "non_regular"
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


def _evaluation_observation(
    target: Path,
    paths: RequirementPaths,
    round_number: int,
    goal: str,
) -> dict[str, str]:
    repository = _repository_snapshot(target)
    current = paths.round(round_number)
    return {
        **repository.as_dict(),
        "goal_sha256": _review_sha256(goal),
        "plan_sha256": _observed_file_digest(paths.plan),
        "implementation_sha256": _observed_file_digest(current.implementation),
    }


def _require_same_evaluation_inputs(
    target: Path,
    paths: RequirementPaths,
    round_number: int,
    goal: str,
    evaluation: dict[str, Any],
) -> dict[str, str]:
    current = _evaluation_observation(
        target,
        paths,
        round_number,
        goal,
    )
    if (
        current["revision"] != evaluation.get("revision")
        or current["workspace_fingerprint"]
        != evaluation.get("workspace_fingerprint")
    ):
        raise HarnessError(
            "workspace snapshot changed since begin-evaluation; "
            "preserve the drift and restart evaluation"
        )
    expected = {
        key: evaluation.get(key)
        for key in ("goal_sha256", "plan_sha256", "implementation_sha256")
    }
    observed_inputs = {
        key: current[key]
        for key in ("goal_sha256", "plan_sha256", "implementation_sha256")
    }
    if observed_inputs != expected:
        raise HarnessError(
            "evaluation inputs changed since begin-evaluation; "
            "preserve the drift and restart evaluation"
        )
    transaction_paths = paths.round(round_number)
    archived_plan = _observed_file_digest(transaction_paths.plan)
    archived_implementation = _observed_file_digest(
        transaction_paths.implementation_snapshot
    )
    if (
        archived_plan != evaluation.get("plan_sha256")
        or archived_implementation != evaluation.get("implementation_sha256")
    ):
        raise HarnessError(
            "archived evaluation inputs changed since begin-evaluation"
        )
    return current


def _snapshot_from_evaluation(value: Any) -> RepositorySnapshot | None:
    if not isinstance(value, dict):
        return None
    revision = value.get("revision")
    fingerprint = value.get("workspace_fingerprint")
    if not isinstance(revision, str) or not isinstance(fingerprint, str):
        return None
    return RepositorySnapshot(revision, fingerprint)


def _require_same_snapshot(
    target: Path,
    expected: RepositorySnapshot,
) -> RepositorySnapshot:
    current = _repository_snapshot(target)
    if current != expected:
        raise HarnessError(
            "workspace snapshot changed since begin-evaluation; "
            "preserve the diff and start a new evaluation snapshot"
        )
    return current


def _control_paths(target: Path) -> tuple[Path, Path]:
    control_root = target / ".vibe-coding"
    requirements_root = control_root / "requirements"
    _reject_symlink(control_root, ".vibe-coding")
    _reject_symlink(requirements_root, "requirements")
    return control_root, requirements_root


def _reject_legacy_state(control_root: Path) -> None:
    legacy = [control_root / "state.json", control_root / "progress.md"]
    if any(path.exists() or path.is_symlink() for path in legacy):
        raise HarnessError(
            "legacy global harness state detected; remove it before schema 3"
        )


def _requirement_paths(
    requirements_root: Path, requirement_id: str
) -> RequirementPaths:
    root = requirements_root / requirement_id
    return RequirementPaths(
        root=root,
        state=root / "state.json",
        plan=root / "plan.md",
        rounds=root / "rounds",
    )


def _list_requirements(requirements_root: Path) -> list[RequirementPaths]:
    _reject_symlink(requirements_root, "requirements")
    if not requirements_root.is_dir():
        return []
    paths: list[RequirementPaths] = []
    for child in requirements_root.iterdir():
        if not REQUIREMENT_PATTERN.fullmatch(child.name):
            continue
        _reject_symlink(child, f"requirement directory {child.name}")
        if child.is_dir():
            paths.append(_requirement_paths(requirements_root, child.name))
    return sorted(paths, key=lambda item: item.root.name)


def _next_requirement_id(requirements_root: Path) -> str:
    numbers = [
        int(match.group(1))
        for paths in _list_requirements(requirements_root)
        if (match := REQUIREMENT_PATTERN.fullmatch(paths.root.name))
    ]
    return f"REQ-{max(numbers, default=0) + 1:03d}"


def _select_requirement(
    requirements_root: Path, requirement_id: str | None
) -> RequirementPaths:
    if requirement_id:
        if not REQUIREMENT_PATTERN.fullmatch(requirement_id):
            raise HarnessError("requirement must match REQ-NNN")
        selected = _requirement_paths(requirements_root, requirement_id)
        _reject_symlink(selected.root, f"requirement directory {requirement_id}")
        _reject_symlink(selected.state, "state.json")
        if not selected.state.is_file():
            raise HarnessError(f"requirement does not exist: {requirement_id}")
        return selected

    nonterminal: list[RequirementPaths] = []
    for paths in _list_requirements(requirements_root):
        _reject_symlink(paths.state, "state.json")
        state = _load_state(paths.state)
        if state.get("status") not in TERMINAL_STATUSES:
            nonterminal.append(paths)
    if not nonterminal:
        raise HarnessError("no nonterminal requirement exists to resume")
    if len(nonterminal) > 1:
        ids = ", ".join(paths.root.name for paths in nonterminal)
        raise HarnessError(
            "multiple nonterminal requirements exist; use --requirement: " + ids
        )
    return nonterminal[0]


@contextmanager
def _init_lock(control_root: Path) -> Iterator[None]:
    control_root.mkdir(parents=True, exist_ok=True)
    lock_path = control_root / ".init.lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError as error:
        raise HarnessError(
            "another init is running; inspect .vibe-coding/.init.lock"
        ) from error
    try:
        os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        os.close(descriptor)
        yield
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        lock_path.unlink(missing_ok=True)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_int=_parse_json_integer,
        )
    except HarnessError:
        raise
    except (OSError, UnicodeError) as error:
        raise HarnessError(f"cannot read {path}: {error}") from error
    except (ValueError, RecursionError) as error:
        raise HarnessError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise HarnessError(f"state must be a JSON object: {path}")
    return value


def _read_artifact(path: Path) -> ArtifactSnapshot:
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return ArtifactSnapshot(exists=False, body=None, error=None)
    except OSError as error:
        return ArtifactSnapshot(
            exists=True,
            body=None,
            error=f"cannot be inspected: {error}",
        )
    if not stat.S_ISREG(metadata.st_mode):
        return ArtifactSnapshot(
            exists=True,
            body=None,
            error="is not a regular file",
        )
    try:
        return ArtifactSnapshot(
            exists=True,
            body=path.read_bytes().decode("utf-8"),
            error=None,
        )
    except UnicodeError:
        return ArtifactSnapshot(
            exists=True,
            body=None,
            error="is not valid UTF-8",
        )
    except OSError as error:
        return ArtifactSnapshot(
            exists=True,
            body=None,
            error=f"cannot be read: {error}",
        )


def _read_required_artifact(path: Path, label: str) -> str:
    artifact = _read_artifact(path)
    if artifact.error is not None:
        raise HarnessError(f"{label}: {path} {artifact.error}")
    if artifact.body is None or not artifact.body.strip():
        raise HarnessError(f"{label}: {path} must be a non-empty UTF-8 file")
    return artifact.body


def _write_text_atomic(path: Path, body: str, label: str) -> None:
    _reject_symlink(path, label)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
    )
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_state(path: Path, state: dict[str, Any]) -> None:
    _write_text_atomic(
        path,
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        "state.json",
    )


def _strip_html_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    visible: list[str] = []
    remaining = line
    while remaining:
        if in_comment:
            end = remaining.find("-->")
            if end < 0:
                return "".join(visible), True
            remaining = remaining[end + 3 :]
            in_comment = False
            continue
        start = remaining.find("<!--")
        if start < 0:
            visible.append(remaining)
            break
        visible.append(remaining[:start])
        remaining = remaining[start + 4 :]
        in_comment = True
    return "".join(visible), in_comment


def _fence_start(line: str) -> tuple[str, int] | None:
    match = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", line)
    if match is None:
        return None
    marker, suffix = match.groups()
    if marker[0] == "`" and "`" in suffix:
        return None
    return marker[0], len(marker)


def _fence_closes(line: str, fence: tuple[str, int]) -> bool:
    marker, length = fence
    return bool(
        re.fullmatch(
            rf" {{0,3}}{re.escape(marker)}{{{length},}}[ \t]*",
            line,
        )
    )


def _markdown_level_two_headings(body: str) -> list[MarkdownHeading]:
    headings: list[MarkdownHeading] = []
    offset = 0
    in_comment = False
    fence: tuple[str, int] | None = None
    for raw_line in body.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        if fence is not None:
            if _fence_closes(line, fence):
                fence = None
            offset += len(raw_line)
            continue
        visible, in_comment = _strip_html_comments(line, in_comment)
        fence = _fence_start(visible)
        if fence is not None:
            in_comment = False
        if fence is None:
            boundary = re.fullmatch(r" {0,3}##[ \t]+([^\r\n]*?)[ \t]*", visible)
            if boundary is not None:
                title = boundary.group(1)
                headings.append(
                    MarkdownHeading(
                        title=title,
                        exact=line == f"## {title}",
                        start=offset,
                        content_start=offset + len(raw_line),
                    )
                )
        offset += len(raw_line)
    return headings


def _exact_markdown_section(body: str, title: str, label: str) -> str:
    headings = _markdown_level_two_headings(body)
    selected = [
        (index, heading)
        for index, heading in enumerate(headings)
        if heading.title == title
    ]
    if len(selected) != 1 or not selected[0][1].exact:
        raise HarnessError(f"{label} requires exactly one exact `## {title}` heading")
    index, heading = selected[0]
    end = headings[index + 1].start if index + 1 < len(headings) else len(body)
    return body[heading.content_start:end]


def _plain_markdown_lines(body: str) -> list[str]:
    lines: list[str] = []
    in_comment = False
    fence: tuple[str, int] | None = None
    for raw_line in body.splitlines():
        line = raw_line.rstrip("\r\n")
        if fence is not None:
            if _fence_closes(line, fence):
                fence = None
            continue
        visible, in_comment = _strip_html_comments(line, in_comment)
        fence = _fence_start(visible)
        if fence is not None:
            in_comment = False
            continue
        if visible.strip():
            lines.append(visible.strip())
    return lines


def _acceptance_criteria(body: str) -> list[str]:
    section = _exact_markdown_section(
        body,
        "Acceptance criteria",
        "plan.md",
    )
    criterion_ids: list[str] = []
    for line in _plain_markdown_lines(section):
        match = re.fullmatch(r"- (AC-\d{3}): (.+)", line)
        if match is None or not match.group(2).strip():
            raise HarnessError(
                "plan.md acceptance criteria must use `- AC-NNN: description`"
            )
        criterion_id = match.group(1)
        if criterion_id in criterion_ids:
            raise HarnessError(
                f"duplicate acceptance criterion id in plan.md: {criterion_id}"
            )
        criterion_ids.append(criterion_id)
    if not criterion_ids:
        raise HarnessError("plan.md requires at least one AC-NNN acceptance criterion")
    return criterion_ids


def _evaluation_record(body: str) -> dict[str, Any]:
    section = _exact_markdown_section(
        body,
        "Evaluation record",
        "review.md",
    )
    match = re.fullmatch(
        r"\s*```json[ \t]*\r?\n(.*?)\r?\n```[ \t]*\s*",
        section,
        flags=re.DOTALL,
    )
    if match is None:
        raise HarnessError(
            "review.md Evaluation record must contain exactly one fenced JSON object"
        )
    try:
        record = json.loads(
            match.group(1),
            object_pairs_hook=_strict_json_object,
            parse_int=_parse_json_integer,
        )
    except HarnessError:
        raise
    except (ValueError, RecursionError) as error:
        raise HarnessError(f"invalid JSON in review.md Evaluation record: {error}") from error
    if not isinstance(record, dict):
        raise HarnessError("review.md Evaluation record must be a JSON object")
    return record


def _canonical_artifact_path(value: Any) -> PurePosixPath | None:
    if not _non_empty_string(value):
        return None
    assert isinstance(value, str)
    if any(
        ord(character) < 32
        or ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        return None
    pure_path = PurePosixPath(value)
    if (
        pure_path.is_absolute()
        or value != pure_path.as_posix()
        or any(part in ("", ".", "..") for part in pure_path.parts)
        or not pure_path.parts
        or any(
            part.casefold() in {".git", ".vibe-coding"}
            for part in pure_path.parts
        )
    ):
        return None
    return pure_path


def _observed_artifact(target: Path, path: PurePosixPath) -> str:
    current = target
    metadata: os.stat_result | None = None
    for part in path.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return "missing"
        except (OSError, ValueError, UnicodeError):
            return "unreadable"
        if stat.S_ISLNK(metadata.st_mode):
            return "symlink"
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        return "non_regular"
    try:
        return "sha256:" + hashlib.sha256(current.read_bytes()).hexdigest()
    except (OSError, ValueError, UnicodeError):
        return "unreadable"


def _validate_observations(
    value: Any,
    prefix: str,
    target: Path,
    errors: list[str],
    *,
    verify_artifacts: bool,
) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{prefix}.observations must be a non-empty list")
        return

    seen: set[tuple[str, str]] = set()
    for index, observation in enumerate(value):
        observation_prefix = f"{prefix}.observations[{index}]"
        if not isinstance(observation, dict):
            errors.append(f"{observation_prefix} must be an object")
            continue

        kind = observation.get("kind")
        if kind == "exact":
            if set(observation) != {"kind", "name", "value"}:
                errors.append(
                    f"{observation_prefix} exact observation must contain exactly "
                    "kind, name, value"
                )
            name = observation.get("name")
            if not isinstance(name, str) or not OBSERVATION_NAME_PATTERN.fullmatch(
                name
            ):
                errors.append(
                    f"{observation_prefix}.name must match "
                    "[a-z][a-z0-9_]{0,63}"
                )
            else:
                identity = ("exact", name)
                if identity in seen:
                    errors.append(f"{observation_prefix} duplicates exact {name}")
                seen.add(identity)
            if not _non_empty_string(observation.get("value")):
                errors.append(
                    f"{observation_prefix}.value must be a non-empty exact string"
                )
        elif kind == "metric":
            if set(observation) != {"kind", "name", "value", "unit"}:
                errors.append(
                    f"{observation_prefix} metric observation must contain exactly "
                    "kind, name, value, unit"
                )
            name = observation.get("name")
            if not isinstance(name, str) or not OBSERVATION_NAME_PATTERN.fullmatch(
                name
            ):
                errors.append(
                    f"{observation_prefix}.name must match "
                    "[a-z][a-z0-9_]{0,63}"
                )
            else:
                identity = ("metric", name)
                if identity in seen:
                    errors.append(f"{observation_prefix} duplicates metric {name}")
                seen.add(identity)
            metric_value = observation.get("value")
            valid_metric = type(metric_value) is int or (
                type(metric_value) is float and math.isfinite(metric_value)
            )
            if not valid_metric:
                errors.append(
                    f"{observation_prefix} metric value must be a finite number"
                )
            if not _non_empty_string(observation.get("unit")):
                errors.append(
                    f"{observation_prefix}.unit must be a non-empty string"
                )
        elif kind == "artifact":
            if set(observation) != {"kind", "path", "sha256"}:
                errors.append(
                    f"{observation_prefix} artifact observation must contain exactly "
                    "kind, path, sha256"
                )
            path_value = observation.get("path")
            pure_path = _canonical_artifact_path(path_value)
            if pure_path is None:
                errors.append(
                    f"{observation_prefix}.path must be a canonical "
                    "repository-relative product path"
                )
            else:
                assert isinstance(path_value, str)
                identity = ("artifact", path_value)
                if identity in seen:
                    errors.append(
                        f"{observation_prefix} duplicates artifact {path_value}"
                    )
                seen.add(identity)

            digest_value = observation.get("sha256")
            if (
                not isinstance(digest_value, str)
                or not DIGEST_PATTERN.fullmatch(digest_value)
            ):
                errors.append(
                    f"{observation_prefix}.sha256 must be a sha256 digest"
                )
            elif pure_path is not None and verify_artifacts:
                observed = _observed_artifact(target, pure_path)
                if observed == "symlink":
                    errors.append(
                        f"{observation_prefix}.path must not traverse "
                        "a symbolic link"
                    )
                elif observed in {"missing", "non_regular"}:
                    errors.append(
                        f"{observation_prefix}.path must name an existing "
                        "regular file"
                    )
                elif observed == "unreadable":
                    errors.append(f"{observation_prefix}.path cannot be read")
                elif digest_value != observed:
                    errors.append(
                        f"{observation_prefix}.sha256 does not match target bytes"
                    )
        else:
            errors.append(
                f"{observation_prefix}.kind must be exact, metric, or artifact"
            )


def _artifact_expectations(record: dict[str, Any]) -> set[tuple[str, str]]:
    expectations: set[tuple[str, str]] = set()
    evidence = record.get("evidence")
    if not isinstance(evidence, list):
        return expectations
    for item in evidence:
        if not isinstance(item, dict):
            continue
        observations = item.get("observations")
        if not isinstance(observations, list):
            continue
        for observation in observations:
            if not isinstance(observation, dict) or observation.get("kind") != "artifact":
                continue
            path_value = observation.get("path")
            digest_value = observation.get("sha256")
            pure_path = _canonical_artifact_path(path_value)
            if (
                pure_path is None
                or not isinstance(path_value, str)
                or not isinstance(digest_value, str)
                or not DIGEST_PATTERN.fullmatch(digest_value)
            ):
                continue
            expectations.add((path_value, digest_value))
    return expectations


def _artifact_drift(record: dict[str, Any], target: Path) -> list[dict[str, str]]:
    drift: dict[tuple[str, str], dict[str, str]] = {}
    for path_value, digest_value in _artifact_expectations(record):
        pure_path = _canonical_artifact_path(path_value)
        if pure_path is None:
            continue
        observed = _observed_artifact(target, pure_path)
        if observed != digest_value:
            drift[(path_value, digest_value)] = {
                "path": path_value,
                "expected_sha256": digest_value,
                "observed": observed,
            }
    return [drift[key] for key in sorted(drift)]


def _validate_evaluation_record(
    record: dict[str, Any],
    criterion_ids: list[str],
    target: Path,
    expected_evaluation: dict[str, Any] | None,
    *,
    verify_artifacts: bool = True,
) -> dict[str, Any]:
    errors: list[str] = []
    expected_keys = {
        "schema_version",
        "requirement_id",
        "round",
        "revision",
        "workspace_fingerprint",
        "goal_sha256",
        "plan_sha256",
        "implementation_sha256",
        "verdict",
        "criteria",
        "evidence",
        "residual_risks",
    }
    if set(record) != expected_keys:
        missing = sorted(expected_keys - set(record))
        extra = sorted(set(record) - expected_keys)
        if missing:
            errors.append("missing fields: " + ", ".join(missing))
        if extra:
            errors.append("unexpected fields: " + ", ".join(extra))

    record_version = record.get("schema_version")
    if (
        type(record_version) is not int
        or record_version != EVALUATION_RECORD_VERSION
    ):
        errors.append(
            f"schema_version must be {EVALUATION_RECORD_VERSION}"
        )

    requirement_id = record.get("requirement_id")
    if (
        not isinstance(requirement_id, str)
        or not REQUIREMENT_PATTERN.fullmatch(requirement_id)
    ):
        errors.append("requirement_id must match REQ-NNN")
    round_number = record.get("round")
    if (
        type(round_number) is not int
        or round_number < 1
    ):
        errors.append("round must be a positive integer")

    expected_snapshot = _snapshot_from_evaluation(expected_evaluation)
    revision = record.get("revision")
    if not isinstance(revision, str) or not OBJECT_ID_PATTERN.fullmatch(revision):
        errors.append("revision must be a canonical full commit OID")
    elif _resolve_revision(target, revision) != revision:
        errors.append("revision must resolve exactly in the target repository")
    if (
        expected_snapshot is not None
        and revision != expected_snapshot.revision
    ):
        errors.append("revision must match the evaluation snapshot")

    fingerprint = record.get("workspace_fingerprint")
    if not isinstance(fingerprint, str) or not DIGEST_PATTERN.fullmatch(fingerprint):
        errors.append("workspace_fingerprint must be a sha256 digest")
    if (
        expected_snapshot is not None
        and fingerprint != expected_snapshot.workspace_fingerprint
    ):
        errors.append("workspace_fingerprint must match the evaluation snapshot")

    for field in (
        "goal_sha256",
        "plan_sha256",
        "implementation_sha256",
    ):
        value = record.get(field)
        if not isinstance(value, str) or not DIGEST_PATTERN.fullmatch(value):
            errors.append(f"{field} must be a sha256 digest")

    if expected_evaluation is not None:
        for field in (
            "requirement_id",
            "round",
            "goal_sha256",
            "plan_sha256",
            "implementation_sha256",
        ):
            if record.get(field) != expected_evaluation.get(field):
                errors.append(f"{field} must match the evaluation transaction")

    record_verdict = record.get("verdict")
    if record_verdict not in ("PASS", "FAIL", "UNVERIFIED"):
        errors.append("verdict must be PASS, FAIL, or UNVERIFIED")

    evidence_value = record.get("evidence")
    evidence_ids: set[str] = set()
    if not isinstance(evidence_value, list):
        errors.append("evidence must be a list")
        evidence_value = []
    for index, item in enumerate(evidence_value):
        prefix = f"evidence[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        evidence_id = item.get("id")
        if not _non_empty_string(evidence_id):
            errors.append(f"{prefix}.id must be a non-empty string")
        elif evidence_id in evidence_ids:
            errors.append(f"duplicate evidence id {evidence_id}")
        else:
            assert isinstance(evidence_id, str)
            evidence_ids.add(evidence_id)
        evidence_kind = item.get("kind")
        if evidence_kind == "command":
            expected_evidence_keys = {
                "id",
                "kind",
                "command",
                "exit_code",
                "summary",
                "observations",
            }
            if set(item) != expected_evidence_keys:
                errors.append(
                    f"{prefix} command evidence must contain exactly "
                    "id, kind, command, exit_code, summary, observations"
                )
            if not _non_empty_string(item.get("command")):
                errors.append(f"{prefix}.command must be a non-empty string")
            exit_code = item.get("exit_code")
            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                errors.append(f"{prefix}.exit_code must be an integer")
            if not _non_empty_string(item.get("summary")):
                errors.append(f"{prefix}.summary must be a non-empty string")
            _validate_observations(
                item.get("observations"),
                prefix,
                target,
                errors,
                verify_artifacts=verify_artifacts,
            )
        elif evidence_kind == "inspection":
            expected_evidence_keys = {
                "id",
                "kind",
                "subject",
                "summary",
                "observations",
            }
            if set(item) != expected_evidence_keys:
                errors.append(
                    f"{prefix} inspection evidence must contain exactly "
                    "id, kind, subject, summary, observations"
                )
            if not _non_empty_string(item.get("subject")):
                errors.append(f"{prefix}.subject must be a non-empty string")
            if not _non_empty_string(item.get("summary")):
                errors.append(f"{prefix}.summary must be a non-empty string")
            _validate_observations(
                item.get("observations"),
                prefix,
                target,
                errors,
                verify_artifacts=verify_artifacts,
            )
        else:
            errors.append(f"{prefix}.kind must be command or inspection")

    criteria_value = record.get("criteria")
    seen_criteria: list[str] = []
    criterion_verdicts: list[str] = []
    if not isinstance(criteria_value, list):
        errors.append("criteria must be a list")
        criteria_value = []
    for index, item in enumerate(criteria_value):
        prefix = f"criteria[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if set(item) != {"id", "verdict", "evidence_ids"}:
            errors.append(
                f"{prefix} must contain exactly id, verdict, evidence_ids"
            )
        criterion_id = item.get("id")
        if not isinstance(criterion_id, str) or not CRITERION_PATTERN.fullmatch(
            criterion_id
        ):
            errors.append(f"{prefix}.id must match AC-NNN")
        elif criterion_id in seen_criteria:
            errors.append(f"duplicate criterion id {criterion_id}")
        else:
            seen_criteria.append(criterion_id)
        verdict = item.get("verdict")
        if verdict not in ("PASS", "FAIL", "UNVERIFIED"):
            errors.append(f"{prefix}.verdict must be PASS, FAIL, or UNVERIFIED")
        else:
            criterion_verdicts.append(verdict)
        references = item.get("evidence_ids")
        if not isinstance(references, list) or any(
            not _non_empty_string(reference) for reference in references
        ):
            errors.append(f"{prefix}.evidence_ids must be a list of non-empty strings")
            references = []
        elif len(set(references)) != len(references):
            errors.append(f"{prefix}.evidence_ids must not contain duplicates")
        if verdict == "PASS" and not references:
            errors.append(f"{prefix} PASS requires at least one evidence id")
        for reference in references:
            if reference not in evidence_ids:
                errors.append(f"{prefix} references unknown evidence id {reference}")

    if seen_criteria != criterion_ids:
        errors.append("criteria ids must exactly match plan.md acceptance criteria")

    if criterion_verdicts and len(criterion_verdicts) == len(criterion_ids):
        if "FAIL" in criterion_verdicts:
            derived_verdict = "FAIL"
        elif "UNVERIFIED" in criterion_verdicts:
            derived_verdict = "UNVERIFIED"
        else:
            derived_verdict = "PASS"
        if record_verdict != derived_verdict:
            errors.append(
                f"verdict must be {derived_verdict} based on criteria"
            )

    risks = record.get("residual_risks")
    if not isinstance(risks, list) or any(
        not _non_empty_string(risk) for risk in risks
    ):
        errors.append("residual_risks must be a list of non-empty strings")

    if errors:
        raise HarnessError("invalid Evaluation record: " + "; ".join(errors))
    return record


def _validated_review(
    body: str,
    criterion_ids: list[str],
    target: Path,
    expected_evaluation: dict[str, Any] | None,
    *,
    verify_artifacts: bool = True,
) -> dict[str, Any]:
    return _validate_evaluation_record(
        _evaluation_record(body),
        criterion_ids,
        target,
        expected_evaluation,
        verify_artifacts=verify_artifacts,
    )


def _reject_round_symlinks(
    paths: RoundPaths,
    round_number: int,
    *,
    allow_current_implementation_symlink: bool = False,
) -> None:
    _reject_symlink(paths.root, f"round {round_number:03d} directory")
    _reject_symlink(paths.inputs, "evaluation-inputs")
    _reject_symlink(paths.plan, "archived plan.md")
    if not allow_current_implementation_symlink:
        _reject_symlink(paths.implementation, "implementation.md")
    _reject_symlink(
        paths.implementation_snapshot,
        "archived implementation.md",
    )
    _reject_symlink(paths.review, "review.md")
    _reject_symlink(paths.interruption, "interruption.json")
    _reject_symlink(paths.attempts, "attempts")


def _reject_requirement_symlinks(
    paths: RequirementPaths,
    *,
    allow_current_plan_symlink: bool = False,
) -> None:
    _reject_symlink(paths.root, f"requirement directory {paths.root.name}")
    _reject_symlink(paths.state, "state.json")
    if not allow_current_plan_symlink:
        _reject_symlink(paths.plan, "plan.md")
    _reject_symlink(paths.rounds, "rounds")


def _load_requirement_state(
    paths: RequirementPaths,
    *,
    allow_current_plan_symlink: bool = False,
) -> dict[str, Any]:
    _reject_requirement_symlinks(
        paths,
        allow_current_plan_symlink=allow_current_plan_symlink,
    )
    return _load_state(paths.state)


def _evaluation_errors(
    value: Any,
    target: Path,
) -> tuple[list[str], RepositorySnapshot | None, str]:
    errors: list[str] = []
    if value is None:
        return errors, None, ""
    if not isinstance(value, dict):
        return ["evaluation must be an object or null"], None, ""
    expected_keys = {
        "requirement_id",
        "round",
        "goal",
        "revision",
        "workspace_fingerprint",
        "goal_sha256",
        "plan_sha256",
        "implementation_sha256",
        "acceptance_criteria",
        "review_sha256",
    }
    if set(value) != expected_keys:
        errors.append(
            "evaluation must contain exactly requirement_id, round, goal, "
            "revision, workspace_fingerprint, goal_sha256, plan_sha256, "
            "implementation_sha256, acceptance_criteria, review_sha256"
        )
    requirement_id = value.get("requirement_id")
    if (
        not isinstance(requirement_id, str)
        or not REQUIREMENT_PATTERN.fullmatch(requirement_id)
    ):
        errors.append("evaluation.requirement_id must match REQ-NNN")
    round_number = value.get("round")
    if type(round_number) is not int or round_number < 1:
        errors.append("evaluation.round must be a positive integer")
    goal = value.get("goal")
    if not _non_empty_string(goal):
        errors.append("evaluation.goal must be a non-empty string")
    revision = value.get("revision")
    fingerprint = value.get("workspace_fingerprint")
    review_digest = value.get("review_sha256")
    if not isinstance(revision, str) or not OBJECT_ID_PATTERN.fullmatch(revision):
        errors.append("evaluation.revision must be a canonical full commit OID")
    elif _resolve_revision(target, revision) != revision:
        errors.append("evaluation.revision must resolve exactly")
    if not isinstance(fingerprint, str) or not DIGEST_PATTERN.fullmatch(fingerprint):
        errors.append("evaluation.workspace_fingerprint must be a sha256 digest")
    for field in (
        "goal_sha256",
        "plan_sha256",
        "implementation_sha256",
    ):
        digest_value = value.get(field)
        if (
            not isinstance(digest_value, str)
            or not DIGEST_PATTERN.fullmatch(digest_value)
        ):
            errors.append(f"evaluation.{field} must be a sha256 digest")
    if (
        isinstance(goal, str)
        and isinstance(value.get("goal_sha256"), str)
        and value["goal_sha256"] != _review_sha256(goal)
    ):
        errors.append("evaluation.goal_sha256 must bind evaluation.goal")
    acceptance_criteria = value.get("acceptance_criteria")
    if (
        not isinstance(acceptance_criteria, list)
        or not acceptance_criteria
        or any(
            not isinstance(item, str)
            or not CRITERION_PATTERN.fullmatch(item)
            for item in acceptance_criteria
        )
        or len(set(acceptance_criteria)) != len(acceptance_criteria)
    ):
        errors.append(
            "evaluation.acceptance_criteria must be unique ordered AC-NNN ids"
        )
    if not isinstance(review_digest, str) or (
        review_digest and not DIGEST_PATTERN.fullmatch(review_digest)
    ):
        errors.append("evaluation.review_sha256 must be empty or a sha256 digest")
        review_digest = ""
    snapshot_value = (
        RepositorySnapshot(revision, fingerprint)
        if isinstance(revision, str)
        and OBJECT_ID_PATTERN.fullmatch(revision)
        and isinstance(fingerprint, str)
        and DIGEST_PATTERN.fullmatch(fingerprint)
        else None
    )
    return errors, snapshot_value, review_digest


def _interruption_record(body: str, target: Path) -> dict[str, Any]:
    try:
        record = json.loads(
            body,
            object_pairs_hook=_strict_json_object,
            parse_int=_parse_json_integer,
        )
    except HarnessError:
        raise
    except (ValueError, RecursionError) as error:
        raise HarnessError(f"invalid JSON in interruption.json: {error}") from error
    if not isinstance(record, dict):
        raise HarnessError("interruption.json must be a JSON object")

    errors: list[str] = []
    expected_keys = {
        "schema_version",
        "reason",
        "prior_verdict",
        "evaluation",
        "observed",
        "artifact_drift",
    }
    if set(record) != expected_keys:
        missing = sorted(expected_keys - set(record))
        extra = sorted(set(record) - expected_keys)
        if missing:
            errors.append("missing fields: " + ", ".join(missing))
        if extra:
            errors.append("unexpected fields: " + ", ".join(extra))

    version = record.get("schema_version")
    if type(version) is not int or version != INTERRUPTION_RECORD_VERSION:
        errors.append(
            f"schema_version must be {INTERRUPTION_RECORD_VERSION}"
        )
    if not _non_empty_string(record.get("reason")):
        errors.append("reason must be a non-empty string")
    prior_verdict = record.get("prior_verdict")
    if prior_verdict not in (None, "PASS", "UNVERIFIED"):
        errors.append("prior_verdict must be PASS, UNVERIFIED, or null")

    evaluation_errors, evaluation_snapshot, review_digest = _evaluation_errors(
        record.get("evaluation"),
        target,
    )
    errors.extend(f"interruption {error}" for error in evaluation_errors)
    if evaluation_snapshot is None:
        errors.append(
            "interruption evaluation must be an object with a valid snapshot"
        )
    observed = record.get("observed")
    observed_snapshot: RepositorySnapshot | None = None
    if not isinstance(observed, dict) or set(observed) != {
        "revision",
        "workspace_fingerprint",
        "goal_sha256",
        "plan_sha256",
        "implementation_sha256",
    }:
        errors.append(
            "observed must contain exactly revision, workspace_fingerprint, "
            "goal_sha256, plan_sha256, implementation_sha256"
        )
    else:
        revision = observed.get("revision")
        fingerprint = observed.get("workspace_fingerprint")
        if not isinstance(revision, str) or not OBJECT_ID_PATTERN.fullmatch(revision):
            errors.append("observed.revision must be a canonical full commit OID")
        elif _resolve_revision(target, revision) != revision:
            errors.append("observed.revision must resolve exactly")
        if (
            not isinstance(fingerprint, str)
            or not DIGEST_PATTERN.fullmatch(fingerprint)
        ):
            errors.append(
                "observed.workspace_fingerprint must be a sha256 digest"
            )
        if (
            isinstance(revision, str)
            and OBJECT_ID_PATTERN.fullmatch(revision)
            and isinstance(fingerprint, str)
            and DIGEST_PATTERN.fullmatch(fingerprint)
        ):
            observed_snapshot = RepositorySnapshot(revision, fingerprint)
        goal_digest = observed.get("goal_sha256")
        if (
            not isinstance(goal_digest, str)
            or not DIGEST_PATTERN.fullmatch(goal_digest)
        ):
            errors.append("observed.goal_sha256 must be a sha256 digest")
        for field in ("plan_sha256", "implementation_sha256"):
            observed_digest = observed.get(field)
            if not (
                isinstance(observed_digest, str)
                and (
                    DIGEST_PATTERN.fullmatch(observed_digest)
                    or observed_digest in OBSERVED_FILE_STATES
                )
            ):
                errors.append(
                    f"observed.{field} must be a sha256 digest or file state"
                )

    artifact_drift = record.get("artifact_drift")
    valid_artifact_drift: list[dict[str, Any]] = []
    seen_artifact_drift: set[tuple[str, str]] = set()
    if not isinstance(artifact_drift, list):
        errors.append("artifact_drift must be a list")
    else:
        for index, item in enumerate(artifact_drift):
            prefix = f"artifact_drift[{index}]"
            if not isinstance(item, dict) or set(item) != {
                "path",
                "expected_sha256",
                "observed",
            }:
                errors.append(
                    f"{prefix} must contain exactly path, expected_sha256, observed"
                )
                continue
            path_value = item.get("path")
            expected_digest = item.get("expected_sha256")
            observed_value = item.get("observed")
            if _canonical_artifact_path(path_value) is None:
                errors.append(
                    f"{prefix}.path must be a canonical repository-relative "
                    "product path"
                )
            if (
                not isinstance(expected_digest, str)
                or not DIGEST_PATTERN.fullmatch(expected_digest)
            ):
                errors.append(f"{prefix}.expected_sha256 must be a sha256 digest")
            if not (
                isinstance(observed_value, str)
                and (
                    DIGEST_PATTERN.fullmatch(observed_value)
                    or observed_value in OBSERVED_FILE_STATES
                )
            ):
                errors.append(
                    f"{prefix}.observed must be a sha256 digest or artifact state"
                )
            if (
                isinstance(path_value, str)
                and isinstance(expected_digest, str)
            ):
                identity = (path_value, expected_digest)
                if identity in seen_artifact_drift:
                    errors.append(f"{prefix} duplicates an artifact drift entry")
                seen_artifact_drift.add(identity)
            if observed_value == expected_digest:
                errors.append(f"{prefix}.observed must differ from expected_sha256")
            valid_artifact_drift.append(item)

    evaluation_observation = None
    if isinstance(record.get("evaluation"), dict):
        evaluation_observation = {
            key: record["evaluation"].get(key)
            for key in (
                "revision",
                "workspace_fingerprint",
                "goal_sha256",
                "plan_sha256",
                "implementation_sha256",
            )
        }
    if (
        evaluation_snapshot is not None
        and observed_snapshot is not None
        and observed == evaluation_observation
        and not valid_artifact_drift
    ):
        errors.append(
            "observed evaluation inputs or artifact_drift must differ from evaluation"
        )
    if prior_verdict is None and review_digest:
        errors.append("null prior_verdict requires an empty review_sha256")
    if prior_verdict is None and valid_artifact_drift:
        errors.append("null prior_verdict requires empty artifact_drift")
    if prior_verdict in ("PASS", "UNVERIFIED") and not review_digest:
        errors.append("recorded prior_verdict requires review_sha256")

    if errors:
        raise HarnessError("invalid interruption.json: " + "; ".join(errors))
    return record


def _validate_state(
    paths: RequirementPaths,
    target: Path,
    *,
    state: dict[str, Any] | None = None,
    require_current_snapshot: bool = False,
    allow_pending_evaluation: bool = False,
    allow_pending_restart: bool = False,
    allow_pending_review: bool = False,
    allow_current_evaluation_input_drift: bool = False,
    verify_current_artifacts: bool = True,
) -> list[str]:
    if state is None:
        _reject_requirement_symlinks(
            paths,
            allow_current_plan_symlink=True,
        )
        state = _load_state(paths.state)
    allow_input_drift = (
        allow_current_evaluation_input_drift
        and state.get("phase") == "EVALUATING"
    )
    _reject_requirement_symlinks(
        paths,
        allow_current_plan_symlink=allow_input_drift,
    )
    errors: list[str] = []
    artifacts: dict[Path, ArtifactSnapshot] = {}

    def artifact(path: Path) -> ArtifactSnapshot:
        if path not in artifacts:
            artifacts[path] = _read_artifact(path)
        return artifacts[path]

    def require_artifact(path: Path, message: str) -> str | None:
        value = artifact(path)
        if value.error is not None:
            errors.append(f"{message}: {path} {value.error}")
        elif value.body is None or not value.body.strip():
            errors.append(message)
        return value.body

    def validate_archived_inputs(
        history_round: int,
        evaluation: dict[str, Any],
        prefix: str,
    ) -> None:
        transaction_paths = paths.round(history_round)
        archived_plan = require_artifact(
            transaction_paths.plan,
            f"{prefix} requires archived evaluation-inputs/plan.md",
        )
        archived_implementation = require_artifact(
            transaction_paths.implementation_snapshot,
            f"{prefix} requires archived evaluation-inputs/implementation.md",
        )
        if (
            archived_plan is not None
            and _review_sha256(archived_plan) != evaluation.get("plan_sha256")
        ):
            errors.append(f"{prefix} archived plan digest changed")
        if (
            archived_implementation is not None
            and _review_sha256(archived_implementation)
            != evaluation.get("implementation_sha256")
        ):
            errors.append(f"{prefix} archived implementation digest changed")
        if archived_plan is not None:
            try:
                archived_ids = _acceptance_criteria(archived_plan)
            except HarnessError as error:
                errors.append(f"{prefix}: {error}")
            else:
                if archived_ids != evaluation.get("acceptance_criteria"):
                    errors.append(
                        f"{prefix} archived acceptance criteria ids changed"
                    )

    state_version = state.get("schema_version")
    if type(state_version) is not int or state_version != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if "last_good_revision" in state:
        errors.append("last_good_revision is not supported by schema 3")
    if state.get("requirement_id") != paths.root.name:
        errors.append("requirement_id must match its directory")
    if not _non_empty_string(state.get("goal")):
        errors.append("goal must be a non-empty string")

    status = state.get("status")
    phase = state.get("phase")
    verdict = state.get("latest_verdict")
    round_number = state.get("active_round")
    if not isinstance(status, str) or status not in RUN_STATUSES:
        errors.append(f"status must be one of {sorted(RUN_STATUSES)}")
    if not isinstance(phase, str) or phase not in PHASES:
        errors.append(f"phase must be one of {sorted(PHASES)}")
    if verdict not in VERDICTS:
        errors.append("latest_verdict must be PASS, FAIL, UNVERIFIED, or null")
    if (
        not isinstance(round_number, int)
        or isinstance(round_number, bool)
        or round_number < 1
    ):
        errors.append("active_round must be a positive integer")
        round_number = 1
    elif round_number > MAX_ROUNDS:
        errors.append(f"active_round must not exceed {MAX_ROUNDS}")
        round_number = 1
    if not _non_empty_string(state.get("next_action")):
        errors.append("next_action must be a non-empty string")
    residual_risks = state.get("residual_risks")
    if not isinstance(residual_risks, list) or any(
        not _non_empty_string(risk) for risk in residual_risks
    ):
        errors.append("residual_risks must be a list of non-empty strings")

    raw_evaluation = state.get("evaluation")
    state_review_digest = (
        raw_evaluation.get("review_sha256")
        if isinstance(raw_evaluation, dict)
        and isinstance(raw_evaluation.get("review_sha256"), str)
        else ""
    )
    pending_evaluation = state.get("pending_evaluation")
    if pending_evaluation is not None:
        if (
            not isinstance(pending_evaluation, dict)
            or set(pending_evaluation)
            != {
                "round",
                "plan_body",
                "implementation_body",
                "evaluation",
            }
        ):
            errors.append(
                "pending_evaluation must contain exactly round, plan_body, "
                "implementation_body, and evaluation"
            )
        else:
            pending_round = pending_evaluation.get("round")
            pending_plan = pending_evaluation.get("plan_body")
            pending_implementation = pending_evaluation.get(
                "implementation_body"
            )
            pending_transaction = pending_evaluation.get("evaluation")
            if pending_round != round_number:
                errors.append(
                    "pending_evaluation.round must equal active_round"
                )
            if not _non_empty_string(pending_plan):
                errors.append(
                    "pending_evaluation.plan_body must be non-empty valid text"
                )
            if not _non_empty_string(pending_implementation):
                errors.append(
                    "pending_evaluation.implementation_body must be "
                    "non-empty valid text"
                )
            pending_errors, _, pending_review_digest = _evaluation_errors(
                pending_transaction,
                target,
            )
            errors.extend(
                f"pending_evaluation.evaluation: {error}"
                for error in pending_errors
            )
            if isinstance(pending_transaction, dict):
                if pending_transaction.get("requirement_id") != paths.root.name:
                    errors.append(
                        "pending evaluation requirement_id must match its "
                        "directory"
                    )
                if pending_transaction.get("round") != round_number:
                    errors.append(
                        "pending evaluation round must equal active_round"
                    )
                if (
                    not allow_pending_evaluation
                    and pending_transaction.get("goal") != state.get("goal")
                ):
                    errors.append(
                        "pending evaluation goal must match state.goal"
                    )
                if pending_review_digest:
                    errors.append(
                        "pending evaluation review_sha256 must be empty"
                    )
                if (
                    isinstance(pending_plan, str)
                    and pending_transaction.get("plan_sha256")
                    != _review_sha256(pending_plan)
                ):
                    errors.append(
                        "pending evaluation plan_sha256 must bind plan_body"
                    )
                if (
                    isinstance(pending_implementation, str)
                    and pending_transaction.get("implementation_sha256")
                    != _review_sha256(pending_implementation)
                ):
                    errors.append(
                        "pending evaluation implementation_sha256 must bind "
                        "implementation_body"
                    )
                if isinstance(pending_plan, str):
                    try:
                        pending_ids = _acceptance_criteria(pending_plan)
                    except HarnessError as error:
                        errors.append(f"pending_evaluation: {error}")
                    else:
                        if (
                            pending_transaction.get("acceptance_criteria")
                            != pending_ids
                        ):
                            errors.append(
                                "pending evaluation acceptance criteria must "
                                "bind plan_body"
                            )
        if (
            status != "ACTIVE"
            or phase != "BUILDING"
            or raw_evaluation is not None
        ):
            errors.append(
                "pending_evaluation requires ACTIVE BUILDING with null "
                "evaluation"
            )
        elif not allow_pending_evaluation:
            errors.append(
                "pending evaluation transaction requires "
                "`begin-evaluation` reconciliation"
            )

    pending_review = state.get("pending_review")
    if pending_review is not None:
        if not isinstance(pending_review, dict) or set(pending_review) != {
            "round",
            "body",
            "sha256",
            "prior_review_sha256",
        }:
            errors.append(
                "pending_review must contain exactly round, body, sha256, "
                "and prior_review_sha256"
            )
        else:
            pending_round = pending_review.get("round")
            pending_body = pending_review.get("body")
            pending_digest = pending_review.get("sha256")
            prior_digest = pending_review.get("prior_review_sha256")
            if pending_round != round_number:
                errors.append("pending_review.round must equal active_round")
            if not _non_empty_string(pending_body):
                errors.append("pending_review.body must be non-empty valid text")
            if (
                not isinstance(pending_digest, str)
                or not DIGEST_PATTERN.fullmatch(pending_digest)
            ):
                errors.append("pending_review.sha256 must be a sha256 digest")
            elif (
                isinstance(pending_body, str)
                and _review_sha256(pending_body) != pending_digest
            ):
                errors.append("pending_review.sha256 must bind pending_review.body")
            if not (
                isinstance(prior_digest, str)
                and (
                    not prior_digest
                    or DIGEST_PATTERN.fullmatch(prior_digest)
                )
            ):
                errors.append(
                    "pending_review.prior_review_sha256 must be empty or a digest"
                )
            if verdict is None and prior_digest:
                errors.append(
                    "first pending review requires empty prior_review_sha256"
                )
            if (
                verdict in ("PASS", "UNVERIFIED")
                and prior_digest != state_review_digest
            ):
                errors.append(
                    "replacement pending review must bind the current review"
                )
        if status != "ACTIVE" or phase != "EVALUATING":
            errors.append("pending_review requires ACTIVE EVALUATING")
        elif not allow_pending_review:
            errors.append(
                "pending review transaction requires `init --resume` or "
                "`record-review` reconciliation"
            )

    pending_interruption = state.get("pending_interruption")
    state_pending_interruption_record: dict[str, Any] | None = None
    if pending_interruption is not None:
        if not isinstance(pending_interruption, dict):
            errors.append("pending_interruption must be an object or null")
        else:
            try:
                state_pending_interruption_record = _interruption_record(
                    json.dumps(
                        pending_interruption,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    target,
                )
            except HarnessError as error:
                errors.append(f"pending_interruption: {error}")
        if phase != "EVALUATING":
            errors.append("pending_interruption requires EVALUATING")
        elif not allow_pending_restart:
            errors.append(
                "pending interruption transaction requires "
                "`restart-evaluation` reconciliation"
            )
    if sum(
        value is not None
        for value in (
            pending_evaluation,
            pending_review,
            pending_interruption,
        )
    ) > 1:
        errors.append("only one pending lifecycle transaction may exist")

    def audit_evaluation(
        value: Any,
        prefix: str,
        expected_round: int,
    ) -> tuple[dict[str, Any] | None, RepositorySnapshot | None, str]:
        evaluation_errors, snapshot_value, digest_value = _evaluation_errors(
            value,
            target,
        )
        errors.extend(f"{prefix} {error}" for error in evaluation_errors)
        if not isinstance(value, dict):
            return None, snapshot_value, digest_value
        if value.get("requirement_id") != paths.root.name:
            errors.append(f"{prefix} requirement_id must match its directory")
        if value.get("round") != expected_round:
            errors.append(f"{prefix} round must match the history entry")
        if not digest_value:
            errors.append(f"{prefix} review_sha256 must be populated")
        return value, snapshot_value, digest_value

    failed_evaluations: dict[
        int,
        tuple[dict[str, Any], RepositorySnapshot | None, str],
    ] = {}
    failed_value = state.get("failed_evaluations")
    if not isinstance(failed_value, list):
        errors.append("failed_evaluations must be a list")
    else:
        previous_failed_round = 0
        for index, item in enumerate(failed_value):
            prefix = f"failed_evaluations[{index}]"
            if not isinstance(item, dict) or set(item) != {
                "round",
                "evaluation",
            }:
                errors.append(
                    f"{prefix} must contain exactly round and evaluation"
                )
                continue
            failed_round = item.get("round")
            if type(failed_round) is not int or failed_round < 1:
                errors.append(f"{prefix}.round must be a positive integer")
                continue
            if failed_round <= previous_failed_round:
                errors.append("failed_evaluations must be ordered by unique round")
            previous_failed_round = failed_round
            failed_evaluation, failed_snapshot, failed_digest = audit_evaluation(
                item.get("evaluation"),
                prefix,
                failed_round,
            )
            if failed_evaluation is not None:
                failed_evaluations[failed_round] = (
                    failed_evaluation,
                    failed_snapshot,
                    failed_digest,
                )

    review_attempts: dict[
        int,
        list[tuple[int, dict[str, Any], RepositorySnapshot | None, str]],
    ] = {}
    attempts_value = state.get("review_attempts")
    if not isinstance(attempts_value, list):
        errors.append("review_attempts must be a list")
    else:
        previous_attempt = (0, 0)
        expected_sequence: dict[int, int] = {}
        for index, item in enumerate(attempts_value):
            prefix = f"review_attempts[{index}]"
            if not isinstance(item, dict) or set(item) != {
                "round",
                "sequence",
                "evaluation",
            }:
                errors.append(
                    f"{prefix} must contain exactly round, sequence, and evaluation"
                )
                continue
            attempt_round = item.get("round")
            sequence = item.get("sequence")
            if type(attempt_round) is not int or attempt_round < 1:
                errors.append(f"{prefix}.round must be a positive integer")
                continue
            if type(sequence) is not int or sequence < 1:
                errors.append(f"{prefix}.sequence must be a positive integer")
                continue
            identity = (attempt_round, sequence)
            if identity <= previous_attempt:
                errors.append(
                    "review_attempts must be ordered by round and sequence"
                )
            previous_attempt = identity
            required_sequence = expected_sequence.get(attempt_round, 1)
            if sequence != required_sequence:
                errors.append(
                    f"{prefix}.sequence must be {required_sequence}"
                )
            expected_sequence[attempt_round] = required_sequence + 1
            attempt_evaluation, attempt_snapshot, attempt_digest = audit_evaluation(
                item.get("evaluation"),
                prefix,
                attempt_round,
            )
            if attempt_evaluation is not None:
                review_attempts.setdefault(attempt_round, []).append(
                    (
                        sequence,
                        attempt_evaluation,
                        attempt_snapshot,
                        attempt_digest,
                    )
                )

    interruption_history: dict[int, str] = {}
    interruption_value = state.get("interruption_history")
    if not isinstance(interruption_value, list):
        errors.append("interruption_history must be a list")
    else:
        previous_interruption_round = 0
        for index, item in enumerate(interruption_value):
            prefix = f"interruption_history[{index}]"
            if not isinstance(item, dict) or set(item) != {"round", "sha256"}:
                errors.append(f"{prefix} must contain exactly round and sha256")
                continue
            interrupted_round = item.get("round")
            digest_value = item.get("sha256")
            if type(interrupted_round) is not int or interrupted_round < 1:
                errors.append(f"{prefix}.round must be a positive integer")
                continue
            if interrupted_round <= previous_interruption_round:
                errors.append(
                    "interruption_history must be ordered by unique round"
                )
            previous_interruption_round = interrupted_round
            if (
                not isinstance(digest_value, str)
                or not DIGEST_PATTERN.fullmatch(digest_value)
            ):
                errors.append(f"{prefix}.sha256 must be a sha256 digest")
                continue
            interruption_history[interrupted_round] = digest_value

    accepted_revision = state.get("accepted_revision")
    if not isinstance(accepted_revision, str):
        errors.append("accepted_revision must be a string")
        accepted_revision = ""
    elif accepted_revision:
        if not OBJECT_ID_PATTERN.fullmatch(accepted_revision):
            errors.append("accepted_revision must be a canonical full commit OID")
        elif _resolve_revision(target, accepted_revision) != accepted_revision:
            errors.append("accepted_revision must resolve exactly")

    evaluation_error_values, evaluation_snapshot, review_digest = _evaluation_errors(
        state.get("evaluation"),
        target,
    )
    errors.extend(evaluation_error_values)
    evaluation_value = state.get("evaluation")
    if isinstance(evaluation_value, dict):
        if evaluation_value.get("requirement_id") != paths.root.name:
            errors.append("evaluation.requirement_id must match its directory")
        if evaluation_value.get("round") != round_number:
            errors.append("evaluation.round must equal active_round")

    if phase == "EVALUATING":
        if evaluation_snapshot is None:
            errors.append("EVALUATING requires an evaluation snapshot")
        if isinstance(evaluation_value, dict):
            validate_archived_inputs(
                round_number,
                evaluation_value,
                "current evaluation",
            )
    elif state.get("evaluation") is not None:
        errors.append("evaluation must be null outside EVALUATING")
    if phase == "EVALUATING" and verdict is None and review_digest:
        errors.append("null latest_verdict requires an empty review_sha256")
    if phase == "EVALUATING" and verdict in ("PASS", "UNVERIFIED") and not review_digest:
        errors.append("recorded latest_verdict requires review_sha256")

    if status != "ACCEPTED" and accepted_revision:
        errors.append("accepted_revision must be empty before acceptance")

    current = paths.round(round_number)
    _reject_round_symlinks(
        current,
        round_number,
        allow_current_implementation_symlink=allow_input_drift,
    )
    plan_ids: list[str] | None = None
    if (
        phase in ("BUILDING", "EVALUATING")
        or status in TERMINAL_STATUSES
    ) and not allow_input_drift:
        plan_body = require_artifact(paths.plan, "phase requires non-empty plan.md")
        if plan_body is not None:
            try:
                plan_ids = _acceptance_criteria(plan_body)
            except HarnessError as error:
                errors.append(str(error))
    if (
        phase == "EVALUATING"
        and isinstance(evaluation_value, dict)
        and plan_ids is not None
        and evaluation_value.get("acceptance_criteria") != plan_ids
    ):
        errors.append(
            "plan.md acceptance criteria ids changed since begin-evaluation"
        )

    if (
        phase == "EVALUATING"
        or status == "ACCEPTED"
    ) and not allow_input_drift:
        require_artifact(
            current.implementation,
            "EVALUATING requires current implementation.md",
        )

    current_review = artifact(current.review)
    current_review_record: dict[str, Any] | None = None
    if (
        pending_review is None
        and phase == "EVALUATING"
        and verdict is None
        and current_review.exists
    ):
        detail = current_review.error or "has pending content"
        errors.append(
            "pending review requires `init --resume` reconciliation: "
            f"{current.review} {detail}"
        )

    if verdict in ("PASS", "UNVERIFIED") or status == "ACCEPTED":
        review_body = require_artifact(
            current.review,
            "verdict requires current review.md",
        )
        if (
            review_body is not None
            and isinstance(evaluation_value, dict)
            and evaluation_snapshot is not None
            and isinstance(evaluation_value.get("acceptance_criteria"), list)
        ):
            actual_digest = _review_sha256(review_body)
            if review_digest != actual_digest:
                errors.append("review digest changed after record-review")
            try:
                record = _validated_review(
                    review_body,
                    evaluation_value["acceptance_criteria"],
                    target,
                    evaluation_value,
                    verify_artifacts=verify_current_artifacts,
                )
            except HarnessError as error:
                errors.append(str(error))
            else:
                current_review_record = record
                if record["verdict"] != verdict:
                    errors.append(
                        "current review verdict must equal latest_verdict "
                        f"{verdict}"
                    )
                review_risks = record["residual_risks"]
                if isinstance(residual_risks, list):
                    if (
                        status == "BLOCKED"
                        and residual_risks[: len(review_risks)] != review_risks
                    ):
                        errors.append(
                            "BLOCKED residual_risks must preserve current "
                            "review risks before orchestration risks"
                        )
                    elif status != "BLOCKED" and residual_risks != review_risks:
                        errors.append(
                            "state residual_risks must match the current review"
                        )

    current_interruption = artifact(current.interruption)
    if current_interruption.exists:
        if current_interruption.error is not None:
            errors.append(
                "current interruption.json "
                f"{current_interruption.error}"
            )
        elif current_interruption.body is None:
            errors.append("current interruption.json must not be empty")
        else:
            try:
                file_interruption_record = _interruption_record(
                    current_interruption.body,
                    target,
                )
            except HarnessError as error:
                errors.append(str(error))
            else:
                if state_pending_interruption_record is None:
                    errors.append(
                        "current interruption.json lacks a matching "
                        "pending_interruption transaction"
                    )
                elif file_interruption_record != state_pending_interruption_record:
                    errors.append(
                        "current interruption.json does not match "
                        "pending_interruption"
                    )
                interruption_artifact_keys = {
                    (item["path"], item["expected_sha256"])
                    for item in file_interruption_record["artifact_drift"]
                }
                if (
                    interruption_artifact_keys
                    and (
                        current_review_record is None
                        or not interruption_artifact_keys.issubset(
                            _artifact_expectations(current_review_record)
                        )
                    )
                ):
                    errors.append(
                        "current interruption artifact_drift must reference "
                        "artifact observations from review"
                    )
                if (
                    file_interruption_record["evaluation"]
                    != state.get("evaluation")
                ):
                    errors.append(
                        "current interruption evaluation must equal state evaluation"
                    )
                if file_interruption_record["prior_verdict"] != verdict:
                    errors.append(
                        "current interruption prior_verdict must equal "
                        "latest_verdict"
                    )
        if phase != "EVALUATING":
            errors.append(
                "current interruption.json is only valid during EVALUATING"
            )
        elif not allow_pending_restart:
            errors.append(
                "pending interruption requires `restart-evaluation` reconciliation"
            )

    for attempt_round, attempt_entries in review_attempts.items():
        if attempt_round > round_number:
            errors.append(
                f"review_attempts round {attempt_round:03d} is in the future"
            )
        attempt_paths = paths.round(attempt_round)
        for sequence, attempt_evaluation, _, attempt_digest in attempt_entries:
            attempt_path = attempt_paths.attempts / f"{sequence:03d}.md"
            _reject_symlink(
                attempt_path,
                f"review attempt {attempt_round:03d}/{sequence:03d}",
            )
            attempt_body = require_artifact(
                attempt_path,
                (
                    f"review attempt {attempt_round:03d}/{sequence:03d} "
                    "requires archived review bytes"
                ),
            )
            if attempt_body is None:
                continue
            if _review_sha256(attempt_body) != attempt_digest:
                errors.append(
                    f"review attempt {attempt_round:03d}/{sequence:03d} "
                    "digest must equal its evaluation"
                )
            try:
                attempt_record = _validated_review(
                    attempt_body,
                    attempt_evaluation["acceptance_criteria"],
                    target,
                    attempt_evaluation,
                    verify_artifacts=False,
                )
            except (HarnessError, KeyError) as error:
                errors.append(
                    f"review attempt {attempt_round:03d}/{sequence:03d}: {error}"
                )
            else:
                if attempt_record["verdict"] not in ("PASS", "UNVERIFIED"):
                    errors.append(
                        f"review attempt {attempt_round:03d}/{sequence:03d} "
                        "must precede replacement from PASS or UNVERIFIED"
                    )

    authoritative_evaluations: dict[int, dict[str, Any]] = {
        failed_round: failed_entry[0]
        for failed_round, failed_entry in failed_evaluations.items()
    }
    if isinstance(evaluation_value, dict):
        authoritative_evaluations[round_number] = evaluation_value

    previous_transition_verdict: str | None = None
    seen_failed_rounds: set[int] = set()
    seen_interrupted_rounds: set[int] = set()
    for history_number in range(1, round_number):
        history = paths.round(history_number)
        _reject_round_symlinks(
            history,
            history_number,
            allow_current_implementation_symlink=True,
        )
        interruption_artifact = artifact(history.interruption)
        if interruption_artifact.exists:
            previous_transition_verdict = None
            seen_interrupted_rounds.add(history_number)
            interruption_body = require_artifact(
                history.interruption,
                (
                    f"history round {history_number:03d} requires a valid "
                    "interruption.json"
                ),
            )
            history_interruption: dict[str, Any] | None = None
            if interruption_body is not None:
                interruption_digest = interruption_history.get(history_number)
                if interruption_digest is None:
                    errors.append(
                        f"history round {history_number:03d} interruption "
                        "requires an interruption_history digest"
                    )
                elif _review_sha256(interruption_body) != interruption_digest:
                    errors.append(
                        f"history round {history_number:03d} interruption "
                        "digest changed after restart-evaluation"
                    )
                try:
                    history_interruption = _interruption_record(
                        interruption_body,
                        target,
                    )
                except HarnessError as error:
                    errors.append(
                        f"history round {history_number:03d}: {error}"
                    )
                else:
                    authoritative_evaluations[history_number] = (
                        history_interruption["evaluation"]
                    )
                    validate_archived_inputs(
                        history_number,
                        history_interruption["evaluation"],
                        f"history round {history_number:03d}",
                    )
                    if (
                        history_interruption["evaluation"].get("requirement_id")
                        != paths.root.name
                    ):
                        errors.append(
                            f"history round {history_number:03d} interruption "
                            "requirement_id must match its directory"
                        )
                    if (
                        history_interruption["evaluation"].get("round")
                        != history_number
                    ):
                        errors.append(
                            f"history round {history_number:03d} interruption "
                            "evaluation round must match"
                        )

            history_review_artifact = artifact(history.review)
            history_review_record: dict[str, Any] | None = None
            if history_review_artifact.exists:
                history_review = require_artifact(
                    history.review,
                    (
                        f"history round {history_number:03d} review.md "
                        "must be valid"
                    ),
                )
                if (
                    history_review is not None
                    and history_interruption is not None
                ):
                    _, _, interrupted_review_digest = (
                        _evaluation_errors(
                            history_interruption["evaluation"],
                            target,
                        )
                    )
                    try:
                        record = _validated_review(
                            history_review,
                            history_interruption["evaluation"][
                                "acceptance_criteria"
                            ],
                            target,
                            history_interruption["evaluation"],
                            verify_artifacts=False,
                        )
                    except (HarnessError, KeyError) as error:
                        errors.append(
                            f"history round {history_number:03d}: {error}"
                        )
                    else:
                        history_review_record = record
                        if (
                            record["verdict"]
                            != history_interruption["prior_verdict"]
                        ):
                            errors.append(
                                f"history round {history_number:03d} review "
                                "verdict must equal interruption prior_verdict"
                            )
                        if (
                            _review_sha256(history_review)
                            != interrupted_review_digest
                        ):
                            errors.append(
                                f"history round {history_number:03d} review "
                                "digest must equal interruption evaluation"
                            )
            elif (
                history_interruption is not None
                and history_interruption["prior_verdict"] is not None
            ):
                errors.append(
                    f"history round {history_number:03d} recorded verdict "
                    "requires review.md"
                )
            if history_interruption is not None:
                interruption_artifact_keys = {
                    (item["path"], item["expected_sha256"])
                    for item in history_interruption["artifact_drift"]
                }
                if (
                    interruption_artifact_keys
                    and (
                        history_review_record is None
                        or not interruption_artifact_keys.issubset(
                            _artifact_expectations(history_review_record)
                        )
                    )
                ):
                    errors.append(
                        f"history round {history_number:03d} artifact_drift "
                        "must reference artifact observations from review"
                    )
            continue

        previous_transition_verdict = "FAIL"
        seen_failed_rounds.add(history_number)
        history_review = require_artifact(
            history.review,
            f"history round {history_number:03d} requires review.md",
        )
        failed_entry = failed_evaluations.get(history_number)
        if failed_entry is None:
            errors.append(
                f"history round {history_number:03d} requires a failed_evaluations "
                "receipt"
            )
        if history_review is not None and failed_entry is not None:
            failed_evaluation, _, failed_digest = failed_entry
            validate_archived_inputs(
                history_number,
                failed_evaluation,
                f"history round {history_number:03d}",
            )
            try:
                record = _validated_review(
                    history_review,
                    failed_evaluation["acceptance_criteria"],
                    target,
                    failed_evaluation,
                    verify_artifacts=False,
                )
            except (HarnessError, KeyError) as error:
                errors.append(
                    f"history round {history_number:03d}: {error}"
                )
            else:
                if _review_sha256(history_review) != failed_digest:
                    errors.append(
                        f"history round {history_number:03d} review digest "
                        "must equal failed evaluation"
                    )
                if record["verdict"] != "FAIL":
                    errors.append(
                        f"history round {history_number:03d} verdict "
                        "must be FAIL"
                    )

    for failed_round in sorted(set(failed_evaluations) - seen_failed_rounds):
        errors.append(
            f"failed_evaluations round {failed_round:03d} is not a failed "
            "historical round"
        )
    for interrupted_round in sorted(
        set(interruption_history) - seen_interrupted_rounds
    ):
        errors.append(
            f"interruption_history round {interrupted_round:03d} is not an "
            "interrupted historical round"
        )

    for attempt_round, attempt_entries in review_attempts.items():
        authority = authoritative_evaluations.get(attempt_round)
        if authority is None:
            errors.append(
                f"review_attempts round {attempt_round:03d} lacks an "
                "authoritative evaluation"
            )
            continue
        authority_identity = {
            key: value
            for key, value in authority.items()
            if key != "review_sha256"
        }
        for sequence, attempt_evaluation, _, _ in attempt_entries:
            attempt_identity = {
                key: value
                for key, value in attempt_evaluation.items()
                if key != "review_sha256"
            }
            if attempt_identity != authority_identity:
                errors.append(
                    f"review attempt {attempt_round:03d}/{sequence:03d} "
                    "must match its round evaluation transaction"
                )

    if phase == "PLANNING":
        if round_number != 1 or verdict is not None:
            errors.append("PLANNING requires round 1 and null latest_verdict")
        if state.get("evaluation") is not None:
            errors.append("PLANNING cannot retain an evaluation snapshot")
    if phase == "BUILDING":
        if round_number > 1 and verdict != previous_transition_verdict:
            expected = previous_transition_verdict or "null"
            errors.append(
                "BUILDING latest_verdict must be "
                f"{expected} based on the previous round transition"
            )
        if round_number == 1 and verdict is not None:
            errors.append("first BUILDING round requires null latest_verdict")
    if verdict == "FAIL":
        if phase != "BUILDING" or round_number < 2:
            errors.append("FAIL requires the next BUILDING round")
    if verdict == "UNVERIFIED" and phase != "EVALUATING":
        errors.append("UNVERIFIED must remain in EVALUATING")
    if verdict == "PASS" and phase != "EVALUATING":
        errors.append("PASS must remain in EVALUATING until accept")

    if status == "DEGRADED" and not _non_empty_string(
        state.get("degradation_acceptance")
    ):
        errors.append("DEGRADED requires degradation_acceptance from the user")

    if status == "ACCEPTED":
        if phase != "EVALUATING" or verdict != "PASS":
            errors.append("ACCEPTED requires EVALUATING with latest_verdict PASS")
        if evaluation_snapshot is None:
            errors.append("ACCEPTED requires an evaluation snapshot")
        elif accepted_revision != evaluation_snapshot.revision:
            errors.append(
                "accepted_revision must equal the evaluated revision"
            )
    if require_current_snapshot:
        if status != "ACCEPTED":
            errors.append("final check requires status ACCEPTED")
        elif (
            evaluation_snapshot is not None
            and isinstance(evaluation_value, dict)
            and isinstance(state.get("goal"), str)
        ):
            try:
                _require_same_evaluation_inputs(
                    target,
                    paths,
                    round_number,
                    state["goal"],
                    evaluation_value,
                )
            except HarnessError as error:
                errors.append(str(error))
    return errors


def _apply_review_transition(
    state: dict[str, Any],
    record: dict[str, Any],
    review_digest: str,
) -> dict[str, Any]:
    updated = dict(state)
    verdict = record["verdict"]
    updated["latest_verdict"] = verdict
    updated["residual_risks"] = list(record["residual_risks"])
    if verdict == "FAIL":
        failed_evaluation = dict(state["evaluation"])
        failed_evaluation["review_sha256"] = review_digest
        failed_evaluations = list(state["failed_evaluations"])
        failed_evaluations.append(
            {
                "round": state["active_round"],
                "evaluation": failed_evaluation,
            }
        )
        updated["failed_evaluations"] = failed_evaluations
        updated["phase"] = "BUILDING"
        updated["active_round"] = state["active_round"] + 1
        updated["evaluation"] = None
        updated["next_action"] = (
            "Dispatch a fresh Implementer for the next round, then begin evaluation."
        )
    else:
        evaluation = dict(state["evaluation"])
        evaluation["review_sha256"] = review_digest
        updated["evaluation"] = evaluation
        updated["phase"] = "EVALUATING"
        updated["next_action"] = (
            "Run accept after PASS."
            if verdict == "PASS"
            else "Resolve missing evidence and replace review.md on this snapshot."
        )
    return updated


def _archive_replaced_review(
    paths: RequirementPaths,
    state: dict[str, Any],
    body: str | None,
) -> dict[str, Any]:
    evaluation = state.get("evaluation")
    if not isinstance(evaluation, dict):
        raise HarnessError("review replacement requires an evaluation transaction")
    expected_digest = evaluation.get("review_sha256")
    if not isinstance(expected_digest, str) or not DIGEST_PATTERN.fullmatch(
        expected_digest
    ):
        raise HarnessError("review replacement requires the prior review digest")
    round_number = state["active_round"]
    current = paths.round(round_number)
    entries = [
        item
        for item in state["review_attempts"]
        if isinstance(item, dict) and item.get("round") == round_number
    ]
    sequence = len(entries) + 1
    archive = current.attempts / f"{sequence:03d}.md"
    _reject_symlink(archive, "archived review attempt")
    existing = _read_artifact(archive)
    if existing.exists:
        if existing.error is not None or existing.body is None:
            raise HarnessError(
                "archived review attempt cannot be read: "
                f"{existing.error or 'empty artifact'}"
            )
        if _review_sha256(existing.body) != expected_digest:
            raise HarnessError(
                "archived review attempt does not match the prior review"
            )
    else:
        if body is None or _review_sha256(body) != expected_digest:
            raise HarnessError(
                "pending review replacement requires its prior archived bytes"
            )
        _write_text_atomic(archive, body, "archived review attempt")

    updated = dict(state)
    attempts = list(state["review_attempts"])
    attempts.append(
        {
            "round": round_number,
            "sequence": sequence,
            "evaluation": dict(evaluation),
        }
    )
    updated["review_attempts"] = attempts
    return updated


def _reconcile_pending_review(
    paths: RequirementPaths,
    target: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    pending = state.get("pending_review")
    if pending is None:
        return state
    if (
        state.get("schema_version") != SCHEMA_VERSION
        or state.get("status") != "ACTIVE"
        or state.get("phase") != "EVALUATING"
        or state.get("latest_verdict") not in (None, "PASS", "UNVERIFIED")
    ):
        return state
    evaluation = state.get("evaluation")
    evaluation_snapshot = _snapshot_from_evaluation(evaluation)
    if (
        evaluation_snapshot is None
        or not isinstance(evaluation, dict)
        or not isinstance(evaluation.get("acceptance_criteria"), list)
    ):
        return state
    round_number = state.get("active_round")
    if not isinstance(round_number, int) or isinstance(round_number, bool):
        return state
    current = paths.round(round_number)
    _reject_round_symlinks(current, round_number)
    if not isinstance(pending, dict):
        raise HarnessError("cannot reconcile malformed pending_review")
    body = pending.get("body")
    digest = pending.get("sha256")
    prior_digest = pending.get("prior_review_sha256")
    if (
        not isinstance(body, str)
        or not isinstance(digest, str)
        or _review_sha256(body) != digest
        or not isinstance(prior_digest, str)
    ):
        raise HarnessError("cannot reconcile malformed pending_review")
    record = _validated_review(
        body,
        evaluation["acceptance_criteria"],
        target,
        evaluation,
        verify_artifacts=False,
    )
    if record["verdict"] == "FAIL" and round_number >= MAX_ROUNDS:
        raise HarnessError(
            "maximum evaluation rounds reached before FAIL transition"
        )

    review = _read_artifact(current.review)
    if review.error is not None:
        raise HarnessError(
            f"cannot reconcile pending review.md: {review.error}"
        )
    working = state
    if prior_digest:
        if review.body is not None and _review_sha256(review.body) == prior_digest:
            working = _archive_replaced_review(paths, state, review.body)
        else:
            working = _archive_replaced_review(paths, state, None)
    if review.body is None or _review_sha256(review.body) != digest:
        if review.body is not None and not prior_digest:
            raise HarnessError(
                "pending first review conflicts with existing review.md"
            )
        _write_text_atomic(current.review, body, "review.md")
    updated = _apply_review_transition(working, record, digest)
    updated["pending_review"] = None
    errors = _validate_state(
        paths,
        target,
        state=updated,
        verify_current_artifacts=False,
    )
    if errors:
        raise HarnessError(
            "reconciled review would create invalid state: " + "; ".join(errors)
        )
    _write_state(paths.state, updated)
    return updated


def init(
    target: Path,
    goal: str | None,
    resume: bool,
    requirement_id: str | None,
) -> dict[str, Any]:
    control_root, requirements_root = _control_paths(target)
    _reject_legacy_state(control_root)
    if resume:
        paths = _select_requirement(requirements_root, requirement_id)
        state = _load_requirement_state(paths)
        state = _reconcile_pending_review(paths, target, state)
        errors = _validate_state(
            paths,
            target,
            state=state,
            verify_current_artifacts=False,
        )
        if errors:
            raise HarnessError("invalid requirement state: " + "; ".join(errors))
        if goal and goal != state.get("goal"):
            raise HarnessError("resume goal does not match the selected requirement")
        return {
            "resumed": True,
            "requirement_id": paths.root.name,
            "goal": state["goal"],
            "status": state["status"],
            "phase": state["phase"],
            "active_round": state["active_round"],
            "next_action": state["next_action"],
            "accepted_revision": state["accepted_revision"],
            "evaluation": state["evaluation"],
        }

    if requirement_id is not None:
        raise HarnessError("--requirement is only valid with --resume")
    if not _non_empty_string(goal):
        raise HarnessError("--goal is required when starting a requirement")

    with _init_lock(control_root):
        requirements_root.mkdir(parents=True, exist_ok=True)
        new_id = _next_requirement_id(requirements_root)
        paths = _requirement_paths(requirements_root, new_id)
        paths.root.mkdir()
        state = {
            "schema_version": SCHEMA_VERSION,
            "requirement_id": new_id,
            "goal": goal,
            "status": "ACTIVE",
            "phase": "PLANNING",
            "active_round": 1,
            "next_action": "Dispatch Planner and persist plan.md.",
            "accepted_revision": "",
            "evaluation": None,
            "latest_verdict": None,
            "residual_risks": [],
            "failed_evaluations": [],
            "review_attempts": [],
            "interruption_history": [],
            "pending_evaluation": None,
            "pending_review": None,
            "pending_interruption": None,
        }
        _write_state(paths.state, state)

    return {
        "created": [f".vibe-coding/requirements/{new_id}/state.json"],
        "requirement_id": new_id,
        "goal": goal,
        "status": "ACTIVE",
        "phase": "PLANNING",
        "active_round": 1,
        "accepted_revision": "",
        "evaluation": None,
    }


def begin_evaluation(
    target: Path,
    requirement_id: str | None,
) -> dict[str, Any]:
    _, requirements_root = _control_paths(target)
    paths = _select_requirement(requirements_root, requirement_id)
    state = _load_requirement_state(paths)
    errors = _validate_state(
        paths,
        target,
        state=state,
        allow_pending_evaluation=True,
    )
    if errors:
        raise HarnessError("invalid requirement state: " + "; ".join(errors))
    if state["status"] != "ACTIVE":
        raise HarnessError("begin-evaluation requires status ACTIVE")
    if state["phase"] != "BUILDING":
        raise HarnessError("begin-evaluation requires BUILDING")

    plan_body = _read_required_artifact(
        paths.plan,
        "begin-evaluation requires plan.md",
    )
    criterion_ids = _acceptance_criteria(plan_body)
    current = paths.round(state["active_round"])
    _reject_round_symlinks(current, state["active_round"])
    implementation_body = _read_required_artifact(
        current.implementation,
        "begin-evaluation requires implementation.md",
    )
    review = _read_artifact(current.review)
    if review.exists:
        raise HarnessError(
            "begin-evaluation requires current review.md to be absent"
        )

    repository_snapshot = _repository_snapshot(target)
    evaluation = {
        "requirement_id": paths.root.name,
        "round": state["active_round"],
        "goal": state["goal"],
        **repository_snapshot.as_dict(),
        "goal_sha256": _review_sha256(state["goal"]),
        "plan_sha256": _review_sha256(plan_body),
        "implementation_sha256": _review_sha256(implementation_body),
        "acceptance_criteria": criterion_ids,
        "review_sha256": "",
    }
    pending_evaluation = {
        "round": state["active_round"],
        "plan_body": plan_body,
        "implementation_body": implementation_body,
        "evaluation": evaluation,
    }
    prepared = state
    if state.get("pending_evaluation") != pending_evaluation:
        prepared = dict(state)
        prepared["pending_evaluation"] = pending_evaluation
        prepared["next_action"] = (
            "Complete the prepared begin-evaluation transaction."
        )
        prepared_errors = _validate_state(
            paths,
            target,
            state=prepared,
            allow_pending_evaluation=True,
        )
        if prepared_errors:
            raise HarnessError(
                "begin-evaluation preparation would create invalid state: "
                + "; ".join(prepared_errors)
            )
        _write_state(paths.state, prepared)

    for archive, body, label in (
        (current.plan, plan_body, "archived plan.md"),
        (
            current.implementation_snapshot,
            implementation_body,
            "archived implementation.md",
        ),
    ):
        archived_input = _read_artifact(archive)
        if archived_input.error is not None or archived_input.body != body:
            _write_text_atomic(archive, body, label)

    observed = _evaluation_observation(
        target,
        paths,
        state["active_round"],
        state["goal"],
    )
    expected_observation = {
        key: evaluation[key]
        for key in (
            "revision",
            "workspace_fingerprint",
            "goal_sha256",
            "plan_sha256",
            "implementation_sha256",
        )
    }
    if observed != expected_observation:
        raise HarnessError(
            "evaluation inputs changed while begin-evaluation was prepared; "
            "rerun begin-evaluation to prepare the current inputs"
        )
    if (
        _observed_file_digest(current.plan) != evaluation["plan_sha256"]
        or _observed_file_digest(current.implementation_snapshot)
        != evaluation["implementation_sha256"]
    ):
        raise HarnessError(
            "prepared evaluation-input archives do not match the pending "
            "evaluation transaction"
        )

    updated = dict(prepared)
    updated["phase"] = "EVALUATING"
    updated["latest_verdict"] = None
    updated["evaluation"] = evaluation
    updated["pending_evaluation"] = None
    updated["next_action"] = (
        "Dispatch a fresh Evaluator for this exact snapshot, then record-review."
    )
    errors = _validate_state(paths, target, state=updated)
    if errors:
        raise HarnessError(
            "begin-evaluation would create invalid state: " + "; ".join(errors)
        )
    _write_state(paths.state, updated)
    return {
        "requirement_id": paths.root.name,
        "active_round": updated["active_round"],
        "evaluation": updated["evaluation"],
    }


def _external_review_source(value: str, target: Path) -> Path:
    source = Path(value).expanduser()
    if not source.is_absolute():
        source = Path.cwd() / source
    _reject_symlink(source, "review source")
    try:
        metadata = source.stat()
    except OSError as error:
        raise HarnessError(f"cannot inspect review source: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise HarnessError("review source must be a regular file")
    resolved = source.resolve()
    if resolved == target or target in resolved.parents:
        raise HarnessError(
            "review source must be outside the target repository"
        )
    return resolved


def record_review(
    target: Path,
    requirement_id: str | None,
    review_source: str,
) -> dict[str, Any]:
    source = _external_review_source(review_source, target)
    _, requirements_root = _control_paths(target)
    paths = _select_requirement(requirements_root, requirement_id)
    state = _load_requirement_state(paths)
    if state.get("pending_review") is not None:
        source_body = _read_required_artifact(
            source,
            "record-review requires a readable review source",
        )
        pending = state["pending_review"]
        if (
            not isinstance(pending, dict)
            or pending.get("sha256") != _review_sha256(source_body)
        ):
            raise HarnessError(
                "record-review source does not match pending_review"
            )
        recovered = _reconcile_pending_review(paths, target, state)
        return {
            "requirement_id": paths.root.name,
            "active_round": recovered["active_round"],
            "phase": recovered["phase"],
            "latest_verdict": recovered["latest_verdict"],
            "evaluation": recovered["evaluation"],
        }
    errors = _validate_state(paths, target, state=state)
    if errors:
        raise HarnessError("invalid requirement state: " + "; ".join(errors))
    if (
        state["status"] != "ACTIVE"
        or state["phase"] != "EVALUATING"
        or state["latest_verdict"] not in (None, "PASS", "UNVERIFIED")
    ):
        raise HarnessError(
            "record-review requires ACTIVE EVALUATING with "
            "null, PASS, or UNVERIFIED latest_verdict"
        )
    evaluation = state.get("evaluation")
    evaluation_snapshot = _snapshot_from_evaluation(evaluation)
    if (
        evaluation_snapshot is None
        or not isinstance(evaluation, dict)
        or not isinstance(evaluation.get("acceptance_criteria"), list)
    ):
        raise HarnessError("record-review requires a valid evaluation snapshot")
    _require_same_evaluation_inputs(
        target,
        paths,
        state["active_round"],
        state["goal"],
        evaluation,
    )

    current = paths.round(state["active_round"])
    _reject_round_symlinks(current, state["active_round"])
    existing = _read_artifact(current.review)
    if state["latest_verdict"] is None and existing.exists:
        raise HarnessError(
            "pending review.md exists; run `init --resume` before record-review"
        )
    if state["latest_verdict"] in ("PASS", "UNVERIFIED") and (
        existing.body is None or existing.error is not None
    ):
        raise HarnessError(
            "review replacement requires the recorded current review.md"
        )

    source_body = _read_required_artifact(
        source,
        "record-review requires a readable review source",
    )
    record = _validated_review(
        source_body,
        evaluation["acceptance_criteria"],
        target,
        evaluation,
    )
    digest = _review_sha256(source_body)
    if record["verdict"] == "FAIL" and state["active_round"] >= MAX_ROUNDS:
        raise HarnessError(
            "maximum evaluation rounds reached before FAIL transition"
        )

    prepared = dict(state)
    prepared["pending_review"] = {
        "round": state["active_round"],
        "body": source_body,
        "sha256": digest,
        "prior_review_sha256": evaluation["review_sha256"],
    }
    _write_state(paths.state, prepared)

    working = prepared
    if state["latest_verdict"] in ("PASS", "UNVERIFIED"):
        working = _archive_replaced_review(paths, prepared, existing.body)
    _write_text_atomic(current.review, source_body, "review.md")
    updated = _apply_review_transition(working, record, digest)
    updated["pending_review"] = None
    errors = _validate_state(paths, target, state=updated)
    if errors:
        raise HarnessError(
            "record-review would create invalid state: " + "; ".join(errors)
        )
    _write_state(paths.state, updated)
    return {
        "requirement_id": paths.root.name,
        "active_round": updated["active_round"],
        "phase": updated["phase"],
        "latest_verdict": updated["latest_verdict"],
        "evaluation": updated["evaluation"],
    }


def restart_evaluation(
    target: Path,
    requirement_id: str | None,
    reason: str,
) -> dict[str, Any]:
    if not _non_empty_string(reason):
        raise HarnessError("restart-evaluation requires a non-empty reason")

    _, requirements_root = _control_paths(target)
    paths = _select_requirement(requirements_root, requirement_id)
    state = _load_requirement_state(
        paths,
        allow_current_plan_symlink=True,
    )
    errors = _validate_state(
        paths,
        target,
        state=state,
        allow_pending_restart=True,
        allow_current_evaluation_input_drift=True,
        verify_current_artifacts=False,
    )
    if errors:
        raise HarnessError("invalid requirement state: " + "; ".join(errors))

    if (
        state["phase"] == "BUILDING"
        and state["active_round"] > 1
        and state["latest_verdict"] is None
    ):
        previous = paths.round(state["active_round"] - 1)
        previous_interruption = _read_artifact(previous.interruption)
        if (
            previous_interruption.body is not None
            and previous_interruption.error is None
        ):
            record = _interruption_record(previous_interruption.body, target)
            if record["reason"] != reason:
                raise HarnessError(
                    "restart-evaluation already completed with a different reason"
                )
            return {
                "requirement_id": paths.root.name,
                "restarted": False,
                "interrupted_round": state["active_round"] - 1,
                "active_round": state["active_round"],
                "phase": state["phase"],
            }

    if (
        state["status"] not in ("ACTIVE", "BLOCKED")
        or state["phase"] != "EVALUATING"
        or state["latest_verdict"] not in (None, "PASS", "UNVERIFIED")
    ):
        raise HarnessError(
            "restart-evaluation requires ACTIVE or BLOCKED EVALUATING"
        )
    if state["active_round"] >= MAX_ROUNDS:
        raise HarnessError(
            "maximum evaluation rounds reached before restart transition"
        )
    evaluation = state.get("evaluation")
    evaluation_snapshot = _snapshot_from_evaluation(evaluation)
    if (
        evaluation_snapshot is None
        or not isinstance(evaluation, dict)
        or not isinstance(evaluation.get("acceptance_criteria"), list)
    ):
        raise HarnessError("restart-evaluation requires an evaluation snapshot")

    current = paths.round(state["active_round"])
    _reject_round_symlinks(
        current,
        state["active_round"],
        allow_current_implementation_symlink=True,
    )
    plan_problem = ""
    try:
        _reject_symlink(paths.plan, "plan.md")
        restart_plan = _read_required_artifact(
            paths.plan,
            "restart-evaluation requires a valid plan.md for the next round",
        )
        _acceptance_criteria(restart_plan)
    except HarnessError as error:
        plan_problem = str(error)
    observed = _evaluation_observation(
        target,
        paths,
        state["active_round"],
        state["goal"],
    )
    expected_observation = {
        key: evaluation[key]
        for key in (
            "revision",
            "workspace_fingerprint",
            "goal_sha256",
            "plan_sha256",
            "implementation_sha256",
        )
    }
    artifact_drift: list[dict[str, str]] = []
    if state["latest_verdict"] in ("PASS", "UNVERIFIED"):
        review_body = _read_required_artifact(
            current.review,
            "restart-evaluation requires current review.md",
        )
        review_record = _validated_review(
            review_body,
            evaluation["acceptance_criteria"],
            target,
            evaluation,
            verify_artifacts=False,
        )
        artifact_drift = _artifact_drift(review_record, target)

    pending_record = state.get("pending_interruption")
    pending_file = _read_artifact(current.interruption)
    working = state
    if pending_record is not None:
        if not isinstance(pending_record, dict):
            raise HarnessError("cannot reconcile malformed pending_interruption")
        interruption = pending_record
        if interruption["evaluation"] != state["evaluation"]:
            raise HarnessError(
                "pending interruption evaluation does not match state"
            )
        if interruption["prior_verdict"] != state["latest_verdict"]:
            raise HarnessError(
                "pending interruption prior_verdict does not match state"
            )
        if interruption["reason"] != reason:
            raise HarnessError(
                "pending interruption uses a different reason"
            )
        interruption_body = (
            json.dumps(interruption, ensure_ascii=False, indent=2) + "\n"
        )
        if pending_file.exists:
            if (
                pending_file.error is not None
                or pending_file.body != interruption_body
            ):
                raise HarnessError(
                    "interruption.json does not match pending_interruption"
                )
        else:
            _write_text_atomic(
                current.interruption,
                interruption_body,
                "interruption.json",
            )
    else:
        if pending_file.exists:
            raise HarnessError(
                "interruption.json lacks a pending_interruption transaction"
            )
        if observed == expected_observation and not artifact_drift:
            raise HarnessError(
                "restart-evaluation requires evaluation input or "
                "evidence artifact drift"
            )
        interruption = {
            "schema_version": INTERRUPTION_RECORD_VERSION,
            "reason": reason,
            "prior_verdict": state["latest_verdict"],
            "evaluation": dict(evaluation),
            "observed": observed,
            "artifact_drift": artifact_drift,
        }
        interruption_body = (
            json.dumps(interruption, ensure_ascii=False, indent=2) + "\n"
        )
        working = dict(state)
        working["pending_interruption"] = interruption
        _write_state(paths.state, working)
        _write_text_atomic(
            current.interruption,
            interruption_body,
            "interruption.json",
        )

    if plan_problem:
        blocked = dict(working)
        blocked["status"] = "BLOCKED"
        blocked["next_action"] = (
            "Restore a valid plan.md, then rerun restart-evaluation with "
            "the same reason."
        )
        blocked_errors = _validate_state(
            paths,
            target,
            state=blocked,
            allow_pending_restart=True,
            allow_current_evaluation_input_drift=True,
            verify_current_artifacts=False,
        )
        if blocked_errors:
            raise HarnessError(
                "restart-evaluation could not persist its blocked recovery "
                "state: " + "; ".join(blocked_errors)
            )
        _write_state(paths.state, blocked)
        raise HarnessError(
            "restart-evaluation preserved the interruption but requires a "
            "valid plan.md before entering the next BUILDING round; "
            f"{plan_problem}; restore plan.md and rerun with the same reason"
        )

    updated = dict(working)
    updated["status"] = "ACTIVE"
    updated["phase"] = "BUILDING"
    updated["active_round"] = state["active_round"] + 1
    updated["latest_verdict"] = None
    updated["evaluation"] = None
    updated["accepted_revision"] = ""
    interruption_history = list(working["interruption_history"])
    interruption_history.append(
        {
            "round": state["active_round"],
            "sha256": _review_sha256(interruption_body),
        }
    )
    updated["interruption_history"] = interruption_history
    updated["pending_interruption"] = None
    updated["next_action"] = (
        "Persist the next implementation handoff, then begin a fresh evaluation."
    )
    errors = _validate_state(paths, target, state=updated)
    if errors:
        raise HarnessError(
            "restart-evaluation would create invalid state: "
            + "; ".join(errors)
        )
    _write_state(paths.state, updated)
    return {
        "requirement_id": paths.root.name,
        "restarted": True,
        "interrupted_round": state["active_round"],
        "active_round": updated["active_round"],
        "phase": updated["phase"],
    }


def accept(
    target: Path,
    requirement_id: str | None,
) -> dict[str, Any]:
    _, requirements_root = _control_paths(target)
    paths = _select_requirement(requirements_root, requirement_id)
    state = _load_requirement_state(paths)
    errors = _validate_state(paths, target, state=state)
    if errors:
        raise HarnessError("invalid requirement state: " + "; ".join(errors))

    evaluation = state.get("evaluation")
    evaluation_snapshot = _snapshot_from_evaluation(evaluation)
    if evaluation_snapshot is None or not isinstance(evaluation, dict):
        raise HarnessError("accept requires an evaluation snapshot")
    if state["status"] == "ACCEPTED":
        _require_same_evaluation_inputs(
            target,
            paths,
            state["active_round"],
            state["goal"],
            evaluation,
        )
        return {
            "requirement_id": paths.root.name,
            "status": "ACCEPTED",
            "accepted_revision": state["accepted_revision"],
        }
    if (
        state["status"] != "ACTIVE"
        or state["phase"] != "EVALUATING"
        or state["latest_verdict"] != "PASS"
    ):
        raise HarnessError("accept requires ACTIVE EVALUATING with PASS")

    _require_same_evaluation_inputs(
        target,
        paths,
        state["active_round"],
        state["goal"],
        evaluation,
    )
    current = paths.round(state["active_round"])
    review_body = _read_required_artifact(
        current.review,
        "accept requires current review.md",
    )
    actual_digest = _review_sha256(review_body)
    if actual_digest != evaluation["review_sha256"]:
        raise HarnessError("review digest changed after record-review")

    updated = dict(state)
    updated["status"] = "ACCEPTED"
    updated["accepted_revision"] = evaluation_snapshot.revision
    updated["next_action"] = "Delivery complete."
    errors = _validate_state(
        paths,
        target,
        state=updated,
        require_current_snapshot=True,
    )
    if errors:
        raise HarnessError(
            "accept would create invalid state: " + "; ".join(errors)
        )
    _write_state(paths.state, updated)
    return {
        "requirement_id": paths.root.name,
        "status": "ACCEPTED",
        "accepted_revision": updated["accepted_revision"],
    }


def check(
    target: Path,
    requirement_id: str | None,
    require_current_snapshot: bool = False,
) -> tuple[dict[str, Any], bool]:
    _, requirements_root = _control_paths(target)
    try:
        paths = _select_requirement(requirements_root, requirement_id)
        state = _load_requirement_state(paths)
        errors = _validate_state(
            paths,
            target,
            state=state,
            require_current_snapshot=require_current_snapshot,
        )
    except HarnessError as error:
        result = {"valid": False, "errors": [str(error)]}
        return result, False
    result = {
        "valid": not errors,
        "errors": errors,
        "requirement_id": paths.root.name,
        "goal": state.get("goal"),
        "status": state.get("status"),
        "phase": state.get("phase"),
        "active_round": state.get("active_round"),
        "latest_verdict": state.get("latest_verdict"),
        "accepted_revision": state.get("accepted_revision"),
        "evaluation": state.get("evaluation"),
    }
    return result, not errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="start or resume a run")
    init_parser.add_argument("--target", required=True)
    init_parser.add_argument("--goal")
    init_parser.add_argument("--resume", action="store_true")
    init_parser.add_argument("--requirement")

    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="capture the Git-visible product workspace",
    )
    snapshot_parser.add_argument("--target", required=True)

    begin_parser = subparsers.add_parser(
        "begin-evaluation",
        help="freeze the snapshot evaluated by the next review",
    )
    begin_parser.add_argument("--target", required=True)
    begin_parser.add_argument("--requirement", required=True)

    review_parser = subparsers.add_parser(
        "record-review",
        help="atomically record a structured review and transition state",
    )
    review_parser.add_argument("--target", required=True)
    review_parser.add_argument("--requirement", required=True)
    review_parser.add_argument("--review-source", required=True)

    restart_parser = subparsers.add_parser(
        "restart-evaluation",
        help="preserve a drifted evaluation and start a new round",
    )
    restart_parser.add_argument("--target", required=True)
    restart_parser.add_argument("--requirement", required=True)
    restart_parser.add_argument("--reason", required=True)

    accept_parser = subparsers.add_parser(
        "accept",
        help="accept an unchanged PASS evaluation transaction",
    )
    accept_parser.add_argument("--target", required=True)
    accept_parser.add_argument("--requirement", required=True)

    check_parser = subparsers.add_parser("check", help="validate requirement state")
    check_parser.add_argument("--target", required=True)
    check_parser.add_argument("--requirement")
    check_parser.add_argument("--final", action="store_true")

    args = parser.parse_args()
    try:
        target = _target_root(args.target)
        if args.command == "init":
            _emit(init(target, args.goal, args.resume, args.requirement))
            return 0
        if args.command == "snapshot":
            _emit(snapshot(target))
            return 0
        if args.command == "begin-evaluation":
            _emit(begin_evaluation(target, args.requirement))
            return 0
        if args.command == "record-review":
            _emit(record_review(target, args.requirement, args.review_source))
            return 0
        if args.command == "restart-evaluation":
            _emit(restart_evaluation(target, args.requirement, args.reason))
            return 0
        if args.command == "accept":
            _emit(accept(target, args.requirement))
            return 0
        result, valid = check(
            target,
            args.requirement,
            require_current_snapshot=args.final,
        )
        _emit(result)
        return 0 if valid else 1
    except HarnessError as error:
        _emit({"error": str(error)}, stream=sys.stderr)
        return 1
    except (OSError, UnicodeError) as error:
        _emit(
            {"error": f"filesystem operation failed: {error}"},
            stream=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
