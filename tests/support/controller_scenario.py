from __future__ import annotations

import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from tests.support.fake_provider import (
    ProviderScript,
    ScenarioProvider,
)
from vibe.config import frozen_config_bytes
from vibe.controller import Controller, ControllerDependencies
from vibe.integrator import Integrator
from vibe.models import FrozenRunConfig
from vibe.prompt_registry import PromptRegistry
from vibe.runners.evaluator import EvaluatorRunner
from vibe.runners.planner import PlannerRunner
from vibe.runners.worker import WorkerRunner
from vibe.scheduler import Scheduler
from vibe.state_store import StateStore
from vibe.verification import VerificationGate
from vibe.worktrees import GitReadOnlyAudit, WorktreeManager


@dataclass(frozen=True)
class ScenarioSpec:
    goal: str
    config: FrozenRunConfig
    scripts: tuple[ProviderScript, ...]
    worker_edits: Mapping[
        str,
        Callable[[Path, object], None],
    ] = field(default_factory=dict)
    fault_points: frozenset[str] = frozenset()


class _InstrumentedIntegrator:
    def __init__(
        self,
        scenario: ControllerScenario,
        delegate: Integrator,
    ) -> None:
        self.scenario = scenario
        self.delegate = delegate

    def integrate(self, *args, **kwargs):
        with self.scenario._integration_lock:
            self.scenario.integration_count += 1
            self.scenario.active_integrations += 1
            self.scenario.maximum_simultaneous_integrations = max(
                self.scenario.maximum_simultaneous_integrations,
                self.scenario.active_integrations,
            )
        try:
            return self.delegate.integrate(*args, **kwargs)
        finally:
            with self.scenario._integration_lock:
                self.scenario.active_integrations -= 1

    def recover(self):
        return self.delegate.recover()


@dataclass
class ControllerScenario:
    target: Path
    run_id: str
    store: StateStore
    provider: ScenarioProvider
    controller: Controller
    dependencies: ControllerDependencies
    thread_errors: queue.Queue[BaseException]
    integration_count: int = 0
    active_integrations: int = 0
    maximum_simultaneous_integrations: int = 0
    _thread: threading.Thread | None = None
    _result: dict[str, object] | None = None
    _integration_lock: threading.Lock = field(
        default_factory=threading.Lock,
    )

    @classmethod
    def build(
        cls,
        temporary_root: Path,
        spec: ScenarioSpec,
    ) -> ControllerScenario:
        target = (temporary_root / "repository").resolve()
        target.mkdir(parents=True)
        _git(target, "init", "-b", "main")
        _git(target, "config", "user.name", "Vibe Tests")
        _git(
            target,
            "config",
            "user.email",
            "vibe-tests@example.invalid",
        )
        (target / "README.md").write_text("base\n", encoding="utf-8")
        _git(target, "add", "README.md")
        _git(target, "commit", "-m", "base")
        provider = ScenarioProvider(list(spec.scripts))
        worktrees = WorktreeManager(target)
        bootstrap_dependencies = ControllerDependencies(
            store_factory=lambda root, run_id: StateStore.for_run(
                root,
                run_id,
            ),
            worktrees=worktrees,
            scheduler=Scheduler(),
            planner=None,
            worker=None,
            evaluator=None,
            verification=None,
            integrator=None,
            clock=Controller.utc_now,
            sleep=time.sleep,
            fault_hook=lambda point: _fault(spec.fault_points, point),
        )
        bootstrap = Controller(
            target,
            spec.config,
            bootstrap_dependencies,
        )
        run_id = bootstrap.create_run(spec.goal, spec.config)
        store = StateStore.for_run(target, run_id)
        baseline = store.load()["repository"]["base_sha"]
        registry = PromptRegistry.default()
        audit = GitReadOnlyAudit(worktrees)
        config_sha = "sha256:" + __import__("hashlib").sha256(
            frozen_config_bytes(spec.config)
        ).hexdigest()
        common = {
            "registry": registry,
            "provider": provider,
            "target_root": target,
            "run_root": store.root,
            "expected_base": baseline,
            "config": spec.config,
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
        gate = VerificationGate(
            spec.config,
            store,
            worktrees=worktrees,
        )
        real_integrator = Integrator(
            worktrees=worktrees,
            store=store,
            verification=gate,
            config=spec.config,
            fault_hook=lambda point: _fault(
                spec.fault_points,
                point,
            ),
        )
        errors: queue.Queue[BaseException] = queue.Queue()
        placeholder = cls(
            target=target,
            run_id=run_id,
            store=store,
            provider=provider,
            controller=bootstrap,
            dependencies=bootstrap_dependencies,
            thread_errors=errors,
        )
        dependencies = ControllerDependencies(
            store_factory=lambda root, requested_run_id: (
                store
                if root.resolve() == target
                and requested_run_id == run_id
                else StateStore.for_run(root, requested_run_id)
            ),
            worktrees=worktrees,
            scheduler=Scheduler(),
            planner=planner,
            worker=worker,
            evaluator=evaluator,
            verification=gate,
            integrator=_InstrumentedIntegrator(
                placeholder,
                real_integrator,
            ),
            clock=Controller.utc_now,
            sleep=lambda seconds: time.sleep(min(seconds, 0.01)),
            fault_hook=bootstrap_dependencies.fault_hook,
        )
        controller = Controller(target, spec.config, dependencies)
        placeholder.dependencies = dependencies
        placeholder.controller = controller
        return placeholder

    def start_controller(self) -> threading.Thread:
        if self._thread is not None:
            raise AssertionError("Controller thread already started")

        def run() -> None:
            try:
                self._result = self.controller.execute(self.run_id)
            except BaseException as error:
                self.thread_errors.put(error)

        self._thread = threading.Thread(
            target=run,
            name=f"controller-{self.run_id}",
            daemon=True,
        )
        self._thread.start()
        return self._thread

    def wait_until(
        self,
        predicate: Callable[[], bool],
        timeout: float = 10,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.thread_errors.empty():
                raise self.thread_errors.get()
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError(
            f"scenario timed out; state={self.store.load()!r}"
        )

    def join_controller(
        self,
        timeout: float = 20,
    ) -> dict[str, object]:
        if self._thread is None:
            raise AssertionError("Controller thread was not started")
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise AssertionError("Controller thread leaked")
        if not self.thread_errors.empty():
            raise self.thread_errors.get()
        self.provider.assert_no_background_failures()
        return self.store.load()

    def new_controller(self) -> Controller:
        return Controller(
            self.target,
            self.controller.config,
            self.dependencies,
        )

    def close(self) -> None:
        self.provider.join_background(timeout=1)
        if self._thread is not None:
            self._thread.join(timeout=1)
            if self._thread.is_alive():
                raise AssertionError("Controller thread leaked during cleanup")

    def git_changed_paths(self, commit: str) -> set[str]:
        base = self.store.load()["repository"]["base_sha"]
        value = _git(
            self.target,
            "diff",
            "--name-only",
            base,
            commit,
        )
        return set(value.splitlines())


def _git(target: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(target), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed: {result.stderr}"
        )
    return result.stdout.strip()


def _fault(points: frozenset[str], point: str) -> None:
    if point in points:
        raise RuntimeError(point)
