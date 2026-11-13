"""S03 concrete project-tool and approval workflow tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_music.agent_harness import ToolCall, ToolContext, ToolPolicyError, ToolRegistry
from agent_music.agent_tools import ProjectToolService
from agent_music.domain import DocumentType, ProjectId, RevisionId, UserId
from agent_music.project_repository import SqliteProjectRepository


class ProjectToolServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = SqliteProjectRepository(Path(self.temporary.name) / "projects.db")
        self.owner = UserId("usr_alice")
        self.project_id = ProjectId("prj_demo")
        self.repository.create_project(
            owner_id=self.owner,
            project_id=self.project_id,
            title="Demo",
            created_at=1.0,
        )
        self.first = self.repository.commit_revision(
            owner_id=self.owner,
            project_id=self.project_id,
            document_type=DocumentType.LYRICS,
            payload={"chorus": "old", "locked": ["verse"]},
            expected_working_revision_id=None,
            actor="user",
            reason="initial",
            revision_id=RevisionId("rev_initial"),
            created_at=2.0,
        )
        self.registry = ToolRegistry()
        ProjectToolService(
            self.repository,
            read_takes=lambda context: ({"take_id": "take_one"},),
            read_jobs=lambda context: ({"job_id": "job_one"},),
        ).register(self.registry)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def test_reject_approve_and_restore_demo_preserves_history(self) -> None:
        base = ToolContext(
            self.owner,
            self.project_id,
            "agent",
            turn_id="turn_rewrite",
        )
        proposal = self.registry.invoke(
            base,
            ToolCall(
                "call_edit_v2",
                "lyrics.edit",
                {
                    "expected_parent_id": str(self.first.id),
                    "reason": "rewrite chorus",
                    "payload": {"chorus": "new", "locked": ["verse"]},
                },
            ),
        )
        second_id = RevisionId(str(proposal["revision_id"]))
        index = self.repository.get_index(owner_id=self.owner, project_id=self.project_id)
        self.assertIsNone(index.selections[0].accepted_revision_id)  # rejected by no acceptance

        approved = ToolContext(
            self.owner,
            self.project_id,
            "agent",
            frozenset({"revision.accept", "revision.restore"}),
            "turn_approve",
        )
        self.registry.invoke(
            approved,
            ToolCall(
                "call_accept_v2",
                "revision.accept",
                {
                    "document_type": "lyrics",
                    "revision_id": str(second_id),
                    "expected_working_id": str(second_id),
                },
            ),
        )
        third = self.registry.invoke(
            base,
            ToolCall(
                "call_edit_v3",
                "lyrics.edit",
                {
                    "expected_parent_id": str(second_id),
                    "reason": "try another chorus",
                    "payload": {"chorus": "third", "locked": ["verse"]},
                },
            ),
        )
        restored = self.registry.invoke(
            approved,
            ToolCall(
                "call_restore",
                "revision.restore",
                {
                    "source_revision_id": str(self.first.id),
                    "expected_working_id": str(third["revision_id"]),
                    "reason": "restore original",
                },
            ),
        )
        restored_revision = self.repository.get_revision(
            owner_id=self.owner,
            project_id=self.project_id,
            revision_id=RevisionId(str(restored["revision_id"])),
        )
        self.assertEqual(
            RevisionId(str(third["revision_id"])), restored_revision.parent_revision_id
        )
        self.assertEqual(
            self.repository.revision_payload(self.first),
            self.repository.revision_payload(restored_revision),
        )
        index = self.repository.get_index(owner_id=self.owner, project_id=self.project_id)
        self.assertEqual(second_id, index.selections[0].accepted_revision_id)
        self.assertEqual("agent", restored_revision.actor)
        self.assertEqual("turn_rewrite", self.registry.audit_events[0].turn_id)
        self.assertEqual(
            second_id, RevisionId(str(self.registry.audit_events[0].result_revision_id))
        )

    def test_read_tools_and_cross_tenant_authorization(self) -> None:
        context = ToolContext(self.owner, self.project_id, "agent")
        self.assertEqual(
            "Demo", self.registry.invoke(context, ToolCall("p", "project.read", {}))["title"]
        )
        self.assertEqual(
            "old",
            self.registry.invoke(context, ToolCall("l", "lyrics.read", {}))["payload"]["chorus"],
        )
        self.assertEqual(
            1, len(self.registry.invoke(context, ToolCall("t", "takes.list", {}))["items"])
        )
        self.assertEqual(
            1, len(self.registry.invoke(context, ToolCall("j", "jobs.list", {}))["items"])
        )
        with self.assertRaises(ToolPolicyError):
            self.registry.invoke(
                ToolContext(UserId("usr_bob"), self.project_id, "agent"),
                ToolCall("cross", "project.read", {}),
            )

    def test_accept_and_restore_require_explicit_approval(self) -> None:
        context = ToolContext(self.owner, self.project_id, "agent")
        for name in ("revision.accept", "revision.restore"):
            with self.subTest(name=name), self.assertRaises(ToolPolicyError):
                self.registry.invoke(context, ToolCall(f"call_{name}", name, {}))

    def test_invalid_mutation_does_not_create_revision(self) -> None:
        before = self.repository.list_revisions(
            owner_id=self.owner,
            project_id=self.project_id,
            document_type=DocumentType.LYRICS,
        )
        with self.assertRaises(ToolPolicyError):
            self.registry.invoke(
                ToolContext(self.owner, self.project_id, "agent"),
                ToolCall(
                    "invalid_edit",
                    "lyrics.edit",
                    {
                        "expected_parent_id": str(self.first.id),
                        "reason": "missing payload",
                    },
                ),
            )
        after = self.repository.list_revisions(
            owner_id=self.owner,
            project_id=self.project_id,
            document_type=DocumentType.LYRICS,
        )
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
