"""Durable queue tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_music.job_store import (
    InvalidJobCursorError,
    LostJobLeaseError,
    SqliteJobStore,
    UserJobQuotaExceededError,
)
from agent_music.models import JobAttemptOutcome, JobState


class SqliteJobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SqliteJobStore(Path(self.temp_dir.name) / "jobs.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_fifo_claim_across_users(self) -> None:
        ids_in_reverse_sort_order = [
            UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            UUID("00000000-0000-0000-0000-000000000000"),
        ]
        with (
            patch("agent_music.job_store.time.time", side_effect=(2_000.0, 1_000.0)),
            patch("agent_music.job_store.uuid.uuid4", side_effect=ids_in_reverse_sort_order),
        ):
            first = self.store.enqueue(user_id="alice", kind="one", payload={})
            second = self.store.enqueue(user_id="bob", kind="two", payload={})

        claimed_first = self.store.claim_next()
        self.assertIsNotNone(claimed_first)
        assert claimed_first is not None
        self.assertEqual(first.id, claimed_first.id)
        self.assertEqual(JobState.RUNNING, claimed_first.state)
        assert claimed_first.lease_token is not None
        self.store.complete(
            claimed_first.id,
            claimed_first.lease_token,
            {"ok": True},
        )

        claimed_second = self.store.claim_next()
        self.assertIsNotNone(claimed_second)
        assert claimed_second is not None
        self.assertEqual(second.id, claimed_second.id)
        self.assertGreater(
            int(claimed_second.lease_token or 0),
            int(claimed_first.lease_token),
        )

    def test_idempotency_is_scoped_per_user(self) -> None:
        first = self.store.enqueue(
            user_id="alice",
            kind="generate",
            payload={"seed": 1},
            idempotency_key="request-1",
        )
        duplicate = self.store.enqueue(
            user_id="alice",
            kind="generate",
            payload={"seed": 999},
            idempotency_key="request-1",
        )
        other_user = self.store.enqueue(
            user_id="bob",
            kind="generate",
            payload={"seed": 1},
            idempotency_key="request-1",
        )

        self.assertEqual(first.id, duplicate.id)
        self.assertNotEqual(first.id, other_user.id)
        self.assertEqual({"seed": 1}, duplicate.payload)

    def test_tenant_pagination_and_active_quota_are_atomic(self) -> None:
        first = self.store.enqueue(
            user_id="alice",
            kind="generate",
            payload={"index": 1},
            idempotency_key="one",
            max_active_jobs=2,
        )
        second = self.store.enqueue(
            user_id="alice",
            kind="generate",
            payload={"index": 2},
            idempotency_key="two",
            max_active_jobs=2,
        )
        replay = self.store.enqueue(
            user_id="alice",
            kind="generate",
            payload={"index": 999},
            idempotency_key="one",
            max_active_jobs=2,
        )
        self.assertEqual(first.id, replay.id)
        with self.assertRaises(UserJobQuotaExceededError):
            self.store.enqueue(
                user_id="alice",
                kind="generate",
                payload={"index": 3},
                idempotency_key="three",
                max_active_jobs=2,
            )
        other = self.store.enqueue(
            user_id="bob",
            kind="generate",
            payload={"index": 1},
            max_active_jobs=2,
        )
        first_page = self.store.list_for_user(user_id="alice", limit=1)
        second_page = self.store.list_for_user(
            user_id="alice",
            limit=1,
            cursor=first_page.next_cursor,
        )
        self.assertEqual((first,), first_page.items)
        self.assertEqual(first.id, first_page.next_cursor)
        self.assertEqual((second,), second_page.items)
        self.assertIsNone(second_page.next_cursor)
        self.assertEqual((other,), self.store.list_for_user(user_id="bob", limit=10).items)
        with self.assertRaises(InvalidJobCursorError):
            self.store.list_for_user(user_id="bob", limit=1, cursor=first.id)

    def test_stale_fencing_token_cannot_commit(self) -> None:
        job = self.store.enqueue(user_id="alice", kind="generate", payload={})
        claimed = self.store.claim_next()
        assert claimed is not None and claimed.lease_token is not None

        with self.assertRaisesRegex(RuntimeError, "Lost GPU job lease"):
            self.store.complete(job.id, claimed.lease_token + 1, {"ok": True})

    def test_lease_renewal_expiry_and_fresh_attempt_fencing(self) -> None:
        with patch("agent_music.job_store.time.time", return_value=1_000.0):
            job = self.store.enqueue(user_id="alice", kind="generate", payload={})
            first = self.store.claim_next(lease_owner="worker-a", lease_seconds=10.0)
        assert first is not None and first.lease_token is not None
        self.assertEqual("worker-a", first.lease_owner)
        self.assertEqual(1, first.attempt_count)
        self.assertEqual(1_010.0, first.lease_expires_at)

        with patch("agent_music.job_store.time.time", return_value=1_005.0):
            self.assertTrue(
                self.store.renew_lease(
                    job_id=job.id,
                    lease_token=first.lease_token,
                    lease_owner="worker-a",
                    lease_seconds=10.0,
                )
            )
        self.assertEqual(1_015.0, self.store.get(job.id).lease_expires_at)

        with patch("agent_music.job_store.time.time", return_value=1_011.0):
            self.assertEqual(0, self.store.recover_expired())
            self.assertIsNone(self.store.claim_next(lease_owner="worker-b", lease_seconds=10.0))

        with patch("agent_music.job_store.time.time", return_value=1_016.0):
            self.assertEqual(1, self.store.recover_expired())
            second = self.store.claim_next(
                lease_owner="worker-b",
                lease_seconds=10.0,
            )
            assert second is not None and second.lease_token is not None
            self.assertEqual(2, second.attempt_count)
            self.assertGreater(second.lease_token, first.lease_token)
            with self.assertRaises(LostJobLeaseError):
                self.store.complete(job.id, first.lease_token, {"stale": True})
            self.store.complete(job.id, second.lease_token, {"ok": True})
        self.assertEqual(
            [JobAttemptOutcome.LEASE_EXPIRED, JobAttemptOutcome.SUCCEEDED],
            [attempt.outcome for attempt in self.store.list_attempts(job.id)],
        )

    def test_expired_cancel_request_converges_to_cancelled(self) -> None:
        self.store.close()
        self.store = SqliteJobStore(
            Path(self.temp_dir.name) / "jobs.db",
            max_attempts=1,
        )
        with patch("agent_music.job_store.time.time", return_value=2_000.0):
            job = self.store.enqueue(user_id="alice", kind="generate", payload={})
            claimed = self.store.claim_next(
                lease_owner="worker-a",
                lease_seconds=10.0,
            )
            assert claimed is not None
            self.store.request_cancel(job.id)

        with patch("agent_music.job_store.time.time", return_value=2_011.0):
            self.assertEqual(1, self.store.recover_expired())

        cancelled = self.store.get(job.id)
        self.assertEqual(JobState.CANCELLED, cancelled.state)
        self.assertEqual(1, cancelled.max_attempts)
        self.assertIsNotNone(cancelled.finished_at)
        self.assertIn("cancelled", (cancelled.error or "").lower())

    def test_attempt_history_and_dead_letter_limit_survive_reopen(self) -> None:
        database_path = Path(self.temp_dir.name) / "jobs.db"
        self.store.close()
        self.store = SqliteJobStore(database_path, max_attempts=2)

        with patch("agent_music.job_store.time.time", return_value=3_000.0):
            job = self.store.enqueue(user_id="alice", kind="generate", payload={})
            first = self.store.claim_next(lease_owner="worker-a", lease_seconds=10.0)
        assert first is not None

        with patch("agent_music.job_store.time.time", return_value=3_011.0):
            self.assertEqual(1, self.store.recover_expired())
        requeued = self.store.get(job.id)
        self.assertEqual(JobState.QUEUED, requeued.state)
        self.assertEqual(2, requeued.max_attempts)
        first_attempt = self.store.list_attempts(job.id)
        self.assertEqual(1, len(first_attempt))
        self.assertEqual(JobAttemptOutcome.LEASE_EXPIRED, first_attempt[0].outcome)
        self.assertEqual(3_011.0, first_attempt[0].finished_at)

        self.store.close()
        self.store = SqliteJobStore(database_path, max_attempts=9)
        self.assertEqual(2, self.store.get(job.id).max_attempts)

        with patch("agent_music.job_store.time.time", return_value=3_020.0):
            second = self.store.claim_next(lease_owner="worker-b", lease_seconds=10.0)
        assert second is not None
        with patch("agent_music.job_store.time.time", return_value=3_031.0):
            self.assertEqual(1, self.store.recover_expired())

        dead_lettered = self.store.get(job.id)
        self.assertEqual(JobState.DEAD_LETTERED, dead_lettered.state)
        self.assertEqual(2, dead_lettered.attempt_count)
        self.assertIsNotNone(dead_lettered.finished_at)
        self.assertIn("dead-lettered", dead_lettered.error or "")
        self.assertIsNone(self.store.claim_next())
        attempts = self.store.list_attempts(job.id)
        self.assertEqual([1, 2], [attempt.attempt_number for attempt in attempts])
        self.assertEqual(
            [JobAttemptOutcome.LEASE_EXPIRED, JobAttemptOutcome.LEASE_EXPIRED],
            [attempt.outcome for attempt in attempts],
        )

    def test_queue_position_and_queued_cancellation(self) -> None:
        first = self.store.enqueue(user_id="alice", kind="one", payload={})
        second = self.store.enqueue(user_id="bob", kind="two", payload={})

        self.assertEqual(1, self.store.queue_position(first.id))
        self.assertEqual(2, self.store.queue_position(second.id))
        cancelled = self.store.request_cancel(first.id)

        self.assertEqual(JobState.CANCELLED, cancelled.state)
        self.assertTrue(cancelled.cancel_requested)
        self.assertIsNone(self.store.queue_position(first.id))
        self.assertEqual(1, self.store.queue_position(second.id))

    def test_running_cancellation_preserves_fencing(self) -> None:
        job = self.store.enqueue(user_id="alice", kind="generate", payload={})
        claimed = self.store.claim_next()
        assert claimed is not None and claimed.lease_token is not None

        requested = self.store.request_cancel(job.id)
        self.assertEqual(JobState.RUNNING, requested.state)
        self.assertTrue(requested.cancel_requested)

        self.store.cancel_running(job.id, claimed.lease_token)
        cancelled = self.store.get(job.id)
        self.assertEqual(JobState.CANCELLED, cancelled.state)
        self.assertEqual("Job cancelled", cancelled.error)
        self.assertEqual(
            JobAttemptOutcome.CANCELLED,
            self.store.list_attempts(job.id)[0].outcome,
        )

    def test_failed_and_interrupted_jobs_have_explicit_states(self) -> None:
        failed_job = self.store.enqueue(user_id="alice", kind="fail", payload={})
        failed_claim = self.store.claim_next()
        assert failed_claim is not None and failed_claim.lease_token is not None
        self.store.fail(failed_job.id, failed_claim.lease_token, "model failed")
        failed = self.store.get(failed_job.id)
        self.assertEqual(JobState.FAILED, failed.state)
        self.assertEqual("model failed", failed.error)
        self.assertEqual(
            JobAttemptOutcome.FAILED,
            self.store.list_attempts(failed_job.id)[0].outcome,
        )

        interrupted_job = self.store.enqueue(user_id="bob", kind="recover", payload={})
        interrupted_claim = self.store.claim_next()
        assert interrupted_claim is not None
        self.assertEqual(1, self.store.recover_interrupted())

        recovered = self.store.get(interrupted_job.id)
        self.assertEqual(JobState.QUEUED, recovered.state)
        self.assertIsNone(recovered.lease_token)
        self.assertIn("safely requeued", recovered.error or "")

    def test_missing_job_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.store.get("missing")

    def test_tenant_scoped_lookup_and_cancel_hide_cross_tenant_jobs(self) -> None:
        job = self.store.enqueue(user_id="alice", kind="generate", payload={})

        with self.assertRaises(KeyError):
            self.store.get_for_user(user_id="bob", job_id=job.id)
        with self.assertRaises(KeyError):
            self.store.request_cancel_for_user(user_id="bob", job_id=job.id)

        unchanged = self.store.get_for_user(user_id="alice", job_id=job.id)
        self.assertEqual(JobState.QUEUED, unchanged.state)
        self.assertFalse(unchanged.cancel_requested)

        cancelled = self.store.request_cancel_for_user(user_id="alice", job_id=job.id)
        self.assertEqual(JobState.CANCELLED, cancelled.state)
        self.assertTrue(cancelled.cancel_requested)


if __name__ == "__main__":
    unittest.main()
