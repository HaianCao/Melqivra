"""Configuration for the GPU harness."""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class HarnessSettings:
    """Validated runtime settings.

    The five-minute idle shutdown is the production default. Tests may inject
    a shorter timeout without changing environment state.
    """

    database_path: Path = Path("data/harness.db")
    artifact_root: Path = Path("data/artifacts")
    transfer_root: Path = Path("data/transfers")
    runner: str = "modal"
    gpu_max_concurrency: int = 1
    idle_timeout_seconds: float = 300.0
    scheduler_poll_seconds: float = 0.5
    job_timeout_seconds: float = 1_800.0

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
    colab_remote_worker_file: Path = DEFAULT_REMOTE_WORKER_FILE
    colab_command_timeout_seconds: float = 900.0

    fake_job_seconds: float = 0.05

    @classmethod
    def from_env(cls) -> HarnessSettings:
        """Load settings from environment variables."""

        settings = cls(
            database_path=Path(os.getenv("HARNESS_DATABASE_PATH", "data/harness.db")),
            artifact_root=Path(os.getenv("ARTIFACT_ROOT", "data/artifacts")),
            transfer_root=Path(os.getenv("TRANSFER_ROOT", "data/transfers")),
            runner=os.getenv("GPU_RUNNER", "modal").strip().lower(),
            gpu_max_concurrency=int(os.getenv("GPU_MAX_CONCURRENCY", "1")),
            idle_timeout_seconds=float(os.getenv("GPU_IDLE_TIMEOUT_SECONDS", "300")),
            scheduler_poll_seconds=float(os.getenv("GPU_SCHEDULER_POLL_SECONDS", "0.5")),
            job_timeout_seconds=float(os.getenv("GPU_JOB_TIMEOUT_SECONDS", "1800")),
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
        if self.gpu_max_concurrency != 1:
            raise ValueError("This harness intentionally supports GPU_MAX_CONCURRENCY=1 only")
        if self.idle_timeout_seconds <= 0:
            raise ValueError("GPU_IDLE_TIMEOUT_SECONDS must be positive")
        if self.scheduler_poll_seconds <= 0:
            raise ValueError("GPU_SCHEDULER_POLL_SECONDS must be positive")
        if self.job_timeout_seconds <= 0:
            raise ValueError("GPU_JOB_TIMEOUT_SECONDS must be positive")
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

    def ensure_directories(self) -> None:
        """Create local state directories."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.transfer_root.mkdir(parents=True, exist_ok=True)
        self.colab_cli_config.parent.mkdir(parents=True, exist_ok=True)
