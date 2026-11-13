"""Versioned FastAPI service boundary for durable GPU jobs."""

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHttpException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_music.artifact_service import ArtifactReader
from agent_music.authentication import (
    AuthenticationError,
    Authenticator,
    DevelopmentHeaderAuthenticator,
    Principal,
)
from agent_music.config import HarnessSettings
from agent_music.domain import ArtifactId, UserId
from agent_music.job_store import (
    InvalidJobCursorError,
    InvalidJobEventCursorError,
    UserJobQuotaExceededError,
)
from agent_music.models import GpuJob, JobEvent, JobState
from agent_music.scheduler import GpuHarness

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EVENT_CURSOR = re.compile(r"^(?:0|[1-9][0-9]{0,18})$")
_TERMINAL_JOB_STATES = {
    JobState.SUCCEEDED,
    JobState.FAILED,
    JobState.CANCELLED,
    JobState.DEAD_LETTERED,
}


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


class JobEventResponse(BaseModel):
    """Tenant-visible durable job event."""

    job_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    job_state: str
    attempt_number: int
    created_at: float


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


class RequestContractMiddleware:
    """Attach request identity and security headers without task-group wrapping."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        supplied = Headers(scope=scope).get("x-request-id", "")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else f"req_{uuid.uuid4().hex}"
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_contract(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Cache-Control"] = "no-store"
            await send(message)

        await self._app(scope, receive, send_with_contract)


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
    artifact_reader: ArtifactReader | None = None,
) -> FastAPI:
    """Build an application with owned or explicitly injected service adapters."""

    resolved_settings = settings or HarnessSettings.from_env()
    if authenticator is None:
        if resolved_settings.api_auth_mode != "development":
            raise RuntimeError("API_AUTH_MODE=injected requires an Authenticator")
        authenticator = DevelopmentHeaderAuthenticator()
    resolved_authenticator = authenticator
    resolved_harness = harness or GpuHarness(resolved_settings)
    resolved_artifact_reader = artifact_reader or resolved_harness.artifact_reader

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
    app.add_middleware(RequestContractMiddleware)

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
        "/v1/gpu/jobs/{job_id}/events",
        responses={
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
        },
    )
    async def stream_job_events(
        job_id: str,
        request: Request,
        principal: PrincipalDependency,
    ) -> Response:
        active = get_harness(request)
        user_id = str(principal.user_id)
        after_sequence = _parse_event_cursor(request.headers.get("last-event-id"))
        try:
            initial_job = active.get_job_for_user(user_id=user_id, job_id=job_id)
            initial_events = active.store.list_events_for_user(
                user_id=user_id,
                job_id=job_id,
                after_sequence=after_sequence,
                limit=resolved_settings.api_sse_batch_size,
            )
        except KeyError:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                "job_not_found",
                "Job not found",
            ) from None
        except InvalidJobEventCursorError as error:
            raise ApiError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_event_cursor",
                "Job event cursor is invalid",
            ) from error

        response_headers = {"X-Accel-Buffering": "no"}
        if initial_job.state in _TERMINAL_JOB_STATES:
            return Response(
                content="".join(_serialize_sse_event(event) for event in initial_events),
                media_type="text/event-stream",
                headers=response_headers,
            )

        async def event_stream() -> AsyncIterator[str]:
            cursor = after_sequence
            pending = initial_events
            loop = asyncio.get_running_loop()
            keepalive_at = loop.time() + resolved_settings.api_sse_keepalive_seconds
            while True:
                for event in pending:
                    cursor = event.sequence
                    yield _serialize_sse_event(event)

                job = await asyncio.to_thread(
                    active.get_job_for_user,
                    user_id=user_id,
                    job_id=job_id,
                )
                pending = await asyncio.to_thread(
                    active.store.list_events_for_user,
                    user_id=user_id,
                    job_id=job_id,
                    after_sequence=cursor,
                    limit=resolved_settings.api_sse_batch_size,
                )
                if pending:
                    continue
                if job.state in _TERMINAL_JOB_STATES:
                    return
                if await request.is_disconnected():
                    return

                await asyncio.sleep(resolved_settings.api_sse_poll_seconds)
                if loop.time() >= keepalive_at:
                    yield ": keepalive\n\n"
                    keepalive_at = loop.time() + resolved_settings.api_sse_keepalive_seconds

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers=response_headers,
        )

    @app.get(
        "/v1/artifacts/{artifact_id}",
        responses={
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def download_artifact(
        artifact_id: str,
        request: Request,
        principal: PrincipalDependency,
    ) -> Response:
        if resolved_artifact_reader is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                "artifact_not_found",
                "Artifact not found",
            )
        try:
            download = await asyncio.to_thread(
                resolved_artifact_reader.read_for_owner,
                owner_id=UserId(str(principal.user_id)),
                artifact_id=ArtifactId(artifact_id),
            )
        except (KeyError, ValueError):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                "artifact_not_found",
                "Artifact not found",
            ) from None
        except Exception as error:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "artifact_unavailable",
                "Artifact is temporarily unavailable",
            ) from error
        return Response(
            content=download.content,
            media_type=download.media_type,
            headers={
                "Cache-Control": "private, no-store",
                "ETag": f'"sha256:{download.sha256}"',
                "X-Artifact-SHA256": download.sha256,
            },
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


def _parse_event_cursor(value: str | None) -> int:
    if value is None or value == "":
        return 0
    if _EVENT_CURSOR.fullmatch(value) is None:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_event_cursor",
            "Job event cursor is invalid",
        )
    sequence = int(value)
    if sequence > 9_223_372_036_854_775_807:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_event_cursor",
            "Job event cursor is invalid",
        )
    return sequence


def _serialize_sse_event(event: JobEvent) -> str:
    body = JobEventResponse(
        job_id=event.job_id,
        sequence=event.sequence,
        event_type=event.event_type,
        payload=event.payload,
        job_state=event.job_state.value,
        attempt_number=event.attempt_number,
        created_at=event.created_at,
    )
    data = json.dumps(
        body.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"id: {event.sequence}\nevent: {event.event_type}\ndata: {data}\n\n"
