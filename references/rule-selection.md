# Dynamic Coding Rule selection

## Selection model

Detect a polyglot project rather than forcing one global language label.

1. Give build manifests and module files stronger weight than raw extension counts.
2. Exclude generated output, vendored dependencies, caches, virtual environments, and fixture snapshots from counts.
3. Record all meaningful languages with evidence, module roots, score, and confidence.
4. Select one primary language for defaults while retaining secondary language rules.
5. Route rules by the paths touched in the current change.
6. Recompute the profile when a manifest, lockfile, module root, or unmapped source type changes.

The detector recognizes:

| Rule id | Strong evidence | Source evidence |
|---|---|---|
| `javascript-typescript` | `package.json`, `tsconfig.json`, JS lockfiles | `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs` |
| `python` | `pyproject.toml`, `requirements*.txt`, `setup.py`, `Pipfile`, Python lockfiles | `.py`, `.pyi` |
| `go` | `go.mod`, `go.work` | `.go` |
| `java` | `pom.xml`, Gradle build files | `.java` |
| `rust` | `Cargo.toml` | `.rs` |

## Routing at edit time

- Always read `.coding-rules/core.md`.
- Read `.vibe-coding/project-profile.json` before the first source edit.
- For a change touching multiple languages, load all matching rules.
- Use the nearest detected module root to resolve ambiguous files.
- Treat shell, SQL, YAML, JSON, Markdown, and generated schemas as cross-cutting artifacts governed by core rules unless a project-specific rule exists.
- If confidence is low and the choice changes architecture or tooling, ask one product-level question; do not ask the user to manually choose rule files.

## Drift control

Store the detector version, manifest fingerprint, selected rule ids, evidence, and standard commands in the project profile. Before a run:

- Refresh when the fingerprint changes.
- Add a newly selected managed rule without deleting user-authored rules.
- Remove a managed language rule only when no module or source evidence remains and no active iteration touches it.
- Report detection conflicts instead of silently rewriting an unmanaged project profile.
