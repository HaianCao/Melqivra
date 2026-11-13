"""Durable approval policy boundary for consequential actions."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from agent_music.domain import ProjectId, UserId, validate_identifier


class ApprovalPolicyError(RuntimeError):
    """An approval is unavailable, mismatched, or not approved."""


class RiskClass(StrEnum):
    """Consequential action categories that require explicit consent."""

    GPU = "gpu"
    EXTERNAL = "external"
    IDENTITY = "identity"
    DESTRUCTIVE = "destructive"


class ApprovalDecision(StrEnum):
    """One-way approval decision state."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """One exact, tenant-scoped consequential-action approval."""

    approval_id: str
    action: str
    risk: RiskClass
    project_id: ProjectId
    user_id: UserId
    decision: ApprovalDecision = ApprovalDecision.PENDING
    created_at: float | None = None
    decided_at: float | None = None
    decision_actor: str | None = None


class ApprovalRepository(Protocol):
    """Tenant-scoped durable authority for approval requests and decisions."""

    def close(self) -> None: ...

    def propose(self, request: ApprovalRequest) -> ApprovalRequest: ...

    def decide(
        self,
        *,
        approval_id: str,
        user_id: UserId,
        approved: bool,
        actor: str = "user",
    ) -> ApprovalRequest: ...

    def approved_actions(self, *, user_id: UserId, project_id: ProjectId) -> frozenset[str]: ...

    def require_approved(
        self,
        *,
        approval_id: str,
        user_id: UserId,
        project_id: ProjectId,
        action: str,
        risk: RiskClass,
    ) -> ApprovalRequest: ...


class InMemoryApprovalRepository:
    """Thread-safe development adapter implementing the durable approval contract."""

    def __init__(self, *, wall_clock: Callable[[], float] = time.time) -> None:
        self._wall_clock = wall_clock
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = threading.RLock()

    def close(self) -> None:
        """Release adapter resources; the in-memory adapter owns none."""

    def propose(self, request: ApprovalRequest) -> ApprovalRequest:
        """Create one pending request and reject identifier reuse."""

        validate_approval_request(request)
        if request.decision is not ApprovalDecision.PENDING:
            raise ValueError("A new approval request must be pending")
        created = replace(request, created_at=request.created_at or self._wall_clock())
        with self._lock:
            if request.approval_id in self._requests:
                raise ValueError("Duplicate approval ID")
            self._requests[request.approval_id] = created
        return created

    def decide(
        self,
        *,
        approval_id: str,
        user_id: UserId,
        approved: bool,
        actor: str = "user",
    ) -> ApprovalRequest:
        """Commit one final decision without revealing another tenant's request."""

        if not actor.strip():
            raise ValueError("Approval decision actor must not be empty")
        with self._lock:
            current = self._requests.get(approval_id)
            if current is None or current.user_id != user_id:
                raise KeyError(approval_id)
            if current.decision is not ApprovalDecision.PENDING:
                raise ValueError("Approval is already decided")
            decided = replace(
                current,
                decision=(ApprovalDecision.APPROVED if approved else ApprovalDecision.REJECTED),
                decided_at=self._wall_clock(),
                decision_actor=actor.strip(),
            )
            self._requests[approval_id] = decided
        return decided

    def approved_actions(self, *, user_id: UserId, project_id: ProjectId) -> frozenset[str]:
        """Return approved actions scoped to exactly one tenant project."""

        with self._lock:
            return frozenset(
                request.action
                for request in self._requests.values()
                if request.user_id == user_id
                and request.project_id == project_id
                and request.decision is ApprovalDecision.APPROVED
            )

    def require_approved(
        self,
        *,
        approval_id: str,
        user_id: UserId,
        project_id: ProjectId,
        action: str,
        risk: RiskClass,
    ) -> ApprovalRequest:
        """Resolve one exact approval without leaking other users' requests."""

        with self._lock:
            current = self._requests.get(approval_id)
        return require_matching_approval(
            current,
            user_id=user_id,
            project_id=project_id,
            action=action,
            risk=risk,
        )


def require_matching_approval(
    current: ApprovalRequest | None,
    *,
    user_id: UserId,
    project_id: ProjectId,
    action: str,
    risk: RiskClass,
) -> ApprovalRequest:
    """Apply the shared non-disclosing exact-approval policy."""

    if current is None or current.user_id != user_id or current.project_id != project_id:
        raise ApprovalPolicyError("Approval is unavailable")
    if current.action != action or current.risk is not risk:
        raise ApprovalPolicyError("Approval does not match the requested action")
    if current.decision is not ApprovalDecision.APPROVED:
        raise ApprovalPolicyError("Approval is not approved")
    return current


def validate_approval_request(request: ApprovalRequest) -> None:
    """Validate one approval proposal consistently across adapters."""

    validate_identifier(request.approval_id)
    validate_identifier(str(request.user_id))
    validate_identifier(str(request.project_id))
    if not request.action.strip() or len(request.action) > 512:
        raise ValueError("Approval action must contain 1 to 512 characters")
