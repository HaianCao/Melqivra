"""Single-capacity GPU scheduler with on-demand start and idle shutdown."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any

from agent_music.config import HarnessSettings
from agent_music.job_store import JobStore, LostJobLeaseError, build_job_store
from agent_music.models import GpuJob, JobState, RunnerSnapshot, RunnerState
from agent_music.result_processing import (
    JobResultProcessor,
    build_default_result_processor,
)
from agent_music.runners.base import EventCallback, GpuRunner
from agent_music.runners.strategy import build_runner

EventSink = Callable[[str, dict[str, Any]], None]


class GpuHarness:
    """Durable FIFO scheduler with exactly one GPU execution slot."""

    def __init__(
        self,
        settings: HarnessSettings,
        *,
        store: JobStore | None = None,
        runner: GpuRunner | None = None,
        event_sink: EventSink | None = None,
        result_processor: JobResultProcessor | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        worker_id: str | None = None,
    ) -> None:
        settings.validate()
        settings.ensure_directories()
        self.settings = settings
        self.store: JobStore = store or build_job_store(settings)
        self.runner = runner or build_runner(settings)
        self._event_sink = event_sink or (lambda _job_id, _event: None)
        self._result_processor = result_processor or build_default_result_processor(settings)
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._worker_id = worker_id or f"worker-{uuid.uuid4()}"
        if not self._worker_id.strip():
            raise ValueError("GPU worker ID must not be empty")
        self._wake = asyncio.Event()
        self._wake_waiter: asyncio.Task[bool] | None = None
        self._stop_requested = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._current_job_id: str | None = None
        self._last_activity_monotonic: float | None = None
        self._last_activity_wall: float | None = None

    async def start(self) -> None:
        """Reconcile expired work and start the scheduling loop."""

        if self._task is not None:
            return
        self.store.recover_expired()
        self._stop_requested.clear()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="melqivra-gpu-harness",
        )

    async def close(self, *, stop_runner: bool = True) -> None:
        """Stop the loop and optionally release the remote session."""

        self._stop_requested.set()
        self._wake.set()
        if self._task is not None:
            task = self._task
            self._task = None
            task.cancel()
            outcome = (await asyncio.gather(task, return_exceptions=True))[0]
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, asyncio.CancelledError
            ):
                raise outcome
        if stop_runner:
            await self.runner.stop()
        self.store.close()

    def enqueue(
        self,
        *,
        user_id: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> GpuJob:
        """Enqueue work and wake the scheduler immediately."""

        job = self.store.enqueue(
            user_id=user_id,
            kind=kind,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        self._wake.set()
        return job

    async def cancel(self, job_id: str) -> GpuJob:
        """Request cancellation."""

        job = self.store.request_cancel(job_id)
        return await self._finish_cancellation_request(job)

    def get_job_for_user(self, *, user_id: str, job_id: str) -> GpuJob:
        """Load one job through the tenant-scoped persistence boundary."""

        return self.store.get_for_user(user_id=user_id, job_id=job_id)

    async def cancel_for_user(self, *, user_id: str, job_id: str) -> GpuJob:
        """Request cancellation without first loading another tenant's job."""

        job = self.store.request_cancel_for_user(user_id=user_id, job_id=job_id)
        return await self._finish_cancellation_request(job)

    async def _finish_cancellation_request(self, job: GpuJob) -> GpuJob:
        if job.state == JobState.RUNNING:
            await self.runner.cancel(job.id)
        self._wake.set()
        return self.store.get(job.id)

    def snapshot(self) -> RunnerSnapshot:
        """Return runner and idle-deadline state."""

        idle_deadline = None
        if self.runner.state == RunnerState.READY and self._last_activity_wall is not None:
            idle_deadline = self._last_activity_wall + self.settings.idle_timeout_seconds
        return RunnerSnapshot(
            state=self.runner.state,
            session_name=self.runner.session_name,
            current_job_id=self._current_job_id,
            last_activity_at=self._last_activity_wall,
            idle_deadline_at=idle_deadline,
        )

    async def _run_loop(self) -> None:
        try:
            while not self._stop_requested.is_set():
                self.store.recover_expired()
                job = self.store.claim_next(
                    lease_owner=self._worker_id,
                    lease_seconds=self.settings.job_lease_seconds,
                )
                if job is not None:
                    await self._execute(job)
                    continue

                if await self._stop_if_idle():
                    continue

                self._wake.clear()
                timeout = self._next_wait_timeout()
                await self._wait_for_wake(timeout)
        finally:
            waiter = self._wake_waiter
            self._wake_waiter = None
            if waiter is not None:
                if not waiter.done():
                    waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    async def _execute(self, job: GpuJob) -> None:
        if job.lease_token is None:
            raise RuntimeError("Claimed job has no fencing token")
        idle_before_job = 0.0
        if self._last_activity_monotonic is not None:
            idle_before_job = max(
                self._monotonic_clock() - self._last_activity_monotonic,
                0.0,
            )
        self._current_job_id = job.id
        self._touch_activity()

        async def on_event(event: dict[str, Any]) -> None:
            self._event_sink(job.id, event)

        try:
            processed = await self._run_with_heartbeat(
                job,
                on_event=on_event,
                idle_before_job=idle_before_job,
            )
            self.store.complete(job.id, job.lease_token, processed)
        except LostJobLeaseError:
            # Another worker may already own a fresh fencing token. The stale
            # worker must not overwrite that attempt with any terminal state.
            pass
        except asyncio.CancelledError:
            latest = self.store.get(job.id)
            if latest.state == JobState.RUNNING:
                try:
                    self.store.cancel_running(job.id, job.lease_token)
                except LostJobLeaseError:
                    pass
            if self._stop_requested.is_set():
                raise
        except Exception as error:
            try:
                self.store.fail(job.id, job.lease_token, f"{type(error).__name__}: {error}")
            except LostJobLeaseError:
                pass
        finally:
            self._current_job_id = None
            self._touch_activity()

    async def _run_with_heartbeat(
        self,
        job: GpuJob,
        *,
        on_event: EventCallback,
        idle_before_job: float,
    ) -> dict[str, Any]:
        """Run and process one job while continuously preserving its lease."""

        async def run_and_process() -> dict[str, Any]:
            await self.runner.ensure_ready()
            result = await self._run_runner_with_timeout(job, on_event)
            lifecycle = result.setdefault("_lifecycle_metrics", {})
            if not isinstance(lifecycle, dict):
                raise ValueError("Runner lifecycle metrics must be an object")
            lifecycle["idle_before_job_seconds"] = idle_before_job
            if self.store.get(job.id).cancel_requested:
                raise asyncio.CancelledError
            processed = await asyncio.to_thread(self._result_processor.process, job, result)
            if self.store.get(job.id).cancel_requested:
                raise asyncio.CancelledError
            return processed

        work = asyncio.create_task(run_and_process(), name=f"gpu-job-{job.id}")
        heartbeat = asyncio.create_task(
            self._maintain_lease(job),
            name=f"gpu-lease-{job.id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {work, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                heartbeat_error = heartbeat.exception()
                if heartbeat_error is None:
                    heartbeat_error = LostJobLeaseError(
                        "GPU job heartbeat stopped before work completed"
                    )
                await self.runner.cancel(job.id)
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                raise heartbeat_error
            return await work
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if not work.done():
                await self.runner.cancel(job.id)
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    async def _maintain_lease(self, job: GpuJob) -> None:
        """Renew one lease until cancellation or authoritative lease loss."""

        if job.lease_token is None:
            raise RuntimeError("Claimed job has no fencing token")
        while True:
            await asyncio.sleep(self.settings.job_heartbeat_seconds)
            renewed = self.store.renew_lease(
                job_id=job.id,
                lease_token=job.lease_token,
                lease_owner=self._worker_id,
                lease_seconds=self.settings.job_lease_seconds,
            )
            if not renewed:
                raise LostJobLeaseError("GPU job heartbeat lost its lease")

    async def _run_runner_with_timeout(
        self,
        job: GpuJob,
        on_event: EventCallback,
    ) -> dict[str, Any]:
        """Run one provider task without leaving an implicit wait-for task behind."""

        runner_task = asyncio.create_task(
            self.runner.run(job, on_event),
            name=f"gpu-provider-{job.id}",
        )
        try:
            try:
                done, _pending = await asyncio.wait(
                    {runner_task},
                    timeout=self.settings.job_timeout_seconds,
                )
            except asyncio.CancelledError:
                runner_task.cancel()
                await asyncio.gather(runner_task, return_exceptions=True)
                raise
            if not done:
                await self.runner.cancel(job.id)
                runner_task.cancel()
                await asyncio.gather(runner_task, return_exceptions=True)
                raise TimeoutError(f"GPU job exceeded {self.settings.job_timeout_seconds} seconds")
            return await runner_task
        finally:
            if not runner_task.done():
                runner_task.cancel()
                await asyncio.gather(runner_task, return_exceptions=True)

    async def _wait_for_wake(self, timeout_seconds: float) -> None:
        """Observe a scheduler deadline without wait-for cancellation leakage."""

        waiter = self._wake_waiter
        if waiter is None:
            waiter = asyncio.create_task(self._wake.wait(), name="gpu-scheduler-wake")
            self._wake_waiter = waiter
        completed, _pending = await asyncio.wait({waiter}, timeout=timeout_seconds)
        if completed:
            await waiter
            self._wake_waiter = None

    async def _stop_if_idle(self) -> bool:
        if self.runner.state != RunnerState.READY:
            return False
        if self.store.has_queued() or self._current_job_id is not None:
            return False
        if self._last_activity_monotonic is None:
            self._touch_activity()
            return False
        idle_for = self._monotonic_clock() - self._last_activity_monotonic
        if idle_for < self.settings.idle_timeout_seconds:
            return False

        # Recheck after reaching the deadline. A job arriving at 4:59 must win
        # over shutdown and reuse the warm runtime.
        if self.store.has_queued():
            return False
        await self.runner.stop()
        self._last_activity_monotonic = None
        self._last_activity_wall = None
        return True

    def _next_wait_timeout(self) -> float:
        timeout = self.settings.scheduler_poll_seconds
        if self.runner.state == RunnerState.READY and self._last_activity_monotonic is not None:
            remaining = self.settings.idle_timeout_seconds - (
                self._monotonic_clock() - self._last_activity_monotonic
            )
            timeout = min(timeout, max(remaining, 0.001))
        return timeout

    def _touch_activity(self) -> None:
        self._last_activity_monotonic = self._monotonic_clock()
        self._last_activity_wall = self._wall_clock()
