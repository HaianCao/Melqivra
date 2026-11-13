"""PostgreSQL-backed global request-rate quota."""

from __future__ import annotations

import math

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from agent_music.postgres_migrations import PostgresMigrator, PostgresPool
from agent_music.rate_limit import (
    RateLimitDecision,
    RateLimiterUnavailableError,
    build_rate_limit_decision,
    rate_limit_subject_sha256,
    validate_rate_limit_policy,
)


class PostgresRateLimiter:
    """Enforce one tenant quota atomically across every API process."""

    def __init__(
        self,
        dsn: str,
        *,
        request_limit: int,
        window_seconds: float,
        min_pool_size: int = 1,
        max_pool_size: int = 4,
        connection_timeout_seconds: float = 10.0,
        migrate: bool = True,
    ) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN must not be empty")
        validate_rate_limit_policy(request_limit, window_seconds)
        if min_pool_size < 1 or max_pool_size < min_pool_size:
            raise ValueError("PostgreSQL pool sizes are invalid")
        if not math.isfinite(connection_timeout_seconds) or connection_timeout_seconds <= 0:
            raise ValueError("PostgreSQL connection timeout must be positive")
        self._request_limit = request_limit
        self._window_seconds = window_seconds
        self._pool: PostgresPool = ConnectionPool(
            dsn,
            min_size=min_pool_size,
            max_size=max_pool_size,
            timeout=connection_timeout_seconds,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        try:
            self._pool.wait(timeout=connection_timeout_seconds)
            if migrate:
                PostgresMigrator(self._pool).apply()
        except BaseException:
            self._pool.close()
            raise

    def close(self) -> None:
        """Close all pooled PostgreSQL connections."""

        self._pool.close()

    def consume(self, subject: str) -> RateLimitDecision:
        """Atomically consume one request in a database-clock fixed window."""

        subject_sha256 = rate_limit_subject_sha256(subject)
        saturation_count = self._request_limit + 1
        try:
            with self._pool.connection() as connection, connection.transaction():
                row = connection.execute(
                    """
                    WITH current_clock AS (
                        SELECT EXTRACT(EPOCH FROM clock_timestamp())::DOUBLE PRECISION AS now
                    ), current_window AS (
                        SELECT
                            now,
                            FLOOR(now / %s) * %s AS window_started_at
                        FROM current_clock
                    ), consumed AS (
                        INSERT INTO api_rate_limit_counters(
                            principal_sha256,
                            window_started_at,
                            request_count,
                            updated_at
                        )
                        SELECT %s, window_started_at, 1, now
                        FROM current_window
                        ON CONFLICT(principal_sha256) DO UPDATE
                        SET window_started_at = EXCLUDED.window_started_at,
                            request_count = CASE
                                WHEN api_rate_limit_counters.window_started_at =
                                     EXCLUDED.window_started_at
                                THEN LEAST(
                                    api_rate_limit_counters.request_count + 1,
                                    %s
                                )
                                ELSE 1
                            END,
                            updated_at = EXCLUDED.updated_at
                        RETURNING request_count, window_started_at
                    )
                    SELECT consumed.request_count, consumed.window_started_at, current_clock.now
                    FROM consumed CROSS JOIN current_clock
                    """,
                    (
                        self._window_seconds,
                        self._window_seconds,
                        subject_sha256,
                        saturation_count,
                    ),
                ).fetchone()
        except Exception as error:
            raise RateLimiterUnavailableError("Request-rate quota is unavailable") from error
        if row is None:
            raise RateLimiterUnavailableError("Request-rate quota returned no decision")
        now = float(row["now"])
        window_started_at = float(row["window_started_at"])
        return build_rate_limit_decision(
            count=int(row["request_count"]),
            request_limit=self._request_limit,
            reset_at=window_started_at + self._window_seconds,
            now=now,
        )
