"""Versioned FastAPI service boundary for durable GPU jobs."""

import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHttpException

from agent_music.authentication import (
    AuthenticationError,
    Authenticator,
    DevelopmentHeaderAuthenticator,
    Principal,
)
from agent_music.config import HarnessSettings
from agent_music.job_store import InvalidJobCursorError, UserJobQuotaExceededError
from agent_music.models import GpuJob
from agent_music.scheduler import GpuHarness

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SubmitJobRequest(BaseModel):
    """API request for one GPU job."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class JobResponse(BaseModel):
    """Serialized durable job without lease or provider credentials."""

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


class JobPageResponse(BaseModel):
    """Stable tenant-scoped job page."""

    items: list[JobResponse]
    next_cursor: str | None


class ErrorDetail(BaseModel):
    """Stable versioned error payload."""

    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    """Top-level API error envelope."""

    error: ErrorDetail


class ApiError(RuntimeError):
    """Safe public service error with a stable machine-readable code."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


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
    authenticator: Authenticator | None = None,
) -> FastAPI:
    """Build an application with owned or explicitly injected service adapters."""

    resolved_settings = settings or HarnessSettings.from_env()
    if authenticator is None:
        if resolved_settings.api_auth_mode != "development":
            raise RuntimeError("API_AUTH_MODE=injected requires an Authenticator")
        authenticator = DevelopmentHeaderAuthenticator()
    resolved_authenticator = authenticator
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
        title="Melqivra Service API",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_contract(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else f"req_{uuid.uuid4().hex}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content=_error_body(request, error.code, error.message),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        del error
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_error_body(request, "invalid_request", "Request validation failed"),
        )

    @app.exception_handler(StarletteHttpException)
    async def http_error_handler(
        request: Request,
        error: StarletteHttpException,
    ) -> JSONResponse:
        code = "route_not_found" if error.status_code == 404 else "http_error"
        message = "Route not found" if error.status_code == 404 else "HTTP request failed"
        return JSONResponse(
            status_code=error.status_code,
            content=_error_body(request, code, message),
        )

    def get_harness(request: Request) -> GpuHarness:
        return cast(GpuHarness, request.app.state.harness)

    def get_principal(request: Request) -> Principal:
        try:
            return resolved_authenticator.authenticate(request.headers)
        except AuthenticationError as error:
            raise ApiError(
                status.HTTP_401_UNAUTHORIZED,
                "authentication_required",
                str(error),
            ) from error

    PrincipalDependency = Annotated[Principal, Depends(get_principal)]

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/gpu/runner")
    async def runner_status(
        request: Request,
        principal: PrincipalDependency,
    ) -> dict[str, Any]:
        del principal
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
        responses={
            401: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
        },
    )
    async def submit_job(
        body: SubmitJobRequest,
        request: Request,
        principal: PrincipalDependency,
    ) -> JobResponse:
        active = get_harness(request)
        payload_bytes = len(
            json.dumps(
                body.payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        if payload_bytes > resolved_settings.api_max_job_payload_bytes:
            raise ApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                "payload_too_large",
                "Job payload exceeds the configured size limit",
            )
        try:
            job = active.enqueue(
                user_id=str(principal.user_id),
                kind=body.kind,
                payload=body.payload,
                idempotency_key=body.idempotency_key,
                max_active_jobs=resolved_settings.api_max_active_jobs_per_user,
            )
        except UserJobQuotaExceededError as error:
            raise ApiError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "active_job_quota_exceeded",
                "Active GPU job quota exceeded",
            ) from error
        return _serialize_job(active, job)

    @app.get(
        "/v1/gpu/jobs",
        response_model=JobPageResponse,
        responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
    )
    async def list_jobs(
        request: Request,
        principal: PrincipalDependency,
        limit: Annotated[int | None, Query(ge=1)] = None,
        cursor: str | None = None,
    ) -> JobPageResponse:
        page_limit = limit or min(20, resolved_settings.api_max_page_size)
        if page_limit > resolved_settings.api_max_page_size:
            raise ApiError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_page_size",
                "Requested page size exceeds the configured maximum",
            )
        active = get_harness(request)
        try:
            page = active.store.list_for_user(
                user_id=str(principal.user_id),
                limit=page_limit,
                cursor=cursor,
            )
        except InvalidJobCursorError as error:
            raise ApiError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_cursor",
                "Job cursor is invalid",
            ) from error
        return JobPageResponse(
            items=[_serialize_job(active, job) for job in page.items],
            next_cursor=page.next_cursor,
        )

    @app.get(
        "/v1/gpu/jobs/{job_id}",
        response_model=JobResponse,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def get_job(
        job_id: str,
        request: Request,
        principal: PrincipalDependency,
    ) -> JobResponse:
        active = get_harness(request)
        try:
            job = active.get_job_for_user(user_id=str(principal.user_id), job_id=job_id)
        except KeyError:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                "job_not_found",
                "Job not found",
            ) from None
        return _serialize_job(active, job)

    @app.delete(
        "/v1/gpu/jobs/{job_id}",
        response_model=JobResponse,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def cancel_job(
        job_id: str,
        request: Request,
        principal: PrincipalDependency,
    ) -> JobResponse:
        active = get_harness(request)
        try:
            job = await active.cancel_for_user(
                user_id=str(principal.user_id),
                job_id=job_id,
            )
        except KeyError:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                "job_not_found",
                "Job not found",
            ) from None
        return _serialize_job(active, job)

    def service_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        if resolved_settings.api_auth_mode == "development":
            security_name = "DevelopmentUserHeader"
            security_schemes[security_name] = {
                "type": "apiKey",
                "in": "header",
                "name": "X-User-Id",
                "description": "Local development identity only",
            }
        else:
            security_name = "BearerAuth"
            security_schemes[security_name] = {
                "type": "http",
                "scheme": "bearer",
            }
        paths = schema.get("paths", {})
        if isinstance(paths, dict):
            for path, operations in paths.items():
                if not isinstance(path, str) or not path.startswith("/v1/gpu"):
                    continue
                if not isinstance(operations, dict):
                    continue
                for operation in operations.values():
                    if isinstance(operation, dict):
                        operation["security"] = [{security_name: []}]
        app.openapi_schema = schema
        return schema

    app.openapi = service_openapi  # type: ignore[method-assign]

    return app


def _error_body(request: Request, code: str, message: str) -> dict[str, dict[str, str]]:
    request_id = getattr(request.state, "request_id", "req_unknown")
    return {"error": {"code": code, "message": message, "request_id": request_id}}
