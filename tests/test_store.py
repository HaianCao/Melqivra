"""Durable queue tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
