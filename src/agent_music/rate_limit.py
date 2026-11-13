"""Provider-neutral request-rate quota contracts and development adapter."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class RateLimiterUnavailableError(RuntimeError):
    """The configured global quota authority is unavailable."""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """One fixed-window request-quota decision without tenant identity."""

    allowed: bool
    limit: int
    remaining: int
    reset_after_seconds: int


class RateLimiter(Protocol):
    """Tenant-scoped request quota shared by API authentication dependencies."""

    def close(self) -> None: ...

    def consume(self, subject: str) -> RateLimitDecision: ...


class InMemoryRateLimiter:
    """Bounded single-process fixed-window limiter for local development."""

    def __init__(
        self,
        *,
        request_limit: int,
        window_seconds: float,
        max_tracked_subjects: int = 10_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        validate_rate_limit_policy(request_limit, window_seconds)
        if max_tracked_subjects < 1:
            raise ValueError("Rate-limit subject bound must be positive")
        self._request_limit = request_limit
        self._window_seconds = window_seconds
        self._max_tracked_subjects = max_tracked_subjects
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[float, int, float]] = {}

    def close(self) -> None:
        """Release all process-local counters."""

        with self._lock:
            self._windows.clear()

    def consume(self, subject: str) -> RateLimitDecision:
        """Consume one request from the current wall-clock window."""

        key = rate_limit_subject_sha256(subject)
        now = float(self._clock())
        if not math.isfinite(now) or now < 0:
            raise RateLimiterUnavailableError("Rate-limit clock is invalid")
        window_started_at = math.floor(now / self._window_seconds) * self._window_seconds
        with self._lock:
            self._remove_expired(window_started_at)
            prior = self._windows.get(key)
            if prior is None or prior[0] != window_started_at:
                count = 1
            else:
                count = min(prior[1] + 1, self._request_limit + 1)
            if key not in self._windows and len(self._windows) >= self._max_tracked_subjects:
                oldest_key = min(self._windows, key=lambda item: self._windows[item][2])
                del self._windows[oldest_key]
            self._windows[key] = (window_started_at, count, now)
        return build_rate_limit_decision(
            count=count,
            request_limit=self._request_limit,
            reset_at=window_started_at + self._window_seconds,
            now=now,
        )

    def _remove_expired(self, current_window: float) -> None:
        expired = [key for key, value in self._windows.items() if value[0] < current_window]
        for key in expired:
            del self._windows[key]


def rate_limit_subject_sha256(subject: str) -> str:
    """Return a stable opaque quota key without storing raw tenant identity."""

    normalized = subject.strip()
    if not normalized or len(normalized.encode("utf-8")) > 256:
        raise ValueError("Rate-limit subject is invalid")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def validate_rate_limit_policy(request_limit: int, window_seconds: float) -> None:
    if request_limit < 1:
        raise ValueError("Rate-limit request count must be positive")
    if not math.isfinite(window_seconds) or window_seconds <= 0:
        raise ValueError("Rate-limit window must be positive")


def build_rate_limit_decision(
    *,
    count: int,
    request_limit: int,
    reset_at: float,
    now: float,
) -> RateLimitDecision:
    return RateLimitDecision(
        allowed=count <= request_limit,
        limit=request_limit,
        remaining=max(0, request_limit - count),
        reset_after_seconds=max(1, math.ceil(reset_at - now)),
    )
