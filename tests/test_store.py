"""Durable queue tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_music.models import JobState
from agent_music.store import SqliteJobStore


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
            patch("agent_music.store.time.time", return_value=1_000.0),
            patch("agent_music.store.uuid.uuid4", side_effect=ids_in_reverse_sort_order),
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

    def test_stale_fencing_token_cannot_commit(self) -> None:
        job = self.store.enqueue(user_id="alice", kind="generate", payload={})
        claimed = self.store.claim_next()
        assert claimed is not None and claimed.lease_token is not None

        with self.assertRaisesRegex(RuntimeError, "Lost GPU job lease"):
            self.store.complete(job.id, claimed.lease_token + 1, {"ok": True})

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

    def test_failed_and_interrupted_jobs_have_explicit_states(self) -> None:
        failed_job = self.store.enqueue(user_id="alice", kind="fail", payload={})
        failed_claim = self.store.claim_next()
        assert failed_claim is not None and failed_claim.lease_token is not None
        self.store.fail(failed_job.id, failed_claim.lease_token, "model failed")
        failed = self.store.get(failed_job.id)
        self.assertEqual(JobState.FAILED, failed.state)
        self.assertEqual("model failed", failed.error)

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
