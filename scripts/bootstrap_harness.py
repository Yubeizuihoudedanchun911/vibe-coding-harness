#!/usr/bin/env python3
"""Install or refresh Vibe Coding Harness infrastructure without clobbering user files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from detect_project_profile import detect_project


MANAGED_MARKER = "Managed by vibe-coding-harness"
BEGIN_MARKER = "<!-- vibe-coding-harness:start -->"
END_MARKER = "<!-- vibe-coding-harness:end -->"
TERMINAL_STATUSES = {"ACCEPTED", "BLOCKED", "DEGRADED", "ABANDONED"}
HARD_GATES = (
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
)


class SafeWriter:
    def __init__(self, target: Path, dry_run: bool) -> None:
        self.target = target
        self.dry_run = dry_run
        self.actions: list[dict[str, str]] = []
        self.conflicts: list[str] = []

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.target).as_posix()

    def _write(self, path: Path, content: str, action: str) -> None:
        if not content.endswith("\n"):
            content += "\n"
        if not self.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.actions.append({"action": action, "path": self._relative(path)})

    def write_managed(self, path: Path, content: str) -> None:
        if path.exists():
            current = path.read_text(encoding="utf-8")
            normalized = content if content.endswith("\n") else content + "\n"
            if current == normalized:
                self.actions.append({"action": "unchanged", "path": self._relative(path)})
                return
            if MANAGED_MARKER not in current:
                self.conflicts.append(self._relative(path))
                return
            self._write(path, content, "updated")
            return
        self._write(path, content, "created")

    def create_once(self, path: Path, content: str) -> None:
        if path.exists():
            self.actions.append({"action": "preserved", "path": self._relative(path)})
            return
        self._write(path, content, "created")

    def upsert_block(self, path: Path, block_body: str) -> None:
        block = f"{BEGIN_MARKER}\n{block_body.rstrip()}\n{END_MARKER}"
        if not path.exists():
            self._write(path, block, "created")
            return
        current = path.read_text(encoding="utf-8")
        has_begin, has_end = BEGIN_MARKER in current, END_MARKER in current
        if has_begin != has_end:
            self.conflicts.append(self._relative(path))
            return
        if has_begin:
            start = current.index(BEGIN_MARKER)
            end = current.index(END_MARKER, start) + len(END_MARKER)
            parts = [part for part in (current[:start].rstrip(), block, current[end:].strip("\n")) if part]
            updated = "\n\n".join(parts) + "\n"
        else:
            updated = current.rstrip() + "\n\n" + block + "\n"
        if updated == current:
            self.actions.append({"action": "unchanged", "path": self._relative(path)})
            return
        self._write(path, updated, "updated")


def _slug(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|`$;&(){}\[\]!]+", "-", value.strip())
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._")
    return (value or "iteration")[:80]


def _render(path: Path, values: dict[str, str]) -> str:
    content = path.read_text(encoding="utf-8")
    for key, value in values.items():
        content = content.replace("{{" + key + "}}", value)
    return content


def _rule_version(skill_root: Path, selected_rules: list[str]) -> str:
    digest = hashlib.sha256()
    for rule_id in selected_rules:
        source = skill_root / "references" / ("rules-core.md" if rule_id == "core" else f"rules-{rule_id}.md")
        digest.update(rule_id.encode("utf-8"))
        digest.update(source.read_bytes())
    return digest.hexdigest()[:16]


def _global_documents(project_name: str, project_goal: str) -> dict[str, str]:
    return {
        "docs/README.md": "# Project Documentation\n\n- [Onboarding](onboard/README.md)\n- [Architecture](architecture/README.md)\n- [Decisions](decisions/README.md)\n- [Iterations](iterations/README.md)\n- [Changelog](changelog.md)\n",
        "docs/onboard/README.md": f"# Onboarding\n\n## Project\n\n{project_name}\n\n## Goal\n\n{project_goal}\n\n## Setup\n\nTo be derived from live project commands.\n\n## Common commands\n\nTo be maintained from `.vibe-coding/project-profile.json`.\n",
        "docs/architecture/README.md": "# Architecture\n\n## System context\n\n## Module boundaries\n\n## Data flow\n\n## External integrations\n\n## Constraints and invariants\n",
        "docs/decisions/README.md": "# Architecture Decision Records\n\n| ID | Decision | Date | Iteration | Status |\n|---|---|---|---|---|\n",
        "docs/iterations/README.md": "# Iterations\n\n| Iteration | Goal | Status | Delivery |\n|---|---|---|---|\n",
        "docs/changelog.md": "# Changelog\n\nRecord accepted user-visible changes by iteration; use Git history for commit-level detail.\n",
    }


def _profile_content(profile: dict[str, Any], rule_version: str) -> str:
    value = dict(profile)
    value.pop("generated_at", None)
    value["managed_by"] = "vibe-coding-harness"
    value["_managed_marker"] = MANAGED_MARKER
    value["rule_version"] = rule_version
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _iteration_manifest(iteration: str, goal: str, policy: str) -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    artifacts = [
        {"type": "iteration", "path": "iteration.md", "status": "active"},
        {"type": "brief", "path": "product-brief.md", "status": "draft"},
        {"type": "plan", "path": "execution-plan.md", "status": "draft"},
        {"type": "contract", "path": "acceptance-contract.md", "status": "draft"},
    ]
    manifest = {
        "schema_version": 1,
        "iteration": iteration,
        "status": "BOOTSTRAP",
        "goal": goal,
        "execution_policy": policy,
        "created_at": now,
        "updated_at": now,
        "artifacts": artifacts,
        "evaluation_rounds": [],
        "gates": {gate: {"status": "UNVERIFIED", "evidence": ""} for gate in HARD_GATES},
        "residual_risks": [],
    }
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    target = Path(args.target).resolve()
    if not target.is_dir():
        raise ValueError(f"Target is not a directory: {target}")
    skill_root = Path(__file__).resolve().parent.parent
    writer = SafeWriter(target, args.dry_run)
    profile = detect_project(target)
    rule_version = _rule_version(skill_root, profile["selected_rules"])

    for rule_id in profile["selected_rules"]:
        source_name = "rules-core.md" if rule_id == "core" else f"rules-{rule_id}.md"
        source = skill_root / "references" / source_name
        writer.write_managed(target / ".coding-rules" / ("core.md" if rule_id == "core" else f"{rule_id}.md"), source.read_text(encoding="utf-8"))

    for agent_name in ("vibe-planner.toml", "vibe-generator.toml", "vibe-evaluator.toml"):
        source = skill_root / "assets" / "agents" / agent_name
        writer.write_managed(target / ".codex" / "agents" / agent_name, source.read_text(encoding="utf-8"))

    writer.write_managed(target / ".vibe-coding" / "project-profile.json", _profile_content(profile, rule_version))
    writer.upsert_block(
        target / "AGENTS.md",
        "## Vibe Coding Harness\n\n"
        "- Read `.vibe-coding/project-profile.json` and `.coding-rules/core.md` before source edits.\n"
        "- Load every selected language rule that matches the files being changed; primary language does not suppress secondary rules.\n"
        "- Re-run the profile detector when manifests, lockfiles, module roots, or unmapped source types change.\n"
        "- Keep Planner and Evaluator read-only; keep Generator as the only business-code writer.\n"
        "- Store durable plans, contracts, handoffs, evaluations, and delivery evidence in the active `docs/iterations/` directory.\n"
        "- Do not report completion while a hard acceptance gate is failing or unverified.",
    )
    writer.upsert_block(target / ".gitignore", ".vibe-coding/runtime/\n.vibe-coding/**/*.tmp")

    for relative, content in _global_documents(args.project_name, args.project_goal).items():
        writer.create_once(target / relative, content)

    iteration = f"{date.today().isoformat()}-{_slug(args.iteration)}"
    iteration_root = target / "docs" / "iterations" / iteration
    values = {
        "PROJECT_NAME": args.project_name,
        "PROJECT_GOAL": args.project_goal,
        "ITERATION": iteration,
        "ITERATION_TOPIC": args.iteration,
        "DATE": date.today().isoformat(),
        "EXECUTION_POLICY": args.policy,
    }
    for template_name in ("iteration.md", "product-brief.md", "execution-plan.md", "acceptance-contract.md"):
        source = skill_root / "assets" / "iteration" / template_name
        writer.create_once(iteration_root / template_name, _render(source, values))
    writer.create_once(iteration_root / "manifest.json", _iteration_manifest(iteration, args.project_goal, args.policy))
    if not args.dry_run:
        for directory in ("handoffs", "evaluations", "evidence"):
            (iteration_root / directory).mkdir(parents=True, exist_ok=True)

    return {
        "target": str(target),
        "iteration": iteration,
        "primary_language": profile["primary_language"],
        "selected_rules": profile["selected_rules"],
        "actions": writer.actions,
        "conflicts": writer.conflicts,
        "dry_run": args.dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--project-name")
    parser.add_argument("--project-goal", default="Product goal to be refined during intake.")
    parser.add_argument("--iteration", default="initial-delivery")
    parser.add_argument("--policy", choices=("fast", "standard", "strict"), default="standard")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    target = Path(args.target)
    if not args.project_name:
        args.project_name = target.resolve().name
    try:
        summary = bootstrap(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if summary["conflicts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
