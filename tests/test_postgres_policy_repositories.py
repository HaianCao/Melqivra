"""Real PostgreSQL approval, tool-idempotency, and audit contract tests."""

from __future__ import annotations

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

import psycopg

from agent_music.agent_harness import (
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolPolicyError,
    ToolRegistry,
)
from agent_music.approval_repository import (
    ApprovalDecision,
    ApprovalPolicyError,
    ApprovalRequest,
    RiskClass,
)
from agent_music.domain import ProjectId, UserId
from agent_music.postgres_approval_repository import PostgresApprovalRepository
from agent_music.postgres_project_repository import PostgresProjectRepository
from agent_music.postgres_tool_execution_repository import PostgresToolExecutionRepository
from agent_music.tool_execution_repository import (
    AuditEvent,
    ToolExecutionDisposition,
    tool_input_sha256,
)

POSTGRES_DSN = os.getenv("MELQIVRA_TEST_POSTGRES_DSN", "")


@unittest.skipUnless(POSTGRES_DSN, "MELQIVRA_TEST_POSTGRES_DSN is not configured")
class PostgresPolicyRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.projects = PostgresProjectRepository(POSTGRES_DSN)
        self.approvals = PostgresApprovalRepository(POSTGRES_DSN)
        self.executions = PostgresToolExecutionRepository(POSTGRES_DSN)
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            connection.execute(
                """
                TRUNCATE TABLE
                    artifacts,
                    artifact_batches,
                    tool_audit_events,
                    tool_executions,
                    approval_requests,
                    project_revision_index,
                    project_revisions,
                    projects
                RESTART IDENTITY
                """
            )
        self.alice = UserId("usr_alice")
        self.bob = UserId("usr_bob")
        self.project_id = ProjectId("prj_policy")
        self.projects.create_project(
            owner_id=self.alice,
            title="Policy project",
            project_id=self.project_id,
            created_at=1.0,
        )
        self.projects.create_project(
            owner_id=self.bob,
            title="Other project",
            project_id=ProjectId("prj_other"),
            created_at=1.0,
        )

    def tearDown(self) -> None:
        self.executions.close()
        self.approvals.close()
        self.projects.close()

    def test_approval_survives_restart_and_remains_exact_and_final(self) -> None:
        request = self.approvals.propose(
            ApprovalRequest(
                "approval_pg",
                "music.generate:checksum",
                RiskClass.GPU,
                self.project_id,
                self.alice,
            )
        )
        self.approvals.close()
        self.approvals = PostgresApprovalRepository(POSTGRES_DSN)
        with self.assertRaisesRegex(ApprovalPolicyError, "not approved"):
            self.approvals.require_approved(
                approval_id=request.approval_id,
                user_id=self.alice,
                project_id=self.project_id,
                action=request.action,
                risk=request.risk,
            )
        decided = self.approvals.decide(
            approval_id=request.approval_id,
            user_id=self.alice,
            approved=True,
            actor="alice",
        )
        self.assertEqual(ApprovalDecision.APPROVED, decided.decision)
        self.assertEqual(
            frozenset({request.action}),
            self.approvals.approved_actions(
                user_id=self.alice,
                project_id=self.project_id,
            ),
        )
        with self.assertRaisesRegex(ApprovalPolicyError, "unavailable"):
            self.approvals.require_approved(
                approval_id=request.approval_id,
                user_id=self.bob,
                project_id=self.project_id,
                action=request.action,
                risk=request.risk,
            )
        with self.assertRaises(ValueError):
            self.approvals.decide(
                approval_id=request.approval_id,
                user_id=self.alice,
                approved=False,
            )

    def test_database_enforces_approval_and_audit_immutability(self) -> None:
        request = self.approvals.propose(
            ApprovalRequest(
                "approval_immutable",
                "revision.accept",
                RiskClass.EXTERNAL,
                self.project_id,
                self.alice,
            )
        )
        self.approvals.decide(
            approval_id=request.approval_id,
            user_id=self.alice,
            approved=True,
        )
        registry = self._registry(self.executions, lambda: None)
        registry.invoke(
            ToolContext(self.alice, self.project_id, "agent", turn_id="turn_one"),
            ToolCall("call_audit", "project.read", {}),
        )
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
            with self.assertRaisesRegex(psycopg.errors.RaiseException, "final"):
                connection.execute(
                    "UPDATE approval_requests SET decision = 'rejected' WHERE approval_id = %s",
                    (request.approval_id,),
                )
            with self.assertRaisesRegex(psycopg.errors.RaiseException, "append-only"):
                connection.execute("DELETE FROM tool_audit_events")
            columns = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'tool_audit_events'
                    """
                ).fetchall()
            }
        self.assertNotIn("arguments", columns)
        self.assertNotIn("result_json", columns)

    def test_tool_replay_survives_restart_without_repeating_handler(self) -> None:
        invocations = 0

        def invoked() -> None:
            nonlocal invocations
            invocations += 1

        context = ToolContext(self.alice, self.project_id, "agent", turn_id="turn_replay")
        call = ToolCall("call_replay", "project.read", {})
        first = self._registry(self.executions, invoked)
        result = first.invoke(context, call)
        self.executions.close()
        self.executions = PostgresToolExecutionRepository(POSTGRES_DSN)
        second = self._registry(self.executions, invoked)

        self.assertEqual(result, second.invoke(context, call))
        self.assertEqual(1, invocations)
        with self.assertRaisesRegex(ToolPolicyError, "reused"):
            second.invoke(
                context,
                ToolCall("call_replay", "project.read", {"changed": True}),
            )
        self.assertEqual(
            ["success", "idempotency_conflict"],
            [event.outcome for event in second.audit_events],
        )

    def test_competing_reservations_allow_exactly_one_executor(self) -> None:
        peer = PostgresToolExecutionRepository(POSTGRES_DSN)
        barrier = threading.Barrier(2)
        fingerprint = tool_input_sha256("lyrics.edit", {"text": "one"})

        def reserve(repository: PostgresToolExecutionRepository) -> ToolExecutionDisposition:
            barrier.wait()
            return repository.begin(
                user_id=self.alice,
                project_id=self.project_id,
                tool_call_id="call_competing",
                tool_name="lyrics.edit",
                input_sha256=fingerprint,
                actor="agent",
                turn_id="turn_competing",
            ).disposition

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                dispositions = tuple(executor.map(reserve, (self.executions, peer)))
        finally:
            peer.close()
        self.assertEqual(
            {ToolExecutionDisposition.EXECUTE, ToolExecutionDisposition.IN_PROGRESS},
            set(dispositions),
        )

    def test_repositories_reject_duplicate_cross_tenant_and_lost_reservations(self) -> None:
        request = ApprovalRequest(
            "approval_edges",
            "revision.accept",
            RiskClass.EXTERNAL,
            self.project_id,
            self.alice,
        )
        self.approvals.propose(request)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.approvals.propose(request)
        with self.assertRaises(KeyError):
            self.approvals.propose(
                ApprovalRequest(
                    "approval_cross_tenant",
                    "revision.accept",
                    RiskClass.EXTERNAL,
                    self.project_id,
                    self.bob,
                )
            )
        with self.assertRaises(KeyError):
            self.approvals.decide(
                approval_id=request.approval_id,
                user_id=self.bob,
                approved=True,
            )
        self.approvals.decide(
            approval_id=request.approval_id,
            user_id=self.alice,
            approved=True,
        )
        with self.assertRaisesRegex(ApprovalPolicyError, "does not match"):
            self.approvals.require_approved(
                approval_id=request.approval_id,
                user_id=self.alice,
                project_id=self.project_id,
                action="revision.restore",
                risk=request.risk,
            )

        fingerprint = tool_input_sha256("lyrics.edit", {"text": "one"})
        with self.assertRaises(KeyError):
            self.executions.begin(
                user_id=self.bob,
                project_id=self.project_id,
                tool_call_id="call_cross_tenant",
                tool_name="lyrics.edit",
                input_sha256=fingerprint,
                actor="agent",
                turn_id="turn_edges",
            )
        with self.assertRaisesRegex(RuntimeError, "reservation"):
            self.executions.complete_success(
                user_id=self.alice,
                project_id=self.project_id,
                tool_call_id="call_missing",
                tool_name="lyrics.edit",
                input_sha256=fingerprint,
                result={"revision_id": "rev_missing"},
                event=AuditEvent(
                    self.alice,
                    self.project_id,
                    "agent",
                    "lyrics.edit",
                    "call_missing",
                    "success",
                ),
            )
        self.assertEqual(
            ToolExecutionDisposition.EXECUTE,
            self.executions.begin(
                user_id=self.alice,
                project_id=self.project_id,
                tool_call_id="call_abandon",
                tool_name="lyrics.edit",
                input_sha256=fingerprint,
                actor="agent",
                turn_id="turn_edges",
            ).disposition,
        )
        self.executions.abandon(
            user_id=self.alice,
            project_id=self.project_id,
            tool_call_id="call_abandon",
            input_sha256=fingerprint,
        )
        self.assertEqual(
            ToolExecutionDisposition.EXECUTE,
            self.executions.begin(
                user_id=self.alice,
                project_id=self.project_id,
                tool_call_id="call_abandon",
                tool_name="lyrics.edit",
                input_sha256=fingerprint,
                actor="agent",
                turn_id="turn_edges",
            ).disposition,
        )
        self.assertEqual(
            (),
            self.executions.list_audit_events(
                user_id=self.bob,
                project_id=ProjectId("prj_other"),
            ),
        )

    @staticmethod
    def _registry(
        repository: PostgresToolExecutionRepository,
        on_invoke: object,
    ) -> ToolRegistry:
        registry = ToolRegistry(repository)

        def handler(context: ToolContext, arguments: dict[str, object]) -> dict[str, object]:
            del context, arguments
            if callable(on_invoke):
                on_invoke()
            return {"project_id": "prj_policy", "revision_id": "rev_policy"}

        registry.register(
            ToolDefinition(
                "project.read",
                "Read project",
                {"type": "object"},
                handler,
                lambda context, arguments: True,
            )
        )
        return registry


if __name__ == "__main__":
    unittest.main()
