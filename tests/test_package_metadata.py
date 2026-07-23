from __future__ import annotations

import importlib.metadata
import unittest
from pathlib import Path

from vibe.prompt_registry import PromptRegistry


ROOT = Path(__file__).resolve().parents[1]


class PackageMetadataTests(unittest.TestCase):
    def test_distribution_and_console_entry_point_are_installed(self) -> None:
        distribution = importlib.metadata.distribution(
            "vibe-coding-harness"
        )
        self.assertEqual(distribution.version, "0.1.0.dev0")
        scripts = {
            item.name: item.value
            for item in distribution.entry_points
            if item.group == "console_scripts"
        }
        self.assertEqual(scripts["vibe"], "vibe.cli:main")

    def test_installed_prompt_and_schema_resources_are_complete(self) -> None:
        registry = PromptRegistry.default()
        for prompt_id in (
            "planner",
            "workers/base",
            "workers/implementation",
            "workers/testing",
            "workers/performance",
            "workers/code-quality",
            "workers/documentation",
            "workers/general",
            "evaluator",
        ):
            reference, body = registry.load(prompt_id, 1)
            self.assertTrue(body)
            self.assertTrue(reference.sha256.startswith("sha256:"))
        for schema_name in (
            "plan-v1.schema.json",
            "worker-result-v1.schema.json",
            "evaluation-v1.schema.json",
        ):
            self.assertTrue(
                (registry.schema_root / schema_name).is_file()
            )

    def test_runtime_and_distribution_versions_match(self) -> None:
        from vibe import __version__

        self.assertEqual(__version__, "0.1.0.dev0")
        self.assertEqual(
            __version__,
            importlib.metadata.version("vibe-coding-harness"),
        )

    def test_pyproject_uses_current_spdx_license_metadata(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('license = "Apache-2.0"', pyproject)
        self.assertIn('license-files = ["LICENSE"]', pyproject)


if __name__ == "__main__":
    unittest.main()
