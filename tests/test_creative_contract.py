"""S04 immutable accepted generation-contract tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_music.creative import (
    BackendCapabilities,
    BriefRequest,
    LyricsRights,
    build_brief,
    compile_generation_plan,
    create_lyrics,
    pin_accepted_contract,
    plan_blueprint,
)
from agent_music.domain import (
    DocumentId,
    DocumentType,
    ProjectId,
    RevisionId,
    SectionId,
    UserId,
    to_json_object,
)
from agent_music.project_store import SqliteProjectRepository


class AcceptedContractTests(unittest.TestCase):
    def test_contract_pins_accepted_immutable_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = SqliteProjectRepository(Path(temporary) / "project.db")
            owner = UserId("usr_alice")
            project_id = ProjectId("prj_contract")
            repository.create_project(
                owner_id=owner, project_id=project_id, title="Contract", created_at=1.0
            )
            brief = build_brief(
                DocumentId("doc_brief"),
                BriefRequest(
                    concept="Bình minh mới",
                    audience="general",
                    language="vi",
                    genres=("acoustic",),
                    moods=("hopeful",),
                    tempo_bpm=88,
                    duration_seconds=120.0,
                    vocal="duet",
                    user_lyrics="Mình đi qua đêm\nĐón ngày bình yên",
                    lyrics_rights=LyricsRights.USER_ORIGINAL,
                ),
            )
            brief_revision = repository.commit_revision(
                owner_id=owner,
                project_id=project_id,
                document_type=DocumentType.BRIEF,
                payload=to_json_object(brief),
                expected_working_revision_id=None,
                actor="user",
                reason="accept brief",
                revision_id=RevisionId("rev_brief"),
                created_at=2.0,
            )
            blueprint = plan_blueprint(DocumentId("doc_blueprint"), brief)
            blueprint_revision = repository.commit_revision(
                owner_id=owner,
                project_id=project_id,
                document_type=DocumentType.BLUEPRINT,
                payload=to_json_object(blueprint),
                expected_working_revision_id=None,
                actor="agent",
                reason="plan arrangement",
                revision_id=RevisionId("rev_blueprint"),
                created_at=3.0,
            )
            lyrics = create_lyrics(
                DocumentId("doc_lyrics"),
                brief,
                blueprint,
                {
                    SectionId("sec_verse_1"): ("Mình đi qua đêm",),
                    SectionId("sec_chorus_1"): ("Đón ngày bình yên",),
                },
            )
            lyrics_revision = repository.commit_revision(
                owner_id=owner,
                project_id=project_id,
                document_type=DocumentType.LYRICS,
                payload=to_json_object(lyrics),
                expected_working_revision_id=None,
                actor="agent",
                reason="structure user lyrics",
                revision_id=RevisionId("rev_lyrics"),
                created_at=4.0,
            )
            compiled = compile_generation_plan(
                DocumentId("doc_plan"),
                brief_revision_id=brief_revision.id,
                blueprint_revision_id=blueprint_revision.id,
                lyrics_revision_id=lyrics_revision.id,
                brief=brief,
                blueprint=blueprint,
                operation="text_to_music",
                take_count=2,
                base_seed=100,
                capabilities=BackendCapabilities(
                    frozenset({"text_to_music"}), 240.0, frozenset({"vi"})
                ),
            )
            plan_revision = repository.commit_revision(
                owner_id=owner,
                project_id=project_id,
                document_type=DocumentType.GENERATION_PLAN,
                payload=to_json_object(compiled.plan),
                expected_working_revision_id=None,
                actor="agent",
                reason="compile contract",
                revision_id=RevisionId("rev_plan"),
                created_at=5.0,
            )
            for document_type, revision_id in (
                (DocumentType.BRIEF, brief_revision.id),
                (DocumentType.BLUEPRINT, blueprint_revision.id),
                (DocumentType.LYRICS, lyrics_revision.id),
                (DocumentType.GENERATION_PLAN, plan_revision.id),
            ):
                repository.accept_revision(
                    owner_id=owner,
                    project_id=project_id,
                    document_type=document_type,
                    revision_id=revision_id,
                    expected_working_revision_id=revision_id,
                )
            contract = pin_accepted_contract(repository, owner_id=owner, project_id=project_id)
            self.assertEqual(brief_revision.id, contract.brief_revision_id)
            self.assertEqual(plan_revision.id, contract.plan_revision_id)
            self.assertEqual(64, len(contract.checksum))

            repository.commit_revision(
                owner_id=owner,
                project_id=project_id,
                document_type=DocumentType.BRIEF,
                payload={"concept": "unaccepted working change"},
                expected_working_revision_id=brief_revision.id,
                actor="agent",
                reason="working change",
                revision_id=RevisionId("rev_brief_working"),
                created_at=6.0,
            )
            self.assertEqual(
                contract,
                pin_accepted_contract(repository, owner_id=owner, project_id=project_id),
            )
            repository.close()


if __name__ == "__main__":
    unittest.main()
