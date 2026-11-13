"""CPU-safe tool idempotency and minimal-audit repository tests."""

from __future__ import annotations

import unittest

from agent_music.domain import ProjectId, UserId
from agent_music.tool_execution_repository import (
    AuditEvent,
    InMemoryToolExecutionRepository,
    ToolExecutionDisposition,
    tool_input_sha256,
)


class InMemoryToolExecutionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryToolExecutionRepository()
        self.user_id = UserId("usr_alice")
        self.project_id = ProjectId("prj_song")
        self.fingerprint = tool_input_sha256("lyrics.edit", {"text": "one"})

    def _begin(self, *, fingerprint: str | None = None):
        return self.repository.begin(
            user_id=self.user_id,
            project_id=self.project_id,
            tool_call_id="call_one",
            tool_name="lyrics.edit",
            input_sha256=fingerprint or self.fingerprint,
            actor="agent",
            turn_id="turn_one",
        )

    def test_reservation_replay_and_conflict_are_explicit(self) -> None:
        self.assertEqual(ToolExecutionDisposition.EXECUTE, self._begin().disposition)
        self.assertEqual(ToolExecutionDisposition.IN_PROGRESS, self._begin().disposition)
        event = AuditEvent(
            self.user_id,
            self.project_id,
            "agent",
            "lyrics.edit",
            "call_one",
            "success",
            "rev_one",
            "turn_one",
        )
        self.repository.complete_success(
            user_id=self.user_id,
            project_id=self.project_id,
            tool_call_id="call_one",
            tool_name="lyrics.edit",
            input_sha256=self.fingerprint,
            result={"revision_id": "rev_one"},
            event=event,
        )

        replay = self._begin()
        self.assertEqual(ToolExecutionDisposition.REPLAY, replay.disposition)
        self.assertEqual({"revision_id": "rev_one"}, replay.result)
        changed = tool_input_sha256("lyrics.edit", {"text": "changed"})
        self.assertEqual(
            ToolExecutionDisposition.CONFLICT,
            self._begin(fingerprint=changed).disposition,
        )
        self.assertEqual((event,), self.repository.list_audit_events())
        self.assertFalse(hasattr(event, "arguments"))

    def test_abandon_releases_only_matching_unfinished_call(self) -> None:
        self.assertEqual(ToolExecutionDisposition.EXECUTE, self._begin().disposition)
        self.repository.abandon(
            user_id=self.user_id,
            project_id=self.project_id,
            tool_call_id="call_one",
            input_sha256="0" * 64,
        )
        self.assertEqual(ToolExecutionDisposition.IN_PROGRESS, self._begin().disposition)
        self.repository.abandon(
            user_id=self.user_id,
            project_id=self.project_id,
            tool_call_id="call_one",
            input_sha256=self.fingerprint,
        )
        self.assertEqual(ToolExecutionDisposition.EXECUTE, self._begin().disposition)


if __name__ == "__main__":
    unittest.main()
