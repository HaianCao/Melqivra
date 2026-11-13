"""Colab runner cancellation and lifecycle tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path

from agent_music.config import HarnessSettings
from agent_music.models import GpuJob, JobState, RunnerState
from agent_music.runners.colab import ColabRunner
from agent_music.runners.colab_cli import (
    ColabCliCompatibilityError,
    ColabCliError,
    ColabCliFailureKind,
    CommandResult,
)
from agent_music.scheduler import GpuHarness


async def wait_until(predicate: Callable[[], bool], *, timeout_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Condition was not reached")
        await asyncio.sleep(0.005)


class BlockingColabClient:
    def __init__(self) -> None:
        self.exec_started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.uploaded_json: list[tuple[str, dict[str, object]]] = []

    def status_argv(self, session_name: str) -> list[str]:
        return ["status", session_name]

    def version_argv(self) -> list[str]:
        return ["version"]

    def new_argv(self, session_name: str, gpu: str) -> list[str]:
        return ["new", session_name, gpu]

    def drivemount_argv(self, session_name: str, mount_path: str) -> list[str]:
        return ["drivemount", session_name, mount_path]

    def upload_argv(self, session_name: str, local_path: Path, remote_path: str) -> list[str]:
        return ["upload", session_name, str(local_path), remote_path]

    def exec_file_argv(self, session_name: str, file_path: Path) -> list[str]:
        return ["exec", session_name, str(file_path)]

    def download_argv(self, session_name: str, remote_path: str, local_path: Path) -> list[str]:
        return ["download", session_name, remote_path, str(local_path)]

    def stop_argv(self, session_name: str) -> list[str]:
        return ["stop", session_name]

    async def run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        on_stdout: object | None = None,
    ) -> CommandResult:
        del check, on_stdout
        if argv[0] == "version":
            return CommandResult(0, "colab 0.8.0", "")
        if argv[0] == "upload":
            local_path = Path(argv[2])
            remote_path = argv[3]
            value = json.loads(await asyncio.to_thread(local_path.read_text, encoding="utf-8"))
            if isinstance(value, dict):
                self.uploaded_json.append((remote_path, value))
            return CommandResult(0, "", "")
        if argv[0] == "exec":
            self.exec_started.set()
            await self.cancelled.wait()
            raise ColabCliError(argv, CommandResult(130, "", "cancelled"))
        return CommandResult(0, "", "")

    async def cancel_current(self) -> None:
        self.cancelled.set()


class ProvisioningColabClient(BlockingColabClient):
    def __init__(
        self,
        *,
        status_returncodes: tuple[int, ...] = (1,),
        status_stderr: tuple[str, ...] | None = None,
        version_output: str = "colab 0.8.0",
    ) -> None:
        super().__init__()
        self.status_returncodes = list(status_returncodes)
        self.status_stderr = list(
            status_stderr
            if status_stderr is not None
            else tuple("" if code == 0 else "session not found" for code in status_returncodes)
        )
        self.version_output = version_output
        self.status_started = asyncio.Event()
        self.release_status = asyncio.Event()
        self.commands: list[str] = []

    async def run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        on_stdout: object | None = None,
    ) -> CommandResult:
        del check, on_stdout
        self.commands.append(argv[0])
        if argv[0] == "version":
            return CommandResult(0, self.version_output, "")
        if argv[0] == "status":
            self.status_started.set()
            await self.release_status.wait()
            returncode = self.status_returncodes.pop(0)
            stderr = self.status_stderr.pop(0)
            return CommandResult(returncode, "", stderr)
        return CommandResult(0, "", "")


class DrainingColabClient(BlockingColabClient):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_started = asyncio.Event()
        self.release_cancel = asyncio.Event()
        self.stop_started = asyncio.Event()
        self.release_stop = asyncio.Event()

    async def run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        on_stdout: object | None = None,
    ) -> CommandResult:
        if argv[0] == "stop":
            self.stop_started.set()
            await self.release_stop.wait()
            return CommandResult(0, "", "")
        return await super().run(argv, check=check, on_stdout=on_stdout)

    async def cancel_current(self) -> None:
        self.cancel_started.set()
        await self.release_cancel.wait()
        self.cancelled.set()


class ArtifactColabClient(BlockingColabClient):
    def __init__(self, *, tamper_download: bool = False) -> None:
        super().__init__()
        self.expected_audio = b"RIFF-colab-private"
        self.tamper_download = tamper_download
        self.uploaded_requests: list[dict[str, object]] = []

    async def run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        on_stdout: object | None = None,
    ) -> CommandResult:
        del check, on_stdout
        if argv[0] == "version":
            return CommandResult(0, "colab 0.8.0", "")
        if argv[0] in {"status", "exec", "stop"}:
            return CommandResult(0, "", "")
        if argv[0] == "upload":
            value = json.loads(await asyncio.to_thread(Path(argv[2]).read_text, encoding="utf-8"))
            if isinstance(value, dict):
                self.uploaded_requests.append(value)
            return CommandResult(0, "", "")
        if argv[0] == "download":
            remote_path = argv[2]
            local_path = Path(argv[3])
            await asyncio.to_thread(local_path.parent.mkdir, parents=True, exist_ok=True)
            if remote_path.endswith("agent_music_result.json"):
                record: dict[str, object] = {
                    "status": "succeeded",
                    "job_id": "job-colab-artifact",
                    "result": {
                        "artifacts": [
                            {
                                "relative_path": "request/take.wav",
                                "size_bytes": len(self.expected_audio),
                                "sha256": hashlib.sha256(self.expected_audio).hexdigest(),
                            }
                        ]
                    },
                }
                if self.uploaded_requests and "cache_contract" in self.uploaded_requests[-1]:
                    record["cache"] = {"status": "verified", "cache_key": "cache-key"}
                await asyncio.to_thread(
                    local_path.write_text,
                    json.dumps(record),
                    encoding="utf-8",
                )
            else:
                payload = b"tampered" if self.tamper_download else self.expected_audio
                await asyncio.to_thread(local_path.write_bytes, payload)
            return CommandResult(0, "", "")
        raise AssertionError(f"Unexpected command: {argv}")


class ColabRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_provisioning_is_single_flight_for_concurrent_callers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                runner="colab",
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                transfer_root=root / "transfers",
                colab_cli_config=root / "sessions.json",
                colab_mount_drive=True,
            )
            client = ProvisioningColabClient()
            runner = ColabRunner(settings, client=client)  # type: ignore[arg-type]

            first = asyncio.create_task(runner.ensure_ready())
            await asyncio.wait_for(client.status_started.wait(), timeout=1.0)
            second = asyncio.create_task(runner.ensure_ready())
            await asyncio.sleep(0)

            self.assertEqual(RunnerState.PROVISIONING, runner.state)
            self.assertIsNone(runner.session_name)
            client.release_status.set()
            await asyncio.gather(first, second)

            self.assertEqual(["version", "status", "new", "drivemount"], client.commands)
            self.assertEqual(RunnerState.READY, runner.state)
            self.assertEqual(settings.colab_session_name, runner.session_name)

            await runner.stop()
            self.assertEqual(RunnerState.RECLAIMED, runner.state)
            self.assertIsNone(runner.session_name)
            self.assertEqual(
                ["version", "status", "new", "drivemount", "stop"],
                client.commands,
            )

    async def test_ready_heartbeat_recovers_one_lost_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                runner="colab",
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                transfer_root=root / "transfers",
                colab_cli_config=root / "sessions.json",
                colab_mount_drive=False,
            )
            client = ProvisioningColabClient(status_returncodes=(0, 1))
            client.release_status.set()
            runner = ColabRunner(settings, client=client)  # type: ignore[arg-type]

            await runner.ensure_ready()
            self.assertEqual(["version", "status"], client.commands)
            self.assertEqual(RunnerState.READY, runner.state)

            await asyncio.gather(runner.ensure_ready(), runner.ensure_ready())

            self.assertEqual(["version", "status", "status", "new"], client.commands)
            self.assertEqual(RunnerState.READY, runner.state)
            self.assertEqual(settings.colab_session_name, runner.session_name)

    async def test_expired_auth_fails_without_attempting_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                runner="colab",
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                transfer_root=root / "transfers",
                colab_cli_config=root / "sessions.json",
                colab_mount_drive=False,
            )
            client = ProvisioningColabClient(status_stderr=("OAuth token expired",))
            client.release_status.set()
            runner = ColabRunner(settings, client=client)  # type: ignore[arg-type]

            with self.assertRaises(ColabCliError) as captured:
                await runner.ensure_ready()

            self.assertEqual(ColabCliFailureKind.AUTHENTICATION, captured.exception.kind)
            self.assertEqual(["version", "status"], client.commands)
            self.assertEqual(RunnerState.FAILED, runner.state)
            self.assertIsNone(runner.session_name)

    async def test_incompatible_cli_fails_before_session_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                runner="colab",
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                transfer_root=root / "transfers",
                colab_cli_config=root / "sessions.json",
                colab_mount_drive=False,
            )
            client = ProvisioningColabClient(version_output="colab 0.9.0")
            client.release_status.set()
            runner = ColabRunner(settings, client=client)  # type: ignore[arg-type]

            with self.assertRaises(ColabCliCompatibilityError):
                await runner.ensure_ready()

            self.assertEqual(["version"], client.commands)
            self.assertEqual(RunnerState.FAILED, runner.state)

    async def test_stop_drains_active_job_before_reclaiming_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root / "worker.py"
            worker.write_text("# worker\n", encoding="utf-8")
            settings = HarnessSettings(
                runner="colab",
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                transfer_root=root / "transfers",
                colab_cli_config=root / "sessions.json",
                colab_remote_worker_file=worker,
                colab_mount_drive=False,
            )
            client = DrainingColabClient()
            runner = ColabRunner(settings, client=client)  # type: ignore[arg-type]
            await runner.ensure_ready()
            job = GpuJob(
                id="job-drain",
                user_id="user",
                kind="harness.healthcheck",
                payload={},
                state=JobState.RUNNING,
                created_at=1.0,
                updated_at=1.0,
                lease_token=1,
            )
            run_task = asyncio.create_task(runner.run(job, self._ignore_event))
            await asyncio.wait_for(client.exec_started.wait(), timeout=1.0)

            stop_task = asyncio.create_task(runner.stop())
            await asyncio.wait_for(client.cancel_started.wait(), timeout=1.0)
            self.assertEqual(RunnerState.DRAINING, runner.state)
            client.release_cancel.set()
            with self.assertRaises(asyncio.CancelledError):
                await run_task

            await asyncio.wait_for(client.stop_started.wait(), timeout=1.0)
            self.assertEqual(RunnerState.STOPPING, runner.state)
            client.release_stop.set()
            await stop_task

            self.assertEqual(RunnerState.RECLAIMED, runner.state)
            self.assertIsNone(runner.session_name)

    async def test_running_cancel_terminates_cli_and_maps_to_cancelled_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root / "worker.py"
            worker.write_text("# worker\n", encoding="utf-8")
            settings = HarnessSettings(
                runner="colab",
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                transfer_root=root / "transfers",
                colab_cli_config=root / "sessions.json",
                colab_remote_worker_file=worker,
                colab_mount_drive=False,
            )
            client = BlockingColabClient()
            runner = ColabRunner(settings, client=client)  # type: ignore[arg-type]
            await runner.ensure_ready()
            job = GpuJob(
                id="job-cancel",
                user_id="user",
                kind="harness.healthcheck",
                payload={},
                state=JobState.RUNNING,
                created_at=1.0,
                updated_at=1.0,
                lease_token=1,
            )

            task = asyncio.create_task(runner.run(job, self._ignore_event))
            await asyncio.wait_for(client.exec_started.wait(), timeout=1.0)
            await runner.cancel(job.id)

            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(RunnerState.READY, runner.state)
            cancel_uploads = [
                (path, value)
                for path, value in client.uploaded_json
                if path.startswith(settings.colab_remote_cancel_root)
            ]
            self.assertEqual(1, len(cancel_uploads))
            self.assertEqual(job.id, cancel_uploads[0][1]["job_id"])

    async def test_scheduler_persists_running_colab_cancel_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root / "worker.py"
            worker.write_text("# worker\n", encoding="utf-8")
            settings = HarnessSettings(
                runner="colab",
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                transfer_root=root / "transfers",
                colab_cli_config=root / "sessions.json",
                colab_remote_worker_file=worker,
                colab_mount_drive=False,
                scheduler_poll_seconds=0.01,
            )
            client = BlockingColabClient()
            runner = ColabRunner(settings, client=client)  # type: ignore[arg-type]
            harness = GpuHarness(settings, runner=runner)
            await harness.start()
            try:
                job = harness.enqueue(
                    user_id="user",
                    kind="harness.healthcheck",
                    payload={},
                )
                await asyncio.wait_for(client.exec_started.wait(), timeout=1.0)
                await harness.cancel(job.id)
                await wait_until(lambda: harness.store.get(job.id).state == JobState.CANCELLED)

                self.assertEqual(JobState.CANCELLED, harness.store.get(job.id).state)
                self.assertEqual("Job cancelled", harness.store.get(job.id).error)
            finally:
                await harness.close()

    async def test_music_artifacts_are_checksummed_before_atomic_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root / "worker.py"
            worker.write_text("# worker\n", encoding="utf-8")
            settings = HarnessSettings(
                runner="colab",
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                transfer_root=root / "transfers",
                colab_cli_config=root / "sessions.json",
                colab_remote_worker_file=worker,
                colab_mount_drive=False,
            )
            client = ArtifactColabClient()
            runner = ColabRunner(settings, client=client)  # type: ignore[arg-type]
            await runner.ensure_ready()
            job = GpuJob(
                id="job-colab-artifact",
                user_id="user",
                kind="music.generate",
                payload={"generation_request": {}},
                state=JobState.RUNNING,
                created_at=1.0,
                updated_at=1.0,
                lease_token=1,
            )

            result = await runner.run(job, self._ignore_event)

            transfer = settings.transfer_root / str(result["_transfer_id"])
            self.assertEqual(
                client.expected_audio,
                (transfer / "request" / "take.wav").read_bytes(),
            )
            self.assertEqual("colab", result["_environment"]["provider"])
            lifecycle = result["_lifecycle_metrics"]
            self.assertEqual("colab", lifecycle["provider"])
            self.assertEqual("attached", lifecycle["readiness"]["mode"])
            self.assertGreaterEqual(lifecycle["artifact_transfer_seconds"], 0.0)
            self.assertGreaterEqual(lifecycle["total_seconds"], 0.0)

            repeated = await runner.run(job, self._ignore_event)
            self.assertEqual(result["_transfer_id"], repeated["_transfer_id"])

    async def test_tampered_colab_transfer_exposes_no_partial_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root / "worker.py"
            worker.write_text("# worker\n", encoding="utf-8")
            settings = HarnessSettings(
                runner="colab",
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                transfer_root=root / "transfers",
                colab_cli_config=root / "sessions.json",
                colab_remote_worker_file=worker,
                colab_mount_drive=False,
            )
            client = ArtifactColabClient(tamper_download=True)
            runner = ColabRunner(settings, client=client)  # type: ignore[arg-type]
            await runner.ensure_ready()
            job = GpuJob(
                id="job-colab-artifact",
                user_id="user",
                kind="music.generate",
                payload={"generation_request": {}},
                state=JobState.RUNNING,
                created_at=1.0,
                updated_at=1.0,
                lease_token=1,
            )

            with self.assertRaisesRegex(ValueError, "size mismatch"):
                await runner.run(job, self._ignore_event)

            self.assertEqual([], list(settings.transfer_root.iterdir()))

    async def test_colab_cache_contract_is_verified_once_per_live_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root / "worker.py"
            worker.write_text("# worker\n", encoding="utf-8")
            manifest = root / "cache-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "status": "succeeded",
                        "cache_key": "cache-key",
                        "source_revision": "source-revision",
                        "verified_assets": [
                            {
                                "id": "weight",
                                "relative_path": "models/weight.bin",
                                "size_bytes": len(b"verified-cache"),
                                "sha256": hashlib.sha256(b"verified-cache").hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            settings = HarnessSettings(
                runner="colab",
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                transfer_root=root / "transfers",
                colab_cli_config=root / "sessions.json",
                colab_remote_worker_file=worker,
                colab_mount_drive=False,
                colab_cache_manifest_path=manifest,
                colab_remote_cache_root="/content/drive/MyDrive/melqivra/cache-key",
            )
            client = ArtifactColabClient()
            runner = ColabRunner(settings, client=client)  # type: ignore[arg-type]
            await runner.ensure_ready()
            job = GpuJob(
                id="job-colab-artifact",
                user_id="user",
                kind="music.generate",
                payload={"generation_request": {}},
                state=JobState.RUNNING,
                created_at=1.0,
                updated_at=1.0,
                lease_token=1,
            )

            await runner.run(job, self._ignore_event)
            await runner.run(job, self._ignore_event)

            requests = [
                request
                for request in client.uploaded_requests
                if request.get("kind") == "music.generate"
            ]
            self.assertIn("cache_contract", requests[0])
            self.assertNotIn("cache_contract", requests[1])

    @staticmethod
    async def _ignore_event(event: dict[str, object]) -> None:
        del event


if __name__ == "__main__":
    unittest.main()
