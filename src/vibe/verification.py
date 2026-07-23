from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from vibe.config import resolve_command_ids
from vibe.models import (
    ArtifactRef,
    CommandSpec,
    ContractError,
    FrozenRunConfig,
)
from vibe.state_store import StateStore, canonical_json_bytes
from vibe.worktrees import WorktreeManager


VERIFY_RE = re.compile(
    r"VERIFY-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)


class VerificationEnvironmentError(ContractError):
    """An authorized gate could not be started in this environment."""


@dataclass(frozen=True)
class CommandEvidence:
    commit_sha: str
    command_id: str
    argv: tuple[str, ...]
    resolved_executable: str
    cwd: str
    env_names: tuple[str, ...]
    started_at: str
    finished_at: str
    timeout_seconds: int
    timed_out: bool
    exit_code: int | None
    stdout: ArtifactRef
    stderr: ArtifactRef

    def as_dict(self) -> dict[str, object]:
        return {
            "commit_sha": self.commit_sha,
            "command_id": self.command_id,
            "argv": list(self.argv),
            "resolved_executable": self.resolved_executable,
            "cwd": self.cwd,
            "env_names": list(self.env_names),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "timeout_seconds": self.timeout_seconds,
            "timed_out": self.timed_out,
            "exit_code": self.exit_code,
            "stdout": self.stdout.as_dict(),
            "stderr": self.stderr.as_dict(),
        }


@dataclass(frozen=True)
class VerificationResult:
    commit_sha: str
    passed: bool
    commands: tuple[CommandEvidence, ...]
    manifest_ref: ArtifactRef


@dataclass(frozen=True)
class _PreparedCommand:
    spec: CommandSpec
    executable: str
    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]


class VerificationGate:
    def __init__(
        self,
        config: FrozenRunConfig,
        store: StateStore,
        *,
        process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        worktrees: WorktreeManager | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.process_factory = process_factory
        self.worktrees = worktrees or WorktreeManager(store.target)

    def run(
        self,
        commit_sha: str,
        worktree: Path,
        command_ids: Sequence[str],
        artifact_prefix: str,
    ) -> VerificationResult:
        commands = resolve_command_ids(self.config, tuple(command_ids))
        prefix = self._validate_prefix(artifact_prefix)
        resolved_worktree = worktree.resolve()
        prepared = tuple(
            self._prepare_command(command, resolved_worktree)
            for command in commands
        )
        actual_before = self._head(resolved_worktree)
        if actual_before != commit_sha:
            raise ContractError(
                "verification worktree HEAD does not match expected commit"
            )
        before = self.worktrees.capture_read_only_audit(resolved_worktree)
        evidence: list[CommandEvidence] = []
        for index, command in enumerate(prepared, start=1):
            evidence.append(
                self._run_command(
                    commit_sha,
                    prefix,
                    index,
                    command,
                )
            )
        actual_after = self._head(resolved_worktree)
        after = self.worktrees.capture_read_only_audit(resolved_worktree)
        if actual_after != commit_sha or after != before:
            raise ContractError(
                "verification changed commit, product, or protected Git state"
            )
        passed = all(
            not command.timed_out and command.exit_code == 0
            for command in evidence
        )
        body = canonical_json_bytes(
            {
                "schema_version": 1,
                "commit_sha": commit_sha,
                "passed": passed,
                "commands": [
                    command.as_dict() for command in evidence
                ],
            }
        )
        manifest_ref = self.store.prepare_artifact(
            f"{prefix}/manifest.json",
            body,
        )
        return VerificationResult(
            commit_sha,
            passed,
            tuple(evidence),
            manifest_ref,
        )

    def _prepare_command(
        self,
        command: CommandSpec,
        worktree: Path,
    ) -> _PreparedCommand:
        executable = shutil.which(command.argv[0])
        if executable is None:
            raise VerificationEnvironmentError(
                f"required command not found: {command.argv[0]}"
            )
        resolved_executable = str(Path(executable).resolve())
        raw_cwd = PurePosixPath(command.cwd)
        if (
            raw_cwd.is_absolute()
            or ".." in raw_cwd.parts
            or command.cwd != raw_cwd.as_posix()
        ):
            raise VerificationEnvironmentError(
                f"configured command cwd is invalid: {command.cwd}"
            )
        resolved_cwd = worktree.joinpath(*raw_cwd.parts).resolve()
        try:
            resolved_cwd.relative_to(worktree)
        except ValueError as error:
            raise VerificationEnvironmentError(
                f"configured command cwd escapes worktree: {command.cwd}"
            ) from error
        if not resolved_cwd.is_dir():
            raise VerificationEnvironmentError(
                f"configured command cwd is not a directory: {command.cwd}"
            )
        environment = {
            name: os.environ[name]
            for name in command.env_allowlist
            if name in os.environ
        }
        return _PreparedCommand(
            command,
            resolved_executable,
            (resolved_executable, *command.argv[1:]),
            resolved_cwd,
            environment,
        )

    def _run_command(
        self,
        commit_sha: str,
        prefix: str,
        index: int,
        command: _PreparedCommand,
    ) -> CommandEvidence:
        started_at = _now()
        try:
            process = self.process_factory(
                command.argv,
                cwd=command.cwd,
                env=command.environment,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise VerificationEnvironmentError(
                f"cannot start required command {command.spec.id}: {error}"
            ) from error
        try:
            stdout, stderr = process.communicate(
                timeout=command.spec.timeout_seconds,
            )
            timed_out = False
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            self._terminate_group(process)
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                self._kill_group(process)
                stdout, stderr = process.communicate()
            timed_out = True
            exit_code = None
        finished_at = _now()
        stdout_ref = self.store.prepare_artifact(
            f"{prefix}/command-{index:03d}.stdout",
            stdout,
        )
        stderr_ref = self.store.prepare_artifact(
            f"{prefix}/command-{index:03d}.stderr",
            stderr,
        )
        return CommandEvidence(
            commit_sha=commit_sha,
            command_id=command.spec.id,
            argv=command.spec.argv,
            resolved_executable=command.executable,
            cwd=command.spec.cwd,
            env_names=tuple(
                name
                for name in command.spec.env_allowlist
                if name in command.environment
            ),
            started_at=started_at,
            finished_at=finished_at,
            timeout_seconds=command.spec.timeout_seconds,
            timed_out=timed_out,
            exit_code=exit_code,
            stdout=stdout_ref,
            stderr=stderr_ref,
        )

    @staticmethod
    def _terminate_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    @staticmethod
    def _kill_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _head(self, worktree: Path) -> str:
        return self.worktrees.runner.run_local(
            worktree,
            "rev-parse",
            "HEAD",
        ).stdout.decode("ascii").strip()

    def _validate_prefix(self, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or value != path.as_posix()
            or ".." in path.parts
            or "." in path.parts
        ):
            raise ContractError("verification artifact prefix is invalid")
        parts = path.parts
        valid_shape = (
            len(parts) == 3
            and parts[:2]
            in {
                ("verification", "global"),
                ("verification", "supplemental"),
            }
        ) or (
            len(parts) == 4
            and parts[:2] == ("verification", "tasks")
            and re.fullmatch(r"TASK-\d{3}-a[1-9]\d*", parts[2])
            is not None
        )
        if not valid_shape or VERIFY_RE.fullmatch(parts[-1]) is None:
            raise ContractError("verification artifact prefix is invalid")
        parsed = uuid.UUID(parts[-1][len("VERIFY-") :])
        if parsed.version != 4:
            raise ContractError("verification operation ID must be UUIDv4")
        destination = self.store.root.joinpath(*parts)
        if destination.exists():
            raise ContractError("verification operation prefix was already used")
        return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
