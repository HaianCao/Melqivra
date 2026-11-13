"""Deterministic no-GPU runner for local development and CI."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_music.models import GpuJob, RunnerState
from agent_music.runners.base import EventCallback, GpuRunner


class FakeRunner(GpuRunner):
    """A runner that enforces and exposes concurrency for tests."""

    def __init__(self, job_seconds: float = 0.05) -> None:
        self._job_seconds = job_seconds
        self._state = RunnerState.STOPPED
        self._session_name: str | None = None
        self._cancelled: set[str] = set()
        self.provision_count = 0
        self.stop_count = 0
        self.cancel_count = 0
        self.active_jobs = 0
        self.max_active_jobs = 0
        self.completed_job_ids: list[str] = []

    @property
    def state(self) -> RunnerState:
        return self._state

    @property
    def session_name(self) -> str | None:
        return self._session_name

    async def ensure_ready(self) -> None:
        if self._state in {RunnerState.READY, RunnerState.BUSY}:
            return
        self._state = RunnerState.STARTING
        await asyncio.sleep(0)
        self.provision_count += 1
        self._session_name = f"fake-session-{self.provision_count}"
        self._state = RunnerState.READY

    async def run(self, job: GpuJob, on_event: EventCallback) -> dict[str, Any]:
        if self._state != RunnerState.READY:
            raise RuntimeError("Fake runner is not ready")
        self._state = RunnerState.BUSY
        self.active_jobs += 1
        self.max_active_jobs = max(self.max_active_jobs, self.active_jobs)
        try:
            await on_event({"type": "stage", "stage": "generating", "progress": 0.1})
            step = max(self._job_seconds / 4, 0.001)
            for index in range(4):
                await asyncio.sleep(step)
                if job.id in self._cancelled:
                    raise asyncio.CancelledError
                await on_event(
                    {
                        "type": "stage",
                        "stage": "generating",
                        "progress": (index + 1) / 4,
                    }
                )
            self.completed_job_ids.append(job.id)
            return {
                "runner": "fake",
                "job_id": job.id,
                "kind": job.kind,
                "payload": job.payload,
            }
        finally:
            self._cancelled.discard(job.id)
            self.active_jobs -= 1
            self._state = RunnerState.READY

    async def cancel(self, job_id: str) -> None:
        self.cancel_count += 1
        self._cancelled.add(job_id)

    async def stop(self) -> None:
        if self._state == RunnerState.STOPPED:
            return
        self._state = RunnerState.STOPPING
        await asyncio.sleep(0)
        self.stop_count += 1
        self._session_name = None
        self._state = RunnerState.STOPPED
