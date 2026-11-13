"""PostgreSQL durable GPU job-store adapter."""

from __future__ import annotations

import time
import uuid
from typing import Any, cast

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from agent_music.job_store import LostJobLeaseError
from agent_music.models import GpuJob, JobAttempt, JobAttemptOutcome, JobState
from agent_music.postgres_migrations import (
    PostgresConnection,
    PostgresMigrator,
    PostgresPool,
)


class PostgresJobStore:
    """PostgreSQL FIFO queue preserving SQLite job and fencing semantics."""

    def __init__(
        self,
        dsn: str,
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 4,
        connection_timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        migrate: bool = True,
    ) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN must not be empty")
        if min_pool_size < 1 or max_pool_size < min_pool_size:
            raise ValueError("PostgreSQL pool sizes are invalid")
        if connection_timeout_seconds <= 0:
            raise ValueError("PostgreSQL connection timeout must be positive")
        if max_attempts < 1:
            raise ValueError("GPU job max attempts must be at least 1")
        self._max_attempts = max_attempts
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

    @staticmethod
    def _database_time(connection: PostgresConnection) -> float:
        """Read the authoritative lease clock after acquiring transaction locks."""

        row = connection.execute(
            """
            SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision
                AS now_epoch
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL clock query returned no value")
        return float(row["now_epoch"])

    @staticmethod
    def _clear_capacity_slot(connection: PostgresConnection) -> None:
        connection.execute(
            """
            UPDATE gpu_capacity_slots
            SET active_job_id = NULL, lease_token = NULL,
                lease_owner = NULL, lease_expires_at = NULL
            WHERE slot_id = 1
            """
        )

    @staticmethod
    def _recover_running_job(
        connection: PostgresConnection,
        *,
        job_id: str,
        lease_token: int,
        now: float,
        attempt_outcome: JobAttemptOutcome,
        retry_error: str,
        cancel_error: str,
        dead_letter_error: str,
    ) -> bool:
        job = connection.execute(
            """
            SELECT cancel_requested, attempt_count, max_attempts
            FROM gpu_jobs
            WHERE id = %s AND state = %s AND lease_token = %s
            FOR UPDATE
            """,
            (job_id, JobState.RUNNING.value, lease_token),
        ).fetchone()
        if job is None:
            return False
        cancel_requested = bool(job["cancel_requested"])
        exhausted = int(job["attempt_count"]) >= int(job["max_attempts"])
        if cancel_requested:
            state = JobState.CANCELLED
            error = cancel_error
            finished_at: float | None = now
            outcome = JobAttemptOutcome.CANCELLED
        elif exhausted:
            state = JobState.DEAD_LETTERED
            error = dead_letter_error
            finished_at = now
            outcome = attempt_outcome
        else:
            state = JobState.QUEUED
            error = retry_error
            finished_at = None
            outcome = attempt_outcome
        updated = connection.execute(
            """
            UPDATE gpu_jobs
            SET state = %s, started_at = NULL, lease_token = NULL,
                lease_owner = NULL, lease_expires_at = NULL,
                finished_at = %s, updated_at = %s, error = %s
            WHERE id = %s AND state = %s AND lease_token = %s
            RETURNING id
            """,
            (
                state.value,
                finished_at,
                now,
                error,
                job_id,
                JobState.RUNNING.value,
                lease_token,
            ),
        ).fetchone()
        if updated is None:
            raise RuntimeError("PostgreSQL recovery lost its running job")
        attempt = connection.execute(
            """
            UPDATE gpu_job_attempts
            SET finished_at = %s, outcome = %s, error = %s
            WHERE job_id = %s AND lease_token = %s AND outcome = %s
            RETURNING job_id
            """,
            (
                now,
                outcome.value,
                error,
                job_id,
                lease_token,
                JobAttemptOutcome.RUNNING.value,
            ),
        ).fetchone()
        if attempt is None:
            raise RuntimeError("PostgreSQL recovery and attempt history diverged")
        return True

    def enqueue(
        self,
        *,
        user_id: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> GpuJob:
        """Append one job or return the prior tenant-scoped idempotent job."""

        now = time.time()
        job_id = str(uuid.uuid4())
        with self._pool.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                INSERT INTO gpu_jobs(
                    id, user_id, kind, payload_json, state,
                    created_at, updated_at, max_attempts, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL
                DO NOTHING
                RETURNING *
                """,
                (
                    job_id,
                    user_id,
                    kind,
                    Jsonb(payload),
                    JobState.QUEUED.value,
                    now,
                    now,
                    self._max_attempts,
                    idempotency_key,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM gpu_jobs
                    WHERE user_id = %s AND idempotency_key = %s
                    """,
                    (user_id, idempotency_key),
                ).fetchone()
            if row is None:
                raise RuntimeError("Idempotent PostgreSQL insert returned no job")
            return self._row_to_job(row)

    def get(self, job_id: str) -> GpuJob:
        """Load one job by its internal identifier."""

        return self._get(job_id=job_id, user_id=None)

    def get_for_user(self, *, user_id: str, job_id: str) -> GpuJob:
        """Load one tenant-owned job without revealing cross-tenant existence."""

        return self._get(job_id=job_id, user_id=user_id)

    def _get(self, *, job_id: str, user_id: str | None) -> GpuJob:
        if user_id is None:
            query = "SELECT * FROM gpu_jobs WHERE id = %s"
            parameters: tuple[object, ...] = (job_id,)
        else:
            query = "SELECT * FROM gpu_jobs WHERE id = %s AND user_id = %s"
            parameters = (job_id, user_id)
        with self._pool.connection() as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row_to_job(row)

    def claim_next(
        self,
        *,
        lease_owner: str = "legacy-worker",
        lease_seconds: float = 60.0,
    ) -> GpuJob | None:
        """Atomically claim the oldest runnable job across competing workers."""

        if not lease_owner:
            raise ValueError("Job lease owner must not be empty")
        if lease_seconds <= 0:
            raise ValueError("Job lease duration must be positive")
        with self._pool.connection() as connection, connection.transaction():
            slot = connection.execute(
                "SELECT * FROM gpu_capacity_slots WHERE slot_id = 1 FOR UPDATE"
            ).fetchone()
            if slot is None:
                raise RuntimeError("PostgreSQL GPU capacity slot is missing")
            now = self._database_time(connection)
            lease_expires_at = now + lease_seconds
            active_job_id = slot["active_job_id"]
            active_expires_at = slot["lease_expires_at"]
            if active_job_id is not None:
                if active_expires_at is not None and float(active_expires_at) > now:
                    return None
                recovered = self._recover_running_job(
                    connection,
                    job_id=str(active_job_id),
                    lease_token=int(slot["lease_token"]),
                    now=now,
                    attempt_outcome=JobAttemptOutcome.LEASE_EXPIRED,
                    retry_error="Worker lease expired; job safely requeued",
                    cancel_error="Job cancelled after worker lease expired",
                    dead_letter_error=(
                        "Worker lease expired after the maximum attempts; job dead-lettered"
                    ),
                )
                if not recovered:
                    raise RuntimeError("PostgreSQL GPU capacity slot and job lease diverged")
                self._clear_capacity_slot(connection)
            row = connection.execute(
                """
                SELECT id FROM gpu_jobs
                WHERE state = %s AND cancel_requested = FALSE
                ORDER BY enqueue_order ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (JobState.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            token_row = connection.execute(
                "SELECT nextval('gpu_fencing_token_seq') AS token"
            ).fetchone()
            if token_row is None:
                raise RuntimeError("PostgreSQL fencing sequence returned no token")
            claimed = connection.execute(
                """
                UPDATE gpu_jobs
                SET state = %s, started_at = %s, updated_at = %s,
                    lease_token = %s, lease_owner = %s, lease_expires_at = %s,
                    attempt_count = attempt_count + 1
                WHERE id = %s AND state = %s
                RETURNING *
                """,
                (
                    JobState.RUNNING.value,
                    now,
                    now,
                    int(token_row["token"]),
                    lease_owner,
                    lease_expires_at,
                    row["id"],
                    JobState.QUEUED.value,
                ),
            ).fetchone()
            if claimed is None:
                raise RuntimeError("PostgreSQL job claim lost its row lock")
            connection.execute(
                """
                INSERT INTO gpu_job_attempts(
                    job_id, attempt_number, lease_token, lease_owner,
                    started_at, lease_expires_at, outcome
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    claimed["id"],
                    claimed["attempt_count"],
                    claimed["lease_token"],
                    lease_owner,
                    now,
                    lease_expires_at,
                    JobAttemptOutcome.RUNNING.value,
                ),
            )
            connection.execute(
                """
                UPDATE gpu_capacity_slots
                SET active_job_id = %s, lease_token = %s,
                    lease_owner = %s, lease_expires_at = %s
                WHERE slot_id = 1
                """,
                (
                    claimed["id"],
                    claimed["lease_token"],
                    lease_owner,
                    lease_expires_at,
                ),
            )
            return self._row_to_job(claimed)

    def renew_lease(
        self,
        *,
        job_id: str,
        lease_token: int,
        lease_owner: str,
        lease_seconds: float,
    ) -> bool:
        """Extend the single live GPU capacity lease."""

        if not lease_owner:
            raise ValueError("Job lease owner must not be empty")
        if lease_seconds <= 0:
            raise ValueError("Job lease duration must be positive")
        with self._pool.connection() as connection, connection.transaction():
            slot = connection.execute(
                """
                SELECT * FROM gpu_capacity_slots
                WHERE slot_id = 1
                FOR UPDATE
                """
            ).fetchone()
            if slot is None:
                raise RuntimeError("PostgreSQL GPU capacity slot is missing")
            now = self._database_time(connection)
            if (
                slot["active_job_id"] != job_id
                or slot["lease_token"] != lease_token
                or slot["lease_owner"] != lease_owner
                or slot["lease_expires_at"] is None
                or float(slot["lease_expires_at"]) <= now
            ):
                return False
            expires_at = now + lease_seconds
            job = connection.execute(
                """
                UPDATE gpu_jobs
                SET lease_expires_at = %s, updated_at = %s
                WHERE id = %s AND state = %s AND lease_token = %s
                  AND lease_owner = %s AND lease_expires_at > %s
                RETURNING id
                """,
                (
                    expires_at,
                    now,
                    job_id,
                    JobState.RUNNING.value,
                    lease_token,
                    lease_owner,
                    now,
                ),
            ).fetchone()
            if job is None:
                raise LostJobLeaseError("GPU capacity lease and job lease diverged")
            attempt = connection.execute(
                """
                UPDATE gpu_job_attempts
                SET lease_expires_at = %s
                WHERE job_id = %s AND lease_token = %s AND outcome = %s
                RETURNING job_id
                """,
                (
                    expires_at,
                    job_id,
                    lease_token,
                    JobAttemptOutcome.RUNNING.value,
                ),
            ).fetchone()
            if attempt is None:
                raise RuntimeError("PostgreSQL job and attempt lease history diverged")
            connection.execute(
                """
                UPDATE gpu_capacity_slots
                SET lease_expires_at = %s
                WHERE slot_id = 1
                """,
                (expires_at,),
            )
            return True

    def complete(self, job_id: str, lease_token: int, result: dict[str, Any]) -> None:
        """Complete only if the caller still owns the fencing token."""

        self._finish(
            job_id=job_id,
            lease_token=lease_token,
            state=JobState.SUCCEEDED,
            result=result,
            error=None,
        )

    def fail(self, job_id: str, lease_token: int, error: str) -> None:
        """Fail only if the caller still owns the fencing token."""

        self._finish(
            job_id=job_id,
            lease_token=lease_token,
            state=JobState.FAILED,
            result=None,
            error=error,
        )

    def cancel_running(self, job_id: str, lease_token: int) -> None:
        """Commit cooperative cancellation under the current fencing token."""

        self._finish(
            job_id=job_id,
            lease_token=lease_token,
            state=JobState.CANCELLED,
            result=None,
            error="Job cancelled",
        )

    def _finish(
        self,
        *,
        job_id: str,
        lease_token: int,
        state: JobState,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        with self._pool.connection() as connection, connection.transaction():
            slot = connection.execute(
                "SELECT * FROM gpu_capacity_slots WHERE slot_id = 1 FOR UPDATE"
            ).fetchone()
            if slot is None:
                raise RuntimeError("PostgreSQL GPU capacity slot is missing")
            now = self._database_time(connection)
            if (
                slot["active_job_id"] != job_id
                or slot["lease_token"] != lease_token
                or slot["lease_expires_at"] is None
                or float(slot["lease_expires_at"]) <= now
            ):
                raise LostJobLeaseError(
                    "Lost GPU job lease; capacity slot rejected the finishing worker"
                )
            row = connection.execute(
                """
                UPDATE gpu_jobs
                SET state = %s, result_json = %s, error = %s,
                    finished_at = %s, updated_at = %s,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE id = %s AND state = %s AND lease_token = %s
                  AND lease_expires_at > %s
                RETURNING id
                """,
                (
                    state.value,
                    Jsonb(result) if result is not None else None,
                    error,
                    now,
                    now,
                    job_id,
                    JobState.RUNNING.value,
                    lease_token,
                    now,
                ),
            ).fetchone()
            if row is None:
                raise LostJobLeaseError("Lost GPU job lease; refusing stale commit")
            attempt = connection.execute(
                """
                UPDATE gpu_job_attempts
                SET finished_at = %s, outcome = %s, error = %s
                WHERE job_id = %s AND lease_token = %s AND outcome = %s
                RETURNING job_id
                """,
                (
                    now,
                    JobAttemptOutcome(state.value).value,
                    error,
                    job_id,
                    lease_token,
                    JobAttemptOutcome.RUNNING.value,
                ),
            ).fetchone()
            if attempt is None:
                raise RuntimeError("PostgreSQL terminal job and attempt history diverged")
            self._clear_capacity_slot(connection)

    def request_cancel(self, job_id: str) -> GpuJob:
        """Request cancellation through the internal scheduler boundary."""

        return self._request_cancel(job_id=job_id, user_id=None)

    def request_cancel_for_user(self, *, user_id: str, job_id: str) -> GpuJob:
        """Request cancellation only when the job belongs to the caller."""

        return self._request_cancel(job_id=job_id, user_id=user_id)

    def _request_cancel(self, *, job_id: str, user_id: str | None) -> GpuJob:
        now = time.time()
        with self._pool.connection() as connection, connection.transaction():
            if user_id is None:
                row = connection.execute(
                    "SELECT * FROM gpu_jobs WHERE id = %s FOR UPDATE",
                    (job_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM gpu_jobs
                    WHERE id = %s AND user_id = %s
                    FOR UPDATE
                    """,
                    (job_id, user_id),
                ).fetchone()
            if row is None:
                raise KeyError(job_id)
            state = JobState(str(row["state"]))
            if state == JobState.QUEUED:
                row = connection.execute(
                    """
                    UPDATE gpu_jobs
                    SET state = %s, cancel_requested = TRUE,
                        finished_at = %s, updated_at = %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (JobState.CANCELLED.value, now, now, job_id),
                ).fetchone()
            elif state == JobState.RUNNING:
                row = connection.execute(
                    """
                    UPDATE gpu_jobs
                    SET cancel_requested = TRUE, updated_at = %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (now, job_id),
                ).fetchone()
            if row is None:
                raise RuntimeError("PostgreSQL cancellation lost its row lock")
            return self._row_to_job(row)

    def has_queued(self) -> bool:
        """Return whether runnable work is waiting."""

        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM gpu_jobs
                WHERE state = %s AND cancel_requested = FALSE
                LIMIT 1
                """,
                (JobState.QUEUED.value,),
            ).fetchone()
        return row is not None

    def queue_position(self, job_id: str) -> int | None:
        """Return the one-based strict FIFO position for a queued job."""

        with self._pool.connection() as connection:
            target = connection.execute(
                """
                SELECT state, enqueue_order
                FROM gpu_jobs WHERE id = %s
                """,
                (job_id,),
            ).fetchone()
            if target is None:
                raise KeyError(job_id)
            if target["state"] != JobState.QUEUED.value:
                return None
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM gpu_jobs
                WHERE state = %s
                  AND cancel_requested = FALSE
                  AND enqueue_order <= %s
                """,
                (
                    JobState.QUEUED.value,
                    target["enqueue_order"],
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL queue position returned no count")
        return int(row["count"])

    def list_attempts(self, job_id: str) -> tuple[JobAttempt, ...]:
        """Return durable attempts in execution order."""

        self.get(job_id)
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM gpu_job_attempts
                WHERE job_id = %s
                ORDER BY attempt_number ASC
                """,
                (job_id,),
            ).fetchall()
        return tuple(self._row_to_attempt(row) for row in rows)

    def recover_interrupted(self) -> int:
        """Reconcile running work after explicit control-plane interruption."""

        with self._pool.connection() as connection, connection.transaction():
            slot = connection.execute(
                "SELECT * FROM gpu_capacity_slots WHERE slot_id = 1 FOR UPDATE"
            ).fetchone()
            if slot is None:
                raise RuntimeError("PostgreSQL GPU capacity slot is missing")
            now = self._database_time(connection)
            running = connection.execute(
                """
                SELECT id, lease_token FROM gpu_jobs
                WHERE state = %s
                FOR UPDATE
                """,
                (JobState.RUNNING.value,),
            ).fetchall()
            for row in running:
                if row["lease_token"] is None:
                    raise RuntimeError("Running PostgreSQL job has no fencing token")
                recovered = self._recover_running_job(
                    connection,
                    job_id=str(row["id"]),
                    lease_token=int(row["lease_token"]),
                    now=now,
                    attempt_outcome=JobAttemptOutcome.INTERRUPTED,
                    retry_error="Control plane restarted; job safely requeued",
                    cancel_error="Job cancelled after control-plane interruption",
                    dead_letter_error=(
                        "Control plane restarted after the maximum attempts; job dead-lettered"
                    ),
                )
                if not recovered:
                    raise RuntimeError("PostgreSQL interruption recovery lost its job")
            self._clear_capacity_slot(connection)
        return len(running)

    def recover_expired(self) -> int:
        """Reconcile the running job whose global capacity lease expired."""

        with self._pool.connection() as connection, connection.transaction():
            slot = connection.execute(
                "SELECT * FROM gpu_capacity_slots WHERE slot_id = 1 FOR UPDATE"
            ).fetchone()
            if slot is None:
                raise RuntimeError("PostgreSQL GPU capacity slot is missing")
            if slot["active_job_id"] is None:
                return 0
            now = self._database_time(connection)
            expires_at = slot["lease_expires_at"]
            if expires_at is not None and float(expires_at) > now:
                return 0
            recovered = self._recover_running_job(
                connection,
                job_id=str(slot["active_job_id"]),
                lease_token=int(slot["lease_token"]),
                now=now,
                attempt_outcome=JobAttemptOutcome.LEASE_EXPIRED,
                retry_error="Worker lease expired; job safely requeued",
                cancel_error="Job cancelled after worker lease expired",
                dead_letter_error=(
                    "Worker lease expired after the maximum attempts; job dead-lettered"
                ),
            )
            if not recovered:
                raise RuntimeError("PostgreSQL GPU capacity slot and job lease diverged")
            self._clear_capacity_slot(connection)
        return 1

    @staticmethod
    def _row_to_job(row: dict[str, Any]) -> GpuJob:
        payload = row["payload_json"]
        result = row["result_json"]
        if not isinstance(payload, dict):
            raise ValueError("PostgreSQL job payload must be a JSON object")
        if result is not None and not isinstance(result, dict):
            raise ValueError("PostgreSQL job result must be a JSON object")
        return GpuJob(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            kind=str(row["kind"]),
            payload=cast(dict[str, Any], payload),
            state=JobState(str(row["state"])),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=(float(row["started_at"]) if row["started_at"] is not None else None),
            finished_at=(float(row["finished_at"]) if row["finished_at"] is not None else None),
            result=cast(dict[str, Any], result) if result is not None else None,
            error=str(row["error"]) if row["error"] is not None else None,
            cancel_requested=bool(row["cancel_requested"]),
            lease_token=(int(row["lease_token"]) if row["lease_token"] is not None else None),
            lease_owner=(str(row["lease_owner"]) if row["lease_owner"] is not None else None),
            lease_expires_at=(
                float(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
            ),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
        )

    @staticmethod
    def _row_to_attempt(row: dict[str, Any]) -> JobAttempt:
        return JobAttempt(
            job_id=str(row["job_id"]),
            attempt_number=int(row["attempt_number"]),
            lease_token=int(row["lease_token"]),
            lease_owner=str(row["lease_owner"]),
            started_at=float(row["started_at"]),
            lease_expires_at=float(row["lease_expires_at"]),
            finished_at=(float(row["finished_at"]) if row["finished_at"] is not None else None),
            outcome=JobAttemptOutcome(str(row["outcome"])),
            error=str(row["error"]) if row["error"] is not None else None,
        )
