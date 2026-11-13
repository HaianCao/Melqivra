"""Colab T4 implementation of the GPU runner."""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from agent_music.config import HarnessSettings
from agent_music.models import GpuJob, RunnerState
from agent_music.runners.base import EventCallback, GpuRunner
from agent_music.runners.colab_cli import ColabCliClient, ColabCliError


class ColabRunner(GpuRunner):
    """On-demand named Colab session with a five-minute external idle policy."""

    _RECONCILABLE_STATES: ClassVar[frozenset[RunnerState]] = frozenset(
        {
            RunnerState.ABSENT,
            RunnerState.READY,
            RunnerState.RECLAIMED,
            RunnerState.FAILED,
        }
    )

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
        self._state = RunnerState.ABSENT
        self._session_present = False
        self._provision_guard = asyncio.Lock()
        self._provision_task: asyncio.Task[None] | None = None
        self._stop_guard = asyncio.Lock()
        self._active_job_id: str | None = None
        self._run_finished = asyncio.Event()
        self._run_finished.set()
        self._cancel_requested: set[str] = set()

    @property
    def state(self) -> RunnerState:
        return self._state

    @property
    def session_name(self) -> str | None:
        return self._settings.colab_session_name if self._session_present else None

    async def ensure_ready(self) -> None:
        async with self._provision_guard:
            if self._state == RunnerState.BUSY:
                return
            task = self._provision_task
            if task is None:
                if self._state not in self._RECONCILABLE_STATES:
                    raise RuntimeError(f"Cannot reconcile Colab runner from {self._state}")
                if self._state != RunnerState.READY:
                    self._state = RunnerState.PROVISIONING
                task = asyncio.create_task(
                    self._provision(),
                    name="melqivra-colab-readiness",
                )
                self._provision_task = task

        await asyncio.shield(task)

    async def run(self, job: GpuJob, on_event: EventCallback) -> dict[str, Any]:
        if self._state != RunnerState.READY:
            raise RuntimeError("Colab runner is not ready")
        worker_file = self._settings.colab_remote_worker_file
        if not worker_file.is_file():
            raise FileNotFoundError(worker_file)

        self._state = RunnerState.BUSY
        self._active_job_id = job.id
        self._run_finished.clear()
        try:
            with tempfile.TemporaryDirectory(prefix="melqivra-job-") as temp_dir:
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

                try:
                    await self._client.run(
                        self._client.exec_file_argv(
                            self._settings.colab_session_name,
                            worker_file,
                        ),
                        on_stdout=forward_line,
                    )
                except ColabCliError:
                    if job.id in self._cancel_requested:
                        raise asyncio.CancelledError from None
                    raise
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
            self._cancel_requested.discard(job.id)
            self._active_job_id = None
            self._run_finished.set()
            if self._state == RunnerState.BUSY:
                self._state = RunnerState.READY

    async def cancel(self, job_id: str) -> None:
        self._cancel_requested.add(job_id)
        await self._client.cancel_current()

    async def stop(self) -> None:
        async with self._stop_guard:
            async with self._provision_guard:
                provision_task = self._provision_task
            if provision_task is not None:
                with contextlib.suppress(Exception):
                    await asyncio.shield(provision_task)

            if self._state in {RunnerState.ABSENT, RunnerState.RECLAIMED}:
                return
            if self._state == RunnerState.BUSY:
                self._state = RunnerState.DRAINING
                if self._active_job_id is not None:
                    self._cancel_requested.add(self._active_job_id)
                await self._client.cancel_current()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._run_finished.wait(),
                        timeout=self._settings.colab_command_timeout_seconds,
                    )

            self._state = RunnerState.STOPPING
            try:
                if self._session_present:
                    await self._client.run(
                        self._client.stop_argv(self._settings.colab_session_name),
                        check=False,
                    )
            finally:
                self._session_present = False
                self._state = RunnerState.RECLAIMED

    async def _provision(self) -> None:
        try:
            status = await self._client.run(
                self._client.status_argv(self._settings.colab_session_name),
                check=False,
            )
            if status.returncode == 0:
                self._session_present = True
            else:
                self._session_present = False
                self._state = RunnerState.PROVISIONING
                await self._client.run(
                    self._client.new_argv(
                        self._settings.colab_session_name,
                        self._settings.colab_gpu,
                    )
                )
                self._session_present = True
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
        finally:
            current_task = asyncio.current_task()
            async with self._provision_guard:
                if self._provision_task is current_task:
                    self._provision_task = None
