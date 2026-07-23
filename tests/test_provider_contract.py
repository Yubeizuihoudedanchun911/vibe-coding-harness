from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from tests.support.fake_provider import ScriptedProvider
from vibe.models import ProviderStatus
from vibe.providers.base import (
    ProviderFailureKind,
    ProviderRequest,
    classify_provider_failure,
)


class ProviderContractTests(unittest.TestCase):
    def test_scripted_provider_persists_result_after_poll_completion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = ProviderRequest.for_test(
                root=root,
                attempt_token="ATTEMPT-1",
                role="planner",
                result_body=b'{"schema_version":1}\n',
            )
            provider = ScriptedProvider()
            handle = provider.start(request)
            self.assertEqual(
                provider.poll(handle),
                ProviderStatus.RUNNING,
            )
            provider.complete(handle.attempt_token)
            self.assertEqual(
                provider.poll(handle),
                ProviderStatus.SUCCEEDED,
            )
            self.assertEqual(
                provider.result(handle).body,
                b'{"schema_version":1}\n',
            )
            self.assertTrue(Path(handle.launch_path).is_file())
            self.assertTrue(Path(handle.exit_path).is_file())

    def test_provider_failures_are_classified_without_consuming_semantic_policy(
        self,
    ) -> None:
        self.assertEqual(
            classify_provider_failure(
                1,
                "rate limit exceeded",
            ).kind,
            ProviderFailureKind.TRANSIENT,
        )
        self.assertEqual(
            classify_provider_failure(
                1,
                "authentication required",
            ).kind,
            ProviderFailureKind.AUTH,
        )
        self.assertEqual(
            classify_provider_failure(124, "timed out").kind,
            ProviderFailureKind.TIMEOUT,
        )
        self.assertEqual(
            classify_provider_failure(
                1,
                "invalid output schema",
            ).kind,
            ProviderFailureKind.INVALID_OUTPUT,
        )

    def test_stop_is_idempotent_and_records_a_terminal_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            request = ProviderRequest.for_test(
                root=Path(temporary),
                attempt_token="ATTEMPT-2",
                role="worker",
                result_body=b"{}\n",
            )
            provider = ScriptedProvider()
            handle = provider.start(request)
            first = provider.stop(handle, 0.1)
            second = provider.stop(handle, 0.1)
            self.assertTrue(first.stopped)
            self.assertEqual(second, first)
            self.assertTrue(
                provider.completion(handle).stop_requested
            )

    def test_poll_race_with_one_completion_has_stable_terminal_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            request = ProviderRequest.for_test(
                root=Path(temporary),
                attempt_token="ATTEMPT-RACE",
                role="planner",
                result_body=b"{}\n",
            )
            provider = ScriptedProvider()
            handle = provider.start(request)
            barrier = threading.Barrier(5)
            statuses: list[ProviderStatus] = []

            def poll_repeatedly() -> None:
                try:
                    barrier.wait(timeout=5)
                    for _ in range(100):
                        statuses.append(provider.poll(handle))
                except BaseException as error:
                    provider.record_background_failure(error)

            threads = [
                threading.Thread(target=poll_repeatedly)
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=5)
            provider.complete(handle.attempt_token)
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            provider.assert_no_background_failures()
            self.assertIn(ProviderStatus.SUCCEEDED, statuses)
            self.assertEqual(
                provider.poll(handle),
                ProviderStatus.SUCCEEDED,
            )
            self.assertEqual(
                provider.completion(handle),
                provider.completion(handle),
            )


if __name__ == "__main__":
    unittest.main()
