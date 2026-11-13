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
        self.assertEqual("sqlite", settings.job_store)
        self.assertEqual("melqivra-gpu-runner", settings.modal_app_name)

    def test_environment_values_are_parsed_explicitly(self) -> None:
        environment = {
            "GPU_RUNNER": "COLAB",
            "JOB_STORE": "POSTGRES",
            "POSTGRES_DSN": "postgresql://database/melqivra",
            "POSTGRES_POOL_MIN_SIZE": "2",
            "POSTGRES_POOL_MAX_SIZE": "6",
            "POSTGRES_CONNECT_TIMEOUT_SECONDS": "12",
            "GPU_MAX_CONCURRENCY": "1",
            "GPU_IDLE_TIMEOUT_SECONDS": "301",
            "GPU_JOB_LEASE_SECONDS": "90",
            "GPU_JOB_HEARTBEAT_SECONDS": "20",
            "GPU_JOB_MAX_ATTEMPTS": "5",
            "API_AUTH_MODE": "INJECTED",
            "API_MAX_ACTIVE_JOBS_PER_USER": "7",
            "API_MAX_JOB_PAYLOAD_BYTES": "12345",
            "API_MAX_PAGE_SIZE": "50",
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
        self.assertEqual("postgres", settings.job_store)
        self.assertEqual("postgresql://database/melqivra", settings.postgres_dsn)
        self.assertEqual(2, settings.postgres_pool_min_size)
        self.assertEqual(6, settings.postgres_pool_max_size)
        self.assertEqual(12.0, settings.postgres_connect_timeout_seconds)
        self.assertEqual(301.0, settings.idle_timeout_seconds)
        self.assertEqual(90.0, settings.job_lease_seconds)
        self.assertEqual(20.0, settings.job_heartbeat_seconds)
        self.assertEqual(5, settings.job_max_attempts)
        self.assertEqual("injected", settings.api_auth_mode)
        self.assertEqual(7, settings.api_max_active_jobs_per_user)
        self.assertEqual(12_345, settings.api_max_job_payload_bytes)
        self.assertEqual(50, settings.api_max_page_size)
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
            HarnessSettings(job_store="unknown"),
            HarnessSettings(job_store="postgres"),
            HarnessSettings(postgres_pool_min_size=0),
            HarnessSettings(postgres_pool_min_size=2, postgres_pool_max_size=1),
            HarnessSettings(postgres_connect_timeout_seconds=0),
            HarnessSettings(idle_timeout_seconds=0),
            HarnessSettings(scheduler_poll_seconds=0),
            HarnessSettings(job_timeout_seconds=0),
            HarnessSettings(job_lease_seconds=0),
            HarnessSettings(job_heartbeat_seconds=0),
            HarnessSettings(job_lease_seconds=10, job_heartbeat_seconds=10),
            HarnessSettings(job_max_attempts=0),
            HarnessSettings(api_auth_mode="unknown"),
            HarnessSettings(api_max_active_jobs_per_user=0),
            HarnessSettings(api_max_job_payload_bytes=1),
            HarnessSettings(api_max_page_size=0),
            HarnessSettings(api_max_page_size=101),
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
