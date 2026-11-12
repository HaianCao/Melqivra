"""Modal T4 implementation of the GPU runner strategy."""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from agent_music.config import HarnessSettings
from agent_music.models import GpuJob, RunnerState
from agent_music.runners.base import EventCallback, GpuRunner
from agent_music.runners.modal_cli import CommandResult, LineCallback, ModalCliClient


class ModalClient(Protocol):
    """Minimal client boundary used by the Modal runner and deterministic tests."""

    def token_info_argv(self) -> list[str]: ...

    def run_job_argv(
        self,
        runner_file: Path,
        request_path: Path,
        result_path: Path,
    ) -> list[str]: ...

    def stop_argv(self) -> list[str]: ...

    async def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        on_stdout: LineCallback | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult: ...

    async def cancel_current(self) -> None: ...


class ModalRunner(GpuRunner):
    """Run bounded Modal jobs, reserving T4 allocation for GPU job kinds."""

    def __init__(
        self,
        settings: HarnessSettings,
        client: ModalClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or ModalCliClient(
            executable=settings.modal_executable,
            app_name=settings.modal_app_name,
            timeout_seconds=settings.modal_command_timeout_seconds,
        )
        self._state = RunnerState.STOPPED
        self._cancel_requested: set[str] = set()

    @property
    def state(self) -> RunnerState:
        return self._state

    @property
    def session_name(self) -> str | None:
        if self._state != RunnerState.BUSY:
            return None
        return self._settings.modal_app_name

    async def ensure_ready(self) -> None:
        if self._state in {RunnerState.READY, RunnerState.BUSY}:
            return
        if not self._settings.modal_runner_file.is_file():
            raise FileNotFoundError(self._settings.modal_runner_file)
        self._state = RunnerState.STARTING
        try:
            await self._client.run(self._client.token_info_argv())
            self._state = RunnerState.READY
        except BaseException:
            self._state = RunnerState.FAILED
            raise

    async def run(self, job: GpuJob, on_event: EventCallback) -> dict[str, Any]:
        if self._state != RunnerState.READY:
            raise RuntimeError("Modal runner is not ready")

        self._state = RunnerState.BUSY
        try:
            with tempfile.TemporaryDirectory(prefix="melqivra-modal-job-") as temp_dir:
                temp_path = Path(temp_dir)
                request_path = temp_path / "job.json"
                result_path = temp_path / "result.json"
                request_path.write_text(
                    json.dumps(
                        {
                            "job_id": job.id,
                            "kind": job.kind,
                            "payload": job.payload,
                            "output_root": self._settings.modal_remote_output_root,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                await on_event({"type": "stage", "stage": "submitting", "progress": 0.05})

                async def forward_line(line: str) -> None:
                    if job.id in self._cancel_requested or not line.startswith("{"):
                        return
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        return
                    if isinstance(event, dict):
                        await on_event(event)

                try:
                    await self._client.run(
                        self._client.run_job_argv(
                            self._settings.modal_runner_file,
                            request_path,
                            result_path,
                        ),
                        on_stdout=forward_line,
                    )
                except Exception:
                    if job.id in self._cancel_requested:
                        raise asyncio.CancelledError from None
                    raise
                if job.id in self._cancel_requested:
                    raise asyncio.CancelledError
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
        self._cancel_requested.add(job_id)
        await self._client.cancel_current()
        await self._client.run(self._client.stop_argv(), check=False)

    async def stop(self) -> None:
        if self._state == RunnerState.STOPPED:
            return
        self._state = RunnerState.STOPPING
        try:
            await self._client.cancel_current()
            await self._client.run(self._client.stop_argv(), check=False)
        finally:
            self._state = RunnerState.STOPPED
