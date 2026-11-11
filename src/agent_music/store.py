"""SQLite-backed durable job queue."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agent_music.models import GpuJob, JobState


class SqliteJobStore:
    """A FIFO queue with atomic claiming and fencing tokens.

    SQLite WAL is sufficient for the single self-hosted control plane. The
    store keeps the concurrency invariant durable across API restarts.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            database_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS gpu_jobs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    result_json TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    lease_token INTEGER,
                    idempotency_key TEXT,
                    UNIQUE(user_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS ix_gpu_jobs_state_created
                    ON gpu_jobs(state, created_at);
                CREATE TABLE IF NOT EXISTS harness_meta (
                    key TEXT PRIMARY KEY,
                    value_integer INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO harness_meta(key, value_integer)
                    VALUES ('next_fencing_token', 1);
                """
            )

    def close(self) -> None:
        """Close the underlying connection."""

        with self._lock:
            self._connection.close()

    def enqueue(
        self,
        *,
        user_id: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> GpuJob:
        """Append a job or return the prior idempotent job."""

        now = time.time()
        job_id = str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            try:
                self._connection.execute(
                    """
                    INSERT INTO gpu_jobs(
                        id, user_id, kind, payload_json, state,
                        created_at, updated_at, idempotency_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        user_id,
                        kind,
                        payload_json,
                        JobState.QUEUED.value,
                        now,
                        now,
                        idempotency_key,
                    ),
                )
            except sqlite3.IntegrityError:
                if idempotency_key is None:
                    raise
                row = self._connection.execute(
                    """
                    SELECT * FROM gpu_jobs
                    WHERE user_id = ? AND idempotency_key = ?
                    """,
                    (user_id, idempotency_key),
                ).fetchone()
                if row is None:
                    raise
                return self._row_to_job(row)
        return self.get(job_id)

    def get(self, job_id: str) -> GpuJob:
        """Load one job."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM gpu_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row_to_job(row)

    def claim_next(self) -> GpuJob | None:
        """Atomically claim the oldest queued job."""

        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    """
                    SELECT id FROM gpu_jobs
                    WHERE state = ? AND cancel_requested = 0
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """,
                    (JobState.QUEUED.value,),
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                token_row = self._connection.execute(
                    """
                    SELECT value_integer FROM harness_meta
                    WHERE key = 'next_fencing_token'
                    """
                ).fetchone()
                token = int(token_row["value_integer"])
                self._connection.execute(
                    """
                    UPDATE harness_meta
                    SET value_integer = value_integer + 1
                    WHERE key = 'next_fencing_token'
                    """
                )
                self._connection.execute(
                    """
                    UPDATE gpu_jobs
                    SET state = ?, started_at = ?, updated_at = ?,
                        lease_token = ?
                    WHERE id = ? AND state = ?
                    """,
                    (
                        JobState.RUNNING.value,
                        now,
                        now,
                        token,
                        row["id"],
                        JobState.QUEUED.value,
                    ),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return self.get(str(row["id"]))

    def complete(
        self,
        job_id: str,
        lease_token: int,
        result: dict[str, Any],
    ) -> None:
        """Complete only if the caller still owns the lease."""

        self._finish(
            job_id=job_id,
            lease_token=lease_token,
            state=JobState.SUCCEEDED,
            result=result,
            error=None,
        )

    def fail(self, job_id: str, lease_token: int, error: str) -> None:
        """Fail only if the caller still owns the lease."""

        self._finish(
            job_id=job_id,
            lease_token=lease_token,
            state=JobState.FAILED,
            result=None,
            error=error,
        )

    def cancel_running(self, job_id: str, lease_token: int) -> None:
        """Commit cooperative cancellation while preserving fencing."""

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
        now = time.time()
        result_json = (
            json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None
        )
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE gpu_jobs
                SET state = ?, result_json = ?, error = ?,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND state = ? AND lease_token = ?
                """,
                (
                    state.value,
                    result_json,
                    error,
                    now,
                    now,
                    job_id,
                    JobState.RUNNING.value,
                    lease_token,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("Lost GPU job lease; refusing stale commit")

    def request_cancel(self, job_id: str) -> GpuJob:
        """Cancel a queued job or flag a running job for cooperative cancellation."""

        now = time.time()
        with self._lock:
            job = self.get(job_id)
            if job.state == JobState.QUEUED:
                self._connection.execute(
                    """
                    UPDATE gpu_jobs
                    SET state = ?, cancel_requested = 1,
                        finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (JobState.CANCELLED.value, now, now, job_id),
                )
            elif job.state == JobState.RUNNING:
                self._connection.execute(
                    """
                    UPDATE gpu_jobs
                    SET cancel_requested = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, job_id),
                )
        return self.get(job_id)

    def has_queued(self) -> bool:
        """Return whether runnable work is waiting."""

        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1 FROM gpu_jobs
                WHERE state = ? AND cancel_requested = 0
                LIMIT 1
                """,
                (JobState.QUEUED.value,),
            ).fetchone()
        return row is not None

    def queue_position(self, job_id: str) -> int | None:
        """Return a one-based FIFO position for a queued job."""

        job = self.get(job_id)
        if job.state != JobState.QUEUED:
            return None
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM gpu_jobs
                WHERE state = ?
                  AND cancel_requested = 0
                  AND (created_at < ? OR (created_at = ? AND id <= ?))
                """,
                (
                    JobState.QUEUED.value,
                    job.created_at,
                    job.created_at,
                    job.id,
                ),
            ).fetchone()
        return int(row["count"])

    def recover_interrupted(self) -> int:
        """Requeue jobs left running by a crashed control plane."""

        now = time.time()
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE gpu_jobs
                SET state = ?, started_at = NULL, lease_token = NULL,
                    updated_at = ?, error = ?
                WHERE state = ?
                """,
                (
                    JobState.QUEUED.value,
                    now,
                    "Control plane restarted; job safely requeued",
                    JobState.RUNNING.value,
                ),
            )
        return int(cursor.rowcount)

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> GpuJob:
        result_json = row["result_json"]
        return GpuJob(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            kind=str(row["kind"]),
            payload=json.loads(str(row["payload_json"])),
            state=JobState(str(row["state"])),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=(float(row["started_at"]) if row["started_at"] is not None else None),
            finished_at=(float(row["finished_at"]) if row["finished_at"] is not None else None),
            result=json.loads(str(result_json)) if result_json else None,
            error=str(row["error"]) if row["error"] is not None else None,
            cancel_requested=bool(row["cancel_requested"]),
            lease_token=(int(row["lease_token"]) if row["lease_token"] is not None else None),
        )
