from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryHealthTests(unittest.TestCase):
    def test_required_community_files_exist(self) -> None:
        required = (
            "README.md",
            "README.zh-CN.md",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            "SECURITY.md",
            "CHANGELOG.md",
            "LICENSE",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/ISSUE_TEMPLATE/config.yml",
            ".github/workflows/ci.yml",
            ".github/dependabot.yml",
        )
        missing = [path for path in required if not (ROOT / path).is_file()]
        self.assertEqual(missing, [])

    def test_schema_four_has_one_external_product_entry(self) -> None:
        self.assertTrue((ROOT / "pyproject.toml").is_file())
        self.assertTrue((ROOT / "src/vibe/cli.py").is_file())
        for removed in (
            "SKILL.md",
            "agents/openai.yaml",
            "scripts/harness.py",
            "tests/test_skill.py",
            "tests/test_harness.py",
        ):
            self.assertFalse((ROOT / removed).exists(), removed)

    def test_readmes_document_install_all_commands_and_migration(self) -> None:
        for name in ("README.md", "README.zh-CN.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for command in (
                "vibe run",
                "vibe resume",
                "vibe status",
                "vibe stop",
                "vibe logs",
                "vibe migrate",
            ):
                self.assertIn(command, text)
            self.assertIn("Python 3.10", text)
            self.assertIn("Codex CLI", text)
            self.assertIn("clean", text.lower())
            self.assertIn("Schema 3", text)
            self.assertIn("LICENSE", text)
            self.assertNotIn("$vibe-coding-harness", text)
            self.assertNotIn("scripts/harness.py", text)

    def test_ci_is_pinned_offline_cross_platform_and_non_publishing(
        self,
    ) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        uses_lines = re.findall(
            r"uses:\s*([^@\s]+)@([^\s]+)",
            workflow,
        )
        self.assertTrue(uses_lines)
        self.assertTrue(
            all(
                re.fullmatch(r"[0-9a-f]{40}", reference)
                for _, reference in uses_lines
            )
        )
        for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
            self.assertIn(f'"{version}"', workflow)
        for required in (
            "runs-on: macos-14",
            "python -m pip install --no-deps -e .",
            "python -m compileall -q src/vibe",
            "python -m unittest discover -s tests -p 'test_*.py' -v",
            "python -m build",
            "python -m twine check",
            "python -m pip check",
        ):
            self.assertIn(required, workflow)
        lowered = workflow.lower()
        for forbidden in (
            "pypi",
            "publish",
            "id-token:",
            "secrets.",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_license_is_apache_2(self) -> None:
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Apache License", license_text)
        self.assertIn("Version 2.0, January 2004", license_text)
        self.assertIn("END OF TERMS AND CONDITIONS", license_text)

    def test_issue_templates_route_security_reports_privately(self) -> None:
        config = (
            ROOT / ".github/ISSUE_TEMPLATE/config.yml"
        ).read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        advisory_path = "vibe-coding-harness/security/advisories/new"
        self.assertIn("blank_issues_enabled: false", config)
        self.assertIn(advisory_path, config)
        self.assertIn(advisory_path, security)

    def test_local_agent_state_is_ignored(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".serena/", gitignore.splitlines())


if __name__ == "__main__":
    unittest.main()
