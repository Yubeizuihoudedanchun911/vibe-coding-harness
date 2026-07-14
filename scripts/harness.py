#!/usr/bin/env python3
"""Create and validate the minimal durable state for a long-running coding task."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
RUN_STATUSES = {"ACTIVE", "BLOCKED", "DEGRADED", "ACCEPTED"}
TERMINAL_STATUSES = {"DEGRADED", "ACCEPTED"}
PHASES = {"PLANNING", "BUILDING", "EVALUATING"}
VERDICTS = {None, "PASS", "FAIL", "UNVERIFIED"}
REQUIREMENT_PATTERN = re.compile(r"REQ-(\d+)\Z")


@dataclass(frozen=True)
class RoundPaths:
    root: Path
    implementation: Path
    review: Path


@dataclass(frozen=True)
class RequirementPaths:
    root: Path
    state: Path
    plan: Path
    rounds: Path

    def round(self, number: int) -> RoundPaths:
        root = self.rounds / f"{number:03d}"
        return RoundPaths(
            root=root,
            implementation=root / "implementation.md",
            review=root / "review.md",
        )


@dataclass(frozen=True)
class ArtifactSnapshot:
    exists: bool
    body: str | None
    error: str | None


class HarnessError(ValueError):
    """Raised when harness state cannot be created or resumed safely."""


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise HarnessError(f"{label} must not be a symbolic link")


def _reject_legacy_state(control_root: Path) -> None:
    legacy = [control_root / "state.json", control_root / "progress.md"]
    if any(path.exists() or path.is_symlink() for path in legacy):
        raise HarnessError(
            "legacy global harness state detected; finish or migrate it before schema 2"
        )


def _emit(value: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


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


def _revision(target: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


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


def _control_paths(target: Path) -> tuple[Path, Path]:
    control_root = target / ".vibe-coding"
    requirements_root = control_root / "requirements"
    _reject_symlink(control_root, ".vibe-coding")
    _reject_symlink(requirements_root, "requirements")
    return control_root, requirements_root


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
        raise HarnessError("another init is running; inspect .vibe-coding/.init.lock") from error
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
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise HarnessError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise HarnessError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise HarnessError(f"state must be a JSON object: {path}")
    return value


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


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
            body=path.read_text(encoding="utf-8"),
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


def _markdown_level_two_headings(
    body: str,
) -> list[tuple[str | None, int, int]]:
    headings: list[tuple[str | None, int, int]] = []
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
            boundary = re.fullmatch(
                r" {0,3}##[ \t]+([^\r\n]*?)[ \t]*", visible
            )
            if boundary is not None:
                machine_heading = re.fullmatch(
                    r" {0,3}## (Overall verdict|Evidence)[ \t]*", line
                )
                headings.append(
                    (
                        machine_heading.group(1)
                        if machine_heading is not None
                        else None,
                        offset,
                        offset + len(raw_line),
                    )
                )
        offset += len(raw_line)
    return headings


def _markdown_level_two_section(body: str, title: str) -> str | None:
    headings = _markdown_level_two_headings(body)
    selected = [
        (index, heading)
        for index, heading in enumerate(headings)
        if heading[0] == title
    ]
    if len(selected) != 1:
        return None
    index, (_, _, content_start) = selected[0]
    content_end = headings[index + 1][1] if index + 1 < len(headings) else len(body)
    return body[content_start:content_end]


def _markdown_evidence_line_is_substantive(line: str) -> bool:
    value = line.strip()
    if re.fullmatch(r"#{1,6}(?:[ \t]+.*)?", value):
        return False
    value = re.sub(
        r"^(?:[-+*]|(?:\d+|[A-Za-z])[.)])(?:[ \t]+|$)",
        "",
        value,
        count=1,
    )
    value = re.sub(r"^\[[ xX]\](?:[ \t]+|$)", "", value, count=1)
    return any(character.isalnum() for character in value)


def _markdown_substantive_lines(
    body: str,
    *,
    include_fenced: bool,
) -> list[str]:
    values: list[str] = []
    in_comment = False
    fence: tuple[str, int] | None = None
    for raw_line in body.splitlines():
        line = raw_line.rstrip("\r\n")
        if fence is not None:
            if _fence_closes(line, fence):
                fence = None
                continue
            if include_fenced and line.strip():
                values.append(line.strip())
            continue
        visible, in_comment = _strip_html_comments(line, in_comment)
        fence = _fence_start(visible)
        if fence is not None:
            in_comment = False
            continue
        value = visible.strip()
        if value and (
            not include_fenced
            or _markdown_evidence_line_is_substantive(value)
        ):
            values.append(value)
    return values


def _review_verdict(body: str | None) -> str | None:
    if body is None:
        return None
    section = _markdown_level_two_section(body, "Overall verdict")
    if section is None:
        return None
    values = _markdown_substantive_lines(section, include_fenced=False)
    return values[0] if len(values) == 1 else None


def _review_has_evidence(body: str | None) -> bool:
    if body is None:
        return False
    section = _markdown_level_two_section(body, "Evidence")
    return section is not None and bool(
        _markdown_substantive_lines(section, include_fenced=True)
    )


def _reject_round_symlinks(paths: RoundPaths, round_number: int) -> None:
    _reject_symlink(paths.root, f"round {round_number:03d} directory")
    _reject_symlink(paths.implementation, "implementation.md")
    _reject_symlink(paths.review, "review.md")


def _reject_requirement_symlinks(paths: RequirementPaths) -> None:
    _reject_symlink(paths.root, f"requirement directory {paths.root.name}")
    _reject_symlink(paths.state, "state.json")
    _reject_symlink(paths.plan, "plan.md")
    _reject_symlink(paths.rounds, "rounds")


def _load_requirement_state(paths: RequirementPaths) -> dict[str, Any]:
    _reject_requirement_symlinks(paths)
    return _load_state(paths.state)


def _validate_state(
    paths: RequirementPaths,
    target: Path,
    *,
    state: dict[str, Any] | None = None,
    require_current_head: bool = False,
) -> list[str]:
    _reject_requirement_symlinks(paths)
    if state is None:
        state = _load_state(paths.state)
    errors: list[str] = []
    artifacts: dict[Path, ArtifactSnapshot] = {}

    def artifact(path: Path) -> ArtifactSnapshot:
        if path not in artifacts:
            artifacts[path] = _read_artifact(path)
        return artifacts[path]

    def require_artifact(path: Path, message: str) -> str | None:
        snapshot = artifact(path)
        if snapshot.error is not None:
            errors.append(f"{message}: {path} {snapshot.error}")
        elif snapshot.body is None or not snapshot.body.strip():
            errors.append(message)
        return snapshot.body

    if state.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
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
    if verdict is not None and (
        not isinstance(verdict, str) or verdict not in VERDICTS
    ):
        errors.append("latest_verdict must be PASS, FAIL, UNVERIFIED, or null")
    if (
        not isinstance(round_number, int)
        or isinstance(round_number, bool)
        or round_number < 1
    ):
        errors.append("active_round must be a positive integer")
        round_number = 1
    if not _non_empty_string(state.get("next_action")):
        errors.append("next_action must be a non-empty string")
    if not isinstance(state.get("residual_risks"), list):
        errors.append("residual_risks must be a list")

    revision = state.get("last_good_revision")
    if not isinstance(revision, str):
        errors.append("last_good_revision must be a string")
    elif revision:
        resolved_revision = _resolve_revision(target, revision)
        if not resolved_revision:
            errors.append("last_good_revision does not resolve in the target repository")
        elif revision != resolved_revision or not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision
        ):
            errors.append("last_good_revision must be a canonical full commit OID")

    current = paths.round(round_number)
    _reject_round_symlinks(current, round_number)
    if phase in {"BUILDING", "EVALUATING"} or status in TERMINAL_STATUSES:
        plan_message = (
            "terminal requirement requires non-empty plan.md"
            if status in TERMINAL_STATUSES
            else "BUILDING requires non-empty plan.md"
        )
        require_artifact(paths.plan, plan_message)
    if phase == "EVALUATING" or status == "ACCEPTED":
        require_artifact(
            current.implementation,
            "EVALUATING requires current implementation.md",
        )
    if verdict in {"PASS", "UNVERIFIED"} or status == "ACCEPTED":
        require_artifact(current.review, "verdict requires current review.md")
    if verdict in {"PASS", "UNVERIFIED"}:
        current_review = artifact(current.review).body
        if _review_verdict(current_review) != verdict:
            errors.append(
                "current review Overall verdict must equal latest_verdict " + verdict
            )
        if verdict == "PASS" and not _review_has_evidence(current_review):
            errors.append("PASS review requires evidence")

    if phase == "EVALUATING" and verdict is None:
        current_review = artifact(current.review)
        if current_review.exists:
            detail = current_review.error or "already exists"
            errors.append(
                "EVALUATING with null latest_verdict requires current review.md "
                f"to be absent; found path that {detail}"
            )
    if phase == "BUILDING" and round_number > 1 and verdict != "FAIL":
        errors.append("BUILDING after round 1 requires latest_verdict FAIL")

    for history_number in range(1, round_number):
        history = paths.round(history_number)
        _reject_round_symlinks(history, history_number)
        require_artifact(
            history.implementation,
            f"history round {history_number:03d} requires non-empty implementation.md",
        )
        history_review = require_artifact(
            history.review,
            f"history round {history_number:03d} requires non-empty review.md",
        )
        if _review_verdict(history_review) != "FAIL":
            errors.append(
                f"history round {history_number:03d} Overall verdict must be FAIL"
            )

    if verdict == "FAIL":
        if phase != "BUILDING" or round_number < 2:
            errors.append("FAIL requires the next BUILDING round")
    if verdict == "UNVERIFIED" and phase != "EVALUATING":
        errors.append("UNVERIFIED must remain in EVALUATING")
    if verdict == "PASS" and phase != "EVALUATING":
        errors.append("PASS must remain in EVALUATING until Goal Gate")

    if status == "DEGRADED" and not _non_empty_string(
        state.get("degradation_acceptance")
    ):
        errors.append("DEGRADED requires degradation_acceptance from the user")
    if require_current_head and status != "ACCEPTED":
        errors.append("final check requires status ACCEPTED")
    if status == "ACCEPTED":
        if verdict != "PASS":
            errors.append("ACCEPTED requires latest_verdict PASS")
        if not _non_empty_string(revision):
            errors.append("ACCEPTED requires last_good_revision")
        elif require_current_head and revision != _revision(target):
            errors.append("ACCEPTED requires last_good_revision to match current HEAD")
    return errors


def _write_state(path: Path, state: dict[str, Any]) -> None:
    _reject_symlink(path, "state.json")
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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
        errors = _validate_state(
            paths,
            target,
            state=state,
            require_current_head=False,
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
            "last_good_revision": state["last_good_revision"],
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
        revision = _revision(target)
        state = {
            "schema_version": SCHEMA_VERSION,
            "requirement_id": new_id,
            "goal": goal,
            "status": "ACTIVE",
            "phase": "PLANNING",
            "active_round": 1,
            "next_action": "Dispatch Planner and persist plan.md.",
            "last_good_revision": revision,
            "latest_verdict": None,
            "residual_risks": [],
        }
        _write_state(paths.state, state)

    return {
        "created": [f".vibe-coding/requirements/{new_id}/state.json"],
        "requirement_id": new_id,
        "goal": goal,
        "status": "ACTIVE",
        "phase": "PLANNING",
        "active_round": 1,
        "last_good_revision": revision,
    }


def check(
    target: Path,
    requirement_id: str | None,
    require_current_head: bool = False,
) -> tuple[dict[str, Any], bool]:
    _, requirements_root = _control_paths(target)
    try:
        paths = _select_requirement(requirements_root, requirement_id)
        state = _load_requirement_state(paths)
        errors = _validate_state(
            paths,
            target,
            state=state,
            require_current_head=require_current_head,
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

    check_parser = subparsers.add_parser("check", help="validate the active state")
    check_parser.add_argument("--target", required=True)
    check_parser.add_argument("--requirement")
    check_parser.add_argument("--final", action="store_true")

    args = parser.parse_args()
    try:
        target = _target_root(args.target)
        if args.command == "init":
            _emit(init(target, args.goal, args.resume, args.requirement))
            return 0
        result, valid = check(
            target,
            args.requirement,
            require_current_head=args.final,
        )
        _emit(result)
        return 0 if valid else 1
    except HarnessError as error:
        _emit({"error": str(error)}, stream=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
