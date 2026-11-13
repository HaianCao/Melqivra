"""Real PostgreSQL global request-rate quota contracts."""

from __future__ import annotations

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

import psycopg

from agent_music.postgres_rate_limit import PostgresRateLimiter
from agent_music.rate_limit import RateLimiterUnavailableError

POSTGRES_DSN = os.getenv("MELQIVRA_TEST_POSTGRES_DSN", "")


class PostgresRateLimiterConfigurationTests(unittest.TestCase):
    def test_constructor_rejects_invalid_connection_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "DSN"):
            PostgresRateLimiter("", request_limit=1, window_seconds=60)
        with self.assertRaisesRegex(ValueError, "pool sizes"):
            PostgresRateLimiter(
                "postgresql://unused",
                request_limit=1,
                window_seconds=60,
                min_pool_size=2,
                max_pool_size=1,
            )
        with self.assertRaisesRegex(ValueError, "timeout"):
            PostgresRateLimiter(
                "postgresql://unused",
                request_limit=1,
                window_seconds=60,
                connection_timeout_seconds=0,
            )


@unittest.skipUnless(POSTGRES_DSN, "MELQIVRA_TEST_POSTGRES_DSN is not configured")
class PostgresRateLimiterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limiter = PostgresRateLimiter(
            POSTGRES_DSN,
            request_limit=2,
            window_seconds=60,
        )
        self.addCleanup(self.limiter.close)
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            connection.execute("TRUNCATE TABLE api_rate_limit_counters")

    def test_limit_is_global_tenant_scoped_hashed_and_resets(self) -> None:
        peer = PostgresRateLimiter(
            POSTGRES_DSN,
            request_limit=2,
            window_seconds=60,
            migrate=False,
        )
        try:
            self.assertTrue(self.limiter.consume("usr_alice").allowed)
            self.assertTrue(peer.consume("usr_alice").allowed)
            denied = self.limiter.consume("usr_alice")
            self.assertFalse(denied.allowed)
            self.assertTrue(peer.consume("usr_bob").allowed)
            with psycopg.connect(POSTGRES_DSN) as connection:
                rows = connection.execute(
                    "SELECT principal_sha256, request_count FROM api_rate_limit_counters"
                ).fetchall()
            self.assertEqual(2, len(rows))
            self.assertNotIn("alice", repr(rows))
            self.assertEqual(3, max(int(row[1]) for row in rows))

            with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
                connection.execute(
                    "UPDATE api_rate_limit_counters SET window_started_at = 0, updated_at = 0"
                )
            reset = self.limiter.consume("usr_alice")
            self.assertTrue(reset.allowed)
            self.assertEqual(1, reset.remaining)
        finally:
            peer.close()

    def test_competing_processes_allow_exactly_one_remaining_request(self) -> None:
        first = PostgresRateLimiter(
            POSTGRES_DSN,
            request_limit=1,
            window_seconds=60,
            migrate=False,
        )
        second = PostgresRateLimiter(
            POSTGRES_DSN,
            request_limit=1,
            window_seconds=60,
            migrate=False,
        )
        barrier = threading.Barrier(2)

        def consume(limiter: PostgresRateLimiter) -> bool:
            barrier.wait()
            return limiter.consume("usr_competing").allowed

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                decisions = tuple(executor.map(consume, (first, second)))
        finally:
            first.close()
            second.close()
        self.assertEqual([False, True], sorted(decisions))

    def test_database_failure_is_sanitized_as_quota_unavailability(self) -> None:
        peer = PostgresRateLimiter(
            POSTGRES_DSN,
            request_limit=1,
            window_seconds=60,
            migrate=False,
        )
        peer.close()
        with self.assertRaisesRegex(
            RateLimiterUnavailableError,
            "Request-rate quota is unavailable",
        ) as captured:
            peer.consume("usr_alice")
        self.assertNotIn(POSTGRES_DSN, str(captured.exception))


if __name__ == "__main__":
    unittest.main()
