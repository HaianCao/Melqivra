"""Configuration for the GPU harness."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_MODAL_RUNNER_FILE = PACKAGE_ROOT / "remote" / "modal_app.py"
DEFAULT_REMOTE_WORKER_FILE = PACKAGE_ROOT / "remote" / "worker.py"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_optional_float(name: str) -> float | None:
    value = os.getenv(name, "").strip()
    return float(value) if value else None


@dataclass(frozen=True, slots=True)
class HarnessSettings:
    """Validated runtime settings.

    The five-minute idle shutdown is the production default. Tests may inject
    a shorter timeout without changing environment state.
    """

    database_path: Path = Path("data/harness.db")
    job_store: str = "sqlite"
    postgres_dsn: str = ""
    postgres_pool_min_size: int = 1
    postgres_pool_max_size: int = 4
    postgres_connect_timeout_seconds: float = 10.0
    artifact_root: Path = Path("data/artifacts")
    artifact_storage: str = "filesystem"
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_create_bucket: bool = False
    s3_addressing_style: str = "auto"
    s3_connect_timeout_seconds: float = 10.0
    s3_read_timeout_seconds: float = 60.0
    s3_max_attempts: int = 3
    artifact_pending_timeout_seconds: float = 3_600.0
    artifact_corrupt_retention_seconds: float = 86_400.0
    artifact_orphan_grace_seconds: float = 86_400.0
    artifact_published_retention_seconds: float | None = None
    artifact_reconcile_batch_limit: int = 100
    artifact_reconcile_object_page_size: int = 100
    service_archive_mode: str = "local"
    service_archive_timeout_seconds: float = 3_600.0
    service_archive_max_database_bytes: int = 1_099_511_627_776
    service_archive_max_objects: int = 100_000
    service_archive_max_total_object_bytes: int = 1_099_511_627_776
    service_archive_object_page_size: int = 1_000
    postgres_dump_executable: str = "pg_dump"
    postgres_restore_executable: str = "pg_restore"
    docker_executable: str = "docker"
    postgres_archive_docker_image: str = (
        "postgres:17-alpine@sha256:d3244e8ce3f165ee3c93d9c4a4b1d0fe48b3a89e2287b717fdcbbde92d405065"
    )
    transfer_root: Path = Path("data/transfers")
    runner: str = "modal"
    gpu_max_concurrency: int = 1
    idle_timeout_seconds: float = 300.0
    scheduler_poll_seconds: float = 0.5
    job_timeout_seconds: float = 1_800.0
    job_lease_seconds: float = 60.0
    job_heartbeat_seconds: float = 15.0
    job_max_attempts: int = 3
    api_auth_mode: str = "development"
    api_embedded_worker: bool = True
    api_max_active_jobs_per_user: int = 10
    api_max_job_payload_bytes: int = 65_536
    api_max_request_body_bytes: int = 131_072
    api_max_page_size: int = 100
    api_rate_limit_requests: int = 120
    api_rate_limit_window_seconds: float = 60.0
    api_sse_poll_seconds: float = 0.25
    api_sse_keepalive_seconds: float = 15.0
    api_sse_batch_size: int = 100

    modal_executable: str = "modal"
    modal_cli_version: str = "1.6.3"
    modal_app_name: str = "melqivra-gpu-runner"
    modal_runner_file: Path = DEFAULT_MODAL_RUNNER_FILE
    modal_remote_output_root: str = "/tmp/agent_music_output"
    modal_artifact_volume: str = ""
    modal_artifact_prefix: str = "runs"
    modal_command_timeout_seconds: float = 1_800.0

    colab_executable: str = "colab"
    colab_cli_version: str = "0.8.0"
    colab_session_name: str = "melqivra-t4"
    colab_gpu: str = "T4"
    colab_auth_provider: str = "oauth2"
    colab_cli_config: Path = Path("data/colab/sessions.json")
    colab_mount_drive: bool = True
    colab_drive_mount_path: str = "/content/drive"
    colab_remote_request_path: str = "/content/agent_music_job.json"
    colab_remote_result_path: str = "/content/agent_music_result.json"
    colab_remote_output_root: str = "/content/agent_music_output"
    colab_remote_cancel_root: str = "/content/agent_music_control"
    colab_cache_manifest_path: Path | None = None
    colab_remote_cache_root: str = ""
    colab_remote_worker_file: Path = DEFAULT_REMOTE_WORKER_FILE
    colab_command_timeout_seconds: float = 900.0

    fake_job_seconds: float = 0.05

    @classmethod
    def from_env(cls) -> HarnessSettings:
        """Load settings from environment variables."""

        colab_cache_manifest = os.getenv("COLAB_CACHE_MANIFEST_PATH", "").strip()
        settings = cls(
            database_path=Path(os.getenv("HARNESS_DATABASE_PATH", "data/harness.db")),
            job_store=os.getenv("JOB_STORE", "sqlite").strip().lower(),
            postgres_dsn=os.getenv("POSTGRES_DSN", "").strip(),
            postgres_pool_min_size=int(os.getenv("POSTGRES_POOL_MIN_SIZE", "1")),
            postgres_pool_max_size=int(os.getenv("POSTGRES_POOL_MAX_SIZE", "4")),
            postgres_connect_timeout_seconds=float(
                os.getenv("POSTGRES_CONNECT_TIMEOUT_SECONDS", "10")
            ),
            artifact_root=Path(os.getenv("ARTIFACT_ROOT", "data/artifacts")),
            artifact_storage=os.getenv("ARTIFACT_STORAGE", "filesystem").strip().lower(),
            s3_endpoint_url=os.getenv("S3_ENDPOINT_URL", "").strip(),
            s3_bucket=os.getenv("S3_BUCKET", "").strip(),
            s3_region=os.getenv("S3_REGION", "us-east-1").strip(),
            s3_create_bucket=_env_bool("S3_CREATE_BUCKET", False),
            s3_addressing_style=os.getenv("S3_ADDRESSING_STYLE", "auto").strip().lower(),
            s3_connect_timeout_seconds=float(os.getenv("S3_CONNECT_TIMEOUT_SECONDS", "10")),
            s3_read_timeout_seconds=float(os.getenv("S3_READ_TIMEOUT_SECONDS", "60")),
            s3_max_attempts=int(os.getenv("S3_MAX_ATTEMPTS", "3")),
            artifact_pending_timeout_seconds=float(
                os.getenv("ARTIFACT_PENDING_TIMEOUT_SECONDS", "3600")
            ),
            artifact_corrupt_retention_seconds=float(
                os.getenv("ARTIFACT_CORRUPT_RETENTION_SECONDS", "86400")
            ),
            artifact_orphan_grace_seconds=float(
                os.getenv("ARTIFACT_ORPHAN_GRACE_SECONDS", "86400")
            ),
            artifact_published_retention_seconds=_env_optional_float(
                "ARTIFACT_PUBLISHED_RETENTION_SECONDS"
            ),
            artifact_reconcile_batch_limit=int(os.getenv("ARTIFACT_RECONCILE_BATCH_LIMIT", "100")),
            artifact_reconcile_object_page_size=int(
                os.getenv("ARTIFACT_RECONCILE_OBJECT_PAGE_SIZE", "100")
            ),
            service_archive_mode=os.getenv("SERVICE_ARCHIVE_MODE", "local").strip().lower(),
            service_archive_timeout_seconds=float(
                os.getenv("SERVICE_ARCHIVE_TIMEOUT_SECONDS", "3600")
            ),
            service_archive_max_database_bytes=int(
                os.getenv("SERVICE_ARCHIVE_MAX_DATABASE_BYTES", "1099511627776")
            ),
            service_archive_max_objects=int(os.getenv("SERVICE_ARCHIVE_MAX_OBJECTS", "100000")),
            service_archive_max_total_object_bytes=int(
                os.getenv("SERVICE_ARCHIVE_MAX_TOTAL_OBJECT_BYTES", "1099511627776")
            ),
            service_archive_object_page_size=int(
                os.getenv("SERVICE_ARCHIVE_OBJECT_PAGE_SIZE", "1000")
            ),
            postgres_dump_executable=os.getenv("POSTGRES_DUMP_EXECUTABLE", "pg_dump"),
            postgres_restore_executable=os.getenv("POSTGRES_RESTORE_EXECUTABLE", "pg_restore"),
            docker_executable=os.getenv("DOCKER_EXECUTABLE", "docker"),
            postgres_archive_docker_image=os.getenv(
                "POSTGRES_ARCHIVE_DOCKER_IMAGE",
                (
                    "postgres:17-alpine@sha256:"
                    "d3244e8ce3f165ee3c93d9c4a4b1d0fe48b3a89e2287b717fdcbbde92d405065"
                ),
            ).strip(),
            transfer_root=Path(os.getenv("TRANSFER_ROOT", "data/transfers")),
            runner=os.getenv("GPU_RUNNER", "modal").strip().lower(),
            gpu_max_concurrency=int(os.getenv("GPU_MAX_CONCURRENCY", "1")),
            idle_timeout_seconds=float(os.getenv("GPU_IDLE_TIMEOUT_SECONDS", "300")),
            scheduler_poll_seconds=float(os.getenv("GPU_SCHEDULER_POLL_SECONDS", "0.5")),
            job_timeout_seconds=float(os.getenv("GPU_JOB_TIMEOUT_SECONDS", "1800")),
            job_lease_seconds=float(os.getenv("GPU_JOB_LEASE_SECONDS", "60")),
            job_heartbeat_seconds=float(os.getenv("GPU_JOB_HEARTBEAT_SECONDS", "15")),
            job_max_attempts=int(os.getenv("GPU_JOB_MAX_ATTEMPTS", "3")),
            api_auth_mode=os.getenv("API_AUTH_MODE", "development").strip().lower(),
            api_embedded_worker=_env_bool("API_EMBEDDED_WORKER", True),
            api_max_active_jobs_per_user=int(os.getenv("API_MAX_ACTIVE_JOBS_PER_USER", "10")),
            api_max_job_payload_bytes=int(os.getenv("API_MAX_JOB_PAYLOAD_BYTES", "65536")),
            api_max_request_body_bytes=int(os.getenv("API_MAX_REQUEST_BODY_BYTES", "131072")),
            api_max_page_size=int(os.getenv("API_MAX_PAGE_SIZE", "100")),
            api_rate_limit_requests=int(os.getenv("API_RATE_LIMIT_REQUESTS", "120")),
            api_rate_limit_window_seconds=float(os.getenv("API_RATE_LIMIT_WINDOW_SECONDS", "60")),
            api_sse_poll_seconds=float(os.getenv("API_SSE_POLL_SECONDS", "0.25")),
            api_sse_keepalive_seconds=float(os.getenv("API_SSE_KEEPALIVE_SECONDS", "15")),
            api_sse_batch_size=int(os.getenv("API_SSE_BATCH_SIZE", "100")),
            modal_executable=os.getenv("MODAL_EXECUTABLE", "modal"),
            modal_cli_version=os.getenv("MODAL_CLI_VERSION", "1.6.3").strip(),
            modal_app_name=os.getenv("MODAL_APP_NAME", "melqivra-gpu-runner"),
            modal_runner_file=Path(os.getenv("MODAL_RUNNER_FILE", str(DEFAULT_MODAL_RUNNER_FILE))),
            modal_remote_output_root=os.getenv(
                "MODAL_REMOTE_OUTPUT_ROOT", "/tmp/agent_music_output"
            ),
            modal_artifact_volume=os.getenv("MODAL_ARTIFACT_VOLUME", "").strip(),
            modal_artifact_prefix=os.getenv("MODAL_ARTIFACT_PREFIX", "runs").strip("/"),
            modal_command_timeout_seconds=float(os.getenv("MODAL_COMMAND_TIMEOUT_SECONDS", "1800")),
            colab_executable=os.getenv("COLAB_EXECUTABLE", "colab"),
            colab_cli_version=os.getenv("COLAB_CLI_VERSION", "0.8.0").strip(),
            colab_session_name=os.getenv("COLAB_SESSION_NAME", "melqivra-t4"),
            colab_gpu=os.getenv("COLAB_GPU", "T4"),
            colab_auth_provider=os.getenv("COLAB_AUTH_PROVIDER", "oauth2"),
            colab_cli_config=Path(os.getenv("COLAB_CLI_CONFIG", "data/colab/sessions.json")),
            colab_mount_drive=_env_bool("COLAB_MOUNT_DRIVE", True),
            colab_drive_mount_path=os.getenv("COLAB_DRIVE_MOUNT_PATH", "/content/drive"),
            colab_remote_request_path=os.getenv(
                "COLAB_REMOTE_REQUEST_PATH", "/content/agent_music_job.json"
            ),
            colab_remote_result_path=os.getenv(
                "COLAB_REMOTE_RESULT_PATH", "/content/agent_music_result.json"
            ),
            colab_remote_output_root=os.getenv(
                "COLAB_REMOTE_OUTPUT_ROOT", "/content/agent_music_output"
            ),
            colab_remote_cancel_root=os.getenv(
                "COLAB_REMOTE_CANCEL_ROOT", "/content/agent_music_control"
            ),
            colab_cache_manifest_path=(
                Path(colab_cache_manifest) if colab_cache_manifest else None
            ),
            colab_remote_cache_root=os.getenv("COLAB_REMOTE_CACHE_ROOT", "").strip(),
            colab_remote_worker_file=Path(
                os.getenv(
                    "COLAB_REMOTE_WORKER_FILE",
                    str(DEFAULT_REMOTE_WORKER_FILE),
                )
            ),
            colab_command_timeout_seconds=float(os.getenv("COLAB_COMMAND_TIMEOUT_SECONDS", "900")),
            fake_job_seconds=float(os.getenv("FAKE_GPU_JOB_SECONDS", "0.05")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """Reject unsafe or unsupported combinations early."""

        if self.runner not in {"modal", "colab", "fake", "local"}:
            raise ValueError("GPU_RUNNER must be modal, colab, fake, or local")
        if self.job_store not in {"sqlite", "postgres"}:
            raise ValueError("JOB_STORE must be sqlite or postgres")
        if self.job_store == "postgres" and not self.postgres_dsn:
            raise ValueError("POSTGRES_DSN is required when JOB_STORE=postgres")
        if self.postgres_pool_min_size < 1:
            raise ValueError("POSTGRES_POOL_MIN_SIZE must be at least 1")
        if self.postgres_pool_max_size < self.postgres_pool_min_size:
            raise ValueError("POSTGRES_POOL_MAX_SIZE must be at least the minimum")
        if self.postgres_connect_timeout_seconds <= 0:
            raise ValueError("POSTGRES_CONNECT_TIMEOUT_SECONDS must be positive")
        if self.artifact_storage not in {"filesystem", "s3"}:
            raise ValueError("ARTIFACT_STORAGE must be filesystem or s3")
        if self.artifact_storage == "s3":
            if self.job_store != "postgres" or not self.postgres_dsn:
                raise ValueError("ARTIFACT_STORAGE=s3 requires JOB_STORE=postgres")
            if not self.s3_bucket:
                raise ValueError("S3_BUCKET is required when ARTIFACT_STORAGE=s3")
        if not self.s3_region:
            raise ValueError("S3_REGION must not be empty")
        if self.s3_addressing_style not in {"auto", "path", "virtual"}:
            raise ValueError("S3_ADDRESSING_STYLE must be auto, path, or virtual")
        if self.s3_connect_timeout_seconds <= 0 or self.s3_read_timeout_seconds <= 0:
            raise ValueError("S3 timeouts must be positive")
        if self.s3_max_attempts < 1 or self.s3_max_attempts > 10:
            raise ValueError("S3_MAX_ATTEMPTS must be between 1 and 10")
        artifact_durations = (
            self.artifact_pending_timeout_seconds,
            self.artifact_corrupt_retention_seconds,
            self.artifact_orphan_grace_seconds,
        )
        if any(not math.isfinite(value) or value < 0 for value in artifact_durations):
            raise ValueError("Artifact reconciliation durations must be finite and non-negative")
        if self.artifact_published_retention_seconds is not None and (
            not math.isfinite(self.artifact_published_retention_seconds)
            or self.artifact_published_retention_seconds < 0
        ):
            raise ValueError("ARTIFACT_PUBLISHED_RETENTION_SECONDS must be non-negative")
        if self.artifact_reconcile_batch_limit < 1 or self.artifact_reconcile_batch_limit > 1_000:
            raise ValueError("ARTIFACT_RECONCILE_BATCH_LIMIT must be between 1 and 1000")
        if (
            self.artifact_reconcile_object_page_size < 1
            or self.artifact_reconcile_object_page_size > 1_000
        ):
            raise ValueError("ARTIFACT_RECONCILE_OBJECT_PAGE_SIZE must be between 1 and 1000")
        if self.service_archive_mode not in {"local", "docker"}:
            raise ValueError("SERVICE_ARCHIVE_MODE must be local or docker")
        if (
            not math.isfinite(self.service_archive_timeout_seconds)
            or self.service_archive_timeout_seconds <= 0
        ):
            raise ValueError("SERVICE_ARCHIVE_TIMEOUT_SECONDS must be positive")
        if self.service_archive_max_database_bytes < 1:
            raise ValueError("SERVICE_ARCHIVE_MAX_DATABASE_BYTES must be at least 1")
        if self.service_archive_max_objects < 1:
            raise ValueError("SERVICE_ARCHIVE_MAX_OBJECTS must be at least 1")
        if self.service_archive_max_total_object_bytes < 1:
            raise ValueError("SERVICE_ARCHIVE_MAX_TOTAL_OBJECT_BYTES must be at least 1")
        if (
            self.service_archive_object_page_size < 1
            or self.service_archive_object_page_size > 1_000
        ):
            raise ValueError("SERVICE_ARCHIVE_OBJECT_PAGE_SIZE must be between 1 and 1000")
        if not self.postgres_dump_executable or not self.postgres_restore_executable:
            raise ValueError("PostgreSQL archive executables must not be empty")
        if not self.docker_executable:
            raise ValueError("DOCKER_EXECUTABLE must not be empty")
        if self.service_archive_mode == "docker" and (
            "@sha256:" not in self.postgres_archive_docker_image
            or self.postgres_archive_docker_image.endswith("@sha256:")
        ):
            raise ValueError("POSTGRES_ARCHIVE_DOCKER_IMAGE must be digest-pinned")
        if self.gpu_max_concurrency != 1:
            raise ValueError("This harness intentionally supports GPU_MAX_CONCURRENCY=1 only")
        if self.idle_timeout_seconds <= 0:
            raise ValueError("GPU_IDLE_TIMEOUT_SECONDS must be positive")
        if self.scheduler_poll_seconds <= 0:
            raise ValueError("GPU_SCHEDULER_POLL_SECONDS must be positive")
        if self.job_timeout_seconds <= 0:
            raise ValueError("GPU_JOB_TIMEOUT_SECONDS must be positive")
        if self.job_lease_seconds <= 0:
            raise ValueError("GPU_JOB_LEASE_SECONDS must be positive")
        if self.job_heartbeat_seconds <= 0:
            raise ValueError("GPU_JOB_HEARTBEAT_SECONDS must be positive")
        if self.job_heartbeat_seconds >= self.job_lease_seconds:
            raise ValueError("GPU_JOB_HEARTBEAT_SECONDS must be shorter than the lease")
        if self.job_max_attempts < 1:
            raise ValueError("GPU_JOB_MAX_ATTEMPTS must be at least 1")
        if self.api_auth_mode not in {"development", "injected"}:
            raise ValueError("API_AUTH_MODE must be development or injected")
        if self.api_max_active_jobs_per_user < 1:
            raise ValueError("API_MAX_ACTIVE_JOBS_PER_USER must be at least 1")
        if self.api_max_job_payload_bytes < 2:
            raise ValueError("API_MAX_JOB_PAYLOAD_BYTES must be at least 2")
        if (
            self.api_max_request_body_bytes < self.api_max_job_payload_bytes
            or self.api_max_request_body_bytes > 16_777_216
        ):
            raise ValueError(
                "API_MAX_REQUEST_BODY_BYTES must cover the job payload and not exceed 16 MiB"
            )
        if self.api_max_page_size < 1 or self.api_max_page_size > 100:
            raise ValueError("API_MAX_PAGE_SIZE must be between 1 and 100")
        if self.api_rate_limit_requests < 1 or self.api_rate_limit_requests > 1_000_000:
            raise ValueError("API_RATE_LIMIT_REQUESTS must be between 1 and 1000000")
        if (
            not math.isfinite(self.api_rate_limit_window_seconds)
            or self.api_rate_limit_window_seconds < 1
            or self.api_rate_limit_window_seconds > 3_600
        ):
            raise ValueError("API_RATE_LIMIT_WINDOW_SECONDS must be between 1 and 3600")
        if self.api_sse_poll_seconds <= 0:
            raise ValueError("API_SSE_POLL_SECONDS must be positive")
        if self.api_sse_keepalive_seconds < self.api_sse_poll_seconds:
            raise ValueError("API_SSE_KEEPALIVE_SECONDS must be at least the poll interval")
        if self.api_sse_batch_size < 1 or self.api_sse_batch_size > 100:
            raise ValueError("API_SSE_BATCH_SIZE must be between 1 and 100")
        if self.modal_command_timeout_seconds <= 0:
            raise ValueError("MODAL_COMMAND_TIMEOUT_SECONDS must be positive")
        if not self.modal_cli_version:
            raise ValueError("MODAL_CLI_VERSION must not be empty")
        modal_prefix_parts = self.modal_artifact_prefix.replace("\\", "/").split("/")
        if any(part in {"", ".", ".."} for part in modal_prefix_parts):
            raise ValueError("MODAL_ARTIFACT_PREFIX must be a safe relative path")
        if not self.modal_app_name or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in self.modal_app_name
        ):
            raise ValueError(
                "MODAL_APP_NAME must contain lowercase ASCII letters, digits, or hyphens"
            )
        if self.colab_command_timeout_seconds <= 0:
            raise ValueError("COLAB_COMMAND_TIMEOUT_SECONDS must be positive")
        if not self.colab_cli_version:
            raise ValueError("COLAB_CLI_VERSION must not be empty")
        cancel_root = self.colab_remote_cancel_root.replace("\\", "/").rstrip("/")
        if cancel_root != "/content/agent_music_control":
            raise ValueError("COLAB_REMOTE_CANCEL_ROOT must match the packaged worker control root")
        cache_configured = self.colab_cache_manifest_path is not None
        if cache_configured != bool(self.colab_remote_cache_root):
            raise ValueError(
                "COLAB_CACHE_MANIFEST_PATH and COLAB_REMOTE_CACHE_ROOT must be configured together"
            )
        if self.colab_remote_cache_root:
            cache_root = self.colab_remote_cache_root.replace("\\", "/").rstrip("/")
            if not cache_root.startswith("/content/drive/") or any(
                part in {"", ".", ".."} for part in cache_root.split("/")[1:]
            ):
                raise ValueError("COLAB_REMOTE_CACHE_ROOT must be a safe Drive path")

    def ensure_directories(self) -> None:
        """Create local state directories."""

        if self.job_store == "sqlite":
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if self.artifact_storage == "filesystem":
            self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.transfer_root.mkdir(parents=True, exist_ok=True)
        self.colab_cli_config.parent.mkdir(parents=True, exist_ok=True)
