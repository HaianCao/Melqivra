"""Colab T4 implementation of the GPU runner."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from agent_music.config import HarnessSettings
from agent_music.models import GpuJob, RunnerState
from agent_music.runners.base import EventCallback, GpuRunner
from agent_music.runners.colab_cli import ColabCliClient


class ColabRunner(GpuRunner):
    """On-demand named Colab session with a five-minute external idle policy."""

    def __init__(
        self,
        settings: HarnessSettings,
        client: ColabCliClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or ColabCliClient(
            executable=settings.colab_executable,
            auth_provider=settings.colab_auth_provider,
            config_path=settings.colab_cli_config,
            timeout_seconds=settings.colab_command_timeout_seconds,
        )
        self._state = RunnerState.STOPPED
        self._cancel_requested: set[str] = set()

    @property
    def state(self) -> RunnerState:
        return self._state

    @property
    def session_name(self) -> str | None:
        if self._state == RunnerState.STOPPED:
            return None
        return self._settings.colab_session_name

    async def ensure_ready(self) -> None:
        if self._state in {RunnerState.READY, RunnerState.BUSY}:
            return
        self._state = RunnerState.STARTING
        try:
            status = await self._client.run(
                self._client.status_argv(self._settings.colab_session_name),
                check=False,
            )
            if status.returncode != 0:
                await self._client.run(
                    self._client.new_argv(
                        self._settings.colab_session_name,
                        self._settings.colab_gpu,
                    )
                )
                if self._settings.colab_mount_drive:
                    await self._client.run(
                        self._client.drivemount_argv(
                            self._settings.colab_session_name,
                            self._settings.colab_drive_mount_path,
                        )
                    )
            self._state = RunnerState.READY
        except BaseException:
            self._state = RunnerState.FAILED
            raise

    async def run(self, job: GpuJob, on_event: EventCallback) -> dict[str, Any]:
        if self._state != RunnerState.READY:
            raise RuntimeError("Colab runner is not ready")
        worker_file = self._settings.colab_remote_worker_file
        if not worker_file.is_file():
            raise FileNotFoundError(worker_file)

        self._state = RunnerState.BUSY
        try:
            with tempfile.TemporaryDirectory(prefix="agent-music-job-") as temp_dir:
                temp_path = Path(temp_dir)
                request_path = temp_path / "job.json"
                result_path = temp_path / "result.json"
                request_path.write_text(
                    json.dumps(
                        {
                            "job_id": job.id,
                            "kind": job.kind,
                            "payload": job.payload,
                            "output_root": self._settings.colab_remote_output_root,
                            "result_path": self._settings.colab_remote_result_path,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                await on_event({"type": "stage", "stage": "uploading", "progress": 0.05})
                await self._client.run(
                    self._client.upload_argv(
                        self._settings.colab_session_name,
                        request_path,
                        self._settings.colab_remote_request_path,
                    )
                )

                async def forward_line(line: str) -> None:
                    if job.id in self._cancel_requested:
                        return
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = {"type": "log", "message": line}
                    if isinstance(event, dict):
                        await on_event(event)

                await self._client.run(
                    self._client.exec_file_argv(
                        self._settings.colab_session_name,
                        worker_file,
                    ),
                    on_stdout=forward_line,
                )
                if job.id in self._cancel_requested:
                    raise asyncio.CancelledError

                await self._client.run(
                    self._client.download_argv(
                        self._settings.colab_session_name,
                        self._settings.colab_remote_result_path,
                        result_path,
                    )
                )
                result = json.loads(result_path.read_text(encoding="utf-8"))
                if not isinstance(result, dict):
                    raise ValueError("Remote result must be a JSON object")
                if result.get("status") != "succeeded":
                    raise RuntimeError(str(result.get("error", "Remote job failed")))
                return result
        finally:
            if self._state != RunnerState.FAILED:
                self._state = RunnerState.READY

    async def cancel(self, job_id: str) -> None:
        # Colab exec does not currently expose a per-execution cancellation API.
        # Marking the job prevents a successful local commit. A future persistent
        # remote worker can consume a cancellation marker cooperatively.
        self._cancel_requested.add(job_id)

    async def stop(self) -> None:
        if self._state == RunnerState.STOPPED:
            return
        self._state = RunnerState.STOPPING
        try:
            await self._client.run(
                self._client.stop_argv(self._settings.colab_session_name),
                check=False,
            )
        finally:
            self._state = RunnerState.STOPPED
