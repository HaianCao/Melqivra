"""Single-capacity GPU scheduler with on-demand start and idle shutdown."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from agent_music.config import HarnessSettings
from agent_music.models import GpuJob, JobState, RunnerSnapshot, RunnerState
from agent_music.result_processing import (
    JobResultProcessor,
    build_default_result_processor,
)
from agent_music.runners.base import GpuRunner
from agent_music.runners.strategy import build_runner
from agent_music.store import SqliteJobStore

EventSink = Callable[[str, dict[str, Any]], None]


class GpuHarness:
    """Durable FIFO scheduler with exactly one GPU execution slot."""

    def __init__(
        self,
        settings: HarnessSettings,
        *,
        store: SqliteJobStore | None = None,
        runner: GpuRunner | None = None,
        event_sink: EventSink | None = None,
        result_processor: JobResultProcessor | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        settings.validate()
        settings.ensure_directories()
        self.settings = settings
        self.store = store or SqliteJobStore(settings.database_path)
        self.runner = runner or build_runner(settings)
        self._event_sink = event_sink or (lambda _job_id, _event: None)
        self._result_processor = result_processor or build_default_result_processor(settings)
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._wake = asyncio.Event()
        self._stop_requested = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._current_job_id: str | None = None
        self._last_activity_monotonic: float | None = None
        self._last_activity_wall: float | None = None

    async def start(self) -> None:
        """Recover interrupted work and start the scheduling loop."""

        if self._task is not None:
            return
        self.store.recover_interrupted()
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
        if job.state == JobState.RUNNING:
            await self.runner.cancel(job_id)
        self._wake.set()
        return self.store.get(job_id)

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
        while not self._stop_requested.is_set():
            job = self.store.claim_next()
            if job is not None:
                await self._execute(job)
                continue

            if await self._stop_if_idle():
                continue

            self._wake.clear()
            timeout = self._next_wait_timeout()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                pass

    async def _execute(self, job: GpuJob) -> None:
        if job.lease_token is None:
            raise RuntimeError("Claimed job has no fencing token")
        self._current_job_id = job.id
        self._touch_activity()

        async def on_event(event: dict[str, Any]) -> None:
            self._event_sink(job.id, event)

        try:
            await self.runner.ensure_ready()
            result = await asyncio.wait_for(
                self.runner.run(job, on_event),
                timeout=self.settings.job_timeout_seconds,
            )
            latest = self.store.get(job.id)
            if latest.cancel_requested:
                raise asyncio.CancelledError
            processed = await asyncio.to_thread(self._result_processor.process, job, result)
            latest = self.store.get(job.id)
            if latest.cancel_requested:
                raise asyncio.CancelledError
            self.store.complete(job.id, job.lease_token, processed)
        except asyncio.CancelledError:
            latest = self.store.get(job.id)
            if latest.state == JobState.RUNNING:
                self.store.cancel_running(job.id, job.lease_token)
            if self._stop_requested.is_set():
                raise
        except Exception as error:
            self.store.fail(job.id, job.lease_token, f"{type(error).__name__}: {error}")
        finally:
            self._current_job_id = None
            self._touch_activity()

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
