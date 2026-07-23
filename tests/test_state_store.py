from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests.test_models import minimal_state
from vibe.models import ContractError, StateConflictError
from vibe.state_store import (
    StateStore,
    artifact_ref,
    canonical_json_bytes,
    load_json_object,
    open_absolute_directory_no_follow,
    publish_immutable_at,
)


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name)
        self.store = StateStore.for_run(self.target, "RUN-20260723-001")

    def _create(self) -> dict[str, object]:
        config_body = b'{"frozen":true}\n'
        intent_body = b'{"run_id":"RUN-20260723-001"}\n'
        state = minimal_state()
        config_ref = artifact_ref("config.json", config_body)
        intent_ref = artifact_ref("creation.intent.json", intent_body)
        state["config"] = config_ref.as_dict()
        state["creation"] = {
            "intent": intent_ref.as_dict(),
            "receipt": None,
        }
        state["artifact_index"] = [
            intent_ref.as_dict(),
            config_ref.as_dict(),
        ]
        with self.store.lock():
            return self.store.create(
                state,
                {
                    "config.json": config_body,
                    "creation.intent.json": intent_body,
                },
            )

    @staticmethod
    def create_at(parent_fd: int, leaf: str, body: bytes) -> None:
        descriptor = os.open(
            leaf,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            view = memoryview(body)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def swap_run_directory_for_symlink(self, external: Path) -> None:
        displaced = self.target / "displaced-run"
        self.store.root.rename(displaced)
        self.store.root.symlink_to(external, target_is_directory=True)

    def test_create_writes_artifact_before_bound_state(self) -> None:
        state = self._create()
        config = self.store.root / "config.json"
        self.assertEqual(config.read_bytes(), b'{"frozen":true}\n')
        self.assertEqual(self.store.load(), state)

    def test_artifact_is_write_once_but_identical_retry_is_idempotent(
        self,
    ) -> None:
        self._create()
        with self.store.lock():
            state = self.store.transact(
                0,
                {"config.json": b'{"frozen":true}\n'},
                lambda current, refs: current.update({"last_error": None}),
            )
        self.assertEqual(state["revision"], 1)

        with self.store.lock(), self.assertRaisesRegex(
            StateConflictError,
            "immutable artifact",
        ):
            self.store.transact(
                1,
                {"config.json": b'{"frozen":false}\n'},
                lambda current, refs: current.update({"last_error": None}),
            )

    def test_mutator_cannot_remove_a_newly_auto_indexed_artifact(self) -> None:
        self._create()

        def remove_new_index_entry(
            current: dict[str, object],
            refs: object,
        ) -> None:
            del refs
            current["artifact_index"].pop()

        with self.store.lock(), self.assertRaisesRegex(
            ContractError,
            "artifact_index is append-only",
        ):
            self.store.transact(
                0,
                {"diagnostics/new.json": b"{}\n"},
                remove_new_index_entry,
            )
        self.assertFalse((self.store.root / "diagnostics/new.json").exists())

    def test_semantic_histories_cannot_be_removed_reordered_or_rewritten(
        self,
    ) -> None:
        self._create()
        plan_bodies = {
            "plan/plan-v001.json": b'{"plan_version":1}\n',
            "plan/plan-v002.json": b'{"plan_version":2}\n',
        }

        def append_plans(
            current: dict[str, object],
            refs: object,
        ) -> None:
            current["plans"].extend(
                refs[path].as_dict() for path in sorted(plan_bodies)
            )
            current["plan_version"] = 2

        with self.store.lock():
            committed = self.store.transact(
                0,
                plan_bodies,
                append_plans,
            )
        for mutation in (
            lambda state: state["plans"].clear(),
            lambda state: state["plans"].reverse(),
            lambda state: state["plans"][0].update(
                {"sha256": "sha256:" + "f" * 64}
            ),
        ):
            with self.subTest(mutation=mutation), self.store.lock():
                with self.assertRaisesRegex(
                    ContractError,
                    "append-only history",
                ):
                    self.store.transact(
                        committed["revision"],
                        {},
                        lambda current, refs: mutation(current),
                    )

    def test_revision_conflict_does_not_write_artifacts(self) -> None:
        self._create()
        with self.store.lock(), self.assertRaisesRegex(
            StateConflictError,
            "expected revision 8",
        ):
            self.store.transact(
                8,
                {"tasks/TASK-001/task.json": b"{}\n"},
                lambda current, refs: None,
            )
        self.assertFalse(
            (self.store.root / "tasks/TASK-001/task.json").exists()
        )

    def test_orphan_artifact_is_not_adopted_by_load(self) -> None:
        self._create()
        orphan = self.store.root / "tasks/TASK-999/task.json"
        orphan.parent.mkdir(parents=True)
        orphan.write_text("{}\n", encoding="utf-8")
        self.assertNotIn("TASK-999", self.store.load()["tasks"])

    def test_tampered_bound_artifact_is_rejected_on_load(self) -> None:
        self._create()
        (self.store.root / "config.json").write_text(
            '{"frozen":false}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ContractError, "artifact digest"):
            self.store.load()

    def test_lock_rejects_a_second_nonblocking_writer(self) -> None:
        with self.store.lock():
            second = StateStore.for_run(
                self.target,
                "RUN-20260723-001",
            )
            with self.assertRaisesRegex(StateConflictError, "run lock"):
                with second.lock(blocking=False):
                    self.fail("second writer acquired the lock")

    def test_same_store_lock_is_explicitly_non_reentrant(self) -> None:
        with self.store.lock(), self.assertRaisesRegex(
            StateConflictError,
            "non-reentrant",
        ):
            with self.store.lock():
                self.fail("nested lock unexpectedly succeeded")

    def test_same_store_mutation_requires_the_owning_thread(self) -> None:
        self._create()
        failures: list[BaseException] = []

        def mutate_from_other_thread() -> None:
            try:
                self.store.transact(0, {}, lambda state, refs: None)
            except BaseException as error:
                failures.append(error)

        with self.store.lock():
            thread = threading.Thread(target=mutate_from_other_thread)
            thread.start()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], StateConflictError)

    def test_strict_loader_rejects_duplicate_keys_and_symlink_state(
        self,
    ) -> None:
        self.store.root.mkdir(parents=True)
        self.store.state_path.write_text(
            '{"schema_version":4,"schema_version":4}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ContractError, "duplicate JSON key"):
            load_json_object(self.store.state_path)

        self.store.state_path.unlink()
        target = self.store.root / "elsewhere.json"
        target.write_text("{}\n", encoding="utf-8")
        self.store.state_path.symlink_to(target)
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            load_json_object(self.store.state_path)

    def test_strict_loader_rejects_non_finite_numbers(self) -> None:
        self.store.root.mkdir(parents=True)
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                self.store.state_path.write_text(
                    f'{{"value":{literal}}}\n',
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ContractError, "non-finite"):
                    load_json_object(self.store.state_path)

    def test_load_and_log_reject_a_symlinked_control_ancestor(self) -> None:
        self._create()
        control = self.target / ".vibe-coding"
        external = self.target / "relocated-control"
        control.rename(external)
        control.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            self.store.load()
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            self.store.append_log({"event": "must-not-escape"})

    def test_append_log_rejects_a_symlink_leaf(self) -> None:
        self._create()
        external = self.target / "external.log"
        external.write_text("", encoding="utf-8")
        self.store.log_path.parent.mkdir(parents=True)
        self.store.log_path.symlink_to(external)
        with self.assertRaisesRegex(ContractError, "symbolic link"):
            self.store.append_log({"event": "must-not-escape"})

    def test_parent_swap_after_open_never_reads_or_writes_external_tree(
        self,
    ) -> None:
        self._create()
        external = self.target / "external"
        external.mkdir()
        sentinel = external / "state.json"
        sentinel.write_text("external\n", encoding="utf-8")
        with self.store.inject_after_run_dir_open(
            lambda: self.swap_run_directory_for_symlink(external)
        ):
            with self.assertRaises(ContractError):
                self.store.load()
        self.assertEqual(
            sentinel.read_text(encoding="utf-8"),
            "external\n",
        )

    def test_racing_immutable_leaf_is_never_replaced(self) -> None:
        self._create()
        with self.store.inject_before_immutable_publish(
            lambda parent_fd, leaf: self.create_at(
                parent_fd,
                leaf,
                b"racer\n",
            )
        ), self.store.lock(), self.assertRaisesRegex(
            StateConflictError,
            "immutable artifact",
        ):
            self.store.transact(
                0,
                {"diagnostics/race.json": b"controller\n"},
                lambda state, refs: None,
            )
        self.assertEqual(
            (self.store.root / "diagnostics/race.json").read_bytes(),
            b"racer\n",
        )

    def test_transaction_orders_artifact_and_state_durability(self) -> None:
        self._create()
        events: list[str] = []
        with mock.patch(
            "vibe.state_store._record_durability_event",
            side_effect=events.append,
        ):
            with self.store.lock():
                self.store.transact(
                    0,
                    {"tasks/TASK-001/task.json": b'{"id":"TASK-001"}\n'},
                    lambda state, refs: state["tasks"].update(
                        {
                            "TASK-001": {
                                "status": "PENDING",
                                "task": refs[
                                    "tasks/TASK-001/task.json"
                                ].as_dict(),
                                "attempt_no": 0,
                                "failure_count": 0,
                                "max_attempts": 3,
                                "active_attempt": None,
                                "attempts": [],
                                "result": None,
                                "verification": None,
                                "source_commits": [],
                                "integrated_commits": [],
                                "last_error": None,
                            }
                        }
                    ),
                )
        self.assertLess(
            events.index("artifact-file-fsync"),
            events.index("tasks-dir-fsync"),
        )
        self.assertLess(
            events.index("tasks-dir-fsync"),
            events.index("task-dir-fsync"),
        )
        self.assertLess(
            events.index("task-dir-fsync"),
            events.index("state-file-fsync"),
        )
        self.assertLess(
            events.index("state-file-fsync"),
            events.index("run-dir-fsync"),
        )

    def test_append_log_retries_partial_writes_and_fsyncs_new_parent(
        self,
    ) -> None:
        self._create()
        real_write = os.write

        def partial_write(descriptor: int, body: object) -> int:
            view = memoryview(body)
            amount = max(1, len(view) // 2)
            return real_write(descriptor, view[:amount])

        with mock.patch(
            "vibe.state_store.os.write",
            side_effect=partial_write,
        ) as write, mock.patch("vibe.state_store.os.fsync") as fsync:
            self.store.append_log({"event": "complete"})

        self.assertGreater(write.call_count, 1)
        self.assertEqual(
            json.loads(self.store.log_path.read_text(encoding="utf-8")),
            {"event": "complete"},
        )
        self.assertGreaterEqual(fsync.call_count, 2)

    def test_state_replace_failure_leaves_only_an_unreferenced_orphan(
        self,
    ) -> None:
        self._create()
        original = self.store.state_path.read_bytes()
        with mock.patch(
            "vibe.state_store.replace_mutable_at",
            side_effect=OSError("injected state crash"),
        ):
            with self.store.lock(), self.assertRaisesRegex(
                OSError,
                "injected",
            ):
                self.store.transact(
                    0,
                    {"tasks/TASK-001/task.json": b'{"id":"TASK-001"}\n'},
                    lambda state, refs: state["tasks"].update(
                        {
                            "TASK-001": {
                                "status": "PENDING",
                                "task": refs[
                                    "tasks/TASK-001/task.json"
                                ].as_dict(),
                                "attempt_no": 0,
                                "failure_count": 0,
                                "max_attempts": 3,
                                "active_attempt": None,
                                "attempts": [],
                                "result": None,
                                "verification": None,
                                "source_commits": [],
                                "integrated_commits": [],
                                "last_error": None,
                            }
                        }
                    ),
                )

        self.assertEqual(self.store.state_path.read_bytes(), original)
        self.assertTrue(
            (self.store.root / "tasks/TASK-001/task.json").is_file()
        )

    def test_each_control_ancestor_swap_fails_closed(self) -> None:
        for component in (".vibe-coding", "runs", "RUN-20260723-001"):
            with self.subTest(component=component):
                with tempfile.TemporaryDirectory() as temporary:
                    target = Path(temporary)
                    store = StateStore.for_run(target, "RUN-20260723-001")
                    config = b'{"frozen":true}\n'
                    intent = b'{"run_id":"RUN-20260723-001"}\n'
                    state = minimal_state()
                    state["config"] = artifact_ref(
                        "config.json",
                        config,
                    ).as_dict()
                    state["creation"]["intent"] = artifact_ref(
                        "creation.intent.json",
                        intent,
                    ).as_dict()
                    state["artifact_index"] = [
                        state["creation"]["intent"],
                        state["config"],
                    ]
                    with store.lock():
                        store.create(
                            state,
                            {
                                "config.json": config,
                                "creation.intent.json": intent,
                            },
                        )
                    paths = {
                        ".vibe-coding": target / ".vibe-coding",
                        "runs": target / ".vibe-coding" / "runs",
                        "RUN-20260723-001": store.root,
                    }
                    victim = paths[component]
                    external = target / f"external-{component}"
                    external.mkdir()
                    displaced = target / f"displaced-{component}"

                    def swap() -> None:
                        victim.rename(displaced)
                        victim.symlink_to(
                            external,
                            target_is_directory=True,
                        )

                    with store.inject_after_run_dir_open(swap):
                        with self.assertRaises(ContractError):
                            store.load()
                    self.assertFalse((external / "state.json").exists())

    def test_two_publishers_racing_different_bytes_never_replace_winner(
        self,
    ) -> None:
        parent = self.target / "race"
        parent.mkdir()
        parent_fd = open_absolute_directory_no_follow(parent.resolve())
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, object]] = []

        def publish(name: str, body: bytes) -> None:
            descriptor = os.dup(parent_fd)
            try:
                barrier.wait(timeout=5)
                publish_immutable_at(descriptor, "winner.json", body)
                outcomes.append((name, body))
            except BaseException as error:
                outcomes.append((name, error))
            finally:
                os.close(descriptor)

        threads = [
            threading.Thread(target=publish, args=("first", b"first\n")),
            threading.Thread(target=publish, args=("second", b"second\n")),
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        finally:
            os.close(parent_fd)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        successes = [
            result for _, result in outcomes if isinstance(result, bytes)
        ]
        failures = [
            result
            for _, result in outcomes
            if isinstance(result, StateConflictError)
        ]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual((parent / "winner.json").read_bytes(), successes[0])

    def _completed_attempt_fixture(
        self,
    ) -> tuple[dict[str, object], dict[str, bytes], dict[str, object]]:
        bodies = {
            "config.json": b'{"frozen":true}\n',
            "creation.intent.json": b'{"run_id":"RUN-20260723-001"}\n',
            "tasks/TASK-001/task.json": b'{"id":"TASK-001"}\n',
            "tasks/TASK-001/attempts/001/preflight.json": b"{}\n",
            "tasks/TASK-001/attempts/001/result.json": b"{}\n",
            "tasks/TASK-001/attempts/001/source-audit.json": b"{}\n",
            "verification/tasks/TASK-001-a1/VERIFY-00000000-0000-4000-8000-000000000001/manifest.json": b"{}\n",
        }
        refs = {
            path: artifact_ref(path, body).as_dict()
            for path, body in bodies.items()
        }
        manifest = {
            "schema_version": 1,
            "role": "worker",
            "operation_id": "WORKER-TASK-001",
            "task_id": "TASK-001",
            "attempt_no": 1,
            "attempt_token": "ATTEMPT-TASK-001-001",
            "status": "SUCCEEDED",
            "created_at": "2026-07-23T10:00:00+08:00",
            "completed_at": "2026-07-23T10:01:00+08:00",
            "expected_base": "a" * 40,
            "branch": "refs/heads/vibe/run-RUN-20260723-001/task-TASK-001",
            "worktree": ".vibe-coding/worktrees/RUN-20260723-001/TASK-001",
            "preflight": refs[
                "tasks/TASK-001/attempts/001/preflight.json"
            ],
            "prompt_versions": [],
            "provider_attempts": [],
            "request": None,
            "launch": None,
            "stdout": None,
            "stderr": None,
            "exit": None,
            "result": refs[
                "tasks/TASK-001/attempts/001/result.json"
            ],
            "source_audit": refs[
                "tasks/TASK-001/attempts/001/source-audit.json"
            ],
            "verification": refs[
                "verification/tasks/TASK-001-a1/"
                "VERIFY-00000000-0000-4000-8000-000000000001/"
                "manifest.json"
            ],
            "last_error": None,
        }
        attempt_path = "tasks/TASK-001/attempts/001/attempt.json"
        bodies[attempt_path] = canonical_json_bytes(manifest)
        refs[attempt_path] = artifact_ref(
            attempt_path,
            bodies[attempt_path],
        ).as_dict()
        state = minimal_state()
        state["config"] = refs["config.json"]
        state["creation"]["intent"] = refs["creation.intent.json"]
        state["tasks"] = {
            "TASK-001": {
                "status": "COMPLETED",
                "task": refs["tasks/TASK-001/task.json"],
                "attempt_no": 1,
                "failure_count": 0,
                "max_attempts": 3,
                "active_attempt": None,
                "attempts": [refs[attempt_path]],
                "result": manifest["result"],
                "verification": manifest["verification"],
                "source_commits": ["b" * 40],
                "integrated_commits": ["c" * 40],
                "last_error": None,
            }
        }
        state["verifications"] = [manifest["verification"]]
        state["artifact_index"] = list(refs.values())
        return state, bodies, manifest

    def test_load_rejects_semantically_mismatched_attempt_manifests(
        self,
    ) -> None:
        mutations = {
            "role": lambda manifest: manifest.update(
                {"role": "evaluator", "task_id": None, "branch": None}
            ),
            "status": lambda manifest: manifest.update({"status": "FAILED"}),
            "verification": lambda manifest: manifest.update(
                {
                    "verification": artifact_ref(
                        "verification/other.json",
                        b"{}\n",
                    ).as_dict()
                }
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    store = StateStore.for_run(
                        Path(temporary),
                        "RUN-20260723-001",
                    )
                    state, bodies, manifest = self._completed_attempt_fixture()
                    broken = deepcopy(manifest)
                    mutate(broken)
                    attempt_path = (
                        "tasks/TASK-001/attempts/001/attempt.json"
                    )
                    bodies[attempt_path] = canonical_json_bytes(broken)
                    old_ref = state["tasks"]["TASK-001"]["attempts"][0]
                    new_ref = artifact_ref(
                        attempt_path,
                        bodies[attempt_path],
                    ).as_dict()
                    state["tasks"]["TASK-001"]["attempts"][0] = new_ref
                    state["artifact_index"].remove(old_ref)
                    state["artifact_index"].append(new_ref)
                    with store.lock():
                        store.create(state, bodies)
                    with self.assertRaises(ContractError):
                        store.load()


if __name__ == "__main__":
    unittest.main()
