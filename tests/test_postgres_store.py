"""Real PostgreSQL migration and job-store contract tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import psycopg

from agent_music.config import HarnessSettings
from agent_music.models import JobState
from agent_music.postgres_store import (
    POSTGRES_MIGRATIONS,
    PostgresJobStore,
    PostgresMigrationError,
)
from agent_music.scheduler import GpuHarness

POSTGRES_DSN = os.getenv("MELQIVRA_TEST_POSTGRES_DSN", "")


@unittest.skipUnless(POSTGRES_DSN, "MELQIVRA_TEST_POSTGRES_DSN is not configured")
class PostgresJobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = PostgresJobStore(POSTGRES_DSN)
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            connection.execute("TRUNCATE TABLE gpu_jobs RESTART IDENTITY")
            connection.execute("ALTER SEQUENCE gpu_fencing_token_seq RESTART WITH 1")

    def tearDown(self) -> None:
        self.store.close()

    def test_migration_is_idempotent_and_checksummed(self) -> None:
        second = PostgresJobStore(POSTGRES_DSN)
        second.close()
        _sql, expected_checksum = POSTGRES_MIGRATIONS[0].read()
        with psycopg.connect(POSTGRES_DSN) as connection:
            row = connection.execute(
                "SELECT name, checksum FROM schema_migrations WHERE version = 1"
            ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual("job_queue", row[0])
        self.assertEqual(expected_checksum, row[1])

    def test_changed_migration_checksum_is_rejected(self) -> None:
        _sql, expected_checksum = POSTGRES_MIGRATIONS[0].read()
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            connection.execute(
                "UPDATE schema_migrations SET checksum = %s WHERE version = 1",
                ("0" * 64,),
            )
        try:
            with self.assertRaisesRegex(PostgresMigrationError, "checksum mismatch"):
                PostgresJobStore(POSTGRES_DSN)
        finally:
            with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
                connection.execute(
                    "UPDATE schema_migrations SET checksum = %s WHERE version = 1",
                    (expected_checksum,),
                )

    def test_fifo_idempotency_and_stale_fencing_match_sqlite(self) -> None:
        ids_in_reverse_sort_order = [
            UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            UUID("00000000-0000-0000-0000-000000000000"),
        ]
        with (
            patch("agent_music.postgres_store.time.time", return_value=1_000.0),
            patch(
                "agent_music.postgres_store.uuid.uuid4",
                side_effect=ids_in_reverse_sort_order,
            ),
        ):
            first = self.store.enqueue(
                user_id="alice",
                kind="one",
                payload={"seed": 1},
                idempotency_key="request-1",
            )
            duplicate = self.store.enqueue(
                user_id="alice",
                kind="ignored",
                payload={"seed": 999},
                idempotency_key="request-1",
            )
            second = self.store.enqueue(user_id="bob", kind="two", payload={})

        self.assertEqual(first.id, duplicate.id)
        self.assertEqual({"seed": 1}, duplicate.payload)
        self.assertEqual(1, self.store.queue_position(first.id))
        self.assertEqual(2, self.store.queue_position(second.id))

        claimed_first = self.store.claim_next()
        assert claimed_first is not None and claimed_first.lease_token is not None
        self.assertEqual(first.id, claimed_first.id)
        with self.assertRaisesRegex(RuntimeError, "Lost GPU job lease"):
            self.store.complete(
                claimed_first.id,
                claimed_first.lease_token + 1,
                {"ok": False},
            )
        self.store.complete(claimed_first.id, claimed_first.lease_token, {"ok": True})

        claimed_second = self.store.claim_next()
        assert claimed_second is not None and claimed_second.lease_token is not None
        self.assertEqual(second.id, claimed_second.id)
        self.assertGreater(claimed_second.lease_token, claimed_first.lease_token)

    def test_tenant_cancel_and_restart_recovery_match_sqlite(self) -> None:
        queued = self.store.enqueue(user_id="alice", kind="queued", payload={})
        with self.assertRaises(KeyError):
            self.store.request_cancel_for_user(user_id="bob", job_id=queued.id)
        unchanged = self.store.get_for_user(user_id="alice", job_id=queued.id)
        self.assertFalse(unchanged.cancel_requested)

        running = self.store.claim_next()
        assert running is not None
        self.assertEqual(1, self.store.recover_interrupted())
        recovered = self.store.get(running.id)
        self.assertEqual(JobState.QUEUED, recovered.state)
        self.assertIsNone(recovered.lease_token)
        self.assertIn("safely requeued", recovered.error or "")

        cancelled = self.store.request_cancel_for_user(
            user_id="alice",
            job_id=queued.id,
        )
        self.assertEqual(JobState.CANCELLED, cancelled.state)

    def test_competing_workers_claim_distinct_jobs(self) -> None:
        first = self.store.enqueue(user_id="alice", kind="one", payload={})
        second = self.store.enqueue(user_id="bob", kind="two", payload={})
        peer = PostgresJobStore(POSTGRES_DSN)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                claims = tuple(
                    executor.map(
                        lambda store: store.claim_next(),
                        (self.store, peer),
                    )
                )
        finally:
            peer.close()

        self.assertEqual({first.id, second.id}, {claim.id for claim in claims if claim})
        tokens = {claim.lease_token for claim in claims if claim}
        self.assertEqual(2, len(tokens))

    def test_scheduler_completes_and_reopens_postgres_job(self) -> None:
        async def exercise() -> str:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = HarnessSettings(
                    job_store="postgres",
                    postgres_dsn=POSTGRES_DSN,
                    database_path=root / "unused.db",
                    artifact_root=root / "artifacts",
                    transfer_root=root / "transfers",
                    colab_cli_config=root / "colab" / "sessions.json",
                    runner="fake",
                    scheduler_poll_seconds=0.01,
                    fake_job_seconds=0.01,
                )
                harness = GpuHarness(settings)
                await harness.start()
                job = harness.enqueue(
                    user_id="alice",
                    kind="harness.healthcheck",
                    payload={},
                )
                try:
                    for _ in range(200):
                        if harness.store.get(job.id).state == JobState.SUCCEEDED:
                            return job.id
                        await asyncio.sleep(0.01)
                    self.fail("PostgreSQL-backed scheduler did not complete the job")
                finally:
                    await harness.close()
            raise AssertionError("unreachable")

        job_id = asyncio.run(exercise())
        reopened = PostgresJobStore(POSTGRES_DSN)
        try:
            completed = reopened.get(job_id)
        finally:
            reopened.close()
        self.assertEqual(JobState.SUCCEEDED, completed.state)
        self.assertIsNotNone(completed.result)
        assert completed.result is not None
        self.assertEqual("fake", completed.result["runner"])


if __name__ == "__main__":
    unittest.main()
