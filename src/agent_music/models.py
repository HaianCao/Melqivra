"""Domain models for durable GPU jobs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class JobState(StrEnum):
    """Lifecycle of a GPU job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunnerState(StrEnum):
    """Lifecycle of the single GPU runner."""

    ABSENT = "absent"
    PROVISIONING = "provisioning"
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    DRAINING = "draining"
    STOPPING = "stopping"
    RECLAIMED = "reclaimed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GpuJob:
    """A durable job owned by one user."""

    id: str
    user_id: str
    kind: str
    payload: dict[str, Any]
    state: JobState
    created_at: float
    updated_at: float
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_requested: bool = False
    lease_token: int | None = None


@dataclass(frozen=True, slots=True)
class RunnerSnapshot:
    """Public runner state shown to API clients."""

    state: RunnerState
    session_name: str | None
    current_job_id: str | None
    last_activity_at: float | None
    idle_deadline_at: float | None
