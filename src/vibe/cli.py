from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import signal
import stat
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath

from vibe import __version__
from vibe.config import (
    frozen_config_bytes,
    load_run_config,
    parse_frozen_config,
)
from vibe.controller import Controller, ControllerDependencies
from vibe.git_runner import GitRunner
from vibe.integrator import Integrator
from vibe.models import (
    ContractError,
    FrozenRunConfig,
    RUN_ID_RE,
    RunStatus,
    VibeError,
)
from vibe.prompt_registry import PromptRegistry
from vibe.providers import provider_adapter
from vibe.providers.codex_cli import (
    ProcessInspection,
    inspect_process,
)
from vibe.runners.evaluator import EvaluatorRunner
from vibe.runners.planner import PlannerRunner
from vibe.runners.worker import WorkerRunner
from vibe.scheduler import Scheduler
from vibe.state_store import (
    MAX_ARTIFACT_BYTES,
    StateStore,
    open_absolute_regular_no_follow,
    parse_json_object_bytes,
    read_bounded,
)
from vibe.verification import VerificationGate
from vibe.worktrees import GitReadOnlyAudit, WorktreeManager


COMMANDS = ("run", "resume", "status", "stop", "logs", "migrate")
EXIT_CODES = {
    "SUCCEEDED": 0,
    "PAUSED": 3,
    "FAILED": 4,
    "STOPPED": 130,
}
_WAKE_EVENT = threading.Event()


class CliUsageError(ContractError):
    """An argparse usage error that does not terminate the process."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "must be a positive integer"
        ) from error
    if value < 1:
        raise argparse.ArgumentTypeError(
            "must be a positive integer"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="vibe")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run")
    run.add_argument("--target", default=".")
    goal = run.add_mutually_exclusive_group(required=True)
    goal.add_argument("--goal")
    goal.add_argument("--goal-file")
    run.add_argument("--max-workers", type=positive_int)
    run.add_argument("--task-attempts", type=positive_int)
    run.add_argument("--provider-retries", type=positive_int)
    run.add_argument("--evidence-rounds", type=positive_int)
    run.add_argument("--repair-rounds", type=positive_int)
    run.add_argument("--allow-project-commands", action="store_true")
    run.add_argument("--json", action="store_true")

    resume = commands.add_parser("resume")
    resume.add_argument("--target", default=".")
    resume.add_argument("run_id")
    resume.add_argument("--replan", action="store_true")
    resume.add_argument("--json", action="store_true")

    status = commands.add_parser("status")
    status.add_argument("--target", default=".")
    status.add_argument("run_id")
    status.add_argument("--json", action="store_true")

    stop = commands.add_parser("stop")
    stop.add_argument("--target", default=".")
    stop.add_argument("run_id")
    stop.add_argument("--json", action="store_true")

    logs = commands.add_parser("logs")
    logs.add_argument("--target", default=".")
    logs.add_argument("run_id")
    logs.add_argument("--task")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--json", action="store_true")

    migrate = commands.add_parser("migrate")
    migrate.add_argument("--target", default=".")
    source = migrate.add_mutually_exclusive_group(required=True)
    source.add_argument("--requirement")
    source.add_argument("--all", action="store_true")
    migrate.add_argument("--base", required=True)
    migrate.add_argument(
        "--allow-project-commands",
        action="store_true",
    )
    migrate.add_argument("--json", action="store_true")
    return parser


def resolve_target(value: str | Path) -> Path:
    candidate = Path(value).resolve(strict=True)
    if not candidate.is_dir():
        raise ContractError("target must be a directory")
    runner = GitRunner(candidate)
    root = Path(
        runner.run_local(
            candidate,
            "rev-parse",
            "--show-toplevel",
        ).stdout.decode("utf-8").strip()
    ).resolve()
    return root


class _ControllerFacade:
    def __init__(
        self,
        target: Path,
        config: FrozenRunConfig,
    ) -> None:
        self.target = target
        self.config = config
        self.worktrees = WorktreeManager(target)

    def _bootstrap(self) -> Controller:
        return Controller(
            self.target,
            self.config,
            ControllerDependencies(
                store_factory=StateStore.for_run,
                worktrees=self.worktrees,
                scheduler=Scheduler(),
                planner=None,
                worker=None,
                evaluator=None,
                verification=None,
                integrator=None,
                clock=Controller.utc_now,
                sleep=_interruptible_sleep,
                fault_hook=lambda point: None,
                wake_signal=_wake_controller,
            ),
        )

    def _runtime(self, run_id: str) -> Controller:
        store = StateStore.for_run(self.target, run_id)
        provider = provider_adapter(self.config.provider_name)
        registry = PromptRegistry.default()
        audit = GitReadOnlyAudit(self.worktrees)
        config_sha = (
            "sha256:"
            + hashlib.sha256(
                frozen_config_bytes(self.config)
            ).hexdigest()
        )
        baseline = store.load()["repository"]["base_sha"]
        common = {
            "registry": registry,
            "provider": provider,
            "target_root": self.target,
            "run_root": store.root,
            "expected_base": baseline,
            "config": self.config,
            "config_sha256": config_sha,
        }
        planner = PlannerRunner(
            **common,
            read_only_audit=audit,
        )
        worker = WorkerRunner(**common)
        evaluator = EvaluatorRunner(
            **common,
            read_only_audit=audit,
        )
        verification = VerificationGate(
            self.config,
            store,
            worktrees=self.worktrees,
        )
        integrator = Integrator(
            worktrees=self.worktrees,
            store=store,
            verification=verification,
            config=self.config,
            fault_hook=lambda point: None,
        )

        def store_factory(
            target: Path,
            requested_run_id: str,
        ) -> StateStore:
            if (
                target.resolve() == self.target
                and requested_run_id == run_id
            ):
                return store
            return StateStore.for_run(target, requested_run_id)

        return Controller(
            self.target,
            self.config,
            ControllerDependencies(
                store_factory=store_factory,
                worktrees=self.worktrees,
                scheduler=Scheduler(),
                planner=planner,
                worker=worker,
                evaluator=evaluator,
                verification=verification,
                integrator=integrator,
                clock=Controller.utc_now,
                sleep=_interruptible_sleep,
                fault_hook=lambda point: None,
                wake_signal=_wake_controller,
            ),
        )

    def create_run(
        self,
        goal: str,
        config: FrozenRunConfig,
    ) -> str:
        return self._bootstrap().create_run(goal, config)

    def execute(self, run_id: str) -> dict[str, object]:
        return self._runtime(run_id).execute(run_id)

    def resume(
        self,
        run_id: str,
        replan: bool = False,
    ) -> dict[str, object]:
        return self._runtime(run_id).resume(
            run_id,
            replan=replan,
        )

    def request_stop(self, run_id: str):
        return self._bootstrap().request_stop(run_id)

    def recover_and_stop(self, run_id: str, request):
        return self._runtime(run_id).recover_and_stop(
            run_id,
            request,
        )


def controller_for_target(
    target: Path,
    config: FrozenRunConfig | None = None,
    run_id: str | None = None,
) -> _ControllerFacade:
    if config is None:
        if run_id is None:
            raise ContractError(
                "run ID is required to load frozen config"
            )
        state = StateStore.for_run(target, run_id).load()
        reference = state["config"]
        path = StateStore.for_run(target, run_id).root.joinpath(
            *PurePosixPath(reference["path"]).parts
        )
        descriptor = open_absolute_regular_no_follow(path)
        try:
            body = read_bounded(
                descriptor,
                max_bytes=MAX_ARTIFACT_BYTES,
            )
        finally:
            os.close(descriptor)
        config = parse_frozen_config(
            parse_json_object_bytes(body)
        )
    return _ControllerFacade(target, config)


def load_status(
    target: Path,
    run_id: str,
) -> dict[str, object]:
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ContractError("invalid run ID")
    return StateStore.for_run(target, run_id).load()


def status_projection(
    state: Mapping[str, object],
) -> dict[str, object]:
    counts = {
        "completed": 0,
        "running": 0,
        "pending": 0,
        "failed": 0,
    }
    for task in state["tasks"].values():
        status = task["status"]
        if status == "COMPLETED":
            counts["completed"] += 1
        elif status in {
            "RUNNING",
            "READY_TO_INTEGRATE",
            "INTEGRATING",
        }:
            counts["running"] += 1
        elif status == "FAILED":
            counts["failed"] += 1
        else:
            counts["pending"] += 1
    return {
        "schema_version": 1,
        "run_id": state["run_id"],
        "status": state["status"],
        "revision": state["revision"],
        "plan_version": state["plan_version"],
        "repair_round": state["repair_round"],
        "tasks": counts,
        "last_error": state["last_error"],
    }


def _read_goal_file(value: str) -> str:
    path = Path(value).resolve(strict=True)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ContractError(
            "goal file must be a regular non-symlink file"
        )
    if metadata.st_size > 4 * 1024 * 1024:
        raise ContractError("goal file is too large")
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeError as error:
        raise ContractError(
            "goal file must be UTF-8"
        ) from error


def _interruptible_sleep(seconds: float) -> None:
    _WAKE_EVENT.wait(timeout=seconds)
    _WAKE_EVENT.clear()


def _wake_controller(identity: dict[str, object]) -> None:
    state = inspect_process(
        identity["pid"],
        identity["process_start_identity"],
        identity["process_group"],
    )
    if state is not ProcessInspection.MATCHING_LIVE:
        return
    os.killpg(identity["process_group"], signal.SIGUSR1)


@contextlib.contextmanager
def _wake_handler() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGUSR1)

    def wake(signum, frame) -> None:
        del signum, frame
        _WAKE_EVENT.set()

    signal.signal(signal.SIGUSR1, wake)
    try:
        yield
    finally:
        signal.signal(signal.SIGUSR1, previous)


def _terminal_exit(status: str) -> int:
    return EXIT_CODES.get(status, 0)


def _command_envelope(
    command: str,
    *,
    ok: bool,
    run_id: str | None = None,
    status: str | None = None,
    detail: object = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "command": command,
        "ok": ok,
        "run_id": run_id,
        "status": status,
        "detail": detail,
    }


def _emit(value: object, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    elif isinstance(value, dict):
        status = value.get("status")
        run_id = value.get("run_id")
        print(" ".join(str(item) for item in (run_id, status) if item))
    else:
        print(value)


def _logs(
    target: Path,
    run_id: str,
    task_id: str | None,
) -> list[dict[str, object]]:
    state = load_status(target, run_id)
    store = StateStore.for_run(target, run_id)
    records: list[dict[str, object]] = []
    for reference in state["artifact_index"]:
        relative = reference["path"]
        if not relative.endswith((".log", ".jsonl")):
            continue
        inferred_task = (
            relative.split("/", 2)[1]
            if relative.startswith("tasks/")
            else None
        )
        if task_id is not None and inferred_task != task_id:
            continue
        path = store.root.joinpath(
            *PurePosixPath(relative).parts
        )
        descriptor = open_absolute_regular_no_follow(path)
        try:
            body = read_bounded(
                descriptor,
                max_bytes=MAX_ARTIFACT_BYTES,
            )
        finally:
            os.close(descriptor)
        records.append(
            {
                "schema_version": 1,
                "run_id": run_id,
                "task_id": inferred_task,
                "path": relative,
                "text": body.decode(
                    "utf-8",
                    errors="replace",
                ),
            }
        )
    if not records:
        records.append(
            {
                "schema_version": 1,
                "run_id": run_id,
                "task_id": task_id,
                "path": None,
                "text": f"status={state['status']}\n",
            }
        )
    return records


def _handle(args: argparse.Namespace) -> int:
    target = resolve_target(args.target)
    if args.command == "run":
        goal = (
            args.goal
            if args.goal is not None
            else _read_goal_file(args.goal_file)
        )
        overrides = {
            "max_workers": args.max_workers,
            "task_attempts": args.task_attempts,
            "provider_retries": args.provider_retries,
            "evidence_rounds": args.evidence_rounds,
            "repair_rounds": args.repair_rounds,
            "allow_project_commands": (
                args.allow_project_commands
            ),
        }
        config = load_run_config(target, overrides)
        controller = controller_for_target(
            target,
            config=config,
        )
        run_id = controller.create_run(goal, config)
        with _wake_handler():
            state = controller.execute(run_id)
        _emit(
            _command_envelope(
                "run",
                ok=True,
                run_id=run_id,
                status=state["status"],
            ),
            json_output=args.json,
        )
        return _terminal_exit(state["status"])
    if args.command == "resume":
        controller = controller_for_target(
            target,
            run_id=args.run_id,
        )
        with _wake_handler():
            state = controller.resume(
                args.run_id,
                replan=args.replan,
            )
        _emit(
            _command_envelope(
                "resume",
                ok=True,
                run_id=args.run_id,
                status=state["status"],
            ),
            json_output=args.json,
        )
        return _terminal_exit(state["status"])
    if args.command == "status":
        state = load_status(target, args.run_id)
        _emit(
            status_projection(state),
            json_output=args.json,
        )
        return 0
    if args.command == "stop":
        controller = controller_for_target(
            target,
            run_id=args.run_id,
        )
        request = controller.request_stop(args.run_id)
        _emit(
            _command_envelope(
                "stop",
                ok=True,
                run_id=args.run_id,
                status="STOP_REQUESTED",
                detail={"nonce": request.nonce},
            ),
            json_output=args.json,
        )
        return 0
    if args.command == "logs":
        records = _logs(
            target,
            args.run_id,
            args.task,
        )
        for record in records:
            _emit(record, json_output=args.json)
        return 0
    if args.command == "migrate":
        try:
            from vibe.migration.schema3 import migrate_schema3
        except ImportError as error:
            raise ContractError(
                "Schema 3 migration is not installed"
            ) from error
        imports = migrate_schema3(
            target=target,
            requirement_id=args.requirement,
            migrate_all=args.all,
            base=args.base,
            allow_project_commands=(
                args.allow_project_commands
            ),
        )
        value = {
            "schema_version": 1,
            "command": "migrate",
            "ok": True,
            "imports": [
                item.as_dict()
                if hasattr(item, "as_dict")
                else item
                for item in imports
            ],
        }
        _emit(value, json_output=args.json)
        return 0
    raise CliUsageError("a command is required")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    json_output = "--json" in arguments
    command = (
        arguments[0]
        if arguments and arguments[0] in COMMANDS
        else "unknown"
    )
    try:
        try:
            args = build_parser().parse_args(arguments)
        except SystemExit as stopped:
            return int(stopped.code or 0)
        return _handle(args)
    except (
        VibeError,
        OSError,
        UnicodeError,
        ValueError,
    ) as error:
        if json_output:
            _emit(
                _command_envelope(
                    command,
                    ok=False,
                    detail=str(error),
                ),
                json_output=True,
            )
        else:
            print(f"vibe: {error}", file=sys.stderr)
        return 2
