from __future__ import annotations

from vibe.prompt_registry import PromptRegistry


EXPECTED_PROMPTS = (
    ("planner", 1),
    ("planner", 2),
    ("workers/base", 1),
    ("workers/base", 2),
    ("workers/implementation", 1),
    ("workers/testing", 1),
    ("workers/performance", 1),
    ("workers/code-quality", 1),
    ("workers/documentation", 1),
    ("workers/general", 1),
    ("workers/kinds/implementation", 1),
    ("workers/kinds/testing", 1),
    ("workers/kinds/performance", 1),
    ("workers/kinds/code-quality", 1),
    ("workers/kinds/documentation", 1),
    ("workers/kinds/general", 1),
    ("workers/domains/frontend", 1),
    ("workers/domains/backend", 1),
    ("workers/domains/client", 1),
    ("workers/domains/data", 1),
    ("workers/domains/infrastructure", 1),
    ("workers/domains/general", 1),
    ("evaluator", 1),
    ("evaluator", 2),
)
EXPECTED_SCHEMAS = (
    "plan-v1.schema.json",
    "plan-v2.schema.json",
    "worker-result-v1.schema.json",
    "evaluation-v1.schema.json",
)


def main() -> None:
    registry = PromptRegistry.default()
    for prompt_id, version in EXPECTED_PROMPTS:
        registry.load(prompt_id, version)
    missing = [
        name
        for name in EXPECTED_SCHEMAS
        if not (registry.schema_root / name).is_file()
    ]
    if missing:
        raise SystemExit(
            "missing installed schemas: " + ", ".join(missing)
        )


if __name__ == "__main__":
    main()
