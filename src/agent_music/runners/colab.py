"""Colab T4 implementation of the GPU runner."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from agent_music.config import HarnessSettings
from agent_music.models import GpuJob, RunnerState
from agent_music.runners.base import EventCallback, GpuRunner
from agent_music.runners.colab_cli import (
    ColabCliClient,
    ColabCliError,
    ColabCliFailureKind,
    validate_colab_cli_version,
)


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
        self._cache_verified = False
        self._preflight_complete = False
        self._last_readiness_metrics: dict[str, object] = {}
        self._provision_guard = asyncio.Lock()
        self._provision_task: asyncio.Task[None] | None = None
        self._stop_guard = asyncio.Lock()
        self._active_job_id: str | None = None
        self._active_cancel_path: str | None = None
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
        cancel_identity = f"{job.id}:{job.lease_token}"
        cancel_name = hashlib.sha256(cancel_identity.encode("utf-8")).hexdigest() + ".json"
        self._active_cancel_path = str(
            PurePosixPath(self._settings.colab_remote_cancel_root) / cancel_name
        )
        self._run_finished.clear()
        run_started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="melqivra-job-") as temp_dir:
                temp_path = Path(temp_dir)
                request_path = temp_path / "job.json"
                result_path = temp_path / "result.json"
                request: dict[str, Any] = {
                    "job_id": job.id,
                    "kind": job.kind,
                    "payload": job.payload,
                    "output_root": self._settings.colab_remote_output_root,
                    "result_path": self._settings.colab_remote_result_path,
                    "control": {"cancel_path": self._active_cancel_path},
                }
                cache_contract = self._cache_contract(job)
                if cache_contract is not None:
                    request["cache_contract"] = cache_contract
                request_path.write_text(
                    json.dumps(request, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                await on_event({"type": "stage", "stage": "uploading", "progress": 0.05})
                upload_started = time.monotonic()
                await self._client.run(
                    self._client.upload_argv(
                        self._settings.colab_session_name,
                        request_path,
                        self._settings.colab_remote_request_path,
                    )
                )
                upload_seconds = time.monotonic() - upload_started

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
                    execution_started = time.monotonic()
                    await self._client.run(
                        self._client.exec_file_argv(
                            self._settings.colab_session_name,
                            worker_file,
                        ),
                        on_stdout=forward_line,
                    )
                    execution_seconds = time.monotonic() - execution_started
                except ColabCliError:
                    if job.id in self._cancel_requested:
                        raise asyncio.CancelledError from None
                    raise
                if job.id in self._cancel_requested:
                    raise asyncio.CancelledError

                result_download_started = time.monotonic()
                await self._client.run(
                    self._client.download_argv(
                        self._settings.colab_session_name,
                        self._settings.colab_remote_result_path,
                        result_path,
                    )
                )
                result_download_seconds = time.monotonic() - result_download_started
                result = json.loads(result_path.read_text(encoding="utf-8"))
                if not isinstance(result, dict):
                    raise ValueError("Remote result must be a JSON object")
                if result.get("status") == "cancelled":
                    raise asyncio.CancelledError
                if result.get("status") != "succeeded":
                    raise RuntimeError(str(result.get("error", "Remote job failed")))
                if cache_contract is not None:
                    cache_result = result.get("cache")
                    if (
                        not isinstance(cache_result, dict)
                        or cache_result.get("status") != "verified"
                    ):
                        raise RuntimeError("Remote cache verification result is missing")
                    self._cache_verified = True
                artifact_transfer_seconds = 0.0
                if job.kind == "music.generate":
                    artifact_transfer_started = time.monotonic()
                    result["_transfer_id"] = await self._download_artifacts(job, result)
                    artifact_transfer_seconds = time.monotonic() - artifact_transfer_started
                    result["_environment"] = {
                        "provider": "colab",
                        "runner": "colab",
                        "session_name": self._settings.colab_session_name,
                    }
                if job.id in self._cancel_requested:
                    raise asyncio.CancelledError
                result["_lifecycle_metrics"] = {
                    "provider": "colab",
                    "readiness": dict(self._last_readiness_metrics),
                    "upload_seconds": upload_seconds,
                    "execution_seconds": execution_seconds,
                    "result_download_seconds": result_download_seconds,
                    "artifact_transfer_seconds": artifact_transfer_seconds,
                    "cache": result.get("cache"),
                    "total_seconds": time.monotonic() - run_started,
                }
                return result
        except Exception:
            if job.id in self._cancel_requested:
                raise asyncio.CancelledError from None
            raise
        finally:
            self._cancel_requested.discard(job.id)
            self._active_job_id = None
            self._active_cancel_path = None
            self._run_finished.set()
            if self._state == RunnerState.BUSY:
                self._state = RunnerState.READY

    async def cancel(self, job_id: str) -> None:
        self._cancel_requested.add(job_id)
        cancel_path = self._active_cancel_path
        await self._client.cancel_current()
        if cancel_path is not None:
            with tempfile.TemporaryDirectory(prefix="melqivra-cancel-") as temp_dir:
                marker_path = Path(temp_dir) / "cancel.json"
                marker_path.write_text(
                    json.dumps({"job_id": job_id}, sort_keys=True),
                    encoding="utf-8",
                )
                await self._client.run(
                    self._client.upload_argv(
                        self._settings.colab_session_name,
                        marker_path,
                        cancel_path,
                    )
                )

    async def stop(self) -> None:
        async with self._stop_guard:
            async with self._provision_guard:
                provision_task = self._provision_task
            if provision_task is not None:
                with contextlib.suppress(Exception):
                    await asyncio.shield(provision_task)

            if self._state in {RunnerState.ABSENT, RunnerState.RECLAIMED}:
                return
            drain_error: TimeoutError | None = None
            if self._state == RunnerState.BUSY:
                self._state = RunnerState.DRAINING
                if self._active_job_id is not None:
                    self._cancel_requested.add(self._active_job_id)
                await self._client.cancel_current()
                drain_waiter = asyncio.create_task(self._run_finished.wait())
                completed, _ = await asyncio.wait(
                    {drain_waiter},
                    timeout=self._settings.colab_command_timeout_seconds,
                )
                if not completed:
                    drain_waiter.cancel()
                    drain_error = TimeoutError("Colab job did not drain before reclaim")

            self._state = RunnerState.STOPPING
            try:
                if self._session_present:
                    await self._client.run(
                        self._client.stop_argv(self._settings.colab_session_name),
                        check=False,
                    )
            except BaseException:
                self._state = RunnerState.FAILED
                raise
            finally:
                self._session_present = False
                self._cache_verified = False
            if drain_error is not None:
                self._state = RunnerState.FAILED
                raise drain_error
            self._state = RunnerState.RECLAIMED

    async def _provision(self) -> None:
        started = time.monotonic()
        had_session = self._session_present
        version_seconds = 0.0
        provision_seconds = 0.0
        mount_seconds = 0.0
        try:
            if not self._preflight_complete:
                version_started = time.monotonic()
                version = await self._client.run(self._client.version_argv())
                validate_colab_cli_version(version.stdout, self._settings.colab_cli_version)
                self._preflight_complete = True
                version_seconds = time.monotonic() - version_started
            heartbeat_started = time.monotonic()
            status = await self._client.run(
                self._client.status_argv(self._settings.colab_session_name),
                check=False,
            )
            heartbeat_seconds = time.monotonic() - heartbeat_started
            if status.returncode == 0:
                self._session_present = True
                mode = "warm" if had_session else "attached"
            else:
                status_error = ColabCliError(
                    self._client.status_argv(self._settings.colab_session_name),
                    status,
                )
                if status_error.kind != ColabCliFailureKind.SESSION_MISSING:
                    raise status_error
                self._session_present = False
                self._cache_verified = False
                self._state = RunnerState.PROVISIONING
                provision_started = time.monotonic()
                await self._client.run(
                    self._client.new_argv(
                        self._settings.colab_session_name,
                        self._settings.colab_gpu,
                    )
                )
                provision_seconds = time.monotonic() - provision_started
                self._session_present = True
                mode = "recovered" if had_session else "cold"
                if self._settings.colab_mount_drive:
                    mount_started = time.monotonic()
                    await self._client.run(
                        self._client.drivemount_argv(
                            self._settings.colab_session_name,
                            self._settings.colab_drive_mount_path,
                        )
                    )
                    mount_seconds = time.monotonic() - mount_started
            self._last_readiness_metrics = {
                "mode": mode,
                "version_seconds": version_seconds,
                "heartbeat_seconds": heartbeat_seconds,
                "provision_seconds": provision_seconds,
                "drive_mount_seconds": mount_seconds,
                "total_seconds": time.monotonic() - started,
            }
            self._state = RunnerState.READY
        except BaseException:
            self._state = RunnerState.FAILED
            raise
        finally:
            current_task = asyncio.current_task()
            async with self._provision_guard:
                if self._provision_task is current_task:
                    self._provision_task = None

    def _cache_contract(self, job: GpuJob) -> dict[str, Any] | None:
        manifest_path = self._settings.colab_cache_manifest_path
        if job.kind != "music.generate" or manifest_path is None or self._cache_verified:
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("status") != "succeeded":
            raise ValueError("Colab cache manifest must be a successful object")
        cache_key = manifest.get("cache_key")
        source_revision = manifest.get("source_revision")
        raw_assets = manifest.get("verified_assets")
        if (
            not isinstance(cache_key, str)
            or not cache_key
            or not isinstance(source_revision, str)
            or not source_revision
            or not isinstance(raw_assets, list)
            or not raw_assets
        ):
            raise ValueError("Colab cache manifest metadata is invalid")
        assets: list[dict[str, object]] = []
        for raw in raw_assets:
            if not isinstance(raw, dict):
                raise ValueError("Colab cache manifest asset is invalid")
            asset_id = raw.get("id")
            relative_path = raw.get("relative_path")
            size_bytes = raw.get("size_bytes")
            checksum = raw.get("sha256")
            if (
                not isinstance(asset_id, str)
                or not isinstance(relative_path, str)
                or not isinstance(size_bytes, int)
                or isinstance(size_bytes, bool)
                or size_bytes < 0
                or not isinstance(checksum, str)
                or len(checksum) != 64
            ):
                raise ValueError("Colab cache manifest asset metadata is invalid")
            path = PurePosixPath(relative_path)
            if (
                path.is_absolute()
                or "\\" in relative_path
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ValueError("Colab cache manifest asset path is unsafe")
            try:
                int(checksum, 16)
            except ValueError:
                raise ValueError("Colab cache manifest checksum is invalid") from None
            assets.append(
                {
                    "id": asset_id,
                    "relative_path": relative_path,
                    "size_bytes": size_bytes,
                    "sha256": checksum.lower(),
                }
            )
        return {
            "cache_key": cache_key,
            "source_revision": source_revision,
            "root": self._settings.colab_remote_cache_root,
            "assets": assets,
        }

    async def _download_artifacts(self, job: GpuJob, result: dict[str, Any]) -> str:
        payload = result.get("result")
        if not isinstance(payload, dict):
            raise ValueError("Remote generation result must be an object")
        raw_artifacts = payload.get("artifacts")
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            raise ValueError("Remote generation result has no artifacts")

        artifacts: list[tuple[PurePosixPath, int, str]] = []
        observed: set[str] = set()
        for raw in raw_artifacts:
            if not isinstance(raw, dict):
                raise ValueError("Remote artifact declaration is invalid")
            path_text = raw.get("relative_path")
            size_bytes = raw.get("size_bytes")
            checksum = raw.get("sha256")
            if (
                not isinstance(path_text, str)
                or not isinstance(size_bytes, int)
                or isinstance(size_bytes, bool)
                or size_bytes < 0
                or not isinstance(checksum, str)
                or len(checksum) != 64
            ):
                raise ValueError("Remote artifact metadata is invalid")
            try:
                int(checksum, 16)
            except ValueError:
                raise ValueError("Remote artifact checksum is invalid") from None
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
            artifacts.append((path, size_bytes, checksum.lower()))

        transfer_id = "transfer_" + hashlib.sha256(job.id.encode("utf-8")).hexdigest()[:24]
        root = self._settings.transfer_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        destination = (root / transfer_id).resolve()
        if destination.parent != root:
            raise ValueError("Local transfer path escaped its root")
        if destination.exists():
            self._verify_transfer(destination, artifacts)
            return transfer_id

        temporary = root / f".{transfer_id}.tmp"
        if temporary.exists():
            shutil.rmtree(temporary)
        try:
            temporary.mkdir(parents=True)
            for relative_path, size_bytes, checksum in artifacts:
                local_path = temporary.joinpath(*relative_path.parts)
                local_path.parent.mkdir(parents=True, exist_ok=True)
                remote_path = str(
                    PurePosixPath(self._settings.colab_remote_output_root) / relative_path
                )
                await self._client.run(
                    self._client.download_argv(
                        self._settings.colab_session_name,
                        remote_path,
                        local_path,
                    )
                )
                if job.id in self._cancel_requested:
                    raise asyncio.CancelledError
                self._verify_artifact(local_path, size_bytes, checksum)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return transfer_id

    @classmethod
    def _verify_transfer(
        cls,
        root: Path,
        artifacts: list[tuple[PurePosixPath, int, str]],
    ) -> None:
        for relative_path, size_bytes, checksum in artifacts:
            cls._verify_artifact(root.joinpath(*relative_path.parts), size_bytes, checksum)

    @staticmethod
    def _verify_artifact(path: Path, size_bytes: int, checksum: str) -> None:
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size != size_bytes:
            raise ValueError(f"Transferred artifact size mismatch: {path.name}")
        hasher = hashlib.sha256()
        with path.open("rb") as artifact_file:
            while chunk := artifact_file.read(1024 * 1024):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if digest != checksum:
            raise ValueError(f"Transferred artifact checksum mismatch: {path.name}")
