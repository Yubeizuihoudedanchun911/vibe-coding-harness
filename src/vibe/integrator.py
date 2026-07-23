from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from vibe.config import effective_command_ids
from vibe.models import (
    ArtifactRef,
    ContractError,
    FrozenRunConfig,
    PendingIntegration,
    TaskContract,
)
from vibe.scheduler import normalize_scope, path_matches_scope
from vibe.state_store import (
    MAX_ARTIFACT_BYTES,
    StateStore,
    canonical_json_bytes,
    open_absolute_regular_no_follow,
    parse_json_object_bytes,
    read_bounded,
)
from vibe.verification import VerificationGate, VerificationResult
from vibe.worktrees import SourceAudit, WorktreeManager


class IntegrationRejected(ContractError):
    """A candidate failed structural, scope, conflict, or gate checks."""


class IntegrationRecovery(str, Enum):
    RETRY_CAS = "RETRY_CAS"
    COMPLETE_STATE = "COMPLETE_STATE"
    PAUSE = "PAUSE"


@dataclass(frozen=True)
class CandidateIntegration:
    operation_id: str
    path: Path
    expected_head: str
    candidate_head: str
    source_audit: SourceAudit
    source_audit_ref: ArtifactRef
    verification: VerificationResult


class Integrator:
    def __init__(
        self,
        *,
        worktrees: WorktreeManager,
        store: StateStore,
        verification: VerificationGate,
        config: FrozenRunConfig,
        fault_hook: Callable[[str], None],
    ) -> None:
        self.worktrees = worktrees
        self.store = store
        self.verification = verification
        self.config = config
        self.fault_hook = fault_hook

    def integrate(
        self,
        contract: TaskContract,
        source_audit: SourceAudit,
    ) -> CandidateIntegration:
        state = self.store.load()
        candidate = self.prepare(state, contract, source_audit)
        self.fault_hook(
            "after_candidate_verification_before_pending_integration"
        )
        task = state["tasks"][contract.id]
        pending = PendingIntegration(
            operation_id=candidate.operation_id,
            task_id=contract.id,
            attempt_no=task["attempt_no"],
            expected_head=candidate.expected_head,
            candidate_head=candidate.candidate_head,
            source_base=source_audit.task_base_sha,
            source_head=source_audit.source_head,
            verification=candidate.verification.manifest_ref,
        )
        self._persist_pending(state, contract, candidate, pending)
        self.fault_hook(
            "after_pending_integration_before_update_ref"
        )
        self.apply_cas(pending)
        self.fault_hook(
            "after_update_ref_before_state_completion"
        )
        self._complete_state(pending)
        return candidate

    def prepare(
        self,
        state: dict[str, object],
        contract: TaskContract,
        source_audit: SourceAudit,
    ) -> CandidateIntegration:
        if state["pending_integration"] is not None:
            raise IntegrationRejected("another integration is already prepared")
        task = state["tasks"].get(contract.id)
        if not isinstance(task, dict) or task.get("status") != "READY_TO_INTEGRATE":
            raise IntegrationRejected("task is not READY_TO_INTEGRATE")
        active = task.get("active_attempt")
        if not isinstance(active, dict) or active.get("status") != "VERIFYING":
            raise IntegrationRejected("task has no VERIFYING Worker Attempt")
        source_ref = self._source_audit_ref(
            state,
            contract.id,
            task["attempt_no"],
        )
        self._verify_source_audit(
            contract,
            task,
            active,
            source_ref,
            source_audit,
        )
        repository = state["repository"]
        expected_head = repository["integration_head"]
        actual = self.worktrees.resolve_ref(repository["integration_ref"])
        if actual != expected_head:
            raise IntegrationRejected(
                "integration ref changed before candidate preparation"
            )
        operation_id = f"INT-{uuid.uuid4()}"
        candidate_path = self.worktrees.create_disposable_worktree(
            state["run_id"],
            "candidates",
            operation_id,
            expected_head,
        )
        try:
            self.worktrees.runner.run_local(
                candidate_path,
                "cherry-pick",
                source_audit.source_head,
            )
        except ContractError as error:
            raise IntegrationRejected(
                f"candidate cherry-pick conflicted: {error}"
            ) from error
        candidate_head = self._head(candidate_path)
        self._verify_candidate_structure(
            candidate_head,
            expected_head,
        )
        candidate_paths = self._changed_paths(
            candidate_path,
            expected_head,
            candidate_head,
        )
        if candidate_paths != source_audit.changed_paths:
            raise IntegrationRejected(
                "candidate delta differs from audited source paths"
            )
        self._require_paths_in_scope(
            candidate_paths,
            contract.path_scope,
        )
        command_ids = effective_command_ids(
            self.config,
            contract.acceptance_checks,
        )
        verification = self.verification.run(
            candidate_head,
            candidate_path,
            command_ids,
            (
                f"verification/tasks/{contract.id}-a{task['attempt_no']}/"
                f"VERIFY-{uuid.uuid4()}"
            ),
        )
        if not verification.passed:
            raise IntegrationRejected("candidate verification failed")
        if verification.commit_sha != candidate_head:
            raise IntegrationRejected(
                "candidate verification is bound to another commit"
            )
        return CandidateIntegration(
            operation_id,
            candidate_path,
            expected_head,
            candidate_head,
            source_audit,
            source_ref,
            verification,
        )

    def apply_cas(self, pending: PendingIntegration) -> None:
        state = self.store.load()
        current = state.get("pending_integration")
        if current != pending.as_dict():
            raise ContractError("pending integration no longer matches CAS")
        ref = state["repository"]["integration_ref"]
        self.worktrees.update_ref(
            ref,
            pending.candidate_head,
            pending.expected_head,
        )

    def classify_recovery(
        self,
        pending: PendingIntegration,
    ) -> IntegrationRecovery:
        state = self.store.load()
        actual = self.worktrees.resolve_ref(
            state["repository"]["integration_ref"]
        )
        if actual == pending.expected_head:
            return IntegrationRecovery.RETRY_CAS
        if actual == pending.candidate_head:
            return IntegrationRecovery.COMPLETE_STATE
        return IntegrationRecovery.PAUSE

    def recover(self) -> IntegrationRecovery:
        state = self.store.load()
        raw = state.get("pending_integration")
        if not isinstance(raw, dict):
            raise ContractError("there is no pending integration to recover")
        pending = self._pending_from_state(raw)
        outcome = self.classify_recovery(pending)
        if outcome is IntegrationRecovery.RETRY_CAS:
            self._verify_pending(state, pending)
            self.apply_cas(pending)
            self._complete_state(pending)
        elif outcome is IntegrationRecovery.COMPLETE_STATE:
            self._verify_pending(state, pending)
            self._complete_state(pending)
        else:
            self._pause_for_ref_movement(state, pending)
        return outcome

    def _persist_pending(
        self,
        state: dict[str, object],
        contract: TaskContract,
        candidate: CandidateIntegration,
        pending: PendingIntegration,
    ) -> None:
        task = state["tasks"][contract.id]
        active = task["active_attempt"]
        completed_at = _now()
        manifest_path = (
            f"tasks/{contract.id}/attempts/"
            f"{task['attempt_no']:03d}/attempt.json"
        )
        manifest_body = canonical_json_bytes(
            {
                "schema_version": 1,
                "role": "worker",
                "operation_id": (
                    f"WORK-{contract.id}-A{task['attempt_no']}"
                ),
                "task_id": contract.id,
                "attempt_no": task["attempt_no"],
                "attempt_token": active["attempt_token"],
                "status": "SUCCEEDED",
                "created_at": active["created_at"],
                "completed_at": completed_at,
                "expected_base": active["task_base_sha"],
                "branch": active["branch"],
                "worktree": active["worktree"],
                "preflight": active["preflight"],
                "prompt_versions": [],
                "provider_attempts": [],
                "request": None,
                "launch": None,
                "stdout": None,
                "stderr": None,
                "exit": None,
                "result": task["result"],
                "source_audit": candidate.source_audit_ref.as_dict(),
                "verification": candidate.verification.manifest_ref.as_dict(),
                "last_error": None,
            }
        )

        def bind(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            if current["pending_integration"] is not None:
                raise ContractError("pending integration was concurrently created")
            current_task = current["tasks"][contract.id]
            current_active = current_task["active_attempt"]
            if (
                current_task["status"] != "READY_TO_INTEGRATE"
                or current_active != active
                or current["repository"]["integration_head"]
                != pending.expected_head
            ):
                raise ContractError(
                    "task or integration head changed before prepare marker"
                )
            attempt_ref = refs[manifest_path].as_dict()
            current_task["attempts"].append(attempt_ref)
            current_task["active_attempt"] = None
            current_task["verification"] = (
                candidate.verification.manifest_ref.as_dict()
            )
            current_task["status"] = "INTEGRATING"
            current["verifications"].append(
                candidate.verification.manifest_ref.as_dict()
            )
            current["pending_integration"] = pending.as_dict()
            current["updated_at"] = completed_at

        self.store.transact(
            state["revision"],
            {manifest_path: manifest_body},
            bind,
        )

    def _complete_state(self, pending: PendingIntegration) -> None:
        state = self.store.load()

        def complete(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            del refs
            if current["pending_integration"] != pending.as_dict():
                raise ContractError(
                    "pending integration changed before completion"
                )
            task = current["tasks"][pending.task_id]
            if task["status"] != "INTEGRATING":
                raise ContractError("integrating Task status changed")
            current["repository"]["integration_head"] = (
                pending.candidate_head
            )
            task["status"] = "COMPLETED"
            task["integrated_commits"] = [pending.candidate_head]
            current["pending_integration"] = None
            current["last_error"] = None
            current["updated_at"] = _now()

        self.store.transact(state["revision"], {}, complete)

    def _verify_pending(
        self,
        state: dict[str, object],
        pending: PendingIntegration,
    ) -> None:
        self._verify_candidate_structure(
            pending.candidate_head,
            pending.expected_head,
        )
        body = self._read_artifact(pending.verification)
        manifest = parse_json_object_bytes(body)
        if (
            manifest.get("commit_sha") != pending.candidate_head
            or manifest.get("passed") is not True
        ):
            raise ContractError(
                "pending verification does not prove candidate commit"
            )
        task = state["tasks"].get(pending.task_id)
        if (
            not isinstance(task, dict)
            or task.get("status") != "INTEGRATING"
            or task.get("verification") != pending.verification.as_dict()
        ):
            raise ContractError("pending integration Task binding is invalid")

    def _verify_source_audit(
        self,
        contract: TaskContract,
        task: dict[str, object],
        active: dict[str, object],
        source_ref: ArtifactRef,
        source_audit: SourceAudit,
    ) -> None:
        body = parse_json_object_bytes(self._read_artifact(source_ref))
        if (
            body.get("candidate_commit") != source_audit.source_head
            or body.get("base_sha") != source_audit.task_base_sha
            or tuple(body.get("source_commits", ()))
            != source_audit.source_commits
            or tuple(body.get("changed_paths", ()))
            != source_audit.changed_paths
            or body.get("gitlinks_changed")
            != source_audit.gitlinks_changed
        ):
            raise IntegrationRejected(
                "state-bound source audit differs from supplied audit"
            )
        if (
            task.get("source_commits") != [source_audit.source_head]
            or active.get("task_base_sha") != source_audit.task_base_sha
            or self.worktrees.resolve_ref(active["branch"])
            != source_audit.source_head
        ):
            raise IntegrationRejected(
                "source commit is not bound to the active task Attempt"
            )
        parents = self._commit_parents(source_audit.source_head)
        if parents != (
            source_audit.source_head,
            source_audit.task_base_sha,
        ):
            raise IntegrationRejected(
                "source commit must be one non-merge commit"
            )
        self._require_paths_in_scope(
            source_audit.changed_paths,
            contract.path_scope,
        )

    def _verify_candidate_structure(
        self,
        candidate: str,
        expected_parent: str,
    ) -> None:
        if self._commit_parents(candidate) != (
            candidate,
            expected_parent,
        ):
            raise IntegrationRejected(
                "candidate must be one non-merge commit"
            )

    def _commit_parents(self, commit: str) -> tuple[str, ...]:
        value = self.worktrees.runner.run_local(
            self.worktrees.target,
            "rev-list",
            "--parents",
            "-n",
            "1",
            commit,
        ).stdout.decode("ascii").strip()
        return tuple(value.split())

    def _changed_paths(
        self,
        worktree: Path,
        base: str,
        candidate: str,
    ) -> tuple[str, ...]:
        raw = self.worktrees.runner.run_local(
            worktree,
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            base,
            candidate,
            "--",
            ".",
        ).stdout
        fields = [item for item in raw.split(b"\0") if item]
        paths: set[str] = set()
        offset = 0
        while offset < len(fields):
            status = fields[offset].decode("ascii")
            offset += 1
            count = 2 if status.startswith(("R", "C")) else 1
            if offset + count > len(fields):
                raise IntegrationRejected("candidate diff is malformed")
            for raw_path in fields[offset : offset + count]:
                path = os.fsdecode(raw_path)
                normalized = PurePosixPath(path).as_posix()
                if (
                    normalized != path
                    or path == ".vibe-coding"
                    or path.startswith(".vibe-coding/")
                ):
                    raise IntegrationRejected(
                        "candidate changed a Controller control path"
                    )
                paths.add(path)
            offset += count
        return tuple(sorted(paths))

    @staticmethod
    def _require_paths_in_scope(
        paths: tuple[str, ...],
        scopes: tuple[str, ...],
    ) -> None:
        normalized = tuple(normalize_scope(scope) for scope in scopes)
        for path in paths:
            if not any(
                path_matches_scope(path, scope)
                for scope in normalized
            ):
                raise IntegrationRejected(
                    f"candidate path is outside task scope: {path}"
                )

    def _source_audit_ref(
        self,
        state: dict[str, object],
        task_id: str,
        attempt_no: int,
    ) -> ArtifactRef:
        expected = (
            f"tasks/{task_id}/attempts/{attempt_no:03d}/"
            "source-audit.json"
        )
        matches = [
            item
            for item in state["artifact_index"]
            if item.get("path") == expected
        ]
        if len(matches) != 1:
            raise IntegrationRejected(
                "state-bound source audit artifact is missing"
            )
        return ArtifactRef(
            path=matches[0]["path"],
            sha256=matches[0]["sha256"],
        )

    def _read_artifact(self, reference: ArtifactRef) -> bytes:
        path = self.store.root.joinpath(
            *PurePosixPath(reference.path).parts
        )
        descriptor = open_absolute_regular_no_follow(path)
        try:
            body = read_bounded(
                descriptor,
                max_bytes=MAX_ARTIFACT_BYTES,
            )
        finally:
            os.close(descriptor)
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if digest != reference.sha256:
            raise ContractError(
                f"artifact digest mismatch: {reference.path}"
            )
        return body

    def _pause_for_ref_movement(
        self,
        state: dict[str, object],
        pending: PendingIntegration,
    ) -> None:
        actual = self.worktrees.resolve_ref(
            state["repository"]["integration_ref"]
        )
        evidence_path = (
            f"recovery/{pending.operation_id}-ref-moved.json"
        )
        evidence_body = canonical_json_bytes(
            {
                "operation_id": pending.operation_id,
                "expected_head": pending.expected_head,
                "candidate_head": pending.candidate_head,
                "actual_head": actual,
            }
        )

        def pause(
            current: dict[str, object],
            refs: Mapping[str, ArtifactRef],
        ) -> None:
            if current["pending_integration"] != pending.as_dict():
                raise ContractError(
                    "pending integration changed before pause"
                )
            current["resume_status"] = current["status"]
            current["status"] = "PAUSED"
            current["last_error"] = {
                "code": "INTEGRATION_REF_MOVED",
                "message": "integration ref moved outside the Controller",
                "retryable": False,
                "evidence": refs[evidence_path].as_dict(),
            }
            current["updated_at"] = _now()

        self.store.transact(
            state["revision"],
            {evidence_path: evidence_body},
            pause,
        )

    @staticmethod
    def _pending_from_state(
        value: dict[str, object],
    ) -> PendingIntegration:
        verification = value["verification"]
        return PendingIntegration(
            operation_id=value["operation_id"],
            task_id=value["task_id"],
            attempt_no=value["attempt_no"],
            expected_head=value["expected_head"],
            candidate_head=value["candidate_head"],
            source_base=value["source_base"],
            source_head=value["source_head"],
            verification=ArtifactRef(
                verification["path"],
                verification["sha256"],
            ),
        )

    def _head(self, worktree: Path) -> str:
        return self.worktrees.runner.run_local(
            worktree,
            "rev-parse",
            "HEAD",
        ).stdout.decode("ascii").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
