from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from tests.support.controller_scenario import (
    ControllerScenario,
    ScenarioSpec,
)
from vibe.models import (
    CommandAuthorization,
    CommandSpec,
    FrozenRunConfig,
)


def controller_config() -> FrozenRunConfig:
    return FrozenRunConfig(
        provider_name="codex-cli",
        max_workers=2,
        task_attempts=2,
        provider_retries=2,
        evidence_rounds=2,
        repair_rounds=2,
        max_plan_tasks=128,
        command_catalog=(
            CommandSpec(
                "unit",
                "Run unit fixture",
                (sys.executable, "-c", "print('ok')"),
            ),
        ),
        required_command_ids=("unit",),
        command_authorization=CommandAuthorization(
            "EXPLICIT_PROJECT_FILE",
            "vibe.json",
            "sha256:" + "f" * 64,
        ),
    )


class ControllerCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.scenario = ControllerScenario.build(
            Path(self.temporary.name),
            ScenarioSpec(
                goal="Implement criterion C1",
                config=controller_config(),
                scripts=(),
            ),
        )
        self.addCleanup(self.scenario.close)

    def test_run_creation_binds_intent_config_and_receipt(self) -> None:
        state = self.scenario.store.load()
        self.assertEqual(state["goal"], "Implement criterion C1")
        self.assertEqual(
            state["creation"]["intent"]["path"],
            "creation.intent.json",
        )
        self.assertEqual(
            state["creation"]["receipt"]["path"],
            "creation.receipt.json",
        )
        self.assertEqual(state["config"]["path"], "config.json")
        self.assertFalse((self.scenario.store.root / "goal.json").exists())
        self.assertEqual(state["status"], "CREATED")

    def test_identical_pristine_create_is_idempotent(self) -> None:
        repeated = self.scenario.controller.create_run(
            "Implement criterion C1",
            controller_config(),
        )
        self.assertEqual(repeated, self.scenario.run_id)


if __name__ == "__main__":
    unittest.main()
