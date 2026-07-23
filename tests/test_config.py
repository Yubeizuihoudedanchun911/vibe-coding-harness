from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from vibe.config import (
    DEFAULT_CONFIG,
    effective_command_ids,
    frozen_config_bytes,
    load_run_config,
    parse_frozen_config,
    resolve_command_ids,
)
from vibe.models import (
    CommandAuthorization,
    CommandSpec,
    ContractError,
)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name)

    def test_defaults_are_finite_and_codex_cli_is_the_only_v1_provider(
        self,
    ) -> None:
        config = load_run_config(self.target, {})
        self.assertEqual(config.provider_name, "codex-cli")
        self.assertEqual(config.max_workers, 4)
        self.assertEqual(config.task_attempts, 3)
        self.assertEqual(config.provider_retries, 3)
        self.assertEqual(config.evidence_rounds, 3)
        self.assertEqual(config.repair_rounds, 3)
        self.assertEqual(config.max_plan_tasks, 128)
        self.assertEqual(config.command_authorization.mode, "EMPTY")

    def test_cli_override_wins_and_the_result_can_be_frozen(self) -> None:
        (self.target / "vibe.json").write_text(
            json.dumps({"scheduler": {"max_workers": 2}}),
            encoding="utf-8",
        )
        config = load_run_config(self.target, {"max_workers": 6})
        frozen = frozen_config_bytes(config)
        (self.target / "vibe.json").write_text("{}", encoding="utf-8")

        self.assertEqual(parse_frozen_config(json.loads(frozen)), config)
        self.assertEqual(config.max_workers, 6)

    def test_project_commands_require_explicit_creation_authorization(
        self,
    ) -> None:
        body = json.dumps(
            {
                "verification": {
                    "command_catalog": [
                        {
                            "id": "unit",
                            "purpose": "Run the unit-test suite",
                            "argv": ["python3", "-m", "unittest"],
                        }
                    ],
                    "required_command_ids": ["unit"],
                }
            },
            sort_keys=True,
        )
        (self.target / "vibe.json").write_text(body, encoding="utf-8")
        with self.assertRaisesRegex(
            ContractError,
            "--allow-project-commands",
        ):
            load_run_config(self.target, {})

        config = load_run_config(
            self.target,
            {"allow_project_commands": True},
        )
        frozen = frozen_config_bytes(config)
        self.assertEqual(
            config.command_authorization.mode,
            "EXPLICIT_PROJECT_FILE",
        )
        self.assertEqual(
            config.command_authorization.source_path,
            "vibe.json",
        )
        self.assertTrue(
            config.command_authorization.source_sha256.startswith("sha256:")
        )

        (self.target / "vibe.json").write_text("{}", encoding="utf-8")
        self.assertEqual(parse_frozen_config(json.loads(frozen)), config)

    def test_command_rejects_shell_strings_absolute_cwd_and_bad_env_names(
        self,
    ) -> None:
        invalid_values = (
            {
                "verification": {
                    "command_catalog": [
                        {
                            "id": "unit",
                            "purpose": "Run unit tests",
                            "argv": "python -m unittest",
                        }
                    ]
                }
            },
            {
                "verification": {
                    "command_catalog": [
                        {
                            "id": "unit",
                            "purpose": "Run unit tests",
                            "argv": ["python3"],
                            "cwd": "/tmp",
                        }
                    ]
                }
            },
            {
                "verification": {
                    "command_catalog": [
                        {
                            "id": "unit",
                            "purpose": "Run unit tests",
                            "argv": ["python3"],
                            "env_allowlist": ["TOKEN=value"],
                        }
                    ]
                }
            },
        )
        for value in invalid_values:
            (self.target / "vibe.json").write_text(
                json.dumps(value),
                encoding="utf-8",
            )
            with self.subTest(value=value), self.assertRaises(ContractError):
                load_run_config(
                    self.target,
                    {"allow_project_commands": True},
                )

    def test_unknown_provider_and_unknown_fields_fail_closed(self) -> None:
        for value in (
            {"provider": {"name": "other"}},
            {"scheduler": {"max_workers": 4, "queue": "remote"}},
        ):
            (self.target / "vibe.json").write_text(
                json.dumps(value),
                encoding="utf-8",
            )
            with self.assertRaises(ContractError):
                load_run_config(self.target, {})

    def test_agent_command_ids_resolve_only_the_frozen_catalog(self) -> None:
        catalog = (
            CommandSpec(
                id="unit",
                purpose="Run the unit-test suite",
                argv=("python3", "-m", "unittest"),
                cwd=".",
                timeout_seconds=900,
                env_allowlist=(),
            ),
            CommandSpec(
                id="models",
                purpose="Run model-contract tests",
                argv=(
                    "python3",
                    "-m",
                    "unittest",
                    "tests.test_models",
                ),
                cwd=".",
                timeout_seconds=120,
                env_allowlist=(),
            ),
        )
        config = dataclasses.replace(
            DEFAULT_CONFIG,
            command_catalog=catalog,
            required_command_ids=("unit",),
            command_authorization=CommandAuthorization(
                mode="EXPLICIT_PROJECT_FILE",
                source_path="vibe.json",
                source_sha256="sha256:" + "a" * 64,
            ),
        )
        self.assertEqual(
            effective_command_ids(config, ("models",)),
            ("unit", "models"),
        )
        self.assertEqual(
            effective_command_ids(config, ()),
            ("unit",),
        )
        with self.assertRaises(ContractError):
            effective_command_ids(config, ("models", "models"))
        self.assertEqual(
            resolve_command_ids(config, ("unit", "models")),
            catalog,
        )
        for ids in (("unknown",), ("unit", "unit")):
            with self.subTest(ids=ids), self.assertRaises(ContractError):
                resolve_command_ids(config, ids)

    def test_project_file_swap_cannot_mix_authorization_and_catalog(
        self,
    ) -> None:
        sources = []
        for command_id in ("unit", "models"):
            sources.append(
                json.dumps(
                    {
                        "verification": {
                            "command_catalog": [
                                {
                                    "id": command_id,
                                    "purpose": f"Run {command_id}",
                                    "argv": ["python3", "-m", command_id],
                                }
                            ],
                            "required_command_ids": [command_id],
                        }
                    },
                    sort_keys=True,
                ).encode("utf-8")
            )
        path = self.target / "vibe.json"
        path.write_bytes(sources[0])
        stop = threading.Event()
        failures: list[BaseException] = []

        def swap() -> None:
            index = 0
            try:
                while not stop.is_set():
                    temporary = self.target / f".vibe-{index}.tmp"
                    temporary.write_bytes(sources[index])
                    os.replace(temporary, path)
                    index = 1 - index
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=swap)
        thread.start()
        try:
            for _ in range(100):
                config = load_run_config(
                    self.target,
                    {"allow_project_commands": True},
                )
                command_id = config.command_catalog[0].id
                source = sources[0] if command_id == "unit" else sources[1]
                self.assertEqual(
                    config.command_authorization.source_sha256,
                    "sha256:" + hashlib.sha256(source).hexdigest(),
                )
                self.assertEqual(
                    config.required_command_ids,
                    (command_id,),
                )
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
