"""Modal CLI command construction tests."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

from agent_music.runners.modal_cli import (
    CommandResult,
    ModalCliClient,
    ModalCliCompatibilityError,
    ModalCliFailureKind,
    classify_modal_failure,
    validate_modal_cli_version,
)


class ModalCliClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ModalCliClient(
            executable="modal",
            app_name="melqivra-gpu-runner",
            timeout_seconds=30,
        )

    def test_job_command_uses_files_and_scale_to_zero_app_name(self) -> None:
        argv = self.client.run_job_argv(
            Path("agent_music/remote/modal_app.py"),
            Path("/tmp/private job.json"),
            Path("/tmp/result.json"),
        )
        self.assertEqual("modal", argv[0])
        self.assertEqual("run", argv[1])
        self.assertIn("melqivra-gpu-runner", argv)
        self.assertIn(str(Path("/tmp/private job.json")), argv)
        self.assertNotIn("lyrics", " ".join(argv))

    def test_stop_command_is_non_interactive_and_scoped(self) -> None:
        self.assertEqual(
            ["modal", "app", "stop", "--yes", "melqivra-gpu-runner"],
            self.client.stop_argv(),
        )

    def test_volume_download_command_keeps_paths_as_separate_arguments(self) -> None:
        argv = self.client.volume_get_argv(
            "melqivra-model-cache",
            "runs/request/take.wav",
            Path("/tmp/private transfer"),
        )
        self.assertEqual(
            [
                "modal",
                "volume",
                "get",
                "melqivra-model-cache",
                "runs/request/take.wav",
                str(Path("/tmp/private transfer")),
            ],
            argv,
        )

    def test_failure_classification_is_explicit(self) -> None:
        self.assertEqual(
            ModalCliFailureKind.AUTHENTICATION,
            classify_modal_failure(CommandResult(1, "", "Token expired; not authenticated")),
        )
        self.assertEqual(
            ModalCliFailureKind.APP_MISSING,
            classify_modal_failure(CommandResult(1, "", "app not found")),
        )
        self.assertEqual(
            ModalCliFailureKind.INCOMPATIBLE,
            classify_modal_failure(CommandResult(2, "", "no such option: --quiet")),
        )

    def test_version_contract_requires_exact_verified_release(self) -> None:
        self.assertEqual(
            "1.6.3",
            validate_modal_cli_version("modal client version: 1.6.3", "1.6.3"),
        )
        with self.assertRaises(ModalCliCompatibilityError):
            validate_modal_cli_version("modal client version: 1.7.0", "1.6.3")
        with self.assertRaises(ModalCliCompatibilityError):
            validate_modal_cli_version("development build", "1.6.3")


class ModalCliCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_process_times_out_and_releases_command_slot(self) -> None:
        client = ModalCliClient(
            executable="modal",
            app_name="melqivra-gpu-runner",
            timeout_seconds=1.0,
        )

        with self.assertRaisesRegex(TimeoutError, "Modal CLI timed out"):
            await client.run([sys.executable, "-c", "import time; time.sleep(30)"])

        result = await client.run([sys.executable, "-c", "print('ready')"])
        self.assertEqual("ready", result.stdout)

    async def test_cancel_current_interrupts_the_active_process(self) -> None:
        client = ModalCliClient(
            executable="modal",
            app_name="melqivra-gpu-runner",
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
