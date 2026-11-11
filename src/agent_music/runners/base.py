"""Runner contract shared by Colab, local CUDA, and tests."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from agent_music.models import GpuJob, RunnerState

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class GpuRunner(ABC):
    """Single-capacity GPU runtime."""

    @property
    @abstractmethod
    def state(self) -> RunnerState:
        """Return the current runtime state."""

    @property
    @abstractmethod
    def session_name(self) -> str | None:
        """Return the external session identifier."""

    @abstractmethod
    async def ensure_ready(self) -> None:
        """Provision and warm the runtime if needed."""

    @abstractmethod
    async def run(self, job: GpuJob, on_event: EventCallback) -> dict[str, Any]:
        """Run one job and return a manifest-like result."""

    @abstractmethod
    async def cancel(self, job_id: str) -> None:
        """Attempt cooperative cancellation."""

    @abstractmethod
    async def stop(self) -> None:
        """Release runtime resources."""
