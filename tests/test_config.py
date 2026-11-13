"""Configuration validation tests."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_music.config import (
    DEFAULT_MODAL_RUNNER_FILE,
    DEFAULT_REMOTE_WORKER_FILE,
    HarnessSettings,
)


class HarnessSettingsTests(unittest.TestCase):
    def test_default_remote_assets_are_installed_package_files(self) -> None:
        settings = HarnessSettings()

        self.assertTrue(DEFAULT_MODAL_RUNNER_FILE.is_file())
        self.assertTrue(DEFAULT_REMOTE_WORKER_FILE.is_file())
        self.assertEqual(DEFAULT_MODAL_RUNNER_FILE, settings.modal_runner_file)
        self.assertEqual(DEFAULT_REMOTE_WORKER_FILE, settings.colab_remote_worker_file)

    def test_modal_is_the_default_runner(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = HarnessSettings.from_env()

        self.assertEqual("modal", settings.runner)
        self.assertEqual("melqivra-gpu-runner", settings.modal_app_name)

    def test_environment_values_are_parsed_explicitly(self) -> None:
        environment = {
            "GPU_RUNNER": "COLAB",
            "GPU_MAX_CONCURRENCY": "1",
            "GPU_IDLE_TIMEOUT_SECONDS": "301",
            "MODAL_CLI_VERSION": "1.6.3",
            "COLAB_CLI_VERSION": "0.8.0",
            "COLAB_REMOTE_CANCEL_ROOT": "/content/agent_music_control",
            "COLAB_CACHE_MANIFEST_PATH": "cache-manifest.json",
            "COLAB_REMOTE_CACHE_ROOT": "/content/drive/MyDrive/melqivra/cache-key",
            "COLAB_MOUNT_DRIVE": "off",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = HarnessSettings.from_env()

        self.assertEqual("colab", settings.runner)
        self.assertEqual(301.0, settings.idle_timeout_seconds)
        self.assertEqual("1.6.3", settings.modal_cli_version)
        self.assertFalse(settings.colab_mount_drive)
        self.assertEqual("0.8.0", settings.colab_cli_version)
        self.assertEqual("/content/agent_music_control", settings.colab_remote_cancel_root)
        self.assertEqual(Path("cache-manifest.json"), settings.colab_cache_manifest_path)
        self.assertEqual(
            "/content/drive/MyDrive/melqivra/cache-key",
            settings.colab_remote_cache_root,
        )
        self.assertEqual("melqivra-t4", settings.colab_session_name)

    def test_unsafe_concurrency_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "GPU_MAX_CONCURRENCY=1"):
            HarnessSettings(gpu_max_concurrency=2).validate()

    def test_invalid_runner_and_non_positive_timeouts_are_rejected(self) -> None:
        invalid_settings = (
            HarnessSettings(runner="unknown"),
            HarnessSettings(idle_timeout_seconds=0),
            HarnessSettings(scheduler_poll_seconds=0),
            HarnessSettings(job_timeout_seconds=0),
            HarnessSettings(modal_command_timeout_seconds=0),
            HarnessSettings(modal_cli_version=""),
            HarnessSettings(modal_app_name="Invalid_Name"),
            HarnessSettings(colab_command_timeout_seconds=0),
            HarnessSettings(colab_cli_version=""),
            HarnessSettings(colab_remote_cancel_root="relative/path"),
            HarnessSettings(colab_cache_manifest_path=Path("cache.json")),
            HarnessSettings(colab_remote_cache_root="/content/drive/cache"),
            HarnessSettings(
                colab_cache_manifest_path=Path("cache.json"),
                colab_remote_cache_root="/tmp/cache",
            ),
        )
        for settings in invalid_settings:
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    settings.validate()
