"""Configuration validation tests."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_music.config import HarnessSettings


class HarnessSettingsTests(unittest.TestCase):
    def test_environment_values_are_parsed_explicitly(self) -> None:
        environment = {
            "GPU_RUNNER": "COLAB",
            "GPU_MAX_CONCURRENCY": "1",
            "GPU_IDLE_TIMEOUT_SECONDS": "301",
            "COLAB_MOUNT_DRIVE": "off",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = HarnessSettings.from_env()

        self.assertEqual("colab", settings.runner)
        self.assertEqual(301.0, settings.idle_timeout_seconds)
        self.assertFalse(settings.colab_mount_drive)

    def test_unsafe_concurrency_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "GPU_MAX_CONCURRENCY=1"):
            HarnessSettings(gpu_max_concurrency=2).validate()

    def test_invalid_runner_and_non_positive_timeouts_are_rejected(self) -> None:
        invalid_settings = (
            HarnessSettings(runner="unknown"),
            HarnessSettings(idle_timeout_seconds=0),
            HarnessSettings(scheduler_poll_seconds=0),
            HarnessSettings(job_timeout_seconds=0),
        )
        for settings in invalid_settings:
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    settings.validate()
