from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from vibe.git_runner import GitInternalOptions, GitRunner
from vibe.models import ContractError, OID_RE, TaskContract


PRODUCT_PATHSPEC = (
    "--",
    ".",
    ":(exclude).vibe-coding",
    ":(exclude).vibe-coding/**",
)
ZERO_OID = "0" * 40


@dataclass(frozen=True)
class RepositorySnapshot:
    revision: str
    workspace_fingerprint: str

    def as_dict(self) -> dict[str, str]:
        return {
            "revision": self.revision,
            "workspace_fingerprint": self.workspace_fingerprint,
        }


@dataclass(frozen=True)
class RepositoryBaseline:
    identity: str
    base_ref: str
    base_sha: str


@dataclass(frozen=True)
class TaskWorktree:
    path: Path
    branch: str
    base_sha: str


@dataclass(frozen=True)
class ProtectedGitSnapshot:
    user_head: str
    index_tree: str
    status_digest: str
    refs: tuple[tuple[str, str], ...]
    packed_refs_digest: str
    config_digest: str
    remote_urls: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class AttemptPreflight:
    role: str
    task_id: str | None
    attempt_created_at: str
    expected_base: str
    branch: str | None
    worktree: str
    snapshot: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "role": self.role,
            "task_id": self.task_id,
            "attempt_created_at": self.attempt_created_at,
            "expected_base": self.expected_base,
            "branch": self.branch,
            "worktree": self.worktree,
            "snapshot": self.snapshot,
        }


@dataclass(frozen=True)
class SourceCommitMetadata:
    run_id: str
    task_id: str
    attempt_no: int
    attempt_created_at: str

    @property
    def message(self) -> str:
        return (
            f"vibe({self.run_id}): {self.task_id} "
            f"attempt {self.attempt_no}"
        )


@dataclass(frozen=True)
class SourceAudit:
    task_base_sha: str
    source_head: str
    source_commits: tuple[str, ...]
    changed_paths: tuple[str, ...]
    gitlinks_changed: bool
    protected_before: ProtectedGitSnapshot
    protected_after: ProtectedGitSnapshot


@dataclass(frozen=True)
class PreparedSourceCommit:
    tree_oid: str
    candidate_commit: str
    source_audit: SourceAudit
    source_audit_body: bytes


def _git_bytes(
    runner: GitRunner,
    target: Path,
    *arguments: str,
    internal: GitInternalOptions = GitInternalOptions(),
) -> bytes:
    return runner.run_local(target, *arguments, internal=internal).stdout


def _git_text(
    runner: GitRunner,
    target: Path,
    *arguments: str,
    internal: GitInternalOptions = GitInternalOptions(),
) -> str:
    return _git_bytes(
        runner,
        target,
        *arguments,
        internal=internal,
    ).decode("utf-8", errors="strict").strip()


def _revision(runner: GitRunner, target: Path) -> str:
    revision = _git_text(runner, target, "rev-parse", "HEAD")
    if OID_RE.fullmatch(revision) is None:
        raise ContractError("HEAD must resolve to a canonical full commit OID")
    return revision


def _resolve_revision(
    runner: GitRunner,
    target: Path,
    revision: str,
) -> str:
    resolved = _git_text(
        runner,
        target,
        "rev-parse",
        "--verify",
        f"{revision}^{{commit}}",
    )
    if OID_RE.fullmatch(resolved) is None:
        raise ContractError("revision must resolve to a canonical commit OID")
    return resolved


def _hash_part(digest: Any, label: bytes, value: bytes) -> None:
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _untracked_fingerprint_stream(
    runner: GitRunner,
    target: Path,
) -> bytes:
    output = _git_bytes(
        runner,
        target,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        *PRODUCT_PATHSPEC,
    )
    paths = sorted(path for path in output.split(b"\0") if path)
    digest = hashlib.sha256()
    for raw_path in paths:
        path = target / os.fsdecode(raw_path)
        try:
            metadata = path.lstat()
        except OSError as error:
            raise ContractError(
                f"cannot inspect untracked path {os.fsdecode(raw_path)!r}: {error}"
            ) from error
        mode = f"{stat.S_IFMT(metadata.st_mode):o}:{metadata.st_mode & 0o111:o}".encode(
            "ascii"
        )
        if stat.S_ISLNK(metadata.st_mode):
            content = os.fsencode(os.readlink(path))
            kind = b"symlink"
        elif stat.S_ISREG(metadata.st_mode):
            try:
                content = path.read_bytes()
            except OSError as error:
                raise ContractError(
                    f"cannot read untracked path {os.fsdecode(raw_path)!r}: {error}"
                ) from error
            kind = b"file"
        else:
            content = b""
            kind = b"special"
        _hash_part(digest, b"path", raw_path)
        _hash_part(digest, b"kind", kind)
        _hash_part(digest, b"mode", mode)
        _hash_part(digest, b"content", content)
    return digest.digest()


def _index_modes(
    runner: GitRunner,
    target: Path,
) -> dict[bytes, set[bytes]]:
    output = _git_bytes(
        runner,
        target,
        "ls-files",
        "--stage",
        "-z",
        *PRODUCT_PATHSPEC,
    )
    modes: dict[bytes, set[bytes]] = {}
    for entry in output.split(b"\0"):
        if not entry:
            continue
        header, separator, raw_path = entry.partition(b"\t")
        fields = header.split()
        if separator and len(fields) == 3:
            modes.setdefault(raw_path, set()).add(fields[0])
    return modes


def _tracked_worktree_fingerprint_stream(
    target: Path,
    index_modes: dict[bytes, set[bytes]],
) -> bytes:
    digest = hashlib.sha256()
    for raw_path in sorted(index_modes):
        if b"160000" in index_modes[raw_path]:
            continue
        display_path = os.fsdecode(raw_path)
        path = target / display_path
        _hash_part(digest, b"path", raw_path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            _hash_part(digest, b"state", b"absent")
            continue
        except OSError as error:
            raise ContractError(
                f"cannot inspect tracked path {display_path!r}: {error}"
            ) from error
        mode = (
            f"{stat.S_IFMT(metadata.st_mode):o}:"
            f"{stat.S_IMODE(metadata.st_mode):o}"
        ).encode("ascii")
        if stat.S_ISLNK(metadata.st_mode):
            content = os.fsencode(os.readlink(path))
            kind = b"symlink"
        elif stat.S_ISREG(metadata.st_mode):
            try:
                content = path.read_bytes()
            except OSError as error:
                raise ContractError(
                    f"cannot read tracked path {display_path!r}: {error}"
                ) from error
            kind = b"file"
        elif stat.S_ISDIR(metadata.st_mode):
            content = b""
            kind = b"directory"
        else:
            content = b""
            kind = b"special"
        _hash_part(digest, b"state", b"present")
        _hash_part(digest, b"kind", kind)
        _hash_part(digest, b"mode", mode)
        _hash_part(digest, b"content", content)
    return digest.digest()


def _submodule_fingerprint_stream(
    runner: GitRunner,
    target: Path,
    ancestry: frozenset[Path],
    index_modes: dict[bytes, set[bytes]],
) -> bytes:
    submodules = [
        raw_path
        for raw_path, modes in index_modes.items()
        if b"160000" in modes
    ]
    digest = hashlib.sha256()
    for raw_path in sorted(submodules):
        display_path = os.fsdecode(raw_path)
        path = target / display_path
        _hash_part(digest, b"path", raw_path)
        if path.is_symlink():
            raise ContractError(
                f"submodule worktree must not be a symbolic link: {display_path}"
            )
        if not path.exists():
            _hash_part(digest, b"state", b"absent")
            continue
        if not path.is_dir():
            raise ContractError(
                f"submodule worktree must be a directory: {display_path}"
            )
        try:
            root = Path(
                _git_text(runner, path, "rev-parse", "--show-toplevel")
            ).resolve()
        except ContractError:
            root = None
        resolved_path = path.resolve()
        if root != resolved_path:
            try:
                has_content = any(path.iterdir())
            except OSError as error:
                raise ContractError(
                    f"cannot inspect submodule worktree {display_path}: {error}"
                ) from error
            if has_content:
                raise ContractError(
                    "uninitialized submodule worktree must be empty: "
                    f"{display_path}"
                )
            _hash_part(digest, b"state", b"uninitialized")
            continue
        nested = _repository_snapshot(runner, resolved_path, ancestry)
        _hash_part(digest, b"state", b"initialized")
        _hash_part(digest, b"revision", nested.revision.encode("ascii"))
        _hash_part(
            digest,
            b"workspace-fingerprint",
            nested.workspace_fingerprint.encode("ascii"),
        )
    return digest.digest()


def _repository_snapshot(
    runner: GitRunner,
    target: Path,
    ancestry: frozenset[Path] | None = None,
) -> RepositorySnapshot:
    target = target.resolve()
    ancestors = ancestry or frozenset()
    if target in ancestors:
        raise ContractError(f"recursive submodule worktree detected: {target}")
    descendants = ancestors | {target}
    status = _git_bytes(
        runner,
        target,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
        *PRODUCT_PATHSPEC,
    )
    staged = _git_bytes(
        runner,
        target,
        "diff",
        "--cached",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--ignore-submodules=none",
        *PRODUCT_PATHSPEC,
    )
    unstaged = _git_bytes(
        runner,
        target,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--ignore-submodules=none",
        *PRODUCT_PATHSPEC,
    )
    untracked = _untracked_fingerprint_stream(runner, target)
    index_modes = _index_modes(runner, target)
    tracked_worktree = _tracked_worktree_fingerprint_stream(target, index_modes)
    submodules = _submodule_fingerprint_stream(
        runner,
        target,
        descendants,
        index_modes,
    )
    digest = hashlib.sha256()
    _hash_part(digest, b"status-v2", status)
    _hash_part(digest, b"staged-diff", staged)
    _hash_part(digest, b"unstaged-diff", unstaged)
    _hash_part(digest, b"untracked", untracked)
    _hash_part(digest, b"tracked-worktree", tracked_worktree)
    _hash_part(digest, b"submodules", submodules)
    return RepositorySnapshot(
        revision=_revision(runner, target),
        workspace_fingerprint="sha256:" + digest.hexdigest(),
    )


def repository_snapshot(target: Path) -> RepositorySnapshot:
    resolved = target.resolve()
    return _repository_snapshot(GitRunner(resolved), resolved)


def canonical_repository_identity(
    target: Path,
    *,
    runner: GitRunner | None = None,
) -> str:
    target = target.resolve()
    active_runner = runner or GitRunner(target)
    root = Path(
        _git_text(active_runner, target, "rev-parse", "--show-toplevel")
    ).resolve()
    common = Path(
        _git_text(
            active_runner,
            target,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
    ).resolve()
    try:
        root_bytes = str(root).encode("utf-8")
        common_bytes = str(common).encode("utf-8")
    except UnicodeEncodeError as error:
        raise ContractError(
            "repository paths must be representable as UTF-8"
        ) from error
    return "sha256:" + hashlib.sha256(
        root_bytes + b"\0" + common_bytes
    ).hexdigest()


class WorktreeManager:
    def __init__(
        self,
        target: Path,
        *,
        runner: GitRunner | None = None,
    ) -> None:
        self.target = target.resolve()
        self.runner = runner or GitRunner(self.target)
        actual_root = Path(
            _git_text(self.runner, self.target, "rev-parse", "--show-toplevel")
        ).resolve()
        if actual_root != self.target:
            raise ContractError(f"target must be the Git root: {actual_root}")
        self.identity = canonical_repository_identity(
            self.target,
            runner=self.runner,
        )
        self.control_root = self.target / ".vibe-coding"
        self.worktree_root = self.control_root / "worktrees"

    def assert_clean_baseline(self) -> RepositoryBaseline:
        snapshot = _repository_snapshot(self.runner, self.target)
        status = _git_bytes(
            self.runner,
            self.target,
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
            *PRODUCT_PATHSPEC,
        )
        if status:
            raise ContractError("run requires a clean product baseline")
        base_sha = _resolve_revision(self.runner, self.target, "HEAD")
        try:
            base_ref = _git_text(
                self.runner,
                self.target,
                "symbolic-ref",
                "-q",
                "HEAD",
            )
        except ContractError:
            base_ref = "HEAD"
        if snapshot.revision != base_sha:
            raise ContractError("repository revision changed during baseline capture")
        return RepositoryBaseline(self.identity, base_ref, base_sha)

    def create_run_ref(self, run_id: str, base_sha: str) -> str:
        self._validate_id(run_id, "run_id")
        base = _resolve_revision(self.runner, self.target, base_sha)
        ref = f"refs/heads/vibe/run-{run_id}"
        self._create_ref(ref, base)
        return ref

    def create_task_worktree(
        self,
        run_id: str,
        task_id: str,
        attempt_no: int,
        base_sha: str,
    ) -> TaskWorktree:
        self._validate_id(run_id, "run_id")
        self._validate_id(task_id, "task_id")
        if type(attempt_no) is not int or attempt_no < 1:
            raise ContractError("attempt_no must be a positive integer")
        base = _resolve_revision(self.runner, self.target, base_sha)
        branch = f"refs/heads/vibe/{run_id}/{task_id}-a{attempt_no}"
        self._create_ref(branch, base)
        run_root = self.worktree_root / run_id
        path = run_root / f"{task_id}-a{attempt_no}"
        self._require_below(path, run_root)
        if path.exists():
            if _revision(self.runner, path) != base:
                raise ContractError("existing task worktree has the wrong base")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.runner.run_local(
                self.target,
                "worktree",
                "add",
                "--detach",
                str(path),
                base,
            )
        return TaskWorktree(path.resolve(), branch, base)

    def create_disposable_worktree(
        self,
        run_id: str,
        category: str,
        operation_id: str,
        base_sha: str,
    ) -> Path:
        self._validate_id(run_id, "run_id")
        self._validate_id(operation_id, "operation_id")
        if category not in {"candidates", "read-only"}:
            raise ContractError("disposable worktree category is invalid")
        base = _resolve_revision(self.runner, self.target, base_sha)
        run_root = self.worktree_root / run_id
        path = run_root / category / operation_id
        self._require_below(path, run_root)
        if path.exists():
            if _revision(self.runner, path) != base:
                raise ContractError(
                    "existing disposable worktree has the wrong base"
                )
        else:
            self._ensure_safe_directory(path.parent)
            self.runner.run_local(
                self.target,
                "worktree",
                "add",
                "--detach",
                str(path),
                base,
            )
        return path.resolve()

    def resolve_ref(self, ref: str) -> str:
        return _resolve_revision(self.runner, self.target, ref)

    def update_ref(
        self,
        ref: str,
        new_oid: str,
        expected_oid: str,
    ) -> None:
        new = _resolve_revision(self.runner, self.target, new_oid)
        expected = _resolve_revision(
            self.runner,
            self.target,
            expected_oid,
        )
        self.runner.run_local(
            self.target,
            "update-ref",
            ref,
            new,
            expected,
        )

    def snapshot_protected_git(
        self,
        excluded_ref: str | None = None,
    ) -> ProtectedGitSnapshot:
        head_oid = _resolve_revision(self.runner, self.target, "HEAD")
        try:
            selector = _git_text(
                self.runner,
                self.target,
                "symbolic-ref",
                "-q",
                "HEAD",
            )
        except ContractError:
            selector = "HEAD"
        index_tree = _git_text(self.runner, self.target, "write-tree")
        status = _git_bytes(
            self.runner,
            self.target,
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
            *PRODUCT_PATHSPEC,
        )
        refs_raw = _git_bytes(
            self.runner,
            self.target,
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00",
        )
        fields = [item.decode("utf-8") for item in refs_raw.split(b"\0") if item]
        refs: list[tuple[str, str]] = []
        for offset in range(0, len(fields) - 1, 2):
            ref = fields[offset].lstrip("\n")
            oid = fields[offset + 1].lstrip("\n")
            if ref and ref != excluded_ref:
                refs.append((ref, oid))
        common_dir = self._common_git_dir()
        packed_refs = self._read_optional(common_dir / "packed-refs")
        config = _git_bytes(
            self.runner,
            self.target,
            "config",
            "--local",
            "--null",
            "--list",
        )
        remote_urls = tuple(sorted(self._remote_urls(config)))
        return ProtectedGitSnapshot(
            user_head=f"{selector}@{head_oid}",
            index_tree=index_tree,
            status_digest="sha256:" + hashlib.sha256(status).hexdigest(),
            refs=tuple(sorted(refs)),
            packed_refs_digest="sha256:" + hashlib.sha256(packed_refs).hexdigest(),
            config_digest="sha256:" + hashlib.sha256(config).hexdigest(),
            remote_urls=remote_urls,
        )

    def capture_worker_preflight(
        self,
        task: TaskWorktree,
        task_id: str,
        attempt_created_at: str,
        protected: ProtectedGitSnapshot,
    ) -> AttemptPreflight:
        if task_id not in task.branch:
            raise ContractError("task identity does not match reserved branch")
        snapshot = {
            "protected_git": self._protected_as_dict(protected),
            "worktree": _repository_snapshot(
                self.runner,
                task.path,
            ).as_dict(),
        }
        return AttemptPreflight(
            role="worker",
            task_id=task_id,
            attempt_created_at=attempt_created_at,
            expected_base=task.base_sha,
            branch=task.branch,
            worktree=str(task.path),
            snapshot=snapshot,
        )

    def prepare_source_commit(
        self,
        contract: TaskContract,
        worktree: TaskWorktree,
        preflight: AttemptPreflight,
        metadata: SourceCommitMetadata,
    ) -> PreparedSourceCommit:
        self._validate_preparation_identity(contract, worktree, preflight, metadata)
        self.runner.assert_no_executable_integrations(self.target)
        if _git_text(
            self.runner,
            worktree.path,
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
        ) != "HEAD":
            raise ContractError("Worker worktree HEAD must remain detached")
        if _revision(self.runner, worktree.path) != worktree.base_sha:
            raise ContractError("Worker worktree HEAD changed after dispatch")
        if _resolve_revision(
            self.runner,
            self.target,
            worktree.branch,
        ) != worktree.base_sha:
            raise ContractError("reserved task ref changed before source CAS")
        base_tree = _git_text(
            self.runner,
            self.target,
            "rev-parse",
            f"{worktree.base_sha}^{{tree}}",
        )
        if _git_text(self.runner, worktree.path, "write-tree") != base_tree:
            raise ContractError("Worker must not stage changes")
        protected_before = self._protected_from_preflight(preflight)
        protected_after = self.snapshot_protected_git(worktree.branch)
        if protected_after != protected_before:
            raise ContractError("Worker changed protected Git state")
        changed_paths, gitlinks_changed = self._audit_raw_delta(worktree.path)
        self._validate_scope(contract.path_scope, changed_paths, worktree.path)

        operation = (
            f"{metadata.run_id}-{metadata.task_id}-a{metadata.attempt_no}"
        )
        index = self.runner.index_root / operation / "index"
        self._ensure_safe_directory(index.parent)
        if index.exists():
            index.unlink()
        internal = GitInternalOptions(index_file=index)
        self.runner.run_local(
            worktree.path,
            "read-tree",
            worktree.base_sha,
            internal=internal,
        )
        literal_paths = tuple(f":(literal){path}" for path in changed_paths)
        self.runner.run_local(
            worktree.path,
            "add",
            "-A",
            "--",
            *literal_paths,
            internal=internal,
        )
        staged_paths, staged_gitlinks = self._cached_delta(
            worktree.path,
            worktree.base_sha,
            internal,
        )
        if staged_paths != changed_paths or staged_gitlinks != gitlinks_changed:
            raise ContractError("temporary index delta differs from audited raw delta")
        tree_oid = _git_text(
            self.runner,
            worktree.path,
            "write-tree",
            internal=internal,
        )
        candidate = _git_text(
            self.runner,
            worktree.path,
            "commit-tree",
            tree_oid,
            "-p",
            worktree.base_sha,
            "-m",
            metadata.message,
            internal=GitInternalOptions(
                commit_timestamp=preflight.attempt_created_at
            ),
        )
        self._verify_source_commit(
            candidate,
            tree_oid,
            worktree.base_sha,
            metadata,
        )
        source_audit = SourceAudit(
            task_base_sha=worktree.base_sha,
            source_head=candidate,
            source_commits=(candidate,),
            changed_paths=changed_paths,
            gitlinks_changed=gitlinks_changed,
            protected_before=protected_before,
            protected_after=protected_after,
        )
        body = self._source_audit_body(
            contract,
            metadata,
            tree_oid,
            candidate,
            source_audit,
        )
        return PreparedSourceCommit(tree_oid, candidate, source_audit, body)

    def apply_source_commit_cas(
        self,
        worktree: TaskWorktree,
        prepared: PreparedSourceCommit,
    ) -> None:
        self._verify_prepared(worktree, prepared)
        self.runner.run_local(
            self.target,
            "update-ref",
            worktree.branch,
            prepared.candidate_commit,
            worktree.base_sha,
        )

    def classify_source_cas(
        self,
        worktree: TaskWorktree,
        prepared: PreparedSourceCommit,
    ) -> str:
        current = _resolve_revision(
            self.runner,
            self.target,
            worktree.branch,
        )
        if current == worktree.base_sha:
            return "RETRY_CAS"
        if current == prepared.candidate_commit:
            self._verify_prepared(worktree, prepared)
            return "COMPLETE_STATE"
        return "PAUSE"

    def capture_read_only_audit(self, worktree: Path) -> dict[str, object]:
        return {
            "protected_git": self._protected_as_dict(
                self.snapshot_protected_git()
            ),
            "worktree": _repository_snapshot(
                self.runner,
                worktree.resolve(),
            ).as_dict(),
        }

    def _audit_raw_delta(
        self,
        worktree: Path,
    ) -> tuple[tuple[str, ...], bool]:
        raw = _git_bytes(
            self.runner,
            worktree,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
            "--",
            ".",
        )
        paths: set[str] = set()
        entries = raw.split(b"\0")
        offset = 0
        while offset < len(entries):
            entry = entries[offset]
            offset += 1
            if not entry:
                continue
            if len(entry) < 4 or entry[2:3] != b" ":
                raise ContractError("cannot parse Worker Git status")
            state = entry[:2].decode("ascii")
            if "U" in state or state in {"AA", "DD"}:
                raise ContractError("Worker delta contains unresolved conflicts")
            if state[0] not in {" ", "?"}:
                raise ContractError("Worker must not stage changes")
            path = os.fsdecode(entry[3:])
            paths.add(self._normalize_product_path(path))
            if state[1] in {"R", "C"}:
                if offset >= len(entries) or not entries[offset]:
                    raise ContractError("rename status is incomplete")
                paths.add(self._normalize_product_path(os.fsdecode(entries[offset])))
                offset += 1
        if not paths:
            raise ContractError("Worker produced no product changes")
        for path in paths:
            if self._is_control_path(path):
                raise ContractError("Worker changed a Controller control path")
        modes = _index_modes(self.runner, worktree)
        gitlinks = any(
            path.encode("utf-8") in modes
            and b"160000" in modes[path.encode("utf-8")]
            for path in paths
        ) or ".gitmodules" in paths
        return tuple(sorted(paths)), gitlinks

    def _cached_delta(
        self,
        worktree: Path,
        base_sha: str,
        internal: GitInternalOptions,
    ) -> tuple[tuple[str, ...], bool]:
        raw = _git_bytes(
            self.runner,
            worktree,
            "diff",
            "--cached",
            "--name-status",
            "-z",
            "--find-renames",
            base_sha,
            "--",
            ".",
            internal=internal,
        )
        fields = [field for field in raw.split(b"\0") if field]
        paths: set[str] = set()
        offset = 0
        while offset < len(fields):
            state = fields[offset].decode("ascii")
            offset += 1
            count = 2 if state.startswith(("R", "C")) else 1
            if offset + count > len(fields):
                raise ContractError("cannot parse temporary index diff")
            for raw_path in fields[offset : offset + count]:
                paths.add(self._normalize_product_path(os.fsdecode(raw_path)))
            offset += count
        modes = self._index_modes_from_internal(worktree, internal)
        gitlinks = any(
            path.encode("utf-8") in modes
            and b"160000" in modes[path.encode("utf-8")]
            for path in paths
        ) or ".gitmodules" in paths
        return tuple(sorted(paths)), gitlinks

    def _index_modes_from_internal(
        self,
        worktree: Path,
        internal: GitInternalOptions,
    ) -> dict[bytes, set[bytes]]:
        output = _git_bytes(
            self.runner,
            worktree,
            "ls-files",
            "--stage",
            "-z",
            "--",
            ".",
            internal=internal,
        )
        modes: dict[bytes, set[bytes]] = {}
        for entry in output.split(b"\0"):
            if not entry:
                continue
            header, separator, raw_path = entry.partition(b"\t")
            fields = header.split()
            if separator and len(fields) == 3:
                modes.setdefault(raw_path, set()).add(fields[0])
        return modes

    def _validate_scope(
        self,
        scopes: tuple[str, ...],
        changed_paths: tuple[str, ...],
        worktree: Path,
    ) -> None:
        if not scopes:
            raise ContractError("task path scope must not be empty")
        normalized_scopes: list[str] = []
        for scope in scopes:
            if scope == ".":
                if len(scopes) != 1:
                    raise ContractError("scope '.' must be exclusive")
                normalized_scopes.append(scope)
                continue
            normalized = self._normalize_product_path(scope.rstrip("/"))
            normalized_scopes.append(
                normalized + "/" if scope.endswith("/") else normalized
            )
            self._assert_safe_ancestors(worktree, normalized)
        for path in changed_paths:
            self._assert_safe_ancestors(worktree, path)
            if not any(self._scope_contains(scope, path) for scope in normalized_scopes):
                raise ContractError(f"Worker changed path outside path scope: {path}")

    @staticmethod
    def _scope_contains(scope: str, path: str) -> bool:
        return scope == "." or path == scope or (
            scope.endswith("/") and path.startswith(scope)
        )

    def _assert_safe_ancestors(self, worktree: Path, path: str) -> None:
        current = worktree
        parts = PurePosixPath(path).parts
        for part in parts[:-1] if len(parts) > 1 else parts:
            current = current / part
            if current.is_symlink():
                raise ContractError(
                    f"path scope has a symbolic link ancestor: {path}"
                )
            if current.exists():
                try:
                    current.resolve().relative_to(worktree.resolve())
                except ValueError as error:
                    raise ContractError(
                        f"path scope resolves outside product worktree: {path}"
                    ) from error

    def _verify_source_commit(
        self,
        candidate: str,
        tree_oid: str,
        base_sha: str,
        metadata: SourceCommitMetadata,
    ) -> None:
        commit_tree = _git_text(
            self.runner,
            self.target,
            "rev-parse",
            f"{candidate}^{{tree}}",
        )
        parents = _git_text(
            self.runner,
            self.target,
            "rev-list",
            "--parents",
            "-n",
            "1",
            candidate,
        ).split()
        if commit_tree != tree_oid or parents != [candidate, base_sha]:
            raise ContractError("Controller source commit structure is invalid")
        body = _git_text(
            self.runner,
            self.target,
            "show",
            "-s",
            "--format=%an%x00%ae%x00%cn%x00%ce%x00%B",
            candidate,
        ).split("\0")
        if body[:4] != [
            "Vibe Controller",
            "vibe-controller@localhost",
            "Vibe Controller",
            "vibe-controller@localhost",
        ] or body[4].strip() != metadata.message:
            raise ContractError("Controller source commit metadata is invalid")

    def _verify_prepared(
        self,
        worktree: TaskWorktree,
        prepared: PreparedSourceCommit,
    ) -> None:
        audit = prepared.source_audit
        if (
            audit.task_base_sha != worktree.base_sha
            or audit.source_head != prepared.candidate_commit
            or audit.source_commits != (prepared.candidate_commit,)
        ):
            raise ContractError("prepared source audit does not match task worktree")
        tree = _git_text(
            self.runner,
            self.target,
            "rev-parse",
            f"{prepared.candidate_commit}^{{tree}}",
        )
        if tree != prepared.tree_oid:
            raise ContractError("prepared source commit tree changed")

    def _source_audit_body(
        self,
        contract: TaskContract,
        metadata: SourceCommitMetadata,
        tree_oid: str,
        candidate: str,
        audit: SourceAudit,
    ) -> bytes:
        body = {
            "schema_version": 1,
            "verdict": "PASS",
            "task": {
                "run_id": metadata.run_id,
                "task_id": metadata.task_id,
                "attempt_no": metadata.attempt_no,
                "attempt_created_at": metadata.attempt_created_at,
                "path_scope": list(contract.path_scope),
            },
            "base_sha": audit.task_base_sha,
            "tree_oid": tree_oid,
            "candidate_commit": candidate,
            "source_commits": list(audit.source_commits),
            "changed_paths": list(audit.changed_paths),
            "gitlinks_changed": audit.gitlinks_changed,
            "protected_before": self._protected_as_dict(audit.protected_before),
            "protected_after": self._protected_as_dict(audit.protected_after),
        }
        return (
            json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def _validate_preparation_identity(
        self,
        contract: TaskContract,
        worktree: TaskWorktree,
        preflight: AttemptPreflight,
        metadata: SourceCommitMetadata,
    ) -> None:
        expected = (
            preflight.role == "worker"
            and preflight.task_id == contract.id == metadata.task_id
            and preflight.expected_base == worktree.base_sha
            and preflight.branch == worktree.branch
            and Path(preflight.worktree).resolve() == worktree.path
            and preflight.attempt_created_at == metadata.attempt_created_at
        )
        if not expected:
            raise ContractError("source preparation identity does not match preflight")

    def _protected_from_preflight(
        self,
        preflight: AttemptPreflight,
    ) -> ProtectedGitSnapshot:
        value = preflight.snapshot.get("protected_git")
        if not isinstance(value, dict):
            raise ContractError("preflight protected Git snapshot is missing")
        try:
            return ProtectedGitSnapshot(
                user_head=str(value["user_head"]),
                index_tree=str(value["index_tree"]),
                status_digest=str(value["status_digest"]),
                refs=tuple(
                    (str(item[0]), str(item[1]))
                    for item in value["refs"]  # type: ignore[union-attr]
                ),
                packed_refs_digest=str(value["packed_refs_digest"]),
                config_digest=str(value["config_digest"]),
                remote_urls=tuple(
                    (str(item[0]), str(item[1]))
                    for item in value["remote_urls"]  # type: ignore[union-attr]
                ),
            )
        except (KeyError, TypeError, IndexError) as error:
            raise ContractError("preflight protected Git snapshot is invalid") from error

    @staticmethod
    def _protected_as_dict(snapshot: ProtectedGitSnapshot) -> dict[str, object]:
        value = asdict(snapshot)
        value["refs"] = [list(item) for item in snapshot.refs]
        value["remote_urls"] = [list(item) for item in snapshot.remote_urls]
        return value

    def _create_ref(self, ref: str, base_sha: str) -> None:
        try:
            current = _resolve_revision(self.runner, self.target, ref)
        except ContractError:
            current = ""
        if current:
            if current != base_sha:
                raise ContractError(f"reserved ref already exists at another commit: {ref}")
            return
        self.runner.run_local(
            self.target,
            "update-ref",
            ref,
            base_sha,
            ZERO_OID,
        )

    def _common_git_dir(self) -> Path:
        return Path(
            _git_text(
                self.runner,
                self.target,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
        ).resolve()

    @staticmethod
    def _read_optional(path: Path) -> bytes:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return b""
        except OSError as error:
            raise ContractError(f"cannot read protected Git file: {path}") from error

    @staticmethod
    def _remote_urls(config: bytes) -> Iterable[tuple[str, str]]:
        for entry in config.split(b"\0"):
            if not entry:
                continue
            key, _, value = entry.partition(b"\n")
            decoded_key = key.decode("utf-8", errors="surrogateescape")
            if decoded_key.startswith("remote.") and decoded_key.endswith(".url"):
                yield (
                    decoded_key,
                    value.decode("utf-8", errors="surrogateescape"),
                )

    @staticmethod
    def _normalize_product_path(path: str) -> str:
        if "\0" in path or "\\" in path:
            raise ContractError("Git path is not a valid POSIX repository path")
        pure = PurePosixPath(path)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            raise ContractError("Git path escapes the product repository")
        normalized = pure.as_posix()
        if normalized in {"", "."}:
            raise ContractError("Git path must identify a product entry")
        return normalized

    @staticmethod
    def _is_control_path(path: str) -> bool:
        return path == ".vibe-coding" or path.startswith(".vibe-coding/")

    @staticmethod
    def _validate_id(value: str, field: str) -> None:
        if (
            not value
            or "/" in value
            or value.startswith(".")
            or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for character in value)
        ):
            raise ContractError(f"{field} is invalid")

    @staticmethod
    def _require_below(path: Path, root: Path) -> None:
        try:
            path.absolute().relative_to(root.absolute())
        except ValueError as error:
            raise ContractError("worktree path escapes Controller worktree root") from error

    def _ensure_safe_directory(self, directory: Path) -> None:
        self._require_below(directory, self.control_root)
        current = self.target
        for part in directory.absolute().relative_to(self.target).parts:
            current = current / part
            if current.is_symlink():
                raise ContractError(
                    "Controller temporary directory must not be a symbolic link"
                )
            if current.exists():
                if not current.is_dir():
                    raise ContractError(
                        "Controller temporary path ancestor must be a directory"
                    )
            else:
                current.mkdir()


@dataclass(frozen=True)
class GitReadOnlyAudit:
    manager: WorktreeManager

    def capture(self, worktree: Path) -> dict[str, object]:
        return self.manager.capture_read_only_audit(worktree)

    def assert_unchanged(
        self,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        if after != before:
            raise ContractError("read-only role changed repository state")
