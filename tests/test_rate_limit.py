"""Request-rate quota contracts."""

from __future__ import annotations

import unittest

from agent_music.rate_limit import (
    InMemoryRateLimiter,
    RateLimiterUnavailableError,
    rate_limit_subject_sha256,
)


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class InMemoryRateLimiterTests(unittest.TestCase):
    def test_fixed_window_is_tenant_scoped_bounded_and_resets(self) -> None:
        clock = MutableClock(120.0)
        limiter = InMemoryRateLimiter(
            request_limit=2,
            window_seconds=60,
            max_tracked_subjects=2,
            clock=clock,
        )

        first = limiter.consume("usr_alice")
        second = limiter.consume("usr_alice")
        denied = limiter.consume("usr_alice")
        other = limiter.consume("usr_bob")

        self.assertTrue(first.allowed)
        self.assertEqual(1, first.remaining)
        self.assertTrue(second.allowed)
        self.assertEqual(0, second.remaining)
        self.assertFalse(denied.allowed)
        self.assertEqual(60, denied.reset_after_seconds)
        self.assertTrue(other.allowed)

        clock.value = 180.0
        reset = limiter.consume("usr_alice")
        self.assertTrue(reset.allowed)
        self.assertEqual(1, reset.remaining)
        limiter.close()

    def test_subjects_are_hashed_and_invalid_clock_or_identity_is_rejected(self) -> None:
        digest = rate_limit_subject_sha256("usr_alice")
        self.assertEqual(64, len(digest))
        self.assertNotIn("alice", digest)
        with self.assertRaises(ValueError):
            rate_limit_subject_sha256("")
        with self.assertRaises(ValueError):
            InMemoryRateLimiter(request_limit=0, window_seconds=60)

        limiter = InMemoryRateLimiter(
            request_limit=1,
            window_seconds=60,
            clock=lambda: float("nan"),
        )
        with self.assertRaises(RateLimiterUnavailableError):
            limiter.consume("usr_alice")

    def test_subject_bound_evicts_without_changing_active_tenant_contract(self) -> None:
        clock = MutableClock(1.0)
        limiter = InMemoryRateLimiter(
            request_limit=2,
            window_seconds=60,
            max_tracked_subjects=1,
            clock=clock,
        )
        self.assertTrue(limiter.consume("usr_alice").allowed)
        clock.value = 2.0
        self.assertTrue(limiter.consume("usr_bob").allowed)
        clock.value = 3.0
        self.assertTrue(limiter.consume("usr_alice").allowed)


if __name__ == "__main__":
    unittest.main()
