"""Explicit client-approval tests for S05 generation submission."""

from __future__ import annotations

import time
import unittest

from agent_music.agent_harness import ApprovalDecision, ApprovalLedger, ToolPolicyError
from agent_music.creative import ApprovedGenerationContract
from agent_music.domain import ProjectId, RevisionId, UserId
from agent_music.generation import MusicGenerationRequest, TakeSpec, VocalMode
from agent_music.generation_service import GenerationSubmissionService
from agent_music.models import GpuJob, JobState


class RecordingJobs:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def enqueue(
        self,
        *,
        user_id: str,
        kind: str,
        payload: dict[str, object],
        idempotency_key: str | None = None,
    ) -> GpuJob:
        self.calls.append(
            {
                "user_id": user_id,
                "kind": kind,
                "payload": payload,
                "idempotency_key": idempotency_key,
            }
        )
        now = time.time()
        return GpuJob("job_queued", user_id, kind, payload, JobState.QUEUED, now, now)


class GenerationSubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user_id = UserId("user_s05")
        self.project_id = ProjectId("project_s05")
        self.contract = ApprovedGenerationContract(
            self.project_id,
            RevisionId("rev_brief"),
            RevisionId("rev_blueprint"),
            RevisionId("rev_lyrics"),
            RevisionId("rev_plan"),
            "a" * 64,
        )
        self.request = MusicGenerationRequest(
            1,
            "job_s05",
            self.project_id,
            self.contract.checksum,
            (
                self.contract.brief_revision_id,
                self.contract.blueprint_revision_id,
                self.contract.lyrics_revision_id,
                self.contract.plan_revision_id,
            ),
            "heartmula_3b",
            "source-pin",
            "text2music",
            "en",
            VocalMode.VOCAL,
            "English pop with acoustic guitar",
            "[Verse]\nWe begin again",
            30.0,
            (
                TakeSpec("take_one", "faithful", 1, {}),
                TakeSpec("take_two", "expressive", 2, {}),
            ),
        )
        self.approvals = ApprovalLedger()
        self.jobs = RecordingJobs()
        self.service = GenerationSubmissionService(self.approvals, self.jobs)

    def test_pending_or_rejected_approval_cannot_schedule_gpu_work(self) -> None:
        approval = self.service.propose_approval(
            user_id=self.user_id,
            contract=self.contract,
            approval_id="approval_s05",
        )
        self.assertEqual(ApprovalDecision.PENDING, approval.decision)
        with self.assertRaisesRegex(ToolPolicyError, "not approved"):
            self._submit()
        self.approvals.decide(
            approval_id=approval.approval_id,
            user_id=self.user_id,
            approved=False,
        )
        with self.assertRaisesRegex(ToolPolicyError, "not approved"):
            self._submit()
        self.assertEqual([], self.jobs.calls)

    def test_exact_approved_contract_queues_one_durable_job(self) -> None:
        approval = self.service.propose_approval(
            user_id=self.user_id,
            contract=self.contract,
            approval_id="approval_s05",
        )
        self.approvals.decide(
            approval_id=approval.approval_id,
            user_id=self.user_id,
            approved=True,
        )
        job = self._submit()
        self.assertEqual(JobState.QUEUED, job.state)
        self.assertEqual("music.generate", self.jobs.calls[0]["kind"])
        self.assertEqual("contract-a", self.jobs.calls[0]["idempotency_key"])

    def test_contract_change_or_cross_user_approval_is_rejected(self) -> None:
        approval = self.service.propose_approval(
            user_id=self.user_id,
            contract=self.contract,
            approval_id="approval_s05",
        )
        self.approvals.decide(
            approval_id=approval.approval_id,
            user_id=self.user_id,
            approved=True,
        )
        changed = MusicGenerationRequest(
            *(
                self.request.schema_version,
                self.request.request_id,
                self.request.project_id,
                "b" * 64,
                self.request.source_revision_ids,
                self.request.backend_id,
                self.request.backend_revision,
                self.request.operation,
                self.request.language,
                self.request.vocal_mode,
                self.request.prompt,
                self.request.lyrics,
                self.request.duration_seconds,
                self.request.takes,
            )
        )
        with self.assertRaisesRegex(ToolPolicyError, "changed after approval"):
            self.service.submit(
                user_id=self.user_id,
                contract=self.contract,
                request=changed,
                approval_id="approval_s05",
                idempotency_key="contract-b",
            )
        with self.assertRaisesRegex(ToolPolicyError, "unavailable"):
            self.service.submit(
                user_id=UserId("other_user"),
                contract=self.contract,
                request=self.request,
                approval_id="approval_s05",
                idempotency_key="contract-a",
            )
        self.assertEqual([], self.jobs.calls)

    def _submit(self) -> GpuJob:
        return self.service.submit(
            user_id=self.user_id,
            contract=self.contract,
            request=self.request,
            approval_id="approval_s05",
            idempotency_key="contract-a",
        )


if __name__ == "__main__":
    unittest.main()
