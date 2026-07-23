from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vibe.models import ContractError


_REMOTE_COMMANDS = {
    "clone",
    "fetch",
    "ls-remote",
    "pull",
    "push",
    "send-email",
    "submodule--helper",
}
_LOCAL_COMMANDS = {
    "add",
    "cat-file",
    "check-ignore",
    "cherry-pick",
    "commit-tree",
    "config",
    "diff",
    "for-each-ref",
    "hash-object",
    "ls-files",
    "ls-tree",
    "merge-base",
    "read-tree",
    "remote",
    "rev-list",
    "rev-parse",
    "show",
    "show-ref",
    "status",
    "symbolic-ref",
    "update-ref",
    "worktree",
    "write-tree",
}
_INDEX_COMMANDS = {"add", "diff", "ls-files", "read-tree", "write-tree"}
_FIXED_PREFIX = (
    "--no-pager",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "commit.gpgSign=false",
    "-c",
    "tag.gpgSign=false",
    "-c",
    "credential.helper=",
)


@dataclass(frozen=True)
class GitInternalOptions:
    index_file: Path | None = None
    commit_timestamp: str | None = None


class GitRunner:
    """The only production boundary allowed to execute Git."""

    def __init__(
        self,
        repository_root: Path,
        *,
        git_binary: str | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        resolved = shutil.which(git_binary or "git")
        if resolved is None:
            raise ContractError("git executable is unavailable")
        self.git_binary = str(Path(resolved).resolve())
        self.index_root = (
            self.repository_root / ".vibe-coding" / "tmp" / "indexes"
        )
        self._environment = {
            "PATH": os.defpath,
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_PAGER": "cat",
        }

    def run_local(
        self,
        cwd: Path,
        *argv: str,
        internal: GitInternalOptions = GitInternalOptions(),
    ) -> subprocess.CompletedProcess[bytes]:
        if not argv:
            raise ContractError("git subcommand is required")
        command = argv[0]
        if command.startswith("-") or command == "-c" or "-c" in argv:
            raise ContractError("caller-supplied Git global options are rejected")
        if command in _REMOTE_COMMANDS:
            raise ContractError(f"remote-capable Git command is rejected: {command}")
        if command not in _LOCAL_COMMANDS:
            raise ContractError(f"Git subcommand is not allowlisted: {command}")
        self._validate_local_shape(command, argv[1:])
        environment = dict(self._environment)
        self._apply_internal_options(command, internal, environment)
        result = subprocess.run(
            [self.git_binary, *_FIXED_PREFIX, "-C", str(cwd.resolve()), *argv],
            check=False,
            capture_output=True,
            env=environment,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ContractError(
                f"git {command} failed: {detail or 'unknown error'}"
            )
        return result

    def assert_no_executable_integrations(self, cwd: Path) -> None:
        result = self.run_local(cwd, "config", "--local", "--null", "--list")
        forbidden: list[str] = []
        for entry in result.stdout.split(b"\0"):
            if not entry:
                continue
            raw_key, _, raw_value = entry.partition(b"\n")
            key = raw_key.decode("utf-8", errors="surrogateescape").lower()
            value = raw_value.decode("utf-8", errors="surrogateescape").strip()
            if key.startswith(("alias.", "include.", "includeif.")):
                forbidden.append(key)
            elif key.startswith("filter.") and key.rsplit(".", 1)[-1] in {
                "clean",
                "smudge",
                "process",
            }:
                forbidden.append(key)
            elif key.startswith("merge.") and key.endswith(".driver"):
                forbidden.append(key)
            elif key.startswith("diff.") and key.rsplit(".", 1)[-1] in {
                "command",
                "textconv",
            }:
                forbidden.append(key)
            elif key == "core.fsmonitor" and value.lower() not in {
                "",
                "false",
                "no",
                "0",
            }:
                forbidden.append(key)
        if forbidden:
            raise ContractError(
                "repository config enables executable Git integration: "
                + ", ".join(sorted(forbidden))
            )

    def _validate_local_shape(
        self,
        command: str,
        arguments: tuple[str, ...],
    ) -> None:
        if command == "remote":
            if not arguments or arguments == ("-v",):
                return
            if arguments[:1] == ("get-url",) and len(arguments) in {2, 3}:
                return
            raise ContractError("mutating or contacting Git remote is rejected")
        if command == "config":
            allowed = {
                ("--local", "--null", "--list"),
                ("--local", "--null", "--get-regexp", r"^remote\..*\.url$"),
            }
            if arguments not in allowed:
                raise ContractError("only read-only local Git config queries are allowed")
        if command == "worktree" and arguments[:1] not in {
            ("add",),
            ("list",),
            ("remove",),
            ("prune",),
        }:
            raise ContractError("Git worktree operation is not allowlisted")

    def _apply_internal_options(
        self,
        command: str,
        internal: GitInternalOptions,
        environment: dict[str, str],
    ) -> None:
        if internal.index_file is not None:
            if command not in _INDEX_COMMANDS:
                raise ContractError(
                    "temporary Git index is invalid for this subcommand"
                )
            environment["GIT_INDEX_FILE"] = str(
                self._validate_index_file(internal.index_file)
            )
        if internal.commit_timestamp is not None:
            if command != "commit-tree":
                raise ContractError(
                    "commit timestamp is valid only for commit-tree"
                )
            timestamp = internal.commit_timestamp
            try:
                parsed = datetime.fromisoformat(timestamp)
            except ValueError as error:
                raise ContractError("commit timestamp must be RFC3339") from error
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ContractError("commit timestamp must include a timezone")
            environment.update(
                {
                    "GIT_AUTHOR_NAME": "Vibe Controller",
                    "GIT_AUTHOR_EMAIL": "vibe-controller@localhost",
                    "GIT_AUTHOR_DATE": timestamp,
                    "GIT_COMMITTER_NAME": "Vibe Controller",
                    "GIT_COMMITTER_EMAIL": "vibe-controller@localhost",
                    "GIT_COMMITTER_DATE": timestamp,
                }
            )
        elif command == "commit-tree":
            raise ContractError("commit-tree requires a frozen commit timestamp")

    def _validate_index_file(self, index_file: Path) -> Path:
        candidate = index_file.absolute()
        root = self.index_root.absolute()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ContractError(
                "temporary Git index must be below the Controller index root"
            ) from error
        current = self.repository_root
        relative_parts = candidate.relative_to(self.repository_root).parts
        for part in relative_parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise ContractError(
                    "temporary Git index ancestors must not be symbolic links"
                )
        return candidate
