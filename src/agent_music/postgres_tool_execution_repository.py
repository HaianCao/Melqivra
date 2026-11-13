"""PostgreSQL authority for idempotent tool execution and minimal audit."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from agent_music.domain import JsonObject, ProjectId, UserId
from agent_music.postgres_migrations import PostgresConnection, PostgresMigrator, PostgresPool
from agent_music.tool_execution_repository import (
    AuditEvent,
    ToolExecutionClaim,
    ToolExecutionDisposition,
    ToolExecutionRepository,
    canonical_result_json,
    decode_result_json,
)


class PostgresToolExecutionRepository(ToolExecutionRepository):
    """PostgreSQL implementation of restart-safe tool replay and audit."""

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

    def begin(
        self,
        *,
        user_id: UserId,
        project_id: ProjectId,
        tool_call_id: str,
        tool_name: str,
        input_sha256: str,
        actor: str,
        turn_id: str,
    ) -> ToolExecutionClaim:
        """Reserve one call atomically or return its durable prior state."""

        with self._pool.connection() as connection, connection.transaction():
            inserted = connection.execute(
                """
                INSERT INTO tool_executions(
                    user_id, project_id, tool_call_id, tool_name, input_sha256,
                    state, actor, turn_id, started_at
                )
                SELECT %s, %s, %s, %s, %s, 'running', %s, %s,
                    EXTRACT(EPOCH FROM clock_timestamp())
                FROM projects
                WHERE id = %s AND owner_id = %s
                ON CONFLICT(user_id, project_id, tool_call_id) DO NOTHING
                RETURNING tool_call_id
                """,
                (
                    str(user_id),
                    str(project_id),
                    tool_call_id,
                    tool_name,
                    input_sha256,
                    actor,
                    turn_id,
                    str(project_id),
                    str(user_id),
                ),
            ).fetchone()
            if inserted is not None:
                return ToolExecutionClaim(ToolExecutionDisposition.EXECUTE)
            row = connection.execute(
                """
                SELECT tool_name, input_sha256, state, result_json
                FROM tool_executions
                WHERE user_id = %s AND project_id = %s AND tool_call_id = %s
                """,
                (str(user_id), str(project_id), tool_call_id),
            ).fetchone()
        if row is None:
            raise KeyError(str(project_id))
        if row["tool_name"] != tool_name or row["input_sha256"] != input_sha256:
            return ToolExecutionClaim(ToolExecutionDisposition.CONFLICT)
        if row["state"] == "running":
            return ToolExecutionClaim(ToolExecutionDisposition.IN_PROGRESS)
        result_json = row["result_json"]
        if not isinstance(result_json, str):
            raise RuntimeError("Completed tool execution has no result")
        return ToolExecutionClaim(
            ToolExecutionDisposition.REPLAY,
            decode_result_json(result_json),
        )

    def complete_success(
        self,
        *,
        user_id: UserId,
        project_id: ProjectId,
        tool_call_id: str,
        tool_name: str,
        input_sha256: str,
        result: JsonObject,
        event: AuditEvent,
    ) -> None:
        """Atomically publish one replay result and append its success audit."""

        result_json = canonical_result_json(result)
        with self._pool.connection() as connection, connection.transaction():
            updated = connection.execute(
                """
                UPDATE tool_executions
                SET state = 'completed', result_json = %s, result_revision_id = %s,
                    completed_at = EXTRACT(EPOCH FROM clock_timestamp())
                WHERE user_id = %s AND project_id = %s AND tool_call_id = %s
                  AND tool_name = %s AND input_sha256 = %s AND state = 'running'
                """,
                (
                    result_json,
                    event.result_revision_id,
                    str(user_id),
                    str(project_id),
                    tool_call_id,
                    tool_name,
                    input_sha256,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Tool execution reservation was lost")
            self._insert_audit(connection, event)

    def abandon(
        self,
        *,
        user_id: UserId,
        project_id: ProjectId,
        tool_call_id: str,
        input_sha256: str,
    ) -> None:
        """Remove only the caller's matching unfinished reservation."""

        with self._pool.connection() as connection:
            connection.execute(
                """
                DELETE FROM tool_executions
                WHERE user_id = %s AND project_id = %s AND tool_call_id = %s
                  AND input_sha256 = %s AND state = 'running'
                """,
                (str(user_id), str(project_id), tool_call_id, input_sha256),
            )

    def append_audit(self, event: AuditEvent) -> None:
        """Append one policy or failure audit without arguments or result content."""

        with self._pool.connection() as connection:
            inserted = self._insert_audit(connection, event)
        if inserted != 1:
            raise KeyError(str(event.project_id))

    def list_audit_events(
        self,
        *,
        user_id: UserId | None = None,
        project_id: ProjectId | None = None,
    ) -> tuple[AuditEvent, ...]:
        """List ordered audit events with optional tenant/project scoping."""

        clauses: list[str] = []
        parameters: list[object] = []
        if user_id is not None:
            clauses.append("user_id = %s")
            parameters.append(str(user_id))
        if project_id is not None:
            clauses.append("project_id = %s")
            parameters.append(str(project_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT user_id, project_id, actor, tool_name, tool_call_id,
                    outcome, result_revision_id, turn_id
                FROM tool_audit_events
                {where}
                ORDER BY event_order
                """,
                tuple(parameters),
            ).fetchall()
        return tuple(self._row_to_event(row) for row in rows)

    @staticmethod
    def _insert_audit(connection: PostgresConnection, event: AuditEvent) -> int:
        cursor = connection.execute(
            """
            INSERT INTO tool_audit_events(
                user_id, project_id, actor, tool_name, tool_call_id,
                outcome, result_revision_id, turn_id, created_at
            )
            SELECT %s, %s, %s, %s, %s, %s, %s, %s,
                EXTRACT(EPOCH FROM clock_timestamp())
            FROM projects
            WHERE id = %s AND owner_id = %s
            """,
            (
                str(event.user_id),
                str(event.project_id),
                event.actor,
                event.tool_name,
                event.tool_call_id,
                event.outcome,
                event.result_revision_id,
                event.turn_id,
                str(event.project_id),
                str(event.user_id),
            ),
        )
        return int(cursor.rowcount)

    @staticmethod
    def _row_to_event(row: dict[str, Any]) -> AuditEvent:
        return AuditEvent(
            user_id=UserId(str(row["user_id"])),
            project_id=ProjectId(str(row["project_id"])),
            actor=str(row["actor"]),
            tool_name=str(row["tool_name"]),
            tool_call_id=str(row["tool_call_id"]),
            outcome=str(row["outcome"]),
            result_revision_id=(
                str(row["result_revision_id"]) if row["result_revision_id"] is not None else None
            ),
            turn_id=str(row["turn_id"]),
        )
