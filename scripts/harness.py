#!/usr/bin/env python3
"""Create and validate the minimal durable state for a long-running coding task."""

from __future__ import annotations

import argparse
import json
import os
import re
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


class HarnessError(ValueError):
    """Raised when harness state cannot be created or resumed safely."""


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


def _revision_exists(target: Path, revision: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(target), "cat-file", "-e", f"{revision}^{{commit}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _control_paths(target: Path) -> tuple[Path, Path]:
    control_root = target / ".vibe-coding"
    return control_root, control_root / "requirements"


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
    if not requirements_root.is_dir():
        return []
    paths: list[RequirementPaths] = []
    for child in requirements_root.iterdir():
        if REQUIREMENT_PATTERN.fullmatch(child.name) and child.is_dir():
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
        if not selected.state.is_file():
            raise HarnessError(f"requirement does not exist: {requirement_id}")
        return selected

    nonterminal: list[RequirementPaths] = []
    for paths in _list_requirements(requirements_root):
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
    except OSError as error:
        raise HarnessError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise HarnessError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise HarnessError(f"state must be a JSON object: {path}")
    return value


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _write_state(path: Path, state: dict[str, Any]) -> None:
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
    if resume:
        paths = _select_requirement(requirements_root, requirement_id)
        state = _load_state(paths.state)
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
    target: Path, requirement_id: str | None
) -> tuple[dict[str, Any], bool]:
    _, requirements_root = _control_paths(target)
    try:
        paths = _select_requirement(requirements_root, requirement_id)
        state = _load_state(paths.state)
    except HarnessError as error:
        result = {"valid": False, "errors": [str(error)]}
        return result, False
    result = {
        "valid": True,
        "errors": [],
        "requirement_id": paths.root.name,
        "goal": state.get("goal"),
        "status": state.get("status"),
    }
    return result, True


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

    args = parser.parse_args()
    try:
        target = _target_root(args.target)
        if args.command == "init":
            _emit(init(target, args.goal, args.resume, args.requirement))
            return 0
        result, valid = check(target, args.requirement)
        _emit(result)
        return 0 if valid else 1
    except HarnessError as error:
        _emit({"error": str(error)}, stream=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
