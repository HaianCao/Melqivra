"""Real PostgreSQL migration and job-store contract tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import psycopg

from agent_music.config import HarnessSettings
from agent_music.job_store import (
    InvalidJobCursorError,
    InvalidJobEventCursorError,
    LostJobLeaseError,
    UserJobQuotaExceededError,
)
from agent_music.models import JobAttemptOutcome, JobState
from agent_music.postgres_job_store import PostgresJobStore
from agent_music.postgres_migrations import (
    POSTGRES_MIGRATIONS,
    PostgresMigrationError,
)
from agent_music.scheduler import GpuHarness

POSTGRES_DSN = os.getenv("MELQIVRA_TEST_POSTGRES_DSN", "")


@unittest.skipUnless(POSTGRES_DSN, "MELQIVRA_TEST_POSTGRES_DSN is not configured")
class PostgresJobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = PostgresJobStore(POSTGRES_DSN)
        self.addCleanup(self._close_store)
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            connection.execute(
                """
                TRUNCATE TABLE
                    gpu_capacity_slots,
                    gpu_job_events,
                    gpu_job_attempts,
                    gpu_jobs
                RESTART IDENTITY
                """
            )
            connection.execute("INSERT INTO gpu_capacity_slots(slot_id) VALUES (1)")
            connection.execute("ALTER SEQUENCE gpu_fencing_token_seq RESTART WITH 1")

    def _close_store(self) -> None:
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
        self.assertEqual([1, 2, 3, 4, 5, 6], [row[0] for row in rows])
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
            connection.execute("DROP TABLE gpu_job_attempts")
            connection.execute("DROP TABLE gpu_capacity_slots")
            connection.execute(
                """
                ALTER TABLE gpu_jobs
                    DROP COLUMN lease_owner,
                    DROP COLUMN lease_expires_at,
                    DROP COLUMN attempt_count,
                    DROP COLUMN max_attempts
                """
            )
            connection.execute("DELETE FROM schema_migrations WHERE version IN (2, 3)")
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
        self.assertEqual(3, migrated.max_attempts)
        self.assertIn("Lease migration", migrated.error or "")

    def test_attempt_history_migration_backfills_live_v2_attempt(self) -> None:
        self.store.close()
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            connection.execute("DROP TABLE gpu_job_attempts")
            connection.execute(
                "ALTER TABLE gpu_jobs DROP CONSTRAINT gpu_jobs_max_attempts_positive"
            )
            connection.execute("ALTER TABLE gpu_jobs DROP COLUMN max_attempts")
            connection.execute("DELETE FROM schema_migrations WHERE version = 3")
            connection.execute(
                """
                INSERT INTO gpu_jobs(
                    id, user_id, kind, payload_json, state,
                    created_at, updated_at, started_at,
                    lease_token, lease_owner, lease_expires_at, attempt_count
                ) VALUES (
                    'v2-running', 'alice', 'generate', '{}'::jsonb, 'running',
                    1000, 1000, 1000, 7, 'worker-a', 1010, 1
                )
                """
            )
            connection.execute(
                """
                UPDATE gpu_capacity_slots
                SET active_job_id = 'v2-running', lease_token = 7,
                    lease_owner = 'worker-a', lease_expires_at = 1010
                WHERE slot_id = 1
                """
            )

        self.store = PostgresJobStore(POSTGRES_DSN)
        migrated = self.store.get("v2-running")
        attempts = self.store.list_attempts(migrated.id)
        self.assertEqual(3, migrated.max_attempts)
        self.assertEqual(1, len(attempts))
        self.assertEqual(JobAttemptOutcome.RUNNING, attempts[0].outcome)
        self.assertEqual(7, attempts[0].lease_token)

    def test_event_migration_backfills_one_v5_state_snapshot(self) -> None:
        self.store.close()
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            connection.execute("DROP TABLE gpu_job_events")
            connection.execute(
                "ALTER TABLE gpu_jobs DROP CONSTRAINT gpu_jobs_next_event_sequence_positive"
            )
            connection.execute("ALTER TABLE gpu_jobs DROP COLUMN next_event_sequence")
            connection.execute("DELETE FROM schema_migrations WHERE version = 6")
            connection.execute(
                """
                INSERT INTO gpu_jobs(
                    id, user_id, kind, payload_json, state,
                    created_at, updated_at, max_attempts
                ) VALUES (
                    'v5-queued', 'alice', 'generate', '{}'::jsonb, 'queued',
                    1000, 1001, 3
                )
                """
            )

        self.store = PostgresJobStore(POSTGRES_DSN)
        events = self.store.list_events_for_user(
            user_id="alice",
            job_id="v5-queued",
            after_sequence=0,
            limit=100,
        )
        self.assertEqual(1, len(events))
        self.assertEqual("job.snapshot", events[0].event_type)
        self.assertEqual(JobState.QUEUED, events[0].job_state)
        self.assertEqual(1_001.0, events[0].created_at)

    def test_fifo_idempotency_and_stale_fencing_match_sqlite(self) -> None:
        ids_in_reverse_sort_order = [
            UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            UUID("00000000-0000-0000-0000-000000000000"),
        ]
        with (
            patch(
                "agent_music.postgres_job_store.time.time",
                side_effect=(2_000.0, 1_500.0, 1_000.0),
            ),
            patch(
                "agent_music.postgres_job_store.uuid.uuid4",
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

    def test_event_stream_is_ordered_fenced_and_tenant_scoped(self) -> None:
        job = self.store.enqueue(user_id="alice", kind="generate", payload={})
        claimed = self.store.claim_next(lease_owner="worker-a", lease_seconds=60.0)
        assert claimed is not None and claimed.lease_token is not None

        def append(index: int) -> int:
            event = self.store.append_event(
                job_id=job.id,
                lease_token=claimed.lease_token,
                event_type="provider.stage",
                payload={"index": index},
            )
            return event.sequence

        with ThreadPoolExecutor(max_workers=4) as executor:
            sequences = tuple(executor.map(append, range(8)))
        self.assertEqual(list(range(3, 11)), sorted(sequences))
        with self.assertRaisesRegex(LostJobLeaseError, "stale event"):
            self.store.append_event(
                job_id=job.id,
                lease_token=claimed.lease_token + 1,
                event_type="provider.stage",
                payload={},
            )
        with self.assertRaisesRegex(ValueError, "size limit"):
            self.store.append_event(
                job_id=job.id,
                lease_token=claimed.lease_token,
                event_type="provider.stage",
                payload={"text": "x" * 16_385},
            )
        self.store.complete(job.id, claimed.lease_token, {"ok": True})

        events = self.store.list_events_for_user(
            user_id="alice",
            job_id=job.id,
            after_sequence=0,
            limit=100,
        )
        self.assertEqual(list(range(1, 12)), [event.sequence for event in events])
        self.assertEqual("job.succeeded", events[-1].event_type)
        self.assertEqual(JobState.SUCCEEDED, events[-1].job_state)
        self.assertEqual(set(range(8)), {event.payload["index"] for event in events[2:-1]})
        with self.assertRaises(KeyError):
            self.store.list_events_for_user(
                user_id="bob",
                job_id=job.id,
                after_sequence=0,
                limit=100,
            )
        with self.assertRaises(InvalidJobEventCursorError):
            self.store.list_events_for_user(
                user_id="alice",
                job_id=job.id,
                after_sequence=12,
                limit=100,
            )

        restarted = PostgresJobStore(POSTGRES_DSN)
        self.addCleanup(restarted.close)
        self.assertEqual(
            [10, 11],
            [
                event.sequence
                for event in restarted.list_events_for_user(
                    user_id="alice",
                    job_id=job.id,
                    after_sequence=9,
                    limit=100,
                )
            ],
        )

    def test_tenant_pagination_and_active_quota_match_sqlite(self) -> None:
        first = self.store.enqueue(
            user_id="alice",
            kind="generate",
            payload={"index": 1},
            idempotency_key="quota-one",
            max_active_jobs=2,
        )
        second = self.store.enqueue(
            user_id="alice",
            kind="generate",
            payload={"index": 2},
            idempotency_key="quota-two",
            max_active_jobs=2,
        )
        replay = self.store.enqueue(
            user_id="alice",
            kind="generate",
            payload={"index": 999},
            idempotency_key="quota-one",
            max_active_jobs=2,
        )
        self.assertEqual(first.id, replay.id)
        with self.assertRaises(UserJobQuotaExceededError):
            self.store.enqueue(
                user_id="alice",
                kind="generate",
                payload={"index": 3},
                idempotency_key="quota-three",
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
        self.assertEqual((other,), self.store.list_for_user(user_id="bob", limit=10).items)
        with self.assertRaises(InvalidJobCursorError):
            self.store.list_for_user(user_id="bob", limit=1, cursor=first.id)

    def test_competing_same_tenant_enqueues_share_one_quota_lock(self) -> None:
        peer = PostgresJobStore(POSTGRES_DSN)
        barrier = threading.Barrier(2)

        def enqueue(
            store_and_key: tuple[PostgresJobStore, str],
        ) -> str:
            store, key = store_and_key
            barrier.wait()
            try:
                return store.enqueue(
                    user_id="alice",
                    kind="generate",
                    payload={"key": key},
                    idempotency_key=key,
                    max_active_jobs=1,
                ).id
            except UserJobQuotaExceededError:
                return "quota_exceeded"

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = tuple(
                    executor.map(
                        enqueue,
                        ((self.store, "concurrent-one"), (peer, "concurrent-two")),
                    )
                )
        finally:
            peer.close()
        self.assertEqual(1, outcomes.count("quota_exceeded"))
        self.assertEqual(1, len([outcome for outcome in outcomes if outcome != "quota_exceeded"]))

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
        self.assertEqual(
            ["job.queued", "job.running", "job.requeued", "job.cancelled"],
            [
                event.event_type
                for event in self.store.list_events_for_user(
                    user_id="alice",
                    job_id=queued.id,
                    after_sequence=0,
                    limit=100,
                )
            ],
        )

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
            patch("agent_music.postgres_job_store.time.time", return_value=1_000.0),
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
        self.assertEqual(
            [JobAttemptOutcome.LEASE_EXPIRED, JobAttemptOutcome.SUCCEEDED],
            [attempt.outcome for attempt in self.store.list_attempts(job.id)],
        )
        recovered_events = self.store.list_events_for_user(
            user_id="alice",
            job_id=job.id,
            after_sequence=0,
            limit=100,
        )
        self.assertIn("job.requeued", [event.event_type for event in recovered_events])
        self.assertEqual("job.succeeded", recovered_events[-1].event_type)
        self.assertEqual(JobState.SUCCEEDED, recovered_events[-1].job_state)

        self.store.close()
        self.store = PostgresJobStore(POSTGRES_DSN, max_attempts=1)
        with (
            patch("agent_music.postgres_job_store.time.time", return_value=2_000.0),
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
        self.assertEqual(1, cancelled.max_attempts)
        self.assertIsNotNone(cancelled.finished_at)
        self.assertEqual(
            JobAttemptOutcome.CANCELLED,
            self.store.list_attempts(cancelled_job.id)[0].outcome,
        )
        cancelled_events = self.store.list_events_for_user(
            user_id="bob",
            job_id=cancelled_job.id,
            after_sequence=0,
            limit=100,
        )
        self.assertEqual("job.cancel_requested", cancelled_events[-2].event_type)
        self.assertEqual("job.cancelled", cancelled_events[-1].event_type)
        self.assertEqual(JobState.CANCELLED, cancelled_events[-1].job_state)

    def test_attempt_history_and_dead_letter_limit_match_sqlite(self) -> None:
        self.store.close()
        self.store = PostgresJobStore(POSTGRES_DSN, max_attempts=2)
        with (
            patch("agent_music.postgres_job_store.time.time", return_value=3_000.0),
            patch.object(self.store, "_database_time", return_value=3_000.0),
        ):
            job = self.store.enqueue(user_id="alice", kind="generate", payload={})
            first = self.store.claim_next(lease_owner="worker-a", lease_seconds=10.0)
        assert first is not None

        with patch.object(self.store, "_database_time", return_value=3_011.0):
            self.assertEqual(1, self.store.recover_expired())
        self.assertEqual(JobState.QUEUED, self.store.get(job.id).state)

        self.store.close()
        self.store = PostgresJobStore(POSTGRES_DSN, max_attempts=9)
        self.assertEqual(2, self.store.get(job.id).max_attempts)
        with patch.object(self.store, "_database_time", return_value=3_020.0):
            second = self.store.claim_next(lease_owner="worker-b", lease_seconds=10.0)
        assert second is not None
        with patch.object(self.store, "_database_time", return_value=3_031.0):
            self.assertEqual(1, self.store.recover_expired())

        dead_lettered = self.store.get(job.id)
        self.assertEqual(JobState.DEAD_LETTERED, dead_lettered.state)
        self.assertEqual(2, dead_lettered.attempt_count)
        self.assertEqual(2, dead_lettered.max_attempts)
        self.assertIsNone(self.store.claim_next())
        attempts = self.store.list_attempts(job.id)
        self.assertEqual([1, 2], [attempt.attempt_number for attempt in attempts])
        self.assertEqual(
            [JobAttemptOutcome.LEASE_EXPIRED, JobAttemptOutcome.LEASE_EXPIRED],
            [attempt.outcome for attempt in attempts],
        )
        dead_letter_events = self.store.list_events_for_user(
            user_id="alice",
            job_id=job.id,
            after_sequence=0,
            limit=100,
        )
        self.assertEqual("job.dead_lettered", dead_letter_events[-1].event_type)
        self.assertEqual(JobState.DEAD_LETTERED, dead_letter_events[-1].job_state)

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
