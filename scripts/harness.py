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
    except OSError as error:
        raise HarnessError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise HarnessError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise HarnessError(f"state must be a JSON object: {path}")
    return value


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _non_empty_file(path: Path) -> bool:
    try:
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError):
        return False


def _require_artifact(errors: list[str], path: Path, message: str) -> None:
    if not _non_empty_file(path):
        errors.append(message)


def _pass_review_has_evidence(path: Path) -> bool:
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    if not re.search(r"(?m)^PASS\s*$", body):
        return False
    marker = "## Evidence"
    if marker not in body:
        return False
    return bool(body.split(marker, 1)[1].strip())


def _reject_round_symlinks(paths: RoundPaths, round_number: int) -> None:
    _reject_symlink(paths.root, f"round {round_number:03d} directory")
    _reject_symlink(paths.implementation, "implementation.md")
    _reject_symlink(paths.review, "review.md")


def _validate_state(
    paths: RequirementPaths,
    target: Path,
    *,
    require_current_head: bool = False,
) -> list[str]:
    _reject_symlink(paths.root, f"requirement directory {paths.root.name}")
    _reject_symlink(paths.state, "state.json")
    _reject_symlink(paths.plan, "plan.md")
    _reject_symlink(paths.rounds, "rounds")
    state = _load_state(paths.state)
    errors: list[str] = []

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
    elif revision and not _revision_exists(target, revision):
        errors.append("last_good_revision does not resolve in the target repository")

    current = paths.round(round_number)
    _reject_round_symlinks(current, round_number)
    if phase in {"BUILDING", "EVALUATING"} or status in TERMINAL_STATUSES:
        plan_message = (
            "terminal requirement requires non-empty plan.md"
            if status in TERMINAL_STATUSES
            else "BUILDING requires non-empty plan.md"
        )
        _require_artifact(errors, paths.plan, plan_message)
    if phase == "EVALUATING" or status == "ACCEPTED":
        _require_artifact(
            errors,
            current.implementation,
            "EVALUATING requires current implementation.md",
        )
    if verdict in {"PASS", "UNVERIFIED"} or status == "ACCEPTED":
        _require_artifact(errors, current.review, "verdict requires current review.md")

    if verdict == "FAIL":
        if phase != "BUILDING" or round_number < 2:
            errors.append("FAIL requires the next BUILDING round")
        previous_number = max(round_number - 1, 1)
        previous = paths.round(previous_number)
        _reject_round_symlinks(previous, previous_number)
        _require_artifact(
            errors,
            previous.implementation,
            "FAIL requires previous implementation.md",
        )
        _require_artifact(errors, previous.review, "FAIL requires previous review.md")
    if verdict == "UNVERIFIED" and phase != "EVALUATING":
        errors.append("UNVERIFIED must remain in EVALUATING")
    if verdict == "PASS" and phase != "EVALUATING":
        errors.append("PASS must remain in EVALUATING until Goal Gate")

    if status == "DEGRADED" and not _non_empty_string(
        state.get("degradation_acceptance")
    ):
        errors.append("DEGRADED requires degradation_acceptance from the user")
    if status == "ACCEPTED":
        if verdict != "PASS":
            errors.append("ACCEPTED requires latest_verdict PASS")
        elif not _pass_review_has_evidence(current.review):
            errors.append("PASS review requires evidence")
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
        errors = _validate_state(paths, target, require_current_head=False)
        if errors:
            raise HarnessError("invalid requirement state: " + "; ".join(errors))
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
    target: Path,
    requirement_id: str | None,
    require_current_head: bool = False,
) -> tuple[dict[str, Any], bool]:
    _, requirements_root = _control_paths(target)
    try:
        paths = _select_requirement(requirements_root, requirement_id)
        errors = _validate_state(
            paths,
            target,
            require_current_head=require_current_head,
        )
        state = _load_state(paths.state)
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
