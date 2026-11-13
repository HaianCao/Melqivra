"""SQLite-backed durable job queue."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from agent_music.models import (
    GpuJob,
    JobAttempt,
    JobAttemptOutcome,
    JobEvent,
    JobPage,
    JobState,
)

if TYPE_CHECKING:
    from agent_music.config import HarnessSettings


class LostJobLeaseError(RuntimeError):
    """A worker no longer owns a live fencing lease for a running job."""


class UserJobQuotaExceededError(RuntimeError):
    """A tenant already owns the configured number of active jobs."""


class InvalidJobCursorError(ValueError):
    """A pagination cursor is missing or does not belong to the tenant."""


class InvalidJobEventCursorError(ValueError):
    """An event sequence is outside the tenant-visible durable stream."""


_JOB_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
MAX_JOB_EVENT_PAYLOAD_BYTES = 16_384


def validate_job_event(event_type: str, payload: dict[str, Any]) -> None:
    """Reject malformed or non-JSON durable event data at the store boundary."""

    if _JOB_EVENT_TYPE.fullmatch(event_type) is None:
        raise ValueError("Job event type is invalid")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_JOB_EVENT_PAYLOAD_BYTES:
        raise ValueError("Job event payload exceeds the durable size limit")


class JobStore(Protocol):
    """Durable job authority required by the concurrency-one scheduler."""

    def close(self) -> None: ...

    def enqueue(
        self,
        *,
        user_id: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
        max_active_jobs: int | None = None,
    ) -> GpuJob: ...

    def get(self, job_id: str) -> GpuJob: ...

    def get_for_user(self, *, user_id: str, job_id: str) -> GpuJob: ...

    def list_for_user(
        self,
        *,
        user_id: str,
        limit: int,
        cursor: str | None = None,
    ) -> JobPage: ...

    def claim_next(
        self,
        *,
        lease_owner: str = "legacy-worker",
        lease_seconds: float = 60.0,
    ) -> GpuJob | None: ...

    def renew_lease(
        self,
        *,
        job_id: str,
        lease_token: int,
        lease_owner: str,
        lease_seconds: float,
    ) -> bool: ...

    def complete(self, job_id: str, lease_token: int, result: dict[str, Any]) -> None: ...

    def fail(self, job_id: str, lease_token: int, error: str) -> None: ...

    def cancel_running(self, job_id: str, lease_token: int) -> None: ...

    def request_cancel(self, job_id: str) -> GpuJob: ...

    def request_cancel_for_user(self, *, user_id: str, job_id: str) -> GpuJob: ...

    def has_queued(self) -> bool: ...

    def queue_position(self, job_id: str) -> int | None: ...

    def list_attempts(self, job_id: str) -> tuple[JobAttempt, ...]: ...

    def append_event(
        self,
        *,
        job_id: str,
        lease_token: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> JobEvent: ...

    def list_events_for_user(
        self,
        *,
        user_id: str,
        job_id: str,
        after_sequence: int,
        limit: int,
    ) -> tuple[JobEvent, ...]: ...

    def recover_interrupted(self) -> int: ...

    def recover_expired(self) -> int: ...


class SqliteJobStore:
    """A FIFO queue with atomic claiming and fencing tokens.

    SQLite WAL is sufficient for the single self-hosted control plane. The
    store keeps the concurrency invariant durable across API restarts.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        max_attempts: int = 3,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("GPU job max attempts must be at least 1")
        self._database_path = database_path
        self._max_attempts = max_attempts
        self._wall_clock = wall_clock
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
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    next_event_sequence INTEGER NOT NULL DEFAULT 1,
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
                CREATE TABLE IF NOT EXISTS gpu_job_attempts (
                    job_id TEXT NOT NULL REFERENCES gpu_jobs(id) ON DELETE CASCADE,
                    attempt_number INTEGER NOT NULL,
                    lease_token INTEGER NOT NULL UNIQUE,
                    lease_owner TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    finished_at REAL,
                    outcome TEXT NOT NULL,
                    error TEXT,
                    PRIMARY KEY(job_id, attempt_number)
                );
                CREATE INDEX IF NOT EXISTS ix_gpu_job_attempts_job_started
                    ON gpu_job_attempts(job_id, started_at);
                CREATE TABLE IF NOT EXISTS gpu_job_events (
                    job_id TEXT NOT NULL REFERENCES gpu_jobs(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    job_state TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(job_id, sequence)
                );
                """
            )
            columns = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(gpu_jobs)").fetchall()
            }
            additions = {
                "lease_owner": "TEXT",
                "lease_expires_at": "REAL",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "max_attempts": "INTEGER NOT NULL DEFAULT 3",
                "next_event_sequence": "INTEGER NOT NULL DEFAULT 1",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    self._connection.execute(
                        f"ALTER TABLE gpu_jobs ADD COLUMN {name} {declaration}"
                    )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO gpu_job_attempts(
                    job_id, attempt_number, lease_token, lease_owner,
                    started_at, lease_expires_at, outcome
                )
                SELECT id, attempt_count, lease_token,
                    COALESCE(lease_owner, 'legacy-worker'),
                    COALESCE(started_at, created_at),
                    COALESCE(lease_expires_at, updated_at),
                    ?
                FROM gpu_jobs
                WHERE state = ? AND attempt_count > 0 AND lease_token IS NOT NULL
                """,
                (JobAttemptOutcome.RUNNING.value, JobState.RUNNING.value),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO gpu_job_events(
                    job_id, sequence, event_type, payload_json,
                    job_state, attempt_number, created_at
                )
                SELECT id, 1, 'job.snapshot', '{}', state, attempt_count, updated_at
                FROM gpu_jobs
                """
            )
            self._connection.execute(
                """
                UPDATE gpu_jobs
                SET next_event_sequence = (
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM gpu_job_events
                    WHERE job_id = gpu_jobs.id
                )
                WHERE next_event_sequence <= (
                    SELECT COALESCE(MAX(sequence), 0)
                    FROM gpu_job_events
                    WHERE job_id = gpu_jobs.id
                )
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
        max_active_jobs: int | None = None,
    ) -> GpuJob:
        """Append a job or return the prior idempotent job."""

        if max_active_jobs is not None and max_active_jobs < 1:
            raise ValueError("Active job quota must be at least 1")
        now = self._now()
        job_id = str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key is not None:
                    prior = self._connection.execute(
                        """
                        SELECT * FROM gpu_jobs
                        WHERE user_id = ? AND idempotency_key = ?
                        """,
                        (user_id, idempotency_key),
                    ).fetchone()
                    if prior is not None:
                        self._connection.execute("COMMIT")
                        return self._row_to_job(prior)
                if max_active_jobs is not None:
                    active = self._connection.execute(
                        """
                        SELECT COUNT(*) AS active_count FROM gpu_jobs
                        WHERE user_id = ? AND state IN (?, ?)
                        """,
                        (user_id, JobState.QUEUED.value, JobState.RUNNING.value),
                    ).fetchone()
                    if int(active["active_count"]) >= max_active_jobs:
                        raise UserJobQuotaExceededError("Active GPU job quota exceeded")
                self._connection.execute(
                    """
                    INSERT INTO gpu_jobs(
                        id, user_id, kind, payload_json, state,
                        created_at, updated_at, max_attempts, idempotency_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        user_id,
                        kind,
                        payload_json,
                        JobState.QUEUED.value,
                        now,
                        now,
                        self._max_attempts,
                        idempotency_key,
                    ),
                )
                self._append_event_locked(
                    job_id=job_id,
                    event_type="job.queued",
                    payload={},
                    created_at=now,
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
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

    def get_for_user(self, *, user_id: str, job_id: str) -> GpuJob:
        """Load one tenant-owned job without revealing cross-tenant existence."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM gpu_jobs WHERE id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row_to_job(row)

    def list_for_user(
        self,
        *,
        user_id: str,
        limit: int,
        cursor: str | None = None,
    ) -> JobPage:
        """List one stable FIFO page without accepting another tenant's cursor."""

        if limit < 1 or limit > 100:
            raise ValueError("Job page limit must be between 1 and 100")
        with self._lock:
            after_rowid = 0
            if cursor is not None:
                cursor_row = self._connection.execute(
                    "SELECT rowid FROM gpu_jobs WHERE id = ? AND user_id = ?",
                    (cursor, user_id),
                ).fetchone()
                if cursor_row is None:
                    raise InvalidJobCursorError("Job cursor is invalid")
                after_rowid = int(cursor_row["rowid"])
            rows = self._connection.execute(
                """
                SELECT * FROM gpu_jobs
                WHERE user_id = ? AND rowid > ?
                ORDER BY rowid ASC
                LIMIT ?
                """,
                (user_id, after_rowid, limit + 1),
            ).fetchall()
        page_rows = rows[:limit]
        next_cursor = str(page_rows[-1]["id"]) if len(rows) > limit else None
        return JobPage(tuple(self._row_to_job(row) for row in page_rows), next_cursor)

    def claim_next(
        self,
        *,
        lease_owner: str = "legacy-worker",
        lease_seconds: float = 60.0,
    ) -> GpuJob | None:
        """Atomically claim the oldest queued job."""

        if not lease_owner:
            raise ValueError("Job lease owner must not be empty")
        if lease_seconds <= 0:
            raise ValueError("Job lease duration must be positive")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                lease_expires_at = now + lease_seconds
                self._recover_expired_locked(now)
                active = self._connection.execute(
                    """
                    SELECT 1 FROM gpu_jobs
                    WHERE state = ? AND lease_expires_at > ?
                    LIMIT 1
                    """,
                    (JobState.RUNNING.value, now),
                ).fetchone()
                if active is not None:
                    self._connection.execute("COMMIT")
                    return None
                row = self._connection.execute(
                    """
                    SELECT id, attempt_count FROM gpu_jobs
                    WHERE state = ? AND cancel_requested = 0
                    ORDER BY rowid ASC
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
                attempt_number = int(row["attempt_count"]) + 1
                claimed = self._connection.execute(
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
                        lease_token = ?, lease_owner = ?, lease_expires_at = ?,
                        attempt_count = attempt_count + 1
                    WHERE id = ? AND state = ?
                    """,
                    (
                        JobState.RUNNING.value,
                        now,
                        now,
                        token,
                        lease_owner,
                        lease_expires_at,
                        row["id"],
                        JobState.QUEUED.value,
                    ),
                )
                if claimed.rowcount != 1:
                    raise RuntimeError("SQLite job claim lost its write lock")
                self._connection.execute(
                    """
                    INSERT INTO gpu_job_attempts(
                        job_id, attempt_number, lease_token, lease_owner,
                        started_at, lease_expires_at, outcome
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        attempt_number,
                        token,
                        lease_owner,
                        now,
                        lease_expires_at,
                        JobAttemptOutcome.RUNNING.value,
                    ),
                )
                self._append_event_locked(
                    job_id=str(row["id"]),
                    event_type="job.running",
                    payload={},
                    created_at=now,
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return self.get(str(row["id"]))

    def renew_lease(
        self,
        *,
        job_id: str,
        lease_token: int,
        lease_owner: str,
        lease_seconds: float,
    ) -> bool:
        """Extend one live lease without reviving an expired owner."""

        if not lease_owner:
            raise ValueError("Job lease owner must not be empty")
        if lease_seconds <= 0:
            raise ValueError("Job lease duration must be positive")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                cursor = self._connection.execute(
                    """
                    UPDATE gpu_jobs
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE id = ? AND state = ? AND lease_token = ?
                      AND lease_owner = ? AND lease_expires_at > ?
                    """,
                    (
                        now + lease_seconds,
                        now,
                        job_id,
                        JobState.RUNNING.value,
                        lease_token,
                        lease_owner,
                        now,
                    ),
                )
                if cursor.rowcount == 1:
                    attempt = self._connection.execute(
                        """
                        UPDATE gpu_job_attempts
                        SET lease_expires_at = ?
                        WHERE job_id = ? AND lease_token = ? AND outcome = ?
                        """,
                        (
                            now + lease_seconds,
                            job_id,
                            lease_token,
                            JobAttemptOutcome.RUNNING.value,
                        ),
                    )
                    if attempt.rowcount != 1:
                        raise RuntimeError("SQLite job and attempt lease history diverged")
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return cursor.rowcount == 1

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
        result_json = (
            json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                cursor = self._connection.execute(
                    """
                    UPDATE gpu_jobs
                    SET state = ?, result_json = ?, error = ?,
                        finished_at = ?, updated_at = ?,
                        lease_owner = NULL, lease_expires_at = NULL
                    WHERE id = ? AND state = ? AND lease_token = ?
                      AND lease_expires_at > ?
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
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LostJobLeaseError("Lost GPU job lease; refusing stale commit")
                attempt = self._connection.execute(
                    """
                    UPDATE gpu_job_attempts
                    SET finished_at = ?, outcome = ?, error = ?
                    WHERE job_id = ? AND lease_token = ? AND outcome = ?
                    """,
                    (
                        now,
                        JobAttemptOutcome(state.value).value,
                        error,
                        job_id,
                        lease_token,
                        JobAttemptOutcome.RUNNING.value,
                    ),
                )
                if attempt.rowcount != 1:
                    raise RuntimeError("SQLite terminal job and attempt history diverged")
                self._append_event_locked(
                    job_id=job_id,
                    event_type=f"job.{state.value}",
                    payload={},
                    created_at=now,
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    def request_cancel(self, job_id: str) -> GpuJob:
        """Cancel a queued job or flag a running job for cooperative cancellation."""

        return self._request_cancel(job_id=job_id, user_id=None)

    def request_cancel_for_user(self, *, user_id: str, job_id: str) -> GpuJob:
        """Request cancellation only when the job belongs to the caller."""

        return self._request_cancel(job_id=job_id, user_id=user_id)

    def _request_cancel(self, *, job_id: str, user_id: str | None) -> GpuJob:
        """Apply a cancellation transition after an optional tenant-scoped lookup."""

        now = self._now()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if user_id is None:
                    row = self._connection.execute(
                        "SELECT * FROM gpu_jobs WHERE id = ?",
                        (job_id,),
                    ).fetchone()
                else:
                    row = self._connection.execute(
                        "SELECT * FROM gpu_jobs WHERE id = ? AND user_id = ?",
                        (job_id, user_id),
                    ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                state = JobState(str(row["state"]))
                if state == JobState.QUEUED:
                    self._connection.execute(
                        """
                        UPDATE gpu_jobs
                        SET state = ?, cancel_requested = 1,
                            finished_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (JobState.CANCELLED.value, now, now, job_id),
                    )
                    self._append_event_locked(
                        job_id=job_id,
                        event_type="job.cancelled",
                        payload={},
                        created_at=now,
                    )
                elif state == JobState.RUNNING and not bool(row["cancel_requested"]):
                    self._connection.execute(
                        """
                        UPDATE gpu_jobs
                        SET cancel_requested = 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, job_id),
                    )
                    self._append_event_locked(
                        job_id=job_id,
                        event_type="job.cancel_requested",
                        payload={},
                        created_at=now,
                    )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return (
            self.get(job_id)
            if user_id is None
            else self.get_for_user(user_id=user_id, job_id=job_id)
        )

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
            target = self._connection.execute(
                "SELECT rowid FROM gpu_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if target is None:
                raise KeyError(job_id)
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM gpu_jobs
                WHERE state = ?
                  AND cancel_requested = 0
                  AND rowid <= ?
                """,
                (
                    JobState.QUEUED.value,
                    int(target["rowid"]),
                ),
            ).fetchone()
        return int(row["count"])

    def list_attempts(self, job_id: str) -> tuple[JobAttempt, ...]:
        """Return durable attempts in execution order."""

        self.get(job_id)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM gpu_job_attempts
                WHERE job_id = ?
                ORDER BY attempt_number ASC
                """,
                (job_id,),
            ).fetchall()
        return tuple(self._row_to_attempt(row) for row in rows)

    def append_event(
        self,
        *,
        job_id: str,
        lease_token: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> JobEvent:
        """Append one provider event only for the current fenced attempt."""

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                event = self._append_event_locked(
                    job_id=job_id,
                    event_type=event_type,
                    payload=payload,
                    created_at=self._now(),
                    expected_lease_token=lease_token,
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return event

    def list_events_for_user(
        self,
        *,
        user_id: str,
        job_id: str,
        after_sequence: int,
        limit: int,
    ) -> tuple[JobEvent, ...]:
        """Read one tenant-owned event batch after an SSE sequence cursor."""

        if after_sequence < 0:
            raise InvalidJobEventCursorError("Job event cursor is invalid")
        if limit < 1 or limit > 100:
            raise ValueError("Job event page limit must be between 1 and 100")
        with self._lock:
            job = self._connection.execute(
                """
                SELECT next_event_sequence FROM gpu_jobs
                WHERE id = ? AND user_id = ?
                """,
                (job_id, user_id),
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            latest_sequence = int(job["next_event_sequence"]) - 1
            if after_sequence > latest_sequence:
                raise InvalidJobEventCursorError("Job event cursor is invalid")
            rows = self._connection.execute(
                """
                SELECT * FROM gpu_job_events
                WHERE job_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (job_id, after_sequence, limit),
            ).fetchall()
        return tuple(self._row_to_event(row) for row in rows)

    def _append_event_locked(
        self,
        *,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: float,
        expected_lease_token: int | None = None,
    ) -> JobEvent:
        validate_job_event(event_type, payload)
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        row = self._connection.execute(
            """
            SELECT next_event_sequence, state, attempt_count, lease_token
            FROM gpu_jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        if expected_lease_token is not None and (
            row["state"] != JobState.RUNNING.value or row["lease_token"] != expected_lease_token
        ):
            raise LostJobLeaseError("Lost GPU job lease; refusing stale event")
        sequence = int(row["next_event_sequence"])
        self._connection.execute(
            """
            UPDATE gpu_jobs
            SET next_event_sequence = next_event_sequence + 1
            WHERE id = ?
            """,
            (job_id,),
        )
        self._connection.execute(
            """
            INSERT INTO gpu_job_events(
                job_id, sequence, event_type, payload_json,
                job_state, attempt_number, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                sequence,
                event_type,
                payload_json,
                row["state"],
                row["attempt_count"],
                created_at,
            ),
        )
        return JobEvent(
            job_id=job_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            job_state=JobState(str(row["state"])),
            attempt_number=int(row["attempt_count"]),
            created_at=created_at,
        )

    def recover_interrupted(self) -> int:
        """Reconcile running work after explicit single-node interruption."""

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                recovered = self._recover_running_locked(
                    now=self._now(),
                    expired_only=False,
                    attempt_outcome=JobAttemptOutcome.INTERRUPTED,
                    retry_error="Control plane restarted; job safely requeued",
                    cancel_error="Job cancelled after control-plane interruption",
                    dead_letter_error=(
                        "Control plane restarted after the maximum attempts; job dead-lettered"
                    ),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return recovered

    def recover_expired(self) -> int:
        """Reconcile only jobs whose heartbeat lease is absent or expired."""

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                recovered = self._recover_expired_locked(now)
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return recovered

    def _now(self) -> float:
        return self._wall_clock() if self._wall_clock is not None else time.time()

    def _recover_expired_locked(self, now: float) -> int:
        return self._recover_running_locked(
            now=now,
            expired_only=True,
            attempt_outcome=JobAttemptOutcome.LEASE_EXPIRED,
            retry_error="Worker lease expired; job safely requeued",
            cancel_error="Job cancelled after worker lease expired",
            dead_letter_error="Worker lease expired after the maximum attempts; job dead-lettered",
        )

    def _recover_running_locked(
        self,
        *,
        now: float,
        expired_only: bool,
        attempt_outcome: JobAttemptOutcome,
        retry_error: str,
        cancel_error: str,
        dead_letter_error: str,
    ) -> int:
        query = """
            SELECT id, lease_token, cancel_requested, attempt_count, max_attempts
            FROM gpu_jobs
            WHERE state = ?
        """
        parameters: tuple[object, ...] = (JobState.RUNNING.value,)
        if expired_only:
            query += " AND (lease_expires_at IS NULL OR lease_expires_at <= ?)"
            parameters = (*parameters, now)
        rows = self._connection.execute(query, parameters).fetchall()
        for row in rows:
            cancel_requested = bool(row["cancel_requested"])
            exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
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
            cursor = self._connection.execute(
                """
                UPDATE gpu_jobs
                SET state = ?, started_at = NULL, lease_token = NULL,
                    lease_owner = NULL, lease_expires_at = NULL,
                    finished_at = ?, updated_at = ?, error = ?
                WHERE id = ? AND state = ?
                """,
                (
                    state.value,
                    finished_at,
                    now,
                    error,
                    row["id"],
                    JobState.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("SQLite recovery lost its running job")
            lease_token = row["lease_token"]
            if lease_token is not None:
                attempt = self._connection.execute(
                    """
                    UPDATE gpu_job_attempts
                    SET finished_at = ?, outcome = ?, error = ?
                    WHERE job_id = ? AND lease_token = ? AND outcome = ?
                    """,
                    (
                        now,
                        outcome.value,
                        error,
                        row["id"],
                        lease_token,
                        JobAttemptOutcome.RUNNING.value,
                    ),
                )
                if attempt.rowcount != 1:
                    raise RuntimeError("SQLite recovery and attempt history diverged")
            event_type = "job.requeued" if state == JobState.QUEUED else f"job.{state.value}"
            self._append_event_locked(
                job_id=str(row["id"]),
                event_type=event_type,
                payload={"reason": attempt_outcome.value},
                created_at=now,
            )
        return len(rows)

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
            lease_owner=(str(row["lease_owner"]) if row["lease_owner"] is not None else None),
            lease_expires_at=(
                float(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
            ),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
        )

    @staticmethod
    def _row_to_attempt(row: sqlite3.Row) -> JobAttempt:
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

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> JobEvent:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError("SQLite job event payload must be a JSON object")
        return JobEvent(
            job_id=str(row["job_id"]),
            sequence=int(row["sequence"]),
            event_type=str(row["event_type"]),
            payload=payload,
            job_state=JobState(str(row["job_state"])),
            attempt_number=int(row["attempt_number"]),
            created_at=float(row["created_at"]),
        )


def build_job_store(settings: HarnessSettings) -> JobStore:
    """Build the configured durable job adapter without silent fallback."""

    if settings.job_store == "sqlite":
        return SqliteJobStore(
            settings.database_path,
            max_attempts=settings.job_max_attempts,
        )
    try:
        from agent_music.postgres_job_store import PostgresJobStore
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "PostgreSQL support is unavailable; install the 'postgres' extra"
        ) from error
    return PostgresJobStore(
        settings.postgres_dsn,
        min_pool_size=settings.postgres_pool_min_size,
        max_pool_size=settings.postgres_pool_max_size,
        connection_timeout_seconds=settings.postgres_connect_timeout_seconds,
        max_attempts=settings.job_max_attempts,
    )
