"""Colab CLI command construction tests."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_music.runners.colab_cli import ColabCliClient


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


class ColabCliCancellationTests(unittest.IsolatedAsyncioTestCase):
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
