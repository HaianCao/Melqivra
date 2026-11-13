"""Approval-bound submission of accepted generation contracts."""

from __future__ import annotations

import uuid
from typing import Protocol

from agent_music.agent_harness import ToolPolicyError
from agent_music.approval_repository import (
    ApprovalPolicyError,
    ApprovalRepository,
    ApprovalRequest,
    RiskClass,
)
from agent_music.creative import ApprovedGenerationContract
from agent_music.domain import UserId
from agent_music.generation import MusicGenerationRequest, request_to_json
from agent_music.models import GpuJob


class JobSubmitter(Protocol):
    """Narrow durable-job boundary used by the generation service."""

    def enqueue(
        self,
        *,
        user_id: str,
        kind: str,
        payload: dict[str, object],
        idempotency_key: str | None = None,
    ) -> GpuJob: ...


def generation_approval_action(contract_checksum: str) -> str:
    """Bind approval to one immutable accepted-contract checksum."""

    return f"music.generate:{contract_checksum}"


class GenerationSubmissionService:
    """Require exact client approval before enqueuing expensive music work."""

    def __init__(self, approvals: ApprovalRepository, jobs: JobSubmitter) -> None:
        self._approvals = approvals
        self._jobs = jobs

    def propose_approval(
        self,
        *,
        user_id: UserId,
        contract: ApprovedGenerationContract,
        approval_id: str | None = None,
    ) -> ApprovalRequest:
        """Create a pending GPU approval for one accepted contract."""

        request = ApprovalRequest(
            approval_id or f"approval_{uuid.uuid4().hex}",
            generation_approval_action(contract.checksum),
            RiskClass.GPU,
            contract.project_id,
            user_id,
        )
        return self._approvals.propose(request)

    def submit(
        self,
        *,
        user_id: UserId,
        contract: ApprovedGenerationContract,
        request: MusicGenerationRequest,
        approval_id: str,
        idempotency_key: str,
    ) -> GpuJob:
        """Queue an exact approved request without changing backend or content."""

        request.validate()
        if request.project_id != contract.project_id:
            raise ToolPolicyError("Generation request project does not match approval")
        if request.contract_checksum != contract.checksum:
            raise ToolPolicyError("Generation request changed after approval")
        expected_revisions = (
            contract.brief_revision_id,
            contract.blueprint_revision_id,
            *((contract.lyrics_revision_id,) if contract.lyrics_revision_id is not None else ()),
            contract.plan_revision_id,
        )
        if request.source_revision_ids != expected_revisions:
            raise ToolPolicyError("Generation request revisions changed after approval")
        try:
            self._approvals.require_approved(
                approval_id=approval_id,
                user_id=user_id,
                project_id=contract.project_id,
                action=generation_approval_action(contract.checksum),
                risk=RiskClass.GPU,
            )
        except ApprovalPolicyError as error:
            raise ToolPolicyError(str(error)) from error
        return self._jobs.enqueue(
            user_id=str(user_id),
            kind="music.generate",
            payload={
                "generation_request": request_to_json(request),
                "plan_revision_id": str(contract.plan_revision_id),
            },
            idempotency_key=idempotency_key,
        )
