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
from agent_music.store import SqliteJobStore


async def wait_until(predicate, deadline_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Condition was not reached")
        await asyncio.sleep(0.005)


class FakeClock:
    def __init__(self) -> None:
        self._monotonic = 0.0
        self._wall = 1_700_000_000.0

    def monotonic(self) -> float:
        return self._monotonic

    def wall(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._wall += seconds


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
        self.clock = FakeClock()
        self.harness = GpuHarness(
            self.settings,
            runner=self.runner,
            monotonic_clock=self.clock.monotonic,
            wall_clock=self.clock.wall,
        )
        await self.harness.start()

    async def asyncTearDown(self) -> None:
        await self.harness.close()
        self.temp_dir.cleanup()

    def advance_clock(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.harness._wake.set()

    async def test_no_job_does_not_provision_gpu(self) -> None:
        self.advance_clock(1.0)
        await asyncio.sleep(0)
        self.assertEqual(0, self.runner.provision_count)
        self.assertEqual(RunnerState.STOPPED, self.runner.state)

    async def test_job_starts_on_demand_and_stops_after_idle_timeout(self) -> None:
        job = self.harness.enqueue(
            user_id="alice",
            kind="harness.echo",
            payload={"message": "hello"},
        )
        await wait_until(
            lambda: (
                self.harness.store.get(job.id).state == JobState.SUCCEEDED
                and self.harness.snapshot().current_job_id is None
            )
        )

        self.assertEqual(1, self.runner.provision_count)
        self.assertEqual(RunnerState.READY, self.runner.state)
        self.advance_clock(0.06)
        await asyncio.sleep(0)
        self.assertEqual(RunnerState.READY, self.runner.state)
        self.advance_clock(0.061)
        await wait_until(lambda: self.runner.state == RunnerState.STOPPED)
        self.assertEqual(1, self.runner.stop_count)

    async def test_job_during_grace_period_reuses_warm_session(self) -> None:
        first = self.harness.enqueue(
            user_id="alice",
            kind="harness.echo",
            payload={"index": 1},
        )
        await wait_until(
            lambda: (
                self.harness.store.get(first.id).state == JobState.SUCCEEDED
                and self.harness.snapshot().current_job_id is None
            )
        )
        self.advance_clock(0.07)
        second = self.harness.enqueue(
            user_id="bob",
            kind="harness.echo",
            payload={"index": 2},
        )
        await wait_until(
            lambda: (
                self.harness.store.get(second.id).state == JobState.SUCCEEDED
                and self.harness.snapshot().current_job_id is None
            )
        )

        self.assertEqual(1, self.runner.provision_count)
        self.assertEqual(
            [first.id, second.id],
            self.runner.completed_job_ids,
        )
        self.advance_clock(0.121)
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

    async def test_exact_idle_boundary_arrival_matrix(self) -> None:
        for arrival_seconds, expected_provisions in ((0.0, 1), (299.0, 1), (300.0, 1), (301.0, 2)):
            with self.subTest(arrival_seconds=arrival_seconds):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    settings = HarnessSettings(
                        database_path=root / "harness.db",
                        artifact_root=root / "artifacts",
                        idle_timeout_seconds=300.0,
                        scheduler_poll_seconds=0.01,
                        job_timeout_seconds=2.0,
                    )
                    runner = FakeRunner(job_seconds=0.001)
                    clock = FakeClock()
                    harness = GpuHarness(
                        settings,
                        runner=runner,
                        monotonic_clock=clock.monotonic,
                        wall_clock=clock.wall,
                    )
                    await harness.start()
                    try:
                        first = harness.enqueue(
                            user_id="alice",
                            kind="harness.echo",
                            payload={"index": 1},
                        )
                        await wait_until(
                            lambda harness=harness, first=first: (
                                harness.store.get(first.id).state == JobState.SUCCEEDED
                                and harness.snapshot().current_job_id is None
                            )
                        )

                        if arrival_seconds > 300.0:
                            clock.advance(300.0)
                            harness._wake.set()
                            await wait_until(
                                lambda runner=runner: runner.state == RunnerState.STOPPED
                            )
                            clock.advance(arrival_seconds - 300.0)
                        else:
                            clock.advance(arrival_seconds)

                        second = harness.enqueue(
                            user_id="bob",
                            kind="harness.echo",
                            payload={"index": 2},
                        )
                        await wait_until(
                            lambda harness=harness, second=second: (
                                harness.store.get(second.id).state == JobState.SUCCEEDED
                            )
                        )

                        self.assertEqual(expected_provisions, runner.provision_count)
                        self.assertEqual([first.id, second.id], runner.completed_job_ids)
                    finally:
                        await harness.close()

    async def test_broker_restart_requeues_with_a_fresh_fencing_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                idle_timeout_seconds=300.0,
                scheduler_poll_seconds=0.01,
                job_timeout_seconds=2.0,
            )
            crashed_store = SqliteJobStore(settings.database_path)
            queued = crashed_store.enqueue(
                user_id="alice",
                kind="harness.echo",
                payload={"restart": True},
            )
            interrupted = crashed_store.claim_next()
            assert interrupted is not None
            original_token = interrupted.lease_token
            crashed_store.close()

            runner = FakeRunner(job_seconds=0.001)
            restarted = GpuHarness(settings, runner=runner)
            await restarted.start()
            try:
                await wait_until(lambda: restarted.store.get(queued.id).state == JobState.SUCCEEDED)
                recovered = restarted.store.get(queued.id)

                self.assertNotEqual(original_token, recovered.lease_token)
                self.assertEqual([queued.id], runner.completed_job_ids)
            finally:
                await restarted.close()


if __name__ == "__main__":
    unittest.main()
