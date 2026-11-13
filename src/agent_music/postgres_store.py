"""PostgreSQL migrations and durable job-store adapter for S07."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from importlib import resources
from typing import Any, cast

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from agent_music.models import GpuJob, JobState
from agent_music.store import LostJobLeaseError

_MIGRATION_LOCK_ID = 1_296_382_033


class PostgresMigrationError(RuntimeError):
    """A tracked PostgreSQL migration is missing or no longer immutable."""


@dataclass(frozen=True, slots=True)
class PostgresMigration:
    """One ordered, checksummed package migration."""

    version: int
    name: str
    resource: str

    def read(self) -> tuple[str, str]:
        """Return migration SQL and its stable SHA-256 checksum."""

        content = resources.files("agent_music").joinpath(self.resource).read_text(encoding="utf-8")
        canonical = content.replace("\r\n", "\n").replace("\r", "\n")
        encoded = canonical.encode("utf-8")
        return canonical, hashlib.sha256(encoded).hexdigest()


POSTGRES_MIGRATIONS = (
    PostgresMigration(
        version=1,
        name="job_queue",
        resource="migrations/postgresql/0001_job_queue.sql",
    ),
    PostgresMigration(
        version=2,
        name="job_leases",
        resource="migrations/postgresql/0002_job_leases.sql",
    ),
)

PostgresConnection = Connection[dict[str, Any]]
PostgresPool = ConnectionPool[PostgresConnection]


class PostgresMigrator:
    """Apply immutable package migrations under one PostgreSQL advisory lock."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def apply(self) -> tuple[int, ...]:
        """Apply pending migrations and return their versions."""

        applied: list[int] = []
        with self._pool.connection() as connection, connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            rows = connection.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            existing = {int(row["version"]): row for row in rows}
            known_versions = {migration.version for migration in POSTGRES_MIGRATIONS}
            unknown = set(existing) - known_versions
            if unknown:
                raise PostgresMigrationError(
                    f"Database has unknown migration versions: {sorted(unknown)}"
                )
            for migration in POSTGRES_MIGRATIONS:
                sql, checksum = migration.read()
                prior = existing.get(migration.version)
                if prior is not None:
                    if prior["name"] != migration.name or prior["checksum"] != checksum:
                        raise PostgresMigrationError(
                            f"PostgreSQL migration {migration.version} checksum mismatch"
                        )
                    continue
                connection.execute(sql, prepare=False)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, name, checksum)
                    VALUES (%s, %s, %s)
                    """,
                    (migration.version, migration.name, checksum),
                )
                applied.append(migration.version)
        return tuple(applied)


class PostgresJobStore:
    """PostgreSQL FIFO queue preserving SQLite job and fencing semantics."""

    def __init__(
        self,
        dsn: str,
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 4,
        connection_timeout_seconds: float = 10.0,
        migrate: bool = True,
    ) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN must not be empty")
        if min_pool_size < 1 or max_pool_size < min_pool_size:
            raise ValueError("PostgreSQL pool sizes are invalid")
        if connection_timeout_seconds <= 0:
            raise ValueError("PostgreSQL connection timeout must be positive")
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
                    created_at, updated_at, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
                connection.execute(
                    """
                    UPDATE gpu_jobs
                    SET state = CASE WHEN cancel_requested THEN %s ELSE %s END,
                        started_at = NULL, lease_token = NULL,
                        lease_owner = NULL, lease_expires_at = NULL,
                        finished_at = CASE WHEN cancel_requested THEN %s ELSE NULL END,
                        updated_at = %s,
                        error = CASE WHEN cancel_requested THEN %s ELSE %s END
                    WHERE id = %s AND state = %s AND lease_token = %s
                    """,
                    (
                        JobState.CANCELLED.value,
                        JobState.QUEUED.value,
                        now,
                        now,
                        "Job cancelled after worker lease expired",
                        "Worker lease expired; job safely requeued",
                        active_job_id,
                        JobState.RUNNING.value,
                        slot["lease_token"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE gpu_capacity_slots
                    SET active_job_id = NULL, lease_token = NULL,
                        lease_owner = NULL, lease_expires_at = NULL
                    WHERE slot_id = 1
                    """
                )
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
            connection.execute(
                """
                UPDATE gpu_capacity_slots
                SET active_job_id = NULL, lease_token = NULL,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE slot_id = 1
                """,
            )

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

    def recover_interrupted(self) -> int:
        """Force-requeue running work for explicit recovery tooling."""

        now = time.time()
        with self._pool.connection() as connection, connection.transaction():
            rows = connection.execute(
                """
                UPDATE gpu_jobs
                SET state = CASE WHEN cancel_requested THEN %s ELSE %s END,
                    started_at = NULL, lease_token = NULL,
                    lease_owner = NULL, lease_expires_at = NULL,
                    finished_at = CASE WHEN cancel_requested THEN %s ELSE NULL END,
                    updated_at = %s,
                    error = CASE WHEN cancel_requested THEN %s ELSE %s END
                WHERE state = %s
                RETURNING id
                """,
                (
                    JobState.CANCELLED.value,
                    JobState.QUEUED.value,
                    now,
                    now,
                    "Job cancelled after control-plane interruption",
                    "Control plane restarted; job safely requeued",
                    JobState.RUNNING.value,
                ),
            ).fetchall()
            connection.execute(
                """
                UPDATE gpu_capacity_slots
                SET active_job_id = NULL, lease_token = NULL,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE slot_id = 1
                """
            )
        return len(rows)

    def recover_expired(self) -> int:
        """Requeue only the running job whose global capacity lease expired."""

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
            rows = connection.execute(
                """
                UPDATE gpu_jobs
                SET state = CASE WHEN cancel_requested THEN %s ELSE %s END,
                    started_at = NULL, lease_token = NULL,
                    lease_owner = NULL, lease_expires_at = NULL,
                    finished_at = CASE WHEN cancel_requested THEN %s ELSE NULL END,
                    updated_at = %s,
                    error = CASE WHEN cancel_requested THEN %s ELSE %s END
                WHERE id = %s AND state = %s AND lease_token = %s
                RETURNING id
                """,
                (
                    JobState.CANCELLED.value,
                    JobState.QUEUED.value,
                    now,
                    now,
                    "Job cancelled after worker lease expired",
                    "Worker lease expired; job safely requeued",
                    slot["active_job_id"],
                    JobState.RUNNING.value,
                    slot["lease_token"],
                ),
            ).fetchall()
            connection.execute(
                """
                UPDATE gpu_capacity_slots
                SET active_job_id = NULL, lease_token = NULL,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE slot_id = 1
                """
            )
        return len(rows)

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
        )
