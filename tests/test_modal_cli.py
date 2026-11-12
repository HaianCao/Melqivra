"""Modal CLI command construction tests."""

from __future__ import annotations

import unittest
from pathlib import Path

from agent_music.runners.modal_cli import ModalCliClient


class ModalCliClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ModalCliClient(
            executable="modal",
            app_name="melqivra-gpu-runner",
            timeout_seconds=30,
        )

    def test_job_command_uses_files_and_scale_to_zero_app_name(self) -> None:
        argv = self.client.run_job_argv(
            Path("scripts/modal/runner_app.py"),
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


if __name__ == "__main__":
    unittest.main()
