"""PostgreSQL authority for tenant-scoped consequential-action approvals."""

from __future__ import annotations

from typing import Any

from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from agent_music.approval_repository import (
    ApprovalDecision,
    ApprovalRepository,
    ApprovalRequest,
    RiskClass,
    require_matching_approval,
    validate_approval_request,
)
from agent_music.domain import ProjectId, UserId
from agent_music.postgres_migrations import PostgresMigrator, PostgresPool


class PostgresApprovalRepository(ApprovalRepository):
    """PostgreSQL implementation of exact, one-way approval semantics."""

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

    def propose(self, request: ApprovalRequest) -> ApprovalRequest:
        """Persist one pending approval bound to an owned project."""

        validate_approval_request(request)
        if request.decision is not ApprovalDecision.PENDING:
            raise ValueError("A new approval request must be pending")
        timestamp_sql = (
            "%s" if request.created_at is not None else "EXTRACT(EPOCH FROM clock_timestamp())"
        )
        parameters: list[object] = [
            request.approval_id,
            str(request.user_id),
            str(request.project_id),
            request.action,
            request.risk.value,
        ]
        if request.created_at is not None:
            parameters.append(request.created_at)
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    INSERT INTO approval_requests(
                        approval_id, user_id, project_id, action, risk_class,
                        decision, created_at
                    )
                    SELECT %s, %s, %s, %s, %s, 'pending', {timestamp_sql}
                    FROM projects
                    WHERE id = %s AND owner_id = %s
                    RETURNING *
                    """,
                    (*parameters, str(request.project_id), str(request.user_id)),
                ).fetchone()
        except errors.UniqueViolation as error:
            raise ValueError("Duplicate approval ID") from error
        if row is None:
            raise KeyError(str(request.project_id))
        return self._row_to_request(row)

    def decide(
        self,
        *,
        approval_id: str,
        user_id: UserId,
        approved: bool,
        actor: str = "user",
    ) -> ApprovalRequest:
        """Commit one final tenant-scoped decision."""

        if not actor.strip():
            raise ValueError("Approval decision actor must not be empty")
        decision = ApprovalDecision.APPROVED if approved else ApprovalDecision.REJECTED
        with self._pool.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                UPDATE approval_requests
                SET decision = %s,
                    decided_at = EXTRACT(EPOCH FROM clock_timestamp()),
                    decision_actor = %s
                WHERE approval_id = %s AND user_id = %s AND decision = 'pending'
                RETURNING *
                """,
                (decision.value, actor.strip(), approval_id, str(user_id)),
            ).fetchone()
            if row is not None:
                return self._row_to_request(row)
            current = connection.execute(
                """
                SELECT * FROM approval_requests
                WHERE approval_id = %s AND user_id = %s
                """,
                (approval_id, str(user_id)),
            ).fetchone()
            if current is None:
                raise KeyError(approval_id)
            raise ValueError("Approval is already decided")

    def approved_actions(self, *, user_id: UserId, project_id: ProjectId) -> frozenset[str]:
        """Return approved actions scoped to exactly one tenant project."""

        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT action FROM approval_requests
                WHERE user_id = %s AND project_id = %s AND decision = 'approved'
                """,
                (str(user_id), str(project_id)),
            ).fetchall()
        return frozenset(str(row["action"]) for row in rows)

    def require_approved(
        self,
        *,
        approval_id: str,
        user_id: UserId,
        project_id: ProjectId,
        action: str,
        risk: RiskClass,
    ) -> ApprovalRequest:
        """Resolve one exact approval without cross-tenant disclosure."""

        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM approval_requests
                WHERE approval_id = %s AND user_id = %s AND project_id = %s
                """,
                (approval_id, str(user_id), str(project_id)),
            ).fetchone()
        current = self._row_to_request(row) if row is not None else None
        return require_matching_approval(
            current,
            user_id=user_id,
            project_id=project_id,
            action=action,
            risk=risk,
        )

    @staticmethod
    def _row_to_request(row: dict[str, Any]) -> ApprovalRequest:
        return ApprovalRequest(
            approval_id=str(row["approval_id"]),
            action=str(row["action"]),
            risk=RiskClass(str(row["risk_class"])),
            project_id=ProjectId(str(row["project_id"])),
            user_id=UserId(str(row["user_id"])),
            decision=ApprovalDecision(str(row["decision"])),
            created_at=float(row["created_at"]),
            decided_at=(float(row["decided_at"]) if row["decided_at"] is not None else None),
            decision_actor=(
                str(row["decision_actor"]) if row["decision_actor"] is not None else None
            ),
        )
