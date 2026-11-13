"""Content-free HTTP metrics, route labels, and trace correlation."""

from __future__ import annotations

import math
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

_TRACEPARENT = re.compile(
    r"^00-(?P<trace_id>[0-9a-f]{32})-(?P<parent_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
_JOB_ITEM = re.compile(r"^/v1/gpu/jobs/[^/]+$")
_JOB_EVENTS = re.compile(r"^/v1/gpu/jobs/[^/]+/events$")
_ARTIFACT_ITEM = re.compile(r"^/v1/artifacts/[^/]+$")


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Validated W3C trace correlation returned to the caller."""

    trace_id: str
    span_id: str
    traceparent: str


@dataclass(frozen=True, slots=True)
class RouteMetrics:
    """Bounded aggregate for one allowlisted route class."""

    requests: int
    duration_milliseconds_total: float
    duration_milliseconds_max: float


@dataclass(frozen=True, slots=True)
class ServiceMetricsSnapshot:
    """Process-local content-free metrics suitable for aggregation."""

    schema_version: int
    process_started_at: float
    active_requests: int
    requests_total: int
    rate_limited_total: int
    responses_by_status_class: dict[str, int]
    routes: dict[str, RouteMetrics]


class InMemoryServiceTelemetry:
    """Thread-safe process metrics with bounded label cardinality."""

    def __init__(self, *, wall_clock: Callable[[], float] = time.time) -> None:
        started_at = float(wall_clock())
        if not math.isfinite(started_at) or started_at < 0:
            raise ValueError("Telemetry wall clock must not be negative")
        self._started_at = started_at
        self._lock = threading.Lock()
        self._active_requests = 0
        self._requests_total = 0
        self._rate_limited_total = 0
        self._status_classes: dict[str, int] = {}
        self._routes: dict[str, RouteMetrics] = {}

    def request_started(self) -> None:
        """Increment the active request gauge."""

        with self._lock:
            self._active_requests += 1

    def request_finished(
        self,
        *,
        route_class: str,
        status_code: int,
        duration_milliseconds: float,
        rate_limited: bool,
    ) -> None:
        """Record one request without tenant, path parameter, or content labels."""

        if route_class not in allowed_route_classes():
            raise ValueError("Telemetry route class is not allowlisted")
        if status_code < 100 or status_code > 599:
            raise ValueError("Telemetry status code is invalid")
        if not math.isfinite(duration_milliseconds) or duration_milliseconds < 0:
            raise ValueError("Telemetry duration must not be negative")
        status_class = f"{status_code // 100}xx"
        with self._lock:
            self._active_requests = max(0, self._active_requests - 1)
            self._requests_total += 1
            if rate_limited:
                self._rate_limited_total += 1
            self._status_classes[status_class] = self._status_classes.get(status_class, 0) + 1
            prior = self._routes.get(route_class, RouteMetrics(0, 0.0, 0.0))
            self._routes[route_class] = RouteMetrics(
                requests=prior.requests + 1,
                duration_milliseconds_total=round(
                    prior.duration_milliseconds_total + duration_milliseconds,
                    3,
                ),
                duration_milliseconds_max=round(
                    max(prior.duration_milliseconds_max, duration_milliseconds),
                    3,
                ),
            )

    def snapshot(self) -> ServiceMetricsSnapshot:
        """Return a stable copy without live mutable mappings."""

        with self._lock:
            return ServiceMetricsSnapshot(
                schema_version=1,
                process_started_at=self._started_at,
                active_requests=self._active_requests,
                requests_total=self._requests_total,
                rate_limited_total=self._rate_limited_total,
                responses_by_status_class=dict(sorted(self._status_classes.items())),
                routes=dict(sorted(self._routes.items())),
            )


def resolve_trace_context(traceparent: str | None) -> TraceContext:
    """Accept one strict W3C trace ID or create a new opaque correlation."""

    matched = _TRACEPARENT.fullmatch(traceparent or "")
    if (
        matched is not None
        and matched.group("trace_id") != "0" * 32
        and matched.group("parent_id") != "0" * 16
    ):
        trace_id = matched.group("trace_id")
        flags = matched.group("flags")
    else:
        trace_id = uuid.uuid4().hex
        flags = "01"
    if trace_id == "0" * 32:
        trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    return TraceContext(
        trace_id=trace_id,
        span_id=span_id,
        traceparent=f"00-{trace_id}-{span_id}-{flags}",
    )


def classify_http_route(path: str) -> str:
    """Map raw request paths to a finite content-free route vocabulary."""

    exact = {
        "/healthz": "health",
        "/openapi.json": "openapi",
        "/docs": "docs",
        "/v1/gpu/runner": "gpu_runner",
        "/v1/gpu/jobs": "gpu_jobs_collection",
        "/v1/operations/metrics": "operations_metrics",
    }
    if path in exact:
        return exact[path]
    if _JOB_EVENTS.fullmatch(path):
        return "gpu_job_events"
    if _JOB_ITEM.fullmatch(path):
        return "gpu_job_item"
    if _ARTIFACT_ITEM.fullmatch(path):
        return "artifact_item"
    return "unknown"


def allowed_route_classes() -> frozenset[str]:
    """Return the complete bounded metrics label vocabulary."""

    return frozenset(
        {
            "artifact_item",
            "docs",
            "gpu_job_events",
            "gpu_job_item",
            "gpu_jobs_collection",
            "gpu_runner",
            "health",
            "openapi",
            "operations_metrics",
            "unknown",
        }
    )
