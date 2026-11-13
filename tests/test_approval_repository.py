"""Shared approval repository policy tests."""

from __future__ import annotations

import unittest

from agent_music.approval_repository import (
    ApprovalDecision,
    ApprovalPolicyError,
    ApprovalRequest,
    InMemoryApprovalRepository,
    RiskClass,
)
from agent_music.domain import ProjectId, UserId


class InMemoryApprovalRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryApprovalRepository(wall_clock=lambda: 123.0)
        self.request = ApprovalRequest(
            "approval_exact",
            "music.generate:checksum",
            RiskClass.GPU,
            ProjectId("prj_song"),
            UserId("usr_alice"),
        )

    def test_exact_binding_and_final_decision(self) -> None:
        proposed = self.repository.propose(self.request)
        self.assertEqual(ApprovalDecision.PENDING, proposed.decision)
        self.assertEqual(123.0, proposed.created_at)
        with self.assertRaisesRegex(ApprovalPolicyError, "not approved"):
            self.repository.require_approved(
                approval_id=proposed.approval_id,
                user_id=proposed.user_id,
                project_id=proposed.project_id,
                action=proposed.action,
                risk=proposed.risk,
            )

        decided = self.repository.decide(
            approval_id=proposed.approval_id,
            user_id=proposed.user_id,
            approved=True,
            actor="alice",
        )
        self.assertEqual(ApprovalDecision.APPROVED, decided.decision)
        self.assertEqual("alice", decided.decision_actor)
        self.assertEqual(
            decided,
            self.repository.require_approved(
                approval_id=decided.approval_id,
                user_id=decided.user_id,
                project_id=decided.project_id,
                action=decided.action,
                risk=decided.risk,
            ),
        )
        with self.assertRaises(ValueError):
            self.repository.decide(
                approval_id=decided.approval_id,
                user_id=decided.user_id,
                approved=False,
            )

    def test_cross_tenant_and_changed_contract_are_non_authoritative(self) -> None:
        proposed = self.repository.propose(self.request)
        self.repository.decide(
            approval_id=proposed.approval_id,
            user_id=proposed.user_id,
            approved=True,
        )
        with self.assertRaisesRegex(ApprovalPolicyError, "unavailable"):
            self.repository.require_approved(
                approval_id=proposed.approval_id,
                user_id=UserId("usr_bob"),
                project_id=proposed.project_id,
                action=proposed.action,
                risk=proposed.risk,
            )
        with self.assertRaisesRegex(ApprovalPolicyError, "does not match"):
            self.repository.require_approved(
                approval_id=proposed.approval_id,
                user_id=proposed.user_id,
                project_id=proposed.project_id,
                action="music.generate:changed",
                risk=proposed.risk,
            )


if __name__ == "__main__":
    unittest.main()
