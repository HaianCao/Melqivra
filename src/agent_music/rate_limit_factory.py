"""Explicit request-rate quota composition."""

from __future__ import annotations

from agent_music.config import HarnessSettings
from agent_music.rate_limit import InMemoryRateLimiter, RateLimiter


def build_rate_limiter(settings: HarnessSettings) -> RateLimiter:
    """Build a process-local development or PostgreSQL-global rate limiter."""

    settings.validate()
    if settings.job_store == "postgres":
        from agent_music.postgres_rate_limit import PostgresRateLimiter

        return PostgresRateLimiter(
            settings.postgres_dsn,
            request_limit=settings.api_rate_limit_requests,
            window_seconds=settings.api_rate_limit_window_seconds,
            min_pool_size=settings.postgres_pool_min_size,
            max_pool_size=settings.postgres_pool_max_size,
            connection_timeout_seconds=settings.postgres_connect_timeout_seconds,
        )
    return InMemoryRateLimiter(
        request_limit=settings.api_rate_limit_requests,
        window_seconds=settings.api_rate_limit_window_seconds,
    )
