from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tests.support.git_repo import GitRepositoryFixture
from vibe.git_runner import GitInternalOptions, GitRunner
from vibe.models import ContractError
from vibe.worktrees import SourceCommitMetadata, WorktreeManager


class GitRunnerTests(GitRepositoryFixture, unittest.TestCase):
    def setUp(self) -> None:
        self.setUpGitRepository()
        self.runner = GitRunner(self.target)

    def test_remote_capable_and_caller_global_options_are_rejected(self) -> None:
        for command in (
            "clone",
            "fetch",
            "ls-remote",
            "pull",
            "push",
            "send-email",
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ContractError, "remote-capable"):
                    self.runner.run_local(self.target, command)
        with self.assertRaisesRegex(ContractError, "global options"):
            self.runner.run_local(self.target, "-c", "alias.x=!touch /tmp/no", "x")
        with self.assertRaisesRegex(ContractError, "remote"):
            self.runner.run_local(self.target, "remote", "add", "x", "/tmp/no")

    def test_resolved_git_binary_is_not_replaced_by_later_path_change(self) -> None:
        fake_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(fake_tmp.cleanup)
        marker = Path(fake_tmp.name) / "called"
        fake_git = Path(fake_tmp.name) / "git"
        fake_git.write_text(
            f"#!/bin/sh\n/usr/bin/touch {marker}\nexit 99\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        previous = os.environ.get("PATH")
        os.environ["PATH"] = fake_tmp.name
        self.addCleanup(self._restore_path, previous)

        oid = self.runner.run_local(
            self.target,
            "rev-parse",
            "HEAD",
        ).stdout.decode("ascii").strip()

        self.assertRegex(oid, r"[0-9a-f]{40}")
        self.assertFalse(marker.exists())

    def test_internal_options_are_subcommand_and_location_bound(self) -> None:
        outside = self.target / "outside-index"
        with self.assertRaisesRegex(ContractError, "index root"):
            self.runner.run_local(
                self.target,
                "read-tree",
                "HEAD",
                internal=GitInternalOptions(index_file=outside),
            )
        allowed = self.runner.index_root / "op-1" / "index"
        with self.assertRaisesRegex(ContractError, "invalid for this subcommand"):
            self.runner.run_local(
                self.target,
                "status",
                "--porcelain=v2",
                internal=GitInternalOptions(index_file=allowed),
            )
        with self.assertRaisesRegex(ContractError, "valid only for commit-tree"):
            self.runner.run_local(
                self.target,
                "status",
                "--porcelain=v2",
                internal=GitInternalOptions(
                    commit_timestamp="2026-07-23T10:00:00+00:00"
                ),
            )
        with self.assertRaisesRegex(ContractError, "requires a frozen"):
            self.runner.run_local(
                self.target,
                "commit-tree",
                self.git("rev-parse", "HEAD^{tree}"),
                "-m",
                "forbidden",
            )

    def test_executable_repository_integrations_are_rejected(self) -> None:
        dangerous = (
            ("alias.attack", "!touch /tmp/alias"),
            ("filter.attack.clean", "/tmp/filter"),
            ("filter.attack.smudge", "/tmp/filter"),
            ("filter.attack.process", "/tmp/filter"),
            ("merge.attack.driver", "/tmp/merge"),
            ("diff.attack.command", "/tmp/diff"),
            ("diff.attack.textconv", "/tmp/textconv"),
            ("core.fsmonitor", "/tmp/fsmonitor"),
            ("include.path", "/tmp/config"),
        )
        for key, value in dangerous:
            with self.subTest(key=key):
                self.git("config", key, value)
                with self.assertRaisesRegex(
                    ContractError,
                    "executable Git integration",
                ):
                    self.runner.assert_no_executable_integrations(self.target)
                self.git("config", "--unset-all", key)

    def test_hostile_hooks_do_not_run_during_controller_commit(self) -> None:
        common = Path(self.git("rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = self.target / common
        hooks = common / "hooks"
        hooks.mkdir(exist_ok=True)
        marker = self.target.parent / f"{self.target.name}-hook-called"
        self.addCleanup(marker.unlink, missing_ok=True)
        for name in ("pre-commit", "post-checkout", "post-commit"):
            hook = hooks / name
            hook.write_text(
                f"#!/bin/sh\n/usr/bin/touch {marker}\nexit 97\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
        manager = WorktreeManager(self.target, runner=self.runner)
        baseline = manager.assert_clean_baseline()
        task = manager.create_task_worktree(
            "RUN-20260723-001",
            "TASK-001",
            1,
            baseline.base_sha,
        )
        contract = self._contract()
        preflight = manager.capture_worker_preflight(
            task,
            contract.id,
            "2026-07-23T10:00:00+00:00",
            manager.snapshot_protected_git(task.branch),
        )
        (task.path / "README.md").write_text("safe\n", encoding="utf-8")

        prepared = manager.prepare_source_commit(
            contract,
            task,
            preflight,
            SourceCommitMetadata(
                "RUN-20260723-001",
                "TASK-001",
                1,
                "2026-07-23T10:00:00+00:00",
            ),
        )
        manager.apply_source_commit_cas(task, prepared)

        self.assertFalse(marker.exists())

    def test_required_clean_filter_is_rejected_before_staging(self) -> None:
        manager = WorktreeManager(self.target, runner=self.runner)
        baseline = manager.assert_clean_baseline()
        task = manager.create_task_worktree(
            "RUN-20260723-001",
            "TASK-001",
            1,
            baseline.base_sha,
        )
        contract = self._contract()
        preflight = manager.capture_worker_preflight(
            task,
            contract.id,
            "2026-07-23T10:00:00+00:00",
            manager.snapshot_protected_git(task.branch),
        )
        (task.path / "README.md").write_text("filtered\n", encoding="utf-8")
        self.git("config", "filter.hostile.clean", "/tmp/never-run")
        self.git("config", "filter.hostile.required", "true")

        with self.assertRaisesRegex(ContractError, "executable Git integration"):
            manager.prepare_source_commit(
                contract,
                task,
                preflight,
                SourceCommitMetadata(
                    "RUN-20260723-001",
                    "TASK-001",
                    1,
                    "2026-07-23T10:00:00+00:00",
                ),
            )
        self.assertEqual(
            self.git("write-tree", cwd=task.path),
            self.git("rev-parse", "HEAD^{tree}"),
        )

    @staticmethod
    def _restore_path(previous: str | None) -> None:
        if previous is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous

    @staticmethod
    def _contract():
        from vibe.models import TaskContract

        return TaskContract(
            id="TASK-001",
            objective="Edit README",
            worker_type="implementation",
            covers=("AC-001",),
            depends_on=(),
            path_scope=("README.md",),
            exclusive_resources=(),
            acceptance_checks=(),
            max_attempts=1,
        )


if __name__ == "__main__":
    unittest.main()
