#!/usr/bin/env python3
"""Validate Vibe Coding Harness infrastructure and iteration invariants."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

sys.dont_write_bytecode = True

from detect_project_profile import detect_project


ALLOWED_STATUSES = {
    "INTAKE",
    "BOOTSTRAP",
    "DISCOVER",
    "PLAN",
    "CONTRACT",
    "IMPLEMENT",
    "VERIFY",
    "REPAIR",
    "GOVERN",
    "RELEASE_GATE",
    "ACCEPTED",
    "BLOCKED",
    "DEGRADED",
    "ABANDONED",
}
TERMINAL_STATUSES = {"ACCEPTED", "BLOCKED", "DEGRADED", "ABANDONED"}
REQUIRED_AGENTS = ("vibe-planner.toml", "vibe-generator.toml", "vibe-evaluator.toml")
REQUIRED_DOCS = (
    "docs/README.md",
    "docs/onboard/README.md",
    "docs/architecture/README.md",
    "docs/decisions/README.md",
    "docs/iterations/README.md",
    "docs/changelog.md",
)
HARD_GATES = {
    "scope",
    "build",
    "static_quality",
    "automated_tests",
    "core_journey",
    "persistence",
    "error_behavior",
    "contracts",
    "security_baseline",
    "repository_governance",
}


def _load_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        errors.append(f"cannot read {path}: {error}")
        return {}
    except json.JSONDecodeError as error:
        errors.append(f"invalid JSON {path}: {error}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"expected JSON object: {path}")
        return {}
    return value


def _safe_artifact_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _validate_iteration(manifest_path: Path, errors: list[str], warnings: list[str]) -> None:
    manifest = _load_json(manifest_path, errors)
    if not manifest:
        return
    label = manifest_path.parent.name
    if manifest.get("schema_version") != 1:
        errors.append(f"{label}: unsupported schema_version")
    if manifest.get("iteration") != label:
        errors.append(f"{label}: manifest iteration does not match directory")
    status = manifest.get("status")
    if status not in ALLOWED_STATUSES:
        errors.append(f"{label}: invalid status {status!r}")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append(f"{label}: artifacts must be a list")
        artifacts = []
    artifact_paths: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            errors.append(f"{label}: artifact {index} lacks a string path")
            continue
        relative = artifact["path"]
        if not _safe_artifact_path(relative):
            errors.append(f"{label}: unsafe artifact path {relative!r}")
            continue
        artifact_paths.add(relative)
        if not (manifest_path.parent / relative).exists():
            errors.append(f"{label}: missing artifact {relative}")
    rounds = manifest.get("evaluation_rounds", [])
    if not isinstance(rounds, list):
        errors.append(f"{label}: evaluation_rounds must be a list")
    else:
        for relative in rounds:
            if not isinstance(relative, str) or not _safe_artifact_path(relative):
                errors.append(f"{label}: invalid evaluation round path {relative!r}")
            elif not (manifest_path.parent / relative).exists():
                errors.append(f"{label}: missing evaluation round {relative}")
    if status in TERMINAL_STATUSES and not (manifest_path.parent / "delivery-report.md").exists():
        errors.append(f"{label}: terminal status requires delivery-report.md")
    gates = manifest.get("gates")
    if not isinstance(gates, dict):
        errors.append(f"{label}: gates must be an object")
        gates = {}
    if status == "ACCEPTED":
        missing = sorted(HARD_GATES - set(gates))
        if missing:
            errors.append(f"{label}: accepted manifest lacks gates {', '.join(missing)}")
        for gate in sorted(HARD_GATES):
            value = gates.get(gate)
            if not isinstance(value, dict) or value.get("status") != "PASS" or not str(value.get("evidence", "")).strip():
                errors.append(f"{label}: accepted gate {gate} lacks PASS evidence")
    if status not in TERMINAL_STATUSES and (manifest_path.parent / "delivery-report.md").exists():
        warnings.append(f"{label}: delivery-report.md exists before terminal status")


def validate(target: Path) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    target = target.resolve()
    if not target.is_dir():
        return {"errors": [f"target is not a directory: {target}"], "warnings": []}

    profile_path = target / ".vibe-coding" / "project-profile.json"
    if not profile_path.exists():
        errors.append("missing .vibe-coding/project-profile.json")
        profile: dict[str, Any] = {}
    else:
        profile = _load_json(profile_path, errors)
        if profile.get("managed_by") != "vibe-coding-harness":
            errors.append("project profile is not managed by vibe-coding-harness")
        current_profile = detect_project(target)
        for key in ("detector_version", "manifest_fingerprint", "selected_rules"):
            if profile.get(key) != current_profile.get(key):
                errors.append(f"project profile is stale for {key}; rerun bootstrap_harness.py")
    selected_rules = profile.get("selected_rules", []) if isinstance(profile, dict) else []
    if not isinstance(selected_rules, list) or "core" not in selected_rules:
        errors.append("project profile must select the core rule")
        selected_rules = []
    for rule_id in selected_rules:
        if not isinstance(rule_id, str):
            errors.append("selected_rules entries must be strings")
            continue
        rule_path = target / ".coding-rules" / ("core.md" if rule_id == "core" else f"{rule_id}.md")
        if not rule_path.exists():
            errors.append(f"missing selected rule {rule_path.relative_to(target)}")
    for agent in REQUIRED_AGENTS:
        if not (target / ".codex" / "agents" / agent).exists():
            errors.append(f"missing .codex/agents/{agent}")
    for relative in REQUIRED_DOCS:
        if not (target / relative).exists():
            errors.append(f"missing {relative}")
    agents_path = target / "AGENTS.md"
    if not agents_path.exists():
        errors.append("missing AGENTS.md")
    else:
        text = agents_path.read_text(encoding="utf-8")
        if "<!-- vibe-coding-harness:start -->" not in text or "<!-- vibe-coding-harness:end -->" not in text:
            errors.append("AGENTS.md lacks the managed Vibe Coding Harness block")

    iterations_root = target / "docs" / "iterations"
    manifests = sorted(iterations_root.glob("*/manifest.json")) if iterations_root.exists() else []
    if not manifests:
        errors.append("no iteration manifest found")
    for manifest_path in manifests:
        _validate_iteration(manifest_path, errors, warnings)
    return {"errors": errors, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    result = validate(Path(args.target))
    result["valid"] = not result["errors"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
