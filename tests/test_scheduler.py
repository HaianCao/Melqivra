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
from agent_music.models import JobAttemptOutcome, JobState, RunnerState
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


class LoseFirstRenewalStore(SqliteJobStore):
    """Inject one authoritative lease loss before allowing later attempts."""

    def __init__(self, database_path: Path) -> None:
        super().__init__(database_path)
        self.renewal_attempts = 0

    def renew_lease(
        self,
        *,
        job_id: str,
        lease_token: int,
        lease_owner: str,
        lease_seconds: float,
    ) -> bool:
        self.renewal_attempts += 1
        if self.renewal_attempts == 1:
            return False
        return super().renew_lease(
            job_id=job_id,
            lease_token=lease_token,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )


class LoseEveryRenewalStore(SqliteJobStore):
    """Inject authoritative lease loss for every claimed attempt."""

    def renew_lease(
        self,
        *,
        job_id: str,
        lease_token: int,
        lease_owner: str,
        lease_seconds: float,
    ) -> bool:
        return False


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
        second_result = self.harness.store.get(second.id).result
        assert second_result is not None
        self.assertAlmostEqual(
            0.07,
            second_result["_lifecycle_metrics"]["idle_before_job_seconds"],
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
                job_lease_seconds=0.3,
                job_heartbeat_seconds=0.05,
            )
            crashed_store = SqliteJobStore(settings.database_path)
            queued = crashed_store.enqueue(
                user_id="alice",
                kind="harness.echo",
                payload={"restart": True},
            )
            interrupted = crashed_store.claim_next(
                lease_owner="crashed-worker",
                lease_seconds=settings.job_lease_seconds,
            )
            assert interrupted is not None
            original_token = interrupted.lease_token
            crashed_store.close()

            runner = FakeRunner(job_seconds=0.001)
            restarted = GpuHarness(settings, runner=runner)
            await restarted.start()
            try:
                await asyncio.sleep(0.03)
                still_owned = restarted.store.get(queued.id)
                self.assertEqual(JobState.RUNNING, still_owned.state)
                self.assertEqual("crashed-worker", still_owned.lease_owner)
                self.assertEqual([], runner.completed_job_ids)

                try:
                    await wait_until(
                        lambda: (
                            restarted.store.get(queued.id).state
                            not in {JobState.QUEUED, JobState.RUNNING}
                        )
                    )
                except TimeoutError:
                    scheduler_task = restarted._task
                    task_error = (
                        scheduler_task.exception()
                        if scheduler_task is not None and scheduler_task.done()
                        else None
                    )
                    self.fail(
                        f"restart state={restarted.store.get(queued.id)!r}; "
                        f"scheduler_error={task_error!r}"
                    )
                recovered = restarted.store.get(queued.id)

                self.assertEqual(JobState.SUCCEEDED, recovered.state, recovered.error)
                self.assertNotEqual(original_token, recovered.lease_token)
                self.assertEqual(2, recovered.attempt_count)
                self.assertEqual([queued.id], runner.completed_job_ids)
            finally:
                await restarted.close()

    async def test_heartbeat_keeps_long_job_on_its_first_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                idle_timeout_seconds=300.0,
                scheduler_poll_seconds=0.005,
                job_timeout_seconds=3.0,
                job_lease_seconds=1.0,
                job_heartbeat_seconds=0.1,
            )
            runner = FakeRunner(job_seconds=1.2)
            harness = GpuHarness(settings, runner=runner, worker_id="steady-worker")
            await harness.start()
            try:
                job = harness.enqueue(
                    user_id="alice",
                    kind="harness.echo",
                    payload={"long": True},
                )
                await wait_until(
                    lambda: (
                        harness.store.get(job.id).state not in {JobState.QUEUED, JobState.RUNNING}
                    )
                )
                completed = harness.store.get(job.id)
                self.assertEqual(JobState.SUCCEEDED, completed.state, completed.error)
                self.assertEqual(1, completed.attempt_count)
                self.assertIsNone(completed.lease_owner)
                self.assertIsNone(completed.lease_expires_at)
                self.assertEqual([job.id], runner.completed_job_ids)
            finally:
                await harness.close()

    async def test_lost_heartbeat_cancels_stale_work_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                idle_timeout_seconds=300.0,
                scheduler_poll_seconds=0.01,
                job_timeout_seconds=3.0,
                job_lease_seconds=0.5,
                job_heartbeat_seconds=0.1,
            )
            store = LoseFirstRenewalStore(settings.database_path)
            runner = FakeRunner(job_seconds=0.7)
            harness = GpuHarness(
                settings,
                store=store,
                runner=runner,
                worker_id="recovering-worker",
            )
            await harness.start()
            try:
                job = harness.enqueue(
                    user_id="alice",
                    kind="harness.echo",
                    payload={"retry": True},
                )
                try:
                    await wait_until(
                        lambda: (
                            harness.store.get(job.id).state
                            not in {JobState.QUEUED, JobState.RUNNING}
                        )
                    )
                except TimeoutError:
                    scheduler_task = harness._task
                    task_error = (
                        scheduler_task.exception()
                        if scheduler_task is not None and scheduler_task.done()
                        else None
                    )
                    self.fail(
                        f"retry state={harness.store.get(job.id)!r}; scheduler_error={task_error!r}"
                    )
                completed = harness.store.get(job.id)
                self.assertEqual(JobState.SUCCEEDED, completed.state, completed.error)
                self.assertEqual(2, completed.attempt_count)
                self.assertEqual(1, runner.cancel_count)
                self.assertEqual([job.id], runner.completed_job_ids)
                self.assertEqual(1, runner.max_active_jobs)
            finally:
                await harness.close()

    async def test_job_timeout_cancels_owned_runner_task_and_fails_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                idle_timeout_seconds=300.0,
                scheduler_poll_seconds=0.01,
                job_timeout_seconds=0.05,
                job_lease_seconds=1.0,
                job_heartbeat_seconds=0.1,
            )
            runner = FakeRunner(job_seconds=0.5)
            harness = GpuHarness(settings, runner=runner)
            await harness.start()
            try:
                job = harness.enqueue(
                    user_id="alice",
                    kind="harness.echo",
                    payload={"timeout": True},
                )
                await wait_until(lambda: harness.store.get(job.id).state == JobState.FAILED)
                failed = harness.store.get(job.id)
                self.assertIn("TimeoutError", failed.error or "")
                self.assertEqual(1, runner.cancel_count)
                self.assertEqual([], runner.completed_job_ids)
            finally:
                await harness.close()

    async def test_repeated_lease_loss_dead_letters_at_snapshot_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                database_path=root / "harness.db",
                artifact_root=root / "artifacts",
                idle_timeout_seconds=300.0,
                scheduler_poll_seconds=0.01,
                job_timeout_seconds=3.0,
                job_lease_seconds=0.15,
                job_heartbeat_seconds=0.05,
                job_max_attempts=2,
            )
            store = LoseEveryRenewalStore(
                settings.database_path,
                max_attempts=settings.job_max_attempts,
            )
            runner = FakeRunner(job_seconds=1.0)
            harness = GpuHarness(settings, store=store, runner=runner)
            await harness.start()
            try:
                job = harness.enqueue(
                    user_id="alice",
                    kind="harness.echo",
                    payload={"dead_letter": True},
                )
                await wait_until(lambda: harness.store.get(job.id).state == JobState.DEAD_LETTERED)
                dead_lettered = harness.store.get(job.id)
                self.assertEqual(2, dead_lettered.attempt_count)
                self.assertEqual(2, dead_lettered.max_attempts)
                self.assertEqual(2, runner.cancel_count)
                self.assertEqual([], runner.completed_job_ids)
                self.assertEqual(
                    [JobAttemptOutcome.LEASE_EXPIRED, JobAttemptOutcome.LEASE_EXPIRED],
                    [attempt.outcome for attempt in harness.store.list_attempts(job.id)],
                )
            finally:
                await harness.close()


if __name__ == "__main__":
    unittest.main()
