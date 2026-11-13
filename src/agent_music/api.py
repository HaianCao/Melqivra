"""FastAPI surface for the durable GPU harness."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, cast

from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from agent_music.config import HarnessSettings
from agent_music.models import GpuJob
from agent_music.scheduler import GpuHarness


class SubmitJobRequest(BaseModel):
    """API request for one GPU job."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)


class JobResponse(BaseModel):
    """Serialized durable job."""

    id: str
    user_id: str
    kind: str
    payload: dict[str, Any]
    state: str
    created_at: float
    updated_at: float
    started_at: float | None
    finished_at: float | None
    result: dict[str, Any] | None
    error: str | None
    cancel_requested: bool
    attempt_count: int
    max_attempts: int
    queue_position: int | None


def _serialize_job(harness: GpuHarness, job: GpuJob) -> JobResponse:
    return JobResponse(
        id=job.id,
        user_id=job.user_id,
        kind=job.kind,
        payload=job.payload,
        state=job.state.value,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        result=job.result,
        error=job.error,
        cancel_requested=job.cancel_requested,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        queue_position=harness.store.queue_position(job.id),
    )


def create_app(
    settings: HarnessSettings | None = None,
    harness: GpuHarness | None = None,
) -> FastAPI:
    """Build an application with an owned or injected harness."""

    resolved_settings = settings or HarnessSettings.from_env()
    resolved_harness = harness or GpuHarness(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.harness = resolved_harness
        await resolved_harness.start()
        try:
            yield
        finally:
            await resolved_harness.close()

    app = FastAPI(
        title="Melqivra GPU Harness",
        version="0.1.0",
        lifespan=lifespan,
    )

    def get_harness(request: Request) -> GpuHarness:
        return cast(GpuHarness, request.app.state.harness)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/gpu/runner")
    async def runner_status(request: Request) -> dict[str, Any]:
        active = get_harness(request)
        snapshot = active.snapshot()
        return {
            **asdict(snapshot),
            "state": snapshot.state.value,
            "strategy": active.settings.runner,
            "idle_timeout_seconds": active.settings.idle_timeout_seconds,
            "max_concurrency": active.settings.gpu_max_concurrency,
        }

    @app.post(
        "/v1/gpu/jobs",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_job(
        body: SubmitJobRequest,
        request: Request,
        x_user_id: str = Header(min_length=1, max_length=200),
    ) -> JobResponse:
        active = get_harness(request)
        job = active.enqueue(
            user_id=x_user_id,
            kind=body.kind,
            payload=body.payload,
            idempotency_key=body.idempotency_key,
        )
        return _serialize_job(active, job)

    @app.get("/v1/gpu/jobs/{job_id}", response_model=JobResponse)
    async def get_job(
        job_id: str,
        request: Request,
        x_user_id: str = Header(min_length=1, max_length=200),
    ) -> JobResponse:
        active = get_harness(request)
        try:
            job = active.get_job_for_user(user_id=x_user_id, job_id=job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Job not found") from None
        return _serialize_job(active, job)

    @app.delete("/v1/gpu/jobs/{job_id}", response_model=JobResponse)
    async def cancel_job(
        job_id: str,
        request: Request,
        x_user_id: str = Header(min_length=1, max_length=200),
    ) -> JobResponse:
        active = get_harness(request)
        try:
            job = await active.cancel_for_user(user_id=x_user_id, job_id=job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Job not found") from None
        return _serialize_job(active, job)

    return app
