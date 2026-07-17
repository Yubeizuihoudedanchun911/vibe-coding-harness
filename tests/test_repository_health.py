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

    def test_readmes_document_install_usage_and_safety(self) -> None:
        for name in ("README.md", "README.zh-CN.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("$vibe-coding-harness", text)
            self.assertIn("scripts/harness.py", text)
            self.assertIn("REQ-NNN", text)
            self.assertIn("Python 3.10", text)
            self.assertIn("LICENSE", text)

    def test_ci_uses_pinned_actions_and_runs_all_tests(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        action_uses = re.findall(r"uses:\s*([^@\s]+)@([0-9a-f]{40})", workflow)
        self.assertEqual(
            action_uses,
            [
                (
                    "actions/checkout",
                    "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
                ),
                (
                    "actions/setup-python",
                    "ece7cb06caefa5fff74198d8649806c4678c61a1",
                ),
            ],
        )
        self.assertIn("python -m py_compile scripts/harness.py", workflow)
        self.assertIn(
            "python -m unittest discover -s tests -p 'test_*.py' -v",
            workflow,
        )
        for version in ("3.10", "3.12", "3.14"):
            self.assertIn(f'"{version}"', workflow)

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
