#!/usr/bin/env python3
"""Detect a repository's language modules and route Vibe Coding rules."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DETECTOR_VERSION = "1.0.2"
EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".coding-rules",
    ".vibe-coding",
    ".quick-init",
    ".codex",
    ".claude",
    ".serena",
    ".superpowers",
    ".idea",
    ".vscode",
    ".next",
    ".nuxt",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "coverage",
    "target",
}


@dataclass(frozen=True)
class LanguageSpec:
    rule_id: str
    manifest_patterns: tuple[str, ...]
    extensions: tuple[str, ...]
    path_globs: tuple[str, ...]


LANGUAGES = (
    LanguageSpec(
        "javascript-typescript",
        (
            "package.json",
            "tsconfig.json",
            "jsconfig.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "bun.lockb",
            "bun.lock",
        ),
        (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"),
        ("**/*.js", "**/*.jsx", "**/*.ts", "**/*.tsx", "**/*.mjs", "**/*.cjs"),
    ),
    LanguageSpec(
        "python",
        (
            "pyproject.toml",
            "requirements*.txt",
            "setup.py",
            "setup.cfg",
            "Pipfile",
            "Pipfile.lock",
            "poetry.lock",
            "uv.lock",
        ),
        (".py", ".pyi"),
        ("**/*.py", "**/*.pyi"),
    ),
    LanguageSpec(
        "go",
        ("go.mod", "go.work"),
        (".go",),
        ("**/*.go",),
    ),
    LanguageSpec(
        "java",
        ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
        (".java",),
        ("**/*.java",),
    ),
    LanguageSpec(
        "rust",
        ("Cargo.toml", "Cargo.lock"),
        (".rs",),
        ("**/*.rs",),
    ),
)

MANIFEST_WEIGHTS = {
    "package.json": 100,
    "tsconfig.json": 25,
    "jsconfig.json": 25,
    "package-lock.json": 10,
    "pnpm-lock.yaml": 10,
    "yarn.lock": 10,
    "bun.lockb": 10,
    "bun.lock": 10,
    "pyproject.toml": 100,
    "setup.py": 80,
    "setup.cfg": 60,
    "Pipfile": 80,
    "Pipfile.lock": 10,
    "poetry.lock": 10,
    "uv.lock": 10,
    "go.mod": 100,
    "go.work": 25,
    "pom.xml": 100,
    "build.gradle": 100,
    "build.gradle.kts": 100,
    "settings.gradle": 20,
    "settings.gradle.kts": 20,
    "Cargo.toml": 100,
    "Cargo.lock": 10,
}


def _relative(path: Path, root: Path) -> str:
    value = path.relative_to(root).as_posix()
    return value or "."


def _matches_manifest(name: str, spec: LanguageSpec) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in spec.manifest_patterns)


def _manifest_weight(name: str) -> int:
    if fnmatch.fnmatch(name, "requirements*.txt"):
        return 80
    return MANIFEST_WEIGHTS.get(name, 50)


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, directories, names in os.walk(root):
        directories[:] = sorted(directory for directory in directories if directory not in EXCLUDED_DIRS)
        current_path = Path(current)
        for name in sorted(names):
            path = current_path / name
            if path.is_symlink():
                continue
            files.append(path)
    return files


def _project_fingerprint(root: Path, manifests: list[Path], sources: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(manifests)):
        digest.update(_relative(path, root).encode("utf-8"))
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError:
            digest.update(b"<unreadable>")
    for path in sorted(set(sources)):
        digest.update(_relative(path, root).encode("utf-8"))
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _javascript_commands(module_root: Path) -> tuple[dict[str, str], list[str]]:
    package = _read_json(module_root / "package.json")
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    if (module_root / "pnpm-lock.yaml").exists():
        runner, install = "pnpm", "pnpm install --frozen-lockfile"
    elif (module_root / "yarn.lock").exists():
        runner, install = "yarn", "yarn install --immutable"
    elif (module_root / "bun.lockb").exists() or (module_root / "bun.lock").exists():
        runner, install = "bun run", "bun install --frozen-lockfile"
    elif (module_root / "package-lock.json").exists():
        runner, install = "npm run", "npm ci"
    else:
        runner, install = "npm run", "npm install"
    commands: dict[str, str] = {"install": install}
    aliases = {
        "dev": ("dev", "start"),
        "build": ("build",),
        "lint": ("lint",),
        "typecheck": ("typecheck", "type-check", "check"),
        "test": ("test",),
        "e2e": ("test:e2e", "e2e"),
    }
    for command_id, candidates in aliases.items():
        match = next((candidate for candidate in candidates if candidate in scripts), None)
        if match:
            commands[command_id] = f"{runner} {match}"
    dependencies: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        value = package.get(key)
        if isinstance(value, dict):
            dependencies.update(value)
    frameworks = []
    for dependency, label in (
        ("next", "Next.js"),
        ("react", "React"),
        ("vue", "Vue"),
        ("svelte", "Svelte"),
        ("@angular/core", "Angular"),
        ("express", "Express"),
        ("@nestjs/core", "NestJS"),
    ):
        if dependency in dependencies:
            frameworks.append(label)
    return commands, frameworks


def _python_commands(module_root: Path) -> tuple[dict[str, str], list[str]]:
    commands: dict[str, str] = {}
    frameworks: list[str] = []
    pyproject = module_root / "pyproject.toml"
    text = ""
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8").lower()
        except OSError:
            text = ""
    if (module_root / "tests").exists() or "pytest" in text or (module_root / "pytest.ini").exists():
        commands["test"] = "python -m pytest"
    if "ruff" in text:
        commands["lint"] = "python -m ruff check ."
        commands["format_check"] = "python -m ruff format --check ."
    if "mypy" in text:
        commands["typecheck"] = "python -m mypy ."
    if pyproject.exists():
        commands["build"] = "python -m build"
    for token, label in (("django", "Django"), ("fastapi", "FastAPI"), ("flask", "Flask")):
        if token in text:
            frameworks.append(label)
    return commands, frameworks


def _module_commands(language: str, module_root: Path) -> tuple[dict[str, str], list[str]]:
    if language == "javascript-typescript":
        return _javascript_commands(module_root)
    if language == "python":
        return _python_commands(module_root)
    if language == "go":
        return (
            {"format_check": "test -z \"$(gofmt -l .)\"", "lint": "go vet ./...", "test": "go test ./...", "build": "go build ./..."},
            [],
        )
    if language == "java":
        if (module_root / "mvnw").exists() or (module_root / "pom.xml").exists():
            return ({"test": "./mvnw test" if (module_root / "mvnw").exists() else "mvn test", "build": "./mvnw verify" if (module_root / "mvnw").exists() else "mvn verify"}, [])
        return ({"test": "./gradlew test", "build": "./gradlew build"}, [])
    if language == "rust":
        return (
            {"format_check": "cargo fmt --check", "lint": "cargo clippy --all-targets --all-features -- -D warnings", "test": "cargo test --all-features", "build": "cargo build --all-features"},
            [],
        )
    return {}, []


def detect_project(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"Target is not a directory: {root}")

    files = _walk_files(root)
    manifest_paths: list[Path] = []
    source_paths: list[Path] = []
    language_rows: list[dict[str, Any]] = []
    modules: list[dict[str, Any]] = []
    all_frameworks: set[str] = set()

    for spec in LANGUAGES:
        manifests = [path for path in files if _matches_manifest(path.name, spec)]
        sources = [path for path in files if path.suffix.lower() in spec.extensions]
        manifest_paths.extend(manifests)
        source_paths.extend(sources)
        roots = sorted({_relative(path.parent, root) for path in manifests})
        if not roots and sources:
            roots = ["."]
        score = sum(_manifest_weight(path.name) for path in manifests) + min(len(sources), 100)
        confidence = "high" if manifests else "medium" if len(sources) >= 3 else "low" if sources else "none"
        language_rows.append(
            {
                "id": spec.rule_id,
                "score": score,
                "confidence": confidence,
                "manifest_evidence": [_relative(path, root) for path in manifests],
                "source_count": len(sources),
                "source_samples": [_relative(path, root) for path in sources[:10]],
                "module_roots": roots,
                "path_globs": list(spec.path_globs),
                "rule_file": f".coding-rules/{spec.rule_id}.md",
            }
        )
        for module_root_text in roots:
            module_root = root if module_root_text == "." else root / module_root_text
            commands, frameworks = _module_commands(spec.rule_id, module_root)
            all_frameworks.update(frameworks)
            modules.append(
                {
                    "language": spec.rule_id,
                    "root": module_root_text,
                    "commands": commands,
                    "frameworks": frameworks,
                }
            )

    ranked = sorted((row for row in language_rows if row["score"] > 0), key=lambda row: (-row["score"], row["id"]))
    primary = ranked[0]["id"] if ranked else "generic"
    selected: list[str] = []
    for row in ranked:
        meaningful = bool(row["manifest_evidence"]) or row["source_count"] >= 2 or row["id"] == primary
        if meaningful:
            selected.append(row["id"])

    selected_rows = [row for row in language_rows if row["id"] in selected]
    selected_modules = [module for module in modules if module["language"] in selected]
    return {
        "schema_version": 1,
        "detector_version": DETECTOR_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "manifest_fingerprint": _project_fingerprint(root, manifest_paths, source_paths),
        "primary_language": primary,
        "selected_rules": ["core", *selected],
        "languages": selected_rows,
        "modules": selected_modules,
        "frameworks": sorted(all_frameworks),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="Repository directory to inspect")
    parser.add_argument("--write", help="Optional JSON output path")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    args = parser.parse_args()

    try:
        profile = detect_project(Path(args.target))
    except ValueError as error:
        parser.error(str(error))
    output = json.dumps(profile, ensure_ascii=False, indent=None if args.compact else 2, sort_keys=True) + "\n"
    if args.write:
        output_path = Path(args.write)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
