"""Colab runner cancellation and lifecycle tests."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path

from agent_music.config import HarnessSettings
from agent_music.models import GpuJob, JobState, RunnerState
from agent_music.runners.colab import ColabRunner
from agent_music.runners.colab_cli import ColabCliError, CommandResult
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

    def status_argv(self, session_name: str) -> list[str]:
        return ["status", session_name]

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
        if argv[0] == "exec":
            self.exec_started.set()
            await self.cancelled.wait()
            raise ColabCliError(argv, CommandResult(130, "", "cancelled"))
        return CommandResult(0, "", "")

    async def cancel_current(self) -> None:
        self.cancelled.set()


class ColabRunnerTests(unittest.IsolatedAsyncioTestCase):
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

    @staticmethod
    async def _ignore_event(event: dict[str, object]) -> None:
        del event


if __name__ == "__main__":
    unittest.main()
