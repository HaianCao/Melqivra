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
        self.assertEqual("filesystem", settings.artifact_storage)
        self.assertEqual("melqivra-gpu-runner", settings.modal_app_name)

    def test_environment_values_are_parsed_explicitly(self) -> None:
        environment = {
            "GPU_RUNNER": "COLAB",
            "JOB_STORE": "POSTGRES",
            "POSTGRES_DSN": "postgresql://database/melqivra",
            "POSTGRES_POOL_MIN_SIZE": "2",
            "POSTGRES_POOL_MAX_SIZE": "6",
            "POSTGRES_CONNECT_TIMEOUT_SECONDS": "12",
            "ARTIFACT_STORAGE": "S3",
            "S3_ENDPOINT_URL": "http://minio:9000",
            "S3_BUCKET": "melqivra-artifacts",
            "S3_REGION": "ap-southeast-1",
            "S3_CREATE_BUCKET": "yes",
            "S3_ADDRESSING_STYLE": "PATH",
            "S3_CONNECT_TIMEOUT_SECONDS": "8",
            "S3_READ_TIMEOUT_SECONDS": "45",
            "S3_MAX_ATTEMPTS": "4",
            "ARTIFACT_PENDING_TIMEOUT_SECONDS": "120",
            "ARTIFACT_CORRUPT_RETENTION_SECONDS": "240",
            "ARTIFACT_ORPHAN_GRACE_SECONDS": "360",
            "ARTIFACT_PUBLISHED_RETENTION_SECONDS": "7200",
            "ARTIFACT_RECONCILE_BATCH_LIMIT": "25",
            "ARTIFACT_RECONCILE_OBJECT_PAGE_SIZE": "50",
            "SERVICE_ARCHIVE_MODE": "DOCKER",
            "SERVICE_ARCHIVE_TIMEOUT_SECONDS": "1800",
            "SERVICE_ARCHIVE_MAX_DATABASE_BYTES": "1000000",
            "SERVICE_ARCHIVE_MAX_OBJECTS": "900",
            "SERVICE_ARCHIVE_MAX_TOTAL_OBJECT_BYTES": "2000000",
            "SERVICE_ARCHIVE_OBJECT_PAGE_SIZE": "75",
            "POSTGRES_DUMP_EXECUTABLE": "pg_dump-custom",
            "POSTGRES_RESTORE_EXECUTABLE": "pg_restore-custom",
            "DOCKER_EXECUTABLE": "docker-custom",
            "POSTGRES_ARCHIVE_DOCKER_IMAGE": "postgres:17@sha256:abc123",
            "GPU_MAX_CONCURRENCY": "1",
            "GPU_IDLE_TIMEOUT_SECONDS": "301",
            "GPU_JOB_LEASE_SECONDS": "90",
            "GPU_JOB_HEARTBEAT_SECONDS": "20",
            "GPU_JOB_MAX_ATTEMPTS": "5",
            "API_AUTH_MODE": "INJECTED",
            "API_MAX_ACTIVE_JOBS_PER_USER": "7",
            "API_MAX_JOB_PAYLOAD_BYTES": "12345",
            "API_MAX_PAGE_SIZE": "50",
            "API_SSE_POLL_SECONDS": "0.5",
            "API_SSE_KEEPALIVE_SECONDS": "12",
            "API_SSE_BATCH_SIZE": "40",
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
        self.assertEqual("s3", settings.artifact_storage)
        self.assertEqual("http://minio:9000", settings.s3_endpoint_url)
        self.assertEqual("melqivra-artifacts", settings.s3_bucket)
        self.assertEqual("ap-southeast-1", settings.s3_region)
        self.assertTrue(settings.s3_create_bucket)
        self.assertEqual("path", settings.s3_addressing_style)
        self.assertEqual(8.0, settings.s3_connect_timeout_seconds)
        self.assertEqual(45.0, settings.s3_read_timeout_seconds)
        self.assertEqual(4, settings.s3_max_attempts)
        self.assertEqual(120.0, settings.artifact_pending_timeout_seconds)
        self.assertEqual(240.0, settings.artifact_corrupt_retention_seconds)
        self.assertEqual(360.0, settings.artifact_orphan_grace_seconds)
        self.assertEqual(7_200.0, settings.artifact_published_retention_seconds)
        self.assertEqual(25, settings.artifact_reconcile_batch_limit)
        self.assertEqual(50, settings.artifact_reconcile_object_page_size)
        self.assertEqual("docker", settings.service_archive_mode)
        self.assertEqual(1_800.0, settings.service_archive_timeout_seconds)
        self.assertEqual(1_000_000, settings.service_archive_max_database_bytes)
        self.assertEqual(900, settings.service_archive_max_objects)
        self.assertEqual(2_000_000, settings.service_archive_max_total_object_bytes)
        self.assertEqual(75, settings.service_archive_object_page_size)
        self.assertEqual("pg_dump-custom", settings.postgres_dump_executable)
        self.assertEqual("pg_restore-custom", settings.postgres_restore_executable)
        self.assertEqual("docker-custom", settings.docker_executable)
        self.assertEqual("postgres:17@sha256:abc123", settings.postgres_archive_docker_image)
        self.assertEqual(301.0, settings.idle_timeout_seconds)
        self.assertEqual(90.0, settings.job_lease_seconds)
        self.assertEqual(20.0, settings.job_heartbeat_seconds)
        self.assertEqual(5, settings.job_max_attempts)
        self.assertEqual("injected", settings.api_auth_mode)
        self.assertEqual(7, settings.api_max_active_jobs_per_user)
        self.assertEqual(12_345, settings.api_max_job_payload_bytes)
        self.assertEqual(50, settings.api_max_page_size)
        self.assertEqual(0.5, settings.api_sse_poll_seconds)
        self.assertEqual(12.0, settings.api_sse_keepalive_seconds)
        self.assertEqual(40, settings.api_sse_batch_size)
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
            HarnessSettings(artifact_storage="unknown"),
            HarnessSettings(artifact_storage="s3"),
            HarnessSettings(
                job_store="postgres",
                postgres_dsn="postgresql://database/melqivra",
                artifact_storage="s3",
            ),
            HarnessSettings(s3_region=""),
            HarnessSettings(s3_addressing_style="unknown"),
            HarnessSettings(s3_connect_timeout_seconds=0),
            HarnessSettings(s3_read_timeout_seconds=0),
            HarnessSettings(s3_max_attempts=0),
            HarnessSettings(s3_max_attempts=11),
            HarnessSettings(artifact_pending_timeout_seconds=-1),
            HarnessSettings(artifact_corrupt_retention_seconds=float("inf")),
            HarnessSettings(artifact_orphan_grace_seconds=-1),
            HarnessSettings(artifact_published_retention_seconds=-1),
            HarnessSettings(artifact_reconcile_batch_limit=0),
            HarnessSettings(artifact_reconcile_object_page_size=1_001),
            HarnessSettings(service_archive_mode="unknown"),
            HarnessSettings(service_archive_timeout_seconds=0),
            HarnessSettings(service_archive_max_database_bytes=0),
            HarnessSettings(service_archive_max_objects=0),
            HarnessSettings(service_archive_max_total_object_bytes=0),
            HarnessSettings(service_archive_object_page_size=1_001),
            HarnessSettings(postgres_dump_executable=""),
            HarnessSettings(postgres_restore_executable=""),
            HarnessSettings(docker_executable=""),
            HarnessSettings(
                service_archive_mode="docker",
                postgres_archive_docker_image="postgres:17",
            ),
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
            HarnessSettings(api_sse_poll_seconds=0),
            HarnessSettings(api_sse_poll_seconds=2, api_sse_keepalive_seconds=1),
            HarnessSettings(api_sse_batch_size=0),
            HarnessSettings(api_sse_batch_size=101),
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
