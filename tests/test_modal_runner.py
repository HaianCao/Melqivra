"""Modal runner state, transport, and failure-contract tests."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_music.config import HarnessSettings
from agent_music.models import GpuJob, JobState, RunnerState
from agent_music.runners.modal import ModalRunner
from agent_music.runners.modal_cli import (
    CommandResult,
    LineCallback,
    ModalCliCompatibilityError,
    ModalCliError,
    ModalCliFailureKind,
)
from agent_music.scheduler import GpuHarness


async def wait_until(predicate: Callable[[], bool], *, timeout_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Condition was not reached")
        await asyncio.sleep(0.005)


class FakeModalClient:
    def __init__(
        self,
        *,
        remote_status: str = "succeeded",
        remote_artifacts: dict[str, bytes] | None = None,
        version_output: str = "modal client version: 1.6.3",
        token_returncode: int = 0,
        token_stderr: str = "",
        block_token: bool = False,
    ) -> None:
        self.remote_status = remote_status
        self.remote_artifacts = remote_artifacts or {}
        self.version_output = version_output
        self.token_returncode = token_returncode
        self.token_stderr = token_stderr
        self.block_token = block_token
        self.token_started = asyncio.Event()
        self.release_token = asyncio.Event()
        if not block_token:
            self.release_token.set()
        self.commands: list[tuple[str, ...]] = []
        self.cancelled = False

    def version_argv(self) -> list[str]:
        return ["modal", "--version"]

    def token_info_argv(self) -> list[str]:
        return ["modal", "token", "info"]

    def run_job_argv(
        self,
        runner_file: Path,
        request_path: Path,
        result_path: Path,
    ) -> list[str]:
        return [
            "modal",
            "run",
            "--write-result",
            str(result_path),
            str(runner_file),
            "--request-file",
            str(request_path),
        ]

    def stop_argv(self) -> list[str]:
        return ["modal", "app", "stop", "--yes", "melqivra-gpu-runner"]

    def volume_get_argv(
        self,
        volume_name: str,
        remote_path: str,
        local_directory: Path,
    ) -> list[str]:
        return ["modal", "volume", "get", volume_name, remote_path, str(local_directory)]

    async def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        on_stdout: LineCallback | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        _ = check, environment
        self.commands.append(tuple(argv))
        if len(argv) > 1 and argv[1] == "--version":
            return CommandResult(returncode=0, stdout=self.version_output, stderr="")
        if len(argv) > 2 and argv[1:3] == ["token", "info"]:
            self.token_started.set()
            await self.release_token.wait()
            result = CommandResult(
                returncode=self.token_returncode,
                stdout="authenticated" if self.token_returncode == 0 else "",
                stderr=self.token_stderr,
            )
            if check and result.returncode != 0:
                raise ModalCliError(argv, result)
            return result
        if len(argv) > 1 and argv[1] == "run":
            result_path = Path(argv[argv.index("--write-result") + 1])
            request_path = Path(argv[argv.index("--request-file") + 1])
            request_text = await asyncio.to_thread(request_path.read_text, encoding="utf-8")
            request = json.loads(request_text)
            if on_stdout is not None:
                await on_stdout('{"type":"stage","stage":"remote_started"}')
                await on_stdout("ignored provider log")
            result_payload: dict[str, object] = {"echo": request["payload"]}
            if request["kind"] == "music.generate":
                result_payload = {
                    "artifacts": [
                        {"relative_path": path.removeprefix("runs/")}
                        for path in self.remote_artifacts
                    ]
                }
            result_text = json.dumps(
                {
                    "status": self.remote_status,
                    "job_id": request["job_id"],
                    "result": result_payload,
                    "error": "deterministic failure" if self.remote_status != "succeeded" else None,
                }
            )
            await asyncio.to_thread(result_path.write_text, result_text, encoding="utf-8")
        elif len(argv) > 2 and argv[1:3] == ["volume", "get"]:
            remote_path = argv[4]
            destination = Path(argv[5])
            await asyncio.to_thread(destination.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(
                (destination / remote_path.rsplit("/", 1)[-1]).write_bytes,
                self.remote_artifacts[remote_path],
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    async def cancel_current(self) -> None:
        self.cancelled = True


class DrainingModalClient(FakeModalClient):
    def __init__(self) -> None:
        super().__init__()
        self.run_started = asyncio.Event()
        self.cancel_started = asyncio.Event()
        self.release_cancel = asyncio.Event()
        self.command_cancelled = asyncio.Event()
        self.stop_started = asyncio.Event()
        self.release_stop = asyncio.Event()

    async def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        on_stdout: LineCallback | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        if len(argv) > 1 and argv[1] == "run":
            self.commands.append(tuple(argv))
            self.run_started.set()
            await self.command_cancelled.wait()
            raise ModalCliError(argv, CommandResult(130, "", "cancelled"))
        if len(argv) > 2 and argv[1:3] == ["app", "stop"]:
            self.commands.append(tuple(argv))
            self.stop_started.set()
            await self.release_stop.wait()
            return CommandResult(0, "", "")
        return await super().run(
            argv,
            check=check,
            on_stdout=on_stdout,
            environment=environment,
        )

    async def cancel_current(self) -> None:
        self.cancel_started.set()
        await self.release_cancel.wait()
        self.cancelled = True
        self.command_cancelled.set()


class BlockingTransferModalClient(FakeModalClient):
    def __init__(self) -> None:
        super().__init__(remote_artifacts={"runs/request/take.wav": b"RIFF-private"})
        self.transfer_started = asyncio.Event()
        self.command_cancelled = asyncio.Event()

    async def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        on_stdout: LineCallback | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        if len(argv) > 2 and argv[1:3] == ["volume", "get"]:
            self.commands.append(tuple(argv))
            self.transfer_started.set()
            await self.command_cancelled.wait()
            raise ModalCliError(argv, CommandResult(130, "", "cancelled"))
        return await super().run(
            argv,
            check=check,
            on_stdout=on_stdout,
            environment=environment,
        )

    async def cancel_current(self) -> None:
        self.cancelled = True
        self.command_cancelled.set()


class BlockingCancellationModalClient(FakeModalClient):
    def __init__(self) -> None:
        super().__init__()
        self.run_started = asyncio.Event()
        self.command_cancelled = asyncio.Event()

    async def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        on_stdout: LineCallback | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        if len(argv) > 1 and argv[1] == "run":
            self.commands.append(tuple(argv))
            self.run_started.set()
            await self.command_cancelled.wait()
            raise ModalCliError(argv, CommandResult(130, "", "cancelled"))
        return await super().run(
            argv,
            check=check,
            on_stdout=on_stdout,
            environment=environment,
        )

    async def cancel_current(self) -> None:
        self.cancelled = True
        self.command_cancelled.set()


class ModalRunnerTests(unittest.IsolatedAsyncioTestCase):
    def _job(self) -> GpuJob:
        return GpuJob(
            id="job-1",
            user_id="user-1",
            kind="harness.echo",
            payload={"message": "private"},
            state=JobState.RUNNING,
            created_at=1.0,
            updated_at=1.0,
            lease_token=1,
        )

    async def test_runner_uses_file_transport_and_structured_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner_file = Path(temporary) / "remote_worker.py"
            runner_file.write_text("# fixture\n", encoding="utf-8")
            client = FakeModalClient()
            runner = ModalRunner(
                HarnessSettings(modal_runner_file=runner_file),
                client=client,
            )
            events: list[dict[str, Any]] = []

            async def capture(event: dict[str, Any]) -> None:
                events.append(event)

            await runner.ensure_ready()
            result = await runner.run(self._job(), capture)

            self.assertEqual(RunnerState.READY, runner.state)
            self.assertIsNone(runner.session_name)
            self.assertEqual("succeeded", result["status"])
            lifecycle = result["_lifecycle_metrics"]
            self.assertEqual("modal", lifecycle["provider"])
            self.assertEqual("cold", lifecycle["readiness"]["mode"])
            self.assertGreaterEqual(lifecycle["execution_seconds"], 0.0)
            self.assertGreaterEqual(lifecycle["total_seconds"], 0.0)
            self.assertEqual("submitting", events[0]["stage"])
            self.assertEqual("remote_started", events[1]["stage"])
            command_text = " ".join(part for command in client.commands for part in command)
            self.assertNotIn("private", command_text)

    async def test_remote_failure_and_stop_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner_file = Path(temporary) / "remote_worker.py"
            runner_file.write_text("# fixture\n", encoding="utf-8")
            client = FakeModalClient(remote_status="failed")
            runner = ModalRunner(
                HarnessSettings(modal_runner_file=runner_file),
                client=client,
            )
            await runner.ensure_ready()

            with self.assertRaisesRegex(RuntimeError, "deterministic failure"):
                await runner.run(self._job(), lambda _event: _completed())
            await runner.stop()

            self.assertEqual(RunnerState.RECLAIMED, runner.state)
            self.assertTrue(client.cancelled)
            self.assertIn(("modal", "app", "stop", "--yes", "melqivra-gpu-runner"), client.commands)

    async def test_music_job_downloads_declared_volume_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner_file = root / "remote_worker.py"
            runner_file.write_text("# fixture\n", encoding="utf-8")
            client = FakeModalClient(remote_artifacts={"runs/request/take.wav": b"RIFF-private"})
            runner = ModalRunner(
                HarnessSettings(
                    modal_runner_file=runner_file,
                    modal_artifact_volume="melqivra-cache",
                    transfer_root=root / "transfers",
                ),
                client=client,
            )
            job = GpuJob(
                id="job-music",
                user_id="user-1",
                kind="music.generate",
                payload={"generation_request": {}},
                state=JobState.RUNNING,
                created_at=1.0,
                updated_at=1.0,
                lease_token=1,
            )
            await runner.ensure_ready()
            result = await runner.run(job, lambda _event: _completed())

            transfer = root / "transfers" / str(result["_transfer_id"])
            self.assertEqual(b"RIFF-private", (transfer / "request" / "take.wav").read_bytes())
            self.assertEqual("modal", result["_environment"]["provider"])

    async def test_readiness_is_single_flight_and_heartbeats_before_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner_file = Path(temporary) / "remote_worker.py"
            runner_file.write_text("# fixture\n", encoding="utf-8")
            client = FakeModalClient(block_token=True)
            runner = ModalRunner(
                HarnessSettings(modal_runner_file=runner_file),
                client=client,
            )

            first = asyncio.create_task(runner.ensure_ready())
            await asyncio.wait_for(client.token_started.wait(), timeout=1.0)
            second = asyncio.create_task(runner.ensure_ready())
            await asyncio.sleep(0)
            self.assertEqual(RunnerState.PROVISIONING, runner.state)

            client.release_token.set()
            await asyncio.gather(first, second)
            self.assertEqual(RunnerState.READY, runner.state)
            self.assertEqual(1, client.commands.count(("modal", "--version")))
            self.assertEqual(1, client.commands.count(("modal", "token", "info")))

            await runner.ensure_ready()
            self.assertEqual(1, client.commands.count(("modal", "--version")))
            self.assertEqual(2, client.commands.count(("modal", "token", "info")))

    async def test_expired_auth_and_incompatible_cli_fail_before_job_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner_file = Path(temporary) / "remote_worker.py"
            runner_file.write_text("# fixture\n", encoding="utf-8")
            auth_client = FakeModalClient(
                token_returncode=1,
                token_stderr="Token expired; not authenticated",
            )
            auth_runner = ModalRunner(
                HarnessSettings(modal_runner_file=runner_file),
                client=auth_client,
            )

            with self.assertRaises(ModalCliError) as captured:
                await auth_runner.ensure_ready()
            self.assertEqual(ModalCliFailureKind.AUTHENTICATION, captured.exception.kind)
            self.assertEqual(RunnerState.FAILED, auth_runner.state)
            self.assertFalse(
                any(len(command) > 1 and command[1] == "run" for command in auth_client.commands)
            )

            version_client = FakeModalClient(version_output="modal client version: 1.7.0")
            version_runner = ModalRunner(
                HarnessSettings(modal_runner_file=runner_file),
                client=version_client,
            )
            with self.assertRaises(ModalCliCompatibilityError):
                await version_runner.ensure_ready()
            self.assertEqual([("modal", "--version")], version_client.commands)
            self.assertEqual(RunnerState.FAILED, version_runner.state)

    async def test_stop_drains_active_job_before_reclaiming_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner_file = Path(temporary) / "remote_worker.py"
            runner_file.write_text("# fixture\n", encoding="utf-8")
            client = DrainingModalClient()
            runner = ModalRunner(
                HarnessSettings(modal_runner_file=runner_file),
                client=client,
            )
            await runner.ensure_ready()

            run_task = asyncio.create_task(runner.run(self._job(), lambda _event: _completed()))
            await asyncio.wait_for(client.run_started.wait(), timeout=1.0)
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

    async def test_cancel_during_artifact_transfer_maps_to_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner_file = root / "remote_worker.py"
            runner_file.write_text("# fixture\n", encoding="utf-8")
            client = BlockingTransferModalClient()
            runner = ModalRunner(
                HarnessSettings(
                    modal_runner_file=runner_file,
                    modal_artifact_volume="melqivra-cache",
                    transfer_root=root / "transfers",
                ),
                client=client,
            )
            job = GpuJob(
                id="job-transfer-cancel",
                user_id="user-1",
                kind="music.generate",
                payload={"generation_request": {}},
                state=JobState.RUNNING,
                created_at=1.0,
                updated_at=1.0,
                lease_token=1,
            )
            await runner.ensure_ready()
            run_task = asyncio.create_task(runner.run(job, lambda _event: _completed()))
            await asyncio.wait_for(client.transfer_started.wait(), timeout=1.0)

            await runner.cancel(job.id)
            with self.assertRaises(asyncio.CancelledError):
                await run_task

            self.assertEqual(RunnerState.READY, runner.state)
            self.assertTrue(client.cancelled)

    async def test_scheduler_persists_running_modal_cancel_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner_file = root / "remote_worker.py"
            runner_file.write_text("# fixture\n", encoding="utf-8")
            settings = HarnessSettings(
                runner="modal",
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                transfer_root=root / "transfers",
                modal_runner_file=runner_file,
                scheduler_poll_seconds=0.01,
            )
            client = BlockingCancellationModalClient()
            runner = ModalRunner(settings, client=client)
            harness = GpuHarness(settings, runner=runner)
            await harness.start()
            try:
                job = harness.enqueue(
                    user_id="user",
                    kind="harness.healthcheck",
                    payload={},
                )
                await asyncio.wait_for(client.run_started.wait(), timeout=1.0)
                await harness.cancel(job.id)
                await wait_until(lambda: harness.store.get(job.id).state == JobState.CANCELLED)

                self.assertEqual(JobState.CANCELLED, harness.store.get(job.id).state)
                self.assertEqual("Job cancelled", harness.store.get(job.id).error)
            finally:
                await harness.close()


async def _completed() -> None:
    return None


if __name__ == "__main__":
    unittest.main()
