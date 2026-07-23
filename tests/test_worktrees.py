from __future__ import annotations

import os
import unittest
from pathlib import Path

from tests.support.git_repo import GitRepositoryFixture
from vibe.models import ContractError, TaskContract
from vibe.worktrees import (
    SourceCommitMetadata,
    WorktreeManager,
)


class WorktreeManagerTests(GitRepositoryFixture, unittest.TestCase):
    def setUp(self) -> None:
        self.setUpGitRepository()
        self.manager = WorktreeManager(self.target)

    def metadata(
        self,
        task_id: str = "TASK-001",
        attempt_no: int = 1,
    ) -> SourceCommitMetadata:
        return SourceCommitMetadata(
            run_id="RUN-20260723-001",
            task_id=task_id,
            attempt_no=attempt_no,
            attempt_created_at="2026-07-23T10:00:00+00:00",
        )

    def contract(
        self,
        *,
        task_id: str = "TASK-001",
        scope: tuple[str, ...] = ("README.md",),
    ) -> TaskContract:
        return TaskContract(
            id=task_id,
            objective="Edit scoped files",
            worker_type="implementation",
            covers=("AC-001",),
            depends_on=(),
            path_scope=scope,
            exclusive_resources=(),
            acceptance_checks=(),
            max_attempts=3,
        )

    def create_task(
        self,
        *,
        task_id: str = "TASK-001",
        attempt_no: int = 1,
    ):
        baseline = self.manager.assert_clean_baseline()
        task = self.manager.create_task_worktree(
            "RUN-20260723-001",
            task_id,
            attempt_no,
            baseline.base_sha,
        )
        return baseline, task

    def preflight(self, task, contract):
        return self.manager.capture_worker_preflight(
            task=task,
            task_id=contract.id,
            attempt_created_at="2026-07-23T10:00:00+00:00",
            protected=self.manager.snapshot_protected_git(task.branch),
        )

    def test_clean_baseline_is_full_oid_and_does_not_change_user_checkout(self) -> None:
        branch = self.git("symbolic-ref", "--short", "HEAD")
        index = self.git("write-tree")

        baseline = self.manager.assert_clean_baseline()

        self.assertRegex(baseline.base_sha, r"[0-9a-f]{40}")
        self.assertEqual(baseline.base_ref, "refs/heads/main")
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD"), branch)
        self.assertEqual(self.git("write-tree"), index)

    def test_detached_head_uses_literal_head_selector(self) -> None:
        self.git("checkout", "--detach", "HEAD")

        baseline = self.manager.assert_clean_baseline()

        self.assertEqual(baseline.base_ref, "HEAD")
        self.assertRegex(baseline.base_sha, r"[0-9a-f]{40}")

    def test_dirty_product_is_rejected_but_control_and_ignored_are_excluded(self) -> None:
        (self.target / "README.md").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "clean product baseline"):
            self.manager.assert_clean_baseline()

        self.git("checkout", "--", "README.md")
        (self.target / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore logs")
        (self.target / "ignored.log").write_text("ignored\n", encoding="utf-8")
        (self.target / ".vibe-coding").mkdir()
        (self.target / ".vibe-coding/state.tmp").write_text(
            "control\n",
            encoding="utf-8",
        )

        self.manager.assert_clean_baseline()

    def test_refs_and_detached_worktree_do_not_checkout_user_branch(self) -> None:
        before = (
            self.git("symbolic-ref", "--short", "HEAD"),
            self.git("write-tree"),
            self.git(
                "status",
                "--porcelain=v2",
                "-z",
                "--",
                ".",
                ":(exclude).vibe-coding",
                ":(exclude).vibe-coding/**",
            ),
        )
        baseline = self.manager.assert_clean_baseline()
        run_ref = self.manager.create_run_ref(
            "RUN-20260723-001",
            baseline.base_sha,
        )
        task = self.manager.create_task_worktree(
            "RUN-20260723-001",
            "TASK-001",
            1,
            baseline.base_sha,
        )

        self.assertEqual(run_ref, "refs/heads/vibe/run-RUN-20260723-001")
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=task.path),
            "HEAD",
        )
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=task.path), baseline.base_sha)
        self.assertEqual(
            self.git("rev-parse", task.branch),
            baseline.base_sha,
        )
        self.assertEqual(
            (
                self.git("symbolic-ref", "--short", "HEAD"),
                self.git("write-tree"),
                self.git(
                    "status",
                    "--porcelain=v2",
                    "-z",
                    "--",
                    ".",
                    ":(exclude).vibe-coding",
                    ":(exclude).vibe-coding/**",
                ),
            ),
            before,
        )

    def test_controller_prepares_deterministic_commit_from_raw_rename(self) -> None:
        baseline, task = self.create_task()
        (task.path / "README.md").rename(task.path / "GUIDE.md")
        contract = self.contract(scope=("README.md", "GUIDE.md"))
        preflight = self.preflight(task, contract)

        prepared = self.manager.prepare_source_commit(
            contract,
            task,
            preflight,
            self.metadata(),
        )
        replay = self.manager.prepare_source_commit(
            contract,
            task,
            preflight,
            self.metadata(),
        )

        self.assertEqual(
            prepared.source_audit.changed_paths,
            ("GUIDE.md", "README.md"),
        )
        self.assertEqual(replay.candidate_commit, prepared.candidate_commit)
        self.assertEqual(self.git("rev-parse", task.branch), baseline.base_sha)
        self.assertEqual(
            self.git("rev-list", "--parents", "-n", "1", prepared.candidate_commit),
            f"{prepared.candidate_commit} {baseline.base_sha}",
        )

    def test_authorized_deletion_is_recorded_and_out_of_scope_deletion_rejected(self) -> None:
        _, task = self.create_task()
        (task.path / "README.md").unlink()
        contract = self.contract(scope=("README.md",))
        prepared = self.manager.prepare_source_commit(
            contract,
            task,
            self.preflight(task, contract),
            self.metadata(),
        )
        self.assertEqual(prepared.source_audit.changed_paths, ("README.md",))

        self.git("worktree", "remove", "--force", str(task.path))
        self.git("update-ref", "-d", task.branch)
        _, escaped = self.create_task(task_id="TASK-002")
        (escaped.path / "README.md").unlink()
        escaped_contract = self.contract(
            task_id="TASK-002",
            scope=("src/",),
        )
        with self.assertRaisesRegex(ContractError, "outside path scope"):
            self.manager.prepare_source_commit(
                escaped_contract,
                escaped,
                self.preflight(escaped, escaped_contract),
                self.metadata(task_id="TASK-002"),
            )

    def test_control_path_and_symlink_ancestor_are_rejected(self) -> None:
        _, task = self.create_task()
        control = task.path / ".vibe-coding"
        control.mkdir(exist_ok=True)
        (control / "owned.txt").write_text("no\n", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "control path"):
            self.manager.prepare_source_commit(
                self.contract(scope=(".",)),
                task,
                self.preflight(task, self.contract(scope=(".",))),
                self.metadata(),
            )

        self.git("worktree", "remove", "--force", str(task.path))
        self.git("update-ref", "-d", task.branch)
        _, linked = self.create_task(task_id="TASK-002")
        outside = Path(self.git_temporary.name).parent / "vibe-outside"
        outside.mkdir(exist_ok=True)
        os.symlink(outside, linked.path / "linked")
        (outside / "escape.py").write_text("escape\n", encoding="utf-8")
        linked_contract = self.contract(
            task_id="TASK-002",
            scope=("linked/",),
        )
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            self.manager.prepare_source_commit(
                linked_contract,
                linked,
                self.preflight(linked, linked_contract),
                self.metadata(task_id="TASK-002"),
            )
        (outside / "escape.py").unlink()
        outside.rmdir()

    def test_protected_ref_config_and_remote_mutation_are_rejected(self) -> None:
        mutations = ("ref", "config", "remote")
        for attempt_no, mutation in enumerate(mutations, start=1):
            with self.subTest(mutation=mutation):
                task_id = f"TASK-{attempt_no:03d}"
                _, task = self.create_task(
                    task_id=task_id,
                    attempt_no=attempt_no,
                )
                contract = self.contract(task_id=task_id)
                preflight = self.preflight(task, contract)
                (task.path / "README.md").write_text(
                    f"{mutation}\n",
                    encoding="utf-8",
                )
                if mutation == "ref":
                    self.git(
                        "update-ref",
                        f"refs/heads/unexpected-{attempt_no}",
                        task.base_sha,
                    )
                elif mutation == "config":
                    self.git("config", "vibe.mutated", mutation)
                else:
                    self.git(
                        "remote",
                        "add",
                        f"sentinel-{attempt_no}",
                        "/tmp/never-contact",
                    )
                with self.assertRaisesRegex(ContractError, "protected Git"):
                    self.manager.prepare_source_commit(
                        contract,
                        task,
                        preflight,
                        self.metadata(task_id=task_id, attempt_no=attempt_no),
                    )
                if mutation == "ref":
                    self.git("update-ref", "-d", f"refs/heads/unexpected-{attempt_no}")
                elif mutation == "config":
                    self.git("config", "--unset", "vibe.mutated")
                else:
                    self.git("remote", "remove", f"sentinel-{attempt_no}")

    def test_source_ref_cas_and_recovery_have_three_outcomes(self) -> None:
        _, task = self.create_task()
        (task.path / "README.md").write_text("candidate\n", encoding="utf-8")
        contract = self.contract()
        prepared = self.manager.prepare_source_commit(
            contract,
            task,
            self.preflight(task, contract),
            self.metadata(),
        )

        self.assertEqual(
            self.manager.classify_source_cas(task, prepared),
            "RETRY_CAS",
        )
        self.manager.apply_source_commit_cas(task, prepared)
        self.assertEqual(
            self.manager.classify_source_cas(task, prepared),
            "COMPLETE_STATE",
        )
        self.git("update-ref", task.branch, task.base_sha)
        other = self.git("commit-tree", f"{task.base_sha}^{{tree}}", "-p", task.base_sha, "-m", "other")
        self.git("update-ref", task.branch, other)
        self.assertEqual(
            self.manager.classify_source_cas(task, prepared),
            "PAUSE",
        )


if __name__ == "__main__":
    unittest.main()
