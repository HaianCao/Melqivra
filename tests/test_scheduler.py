"""On-demand provisioning, serialized execution, and idle shutdown tests."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_music.config import HarnessSettings
from agent_music.models import JobState, RunnerState
from agent_music.runners.fake import FakeRunner
from agent_music.scheduler import GpuHarness


async def wait_until(predicate, deadline_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Condition was not reached")
        await asyncio.sleep(0.005)


class GpuHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = HarnessSettings(
            database_path=root / "harness.db",
            artifact_root=root / "artifacts",
            idle_timeout_seconds=0.12,
            scheduler_poll_seconds=0.01,
            job_timeout_seconds=2.0,
        )
        self.runner = FakeRunner(job_seconds=0.03)
        self.harness = GpuHarness(self.settings, runner=self.runner)
        await self.harness.start()

    async def asyncTearDown(self) -> None:
        await self.harness.close()
        self.temp_dir.cleanup()

    async def test_no_job_does_not_provision_gpu(self) -> None:
        await asyncio.sleep(0.16)
        self.assertEqual(0, self.runner.provision_count)
        self.assertEqual(RunnerState.STOPPED, self.runner.state)

    async def test_job_starts_on_demand_and_stops_after_idle_timeout(self) -> None:
        job = self.harness.enqueue(
            user_id="alice",
            kind="harness.echo",
            payload={"message": "hello"},
        )
        await wait_until(lambda: self.harness.store.get(job.id).state == JobState.SUCCEEDED)

        self.assertEqual(1, self.runner.provision_count)
        self.assertEqual(RunnerState.READY, self.runner.state)
        await asyncio.sleep(0.06)
        self.assertEqual(RunnerState.READY, self.runner.state)
        await wait_until(lambda: self.runner.state == RunnerState.STOPPED)
        self.assertEqual(1, self.runner.stop_count)

    async def test_job_during_grace_period_reuses_warm_session(self) -> None:
        first = self.harness.enqueue(
            user_id="alice",
            kind="harness.echo",
            payload={"index": 1},
        )
        await wait_until(lambda: self.harness.store.get(first.id).state == JobState.SUCCEEDED)
        await asyncio.sleep(0.07)
        second = self.harness.enqueue(
            user_id="bob",
            kind="harness.echo",
            payload={"index": 2},
        )
        await wait_until(lambda: self.harness.store.get(second.id).state == JobState.SUCCEEDED)

        self.assertEqual(1, self.runner.provision_count)
        self.assertEqual(
            [first.id, second.id],
            self.runner.completed_job_ids,
        )
        await wait_until(lambda: self.runner.state == RunnerState.STOPPED)
        self.assertEqual(1, self.runner.stop_count)

    async def test_multiple_users_are_strictly_serialized(self) -> None:
        jobs = [
            self.harness.enqueue(
                user_id=f"user-{index}",
                kind="harness.echo",
                payload={"index": index},
            )
            for index in range(5)
        ]
        await wait_until(
            lambda: all(self.harness.store.get(job.id).state == JobState.SUCCEEDED for job in jobs)
        )

        self.assertEqual(1, self.runner.max_active_jobs)
        self.assertEqual([job.id for job in jobs], self.runner.completed_job_ids)
        self.assertEqual(1, self.runner.provision_count)

    async def test_idempotent_request_runs_once(self) -> None:
        first = self.harness.enqueue(
            user_id="alice",
            kind="harness.echo",
            payload={"index": 1},
            idempotency_key="same-request",
        )
        duplicate = self.harness.enqueue(
            user_id="alice",
            kind="harness.echo",
            payload={"index": 2},
            idempotency_key="same-request",
        )
        self.assertEqual(first.id, duplicate.id)
        await wait_until(lambda: self.harness.store.get(first.id).state == JobState.SUCCEEDED)
        self.assertEqual([first.id], self.runner.completed_job_ids)


if __name__ == "__main__":
    unittest.main()
