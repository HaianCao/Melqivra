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
from agent_music.store import LostJobLeaseError

POSTGRES_DSN = os.getenv("MELQIVRA_TEST_POSTGRES_DSN", "")


@unittest.skipUnless(POSTGRES_DSN, "MELQIVRA_TEST_POSTGRES_DSN is not configured")
class PostgresJobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = PostgresJobStore(POSTGRES_DSN)
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            connection.execute("TRUNCATE TABLE gpu_capacity_slots, gpu_jobs RESTART IDENTITY")
            connection.execute("INSERT INTO gpu_capacity_slots(slot_id) VALUES (1)")
            connection.execute("ALTER SEQUENCE gpu_fencing_token_seq RESTART WITH 1")

    def tearDown(self) -> None:
        self.store.close()

    def test_migration_is_idempotent_and_checksummed(self) -> None:
        second = PostgresJobStore(POSTGRES_DSN)
        second.close()
        expected = {
            migration.version: (migration.name, migration.read()[1])
            for migration in POSTGRES_MIGRATIONS
        }
        with psycopg.connect(POSTGRES_DSN) as connection:
            rows = connection.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        self.assertEqual([1, 2], [row[0] for row in rows])
        self.assertEqual(
            expected,
            {row[0]: (row[1], row[2]) for row in rows},
        )

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

    def test_lease_migration_reconciles_v1_running_jobs(self) -> None:
        self.store.close()
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            connection.execute("DROP TABLE gpu_capacity_slots")
            connection.execute(
                """
                ALTER TABLE gpu_jobs
                    DROP COLUMN lease_owner,
                    DROP COLUMN lease_expires_at,
                    DROP COLUMN attempt_count
                """
            )
            connection.execute("DELETE FROM schema_migrations WHERE version = 2")
            connection.execute(
                """
                INSERT INTO gpu_jobs(
                    id, user_id, kind, payload_json, state,
                    created_at, updated_at, started_at, lease_token
                ) VALUES (
                    'pre-lease-running', 'alice', 'generate', '{}'::jsonb, 'running',
                    1000, 1000, 1000, 7
                )
                """
            )

        self.store = PostgresJobStore(POSTGRES_DSN)
        migrated = self.store.get("pre-lease-running")
        self.assertEqual(JobState.QUEUED, migrated.state)
        self.assertIsNone(migrated.lease_token)
        self.assertEqual(0, migrated.attempt_count)
        self.assertIn("Lease migration", migrated.error or "")

    def test_fifo_idempotency_and_stale_fencing_match_sqlite(self) -> None:
        ids_in_reverse_sort_order = [
            UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            UUID("00000000-0000-0000-0000-000000000000"),
        ]
        with (
            patch(
                "agent_music.postgres_store.time.time",
                side_effect=(2_000.0, 1_500.0, 1_000.0),
            ),
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

    def test_global_capacity_serializes_competing_workers(self) -> None:
        first = self.store.enqueue(user_id="alice", kind="one", payload={})
        second = self.store.enqueue(user_id="bob", kind="two", payload={})
        peer = PostgresJobStore(POSTGRES_DSN)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                claims = tuple(
                    executor.map(
                        lambda item: item[0].claim_next(
                            lease_owner=item[1],
                            lease_seconds=60.0,
                        ),
                        ((self.store, "worker-a"), (peer, "worker-b")),
                    )
                )
            claimed = [claim for claim in claims if claim is not None]
            self.assertEqual(1, len(claimed))
            self.assertEqual(first.id, claimed[0].id)
            assert claimed[0].lease_token is not None
            self.store.complete(first.id, claimed[0].lease_token, {"ok": True})

            claimed_second = peer.claim_next(
                lease_owner="worker-c",
                lease_seconds=60.0,
            )
            assert claimed_second is not None
            self.assertEqual(second.id, claimed_second.id)
        finally:
            peer.close()

    def test_lease_renewal_expiry_and_fresh_attempt_fencing(self) -> None:
        with (
            patch("agent_music.postgres_store.time.time", return_value=1_000.0),
            patch.object(self.store, "_database_time", return_value=1_000.0),
        ):
            job = self.store.enqueue(user_id="alice", kind="generate", payload={})
            first = self.store.claim_next(
                lease_owner="worker-a",
                lease_seconds=10.0,
            )
        assert first is not None and first.lease_token is not None
        self.assertEqual(1, first.attempt_count)

        with patch.object(self.store, "_database_time", return_value=1_005.0):
            self.assertTrue(
                self.store.renew_lease(
                    job_id=job.id,
                    lease_token=first.lease_token,
                    lease_owner="worker-a",
                    lease_seconds=10.0,
                )
            )

        with patch.object(self.store, "_database_time", return_value=1_011.0):
            self.assertEqual(0, self.store.recover_expired())
            self.assertIsNone(
                self.store.claim_next(
                    lease_owner="worker-b",
                    lease_seconds=10.0,
                )
            )

        with patch.object(self.store, "_database_time", return_value=1_016.0):
            self.assertEqual(1, self.store.recover_expired())
            second = self.store.claim_next(
                lease_owner="worker-b",
                lease_seconds=10.0,
            )
            assert second is not None and second.lease_token is not None
            self.assertEqual(2, second.attempt_count)
            with self.assertRaises(LostJobLeaseError):
                self.store.complete(job.id, first.lease_token, {"stale": True})
            self.store.complete(job.id, second.lease_token, {"ok": True})

        with (
            patch("agent_music.postgres_store.time.time", return_value=2_000.0),
            patch.object(self.store, "_database_time", return_value=2_000.0),
        ):
            cancelled_job = self.store.enqueue(
                user_id="bob",
                kind="generate",
                payload={},
            )
            cancelled_claim = self.store.claim_next(
                lease_owner="worker-a",
                lease_seconds=10.0,
            )
            assert cancelled_claim is not None
            self.store.request_cancel(cancelled_job.id)
        with patch.object(self.store, "_database_time", return_value=2_011.0):
            self.assertEqual(1, self.store.recover_expired())
        cancelled = self.store.get(cancelled_job.id)
        self.assertEqual(JobState.CANCELLED, cancelled.state)
        self.assertIsNotNone(cancelled.finished_at)

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
