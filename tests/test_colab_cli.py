"""Colab CLI command construction tests."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_music.runners.colab_cli import (
    ColabCliClient,
    ColabCliCompatibilityError,
    ColabCliFailureKind,
    CommandResult,
    classify_colab_failure,
    validate_colab_cli_version,
)


class ColabCliClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ColabCliClient(
            executable="colab",
            auth_provider="oauth2",
            config_path=Path("/state/sessions.json"),
            timeout_seconds=30,
        )

    def test_new_command_uses_argument_array(self) -> None:
        self.assertEqual(
            [
                "colab",
                "--auth",
                "oauth2",
                "--config",
                str(Path("/state/sessions.json")),
                "new",
                "-s",
                "melqivra-t4",
                "--gpu",
                "T4",
            ],
            self.client.new_argv("melqivra-t4", "T4"),
        )

    def test_user_path_is_one_argument(self) -> None:
        argv = self.client.upload_argv(
            "session",
            Path("/tmp/a file;not-shell.json"),
            "/content/job.json",
        )
        self.assertIn(str(Path("/tmp/a file;not-shell.json")), argv)
        self.assertEqual("/content/job.json", argv[-1])

    def test_failure_classification_is_explicit(self) -> None:
        self.assertEqual(
            ColabCliFailureKind.AUTHENTICATION,
            classify_colab_failure(CommandResult(1, "", "OAuth token expired")),
        )
        self.assertEqual(
            ColabCliFailureKind.SESSION_MISSING,
            classify_colab_failure(CommandResult(1, "", "session not found")),
        )
        self.assertEqual(
            ColabCliFailureKind.INCOMPATIBLE,
            classify_colab_failure(CommandResult(2, "", "unknown command status")),
        )

    def test_version_contract_requires_exact_verified_release(self) -> None:
        self.assertEqual("0.8.0", validate_colab_cli_version("colab 0.8.0", "0.8.0"))
        with self.assertRaises(ColabCliCompatibilityError):
            validate_colab_cli_version("colab 0.9.0", "0.8.0")
        with self.assertRaises(ColabCliCompatibilityError):
            validate_colab_cli_version("development build", "0.8.0")


class ColabCliCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_process_times_out_and_releases_command_slot(self) -> None:
        client = ColabCliClient(
            executable="colab",
            auth_provider="oauth2",
            config_path=Path("/state/sessions.json"),
            timeout_seconds=1.0,
        )

        with self.assertRaisesRegex(TimeoutError, "Colab CLI timed out"):
            await client.run([sys.executable, "-c", "import time; time.sleep(30)"])

        result = await client.run([sys.executable, "-c", "print('ready')"])
        self.assertEqual("ready", result.stdout)

    async def test_cancel_current_interrupts_the_active_process(self) -> None:
        client = ColabCliClient(
            executable="colab",
            auth_provider="oauth2",
            config_path=Path("/state/sessions.json"),
            timeout_seconds=30,
        )
        task = asyncio.create_task(
            client.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                check=False,
            )
        )
        await asyncio.sleep(0.1)

        await client.cancel_current()
        result = await asyncio.wait_for(task, timeout=5.0)

        self.assertNotEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
