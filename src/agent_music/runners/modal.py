"""Scale-to-zero Modal implementation of the execution-runner strategy."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from agent_music.config import HarnessSettings
from agent_music.models import GpuJob, RunnerState
from agent_music.runners.base import EventCallback, GpuRunner
from agent_music.runners.modal_cli import (
    CommandResult,
    LineCallback,
    ModalCliClient,
    validate_modal_cli_version,
)


class ModalClient(Protocol):
    """Minimal client boundary used by the Modal runner and deterministic tests."""

    def version_argv(self) -> list[str]: ...

    def token_info_argv(self) -> list[str]: ...

    def run_job_argv(
        self,
        runner_file: Path,
        request_path: Path,
        result_path: Path,
    ) -> list[str]: ...

    def stop_argv(self) -> list[str]: ...

    def volume_get_argv(
        self,
        volume_name: str,
        remote_path: str,
        local_directory: Path,
    ) -> list[str]: ...

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
        self._state = RunnerState.ABSENT
        self._preflight_complete = False
        self._readiness_guard = asyncio.Lock()
        self._readiness_task: asyncio.Task[None] | None = None
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
        async with self._readiness_guard:
            if self._state == RunnerState.BUSY:
                return
            task = self._readiness_task
            if task is None:
                if self._state not in {
                    RunnerState.ABSENT,
                    RunnerState.READY,
                    RunnerState.RECLAIMED,
                    RunnerState.FAILED,
                }:
                    raise RuntimeError(f"Cannot reconcile Modal runner from {self._state}")
                if self._state != RunnerState.READY:
                    self._state = RunnerState.PROVISIONING
                task = asyncio.create_task(
                    self._reconcile_readiness(),
                    name="melqivra-modal-readiness",
                )
                self._readiness_task = task

        await asyncio.shield(task)

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
                if job.kind == "music.generate":
                    result["_transfer_id"] = await self._download_artifacts(job, result)
                    result["_environment"] = {
                        "provider": "modal",
                        "runner": "modal",
                        "app_name": self._settings.modal_app_name,
                    }
                return result
        finally:
            if self._state != RunnerState.FAILED:
                self._state = RunnerState.READY

    async def cancel(self, job_id: str) -> None:
        self._cancel_requested.add(job_id)
        await self._client.cancel_current()
        await self._client.run(self._client.stop_argv(), check=False)

    async def stop(self) -> None:
        async with self._readiness_guard:
            readiness_task = self._readiness_task
        if readiness_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(readiness_task)
        if self._state in {RunnerState.ABSENT, RunnerState.RECLAIMED}:
            return
        self._state = RunnerState.STOPPING
        try:
            await self._client.cancel_current()
            await self._client.run(self._client.stop_argv(), check=False)
        finally:
            self._state = RunnerState.RECLAIMED

    async def _reconcile_readiness(self) -> None:
        try:
            if not self._settings.modal_runner_file.is_file():
                raise FileNotFoundError(self._settings.modal_runner_file)
            if not self._preflight_complete:
                version = await self._client.run(self._client.version_argv())
                validate_modal_cli_version(version.stdout, self._settings.modal_cli_version)
                self._preflight_complete = True
            await self._client.run(self._client.token_info_argv())
            self._state = RunnerState.READY
        except BaseException:
            self._state = RunnerState.FAILED
            raise
        finally:
            current_task = asyncio.current_task()
            async with self._readiness_guard:
                if self._readiness_task is current_task:
                    self._readiness_task = None

    async def _download_artifacts(self, job: GpuJob, result: dict[str, Any]) -> str:
        if not self._settings.modal_artifact_volume:
            raise RuntimeError("MODAL_ARTIFACT_VOLUME is required for music.generate")
        payload = result.get("result")
        if not isinstance(payload, dict):
            raise ValueError("Remote generation result must be an object")
        raw_artifacts = payload.get("artifacts")
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            raise ValueError("Remote generation result has no artifacts")

        relative_paths: list[PurePosixPath] = []
        observed: set[str] = set()
        for raw in raw_artifacts:
            if not isinstance(raw, dict) or not isinstance(raw.get("relative_path"), str):
                raise ValueError("Remote artifact path is invalid")
            path_text = str(raw["relative_path"])
            path = PurePosixPath(path_text)
            if (
                path.is_absolute()
                or "\\" in path_text
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ValueError("Remote artifact path escaped the transfer root")
            if path_text in observed:
                raise ValueError("Remote artifact paths must be unique")
            observed.add(path_text)
            relative_paths.append(path)

        transfer_id = "transfer_" + hashlib.sha256(job.id.encode("utf-8")).hexdigest()[:24]
        root = self._settings.transfer_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        destination = (root / transfer_id).resolve()
        if destination.parent != root:
            raise ValueError("Local transfer path escaped its root")
        if destination.exists():
            return transfer_id

        staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=root))
        try:
            for relative in relative_paths:
                local_parent = staging.joinpath(*relative.parts[:-1])
                local_parent.mkdir(parents=True, exist_ok=True)
                remote_path = str(PurePosixPath(self._settings.modal_artifact_prefix) / relative)
                await self._client.run(
                    self._client.volume_get_argv(
                        self._settings.modal_artifact_volume,
                        remote_path,
                        local_parent,
                    )
                )
                if not (staging.joinpath(*relative.parts)).is_file():
                    raise FileNotFoundError(str(relative))
            os.replace(staging, destination)
        finally:
            await asyncio.to_thread(_remove_tree, staging)
        return transfer_id


def _remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
