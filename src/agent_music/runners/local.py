"""Local CUDA runner placeholder using the same job contract."""

from __future__ import annotations

from typing import Any

from agent_music.models import GpuJob, RunnerState
from agent_music.runners.base import EventCallback, GpuRunner


class LocalCudaRunner(GpuRunner):
    """Explicit placeholder until the shared ACE-Step service is added.

    It fails clearly instead of silently falling back to CPU. The interface is
    already production-shaped and covered by the same scheduler.
    """

    def __init__(self) -> None:
        self._state = RunnerState.STOPPED

    @property
    def state(self) -> RunnerState:
        return self._state

    @property
    def session_name(self) -> str | None:
        return "local-cuda" if self._state != RunnerState.STOPPED else None

    async def ensure_ready(self) -> None:
        raise RuntimeError(
            "LocalCudaRunner requires the ACE-Step worker implementation; "
            "use GPU_RUNNER=fake or GPU_RUNNER=colab for this harness release"
        )

    async def run(self, job: GpuJob, on_event: EventCallback) -> dict[str, Any]:
        _ = job, on_event
        raise RuntimeError("Local CUDA worker is not configured")

    async def cancel(self, job_id: str) -> None:
        _ = job_id

    async def stop(self) -> None:
        self._state = RunnerState.STOPPED
