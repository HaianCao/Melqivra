"""S04 creative capabilities through the authenticated S03 registry."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_music.agent_harness import ToolCall, ToolContext, ToolPolicyError, ToolRegistry
from agent_music.creative import BriefRequest, LyricsRights, build_brief, plan_blueprint
from agent_music.creative_tools import CreativeToolService
from agent_music.domain import DocumentId, JsonObject, ProjectId, UserId, to_json_object
from agent_music.project_store import SqliteProjectRepository


class CreativeToolServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = SqliteProjectRepository(Path(self.temporary.name) / "projects.db")
        self.owner = UserId("usr_alice")
        self.project_id = ProjectId("prj_creative")
        self.repository.create_project(
            owner_id=self.owner,
            project_id=self.project_id,
            title="Creative",
            created_at=1.0,
        )
        self.context = ToolContext(self.owner, self.project_id, "agent")
        self.registry = ToolRegistry()
        CreativeToolService(self.repository).register(self.registry)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def test_agent_can_propose_reviewable_contract_without_gpu(self) -> None:
        brief_result = self.registry.invoke(
            self.context,
            ToolCall(
                "brief",
                "creative.brief.propose",
                {
                    "document_id": "doc_brief",
                    "concept": "Một ngày mới",
                    "audience": "general",
                    "language": "vi",
                    "genres": ["acoustic"],
                    "moods": ["hopeful"],
                    "tempo_bpm": 90,
                    "duration_seconds": 120.0,
                    "vocal": "duet",
                    "lyrics_rights": "user_original",
                    "style_description": "warm acoustic pop",
                },
            ),
        )
        brief_payload = brief_result["brief"]
        self.assertIsInstance(brief_payload, dict)
        blueprint_result = self.registry.invoke(
            self.context,
            ToolCall(
                "blueprint",
                "creative.blueprint.propose",
                {"document_id": "doc_blueprint", "brief": brief_payload},
            ),
        )
        blueprint_payload = blueprint_result["blueprint"]
        self.assertIsInstance(blueprint_payload, dict)
        lyrics_result = self.registry.invoke(
            self.context,
            ToolCall(
                "lyrics",
                "creative.lyrics.propose",
                {
                    "document_id": "doc_lyrics",
                    "brief": brief_payload,
                    "blueprint": blueprint_payload,
                    "candidates": {
                        "sec_verse_1": ["Mình đi qua đêm"],
                        "sec_chorus_1": ["Đón ngày bình yên"],
                    },
                    "locked_sections": ["sec_verse_1"],
                },
            ),
        )
        lyrics_payload = lyrics_result["lyrics"]
        continued = self.registry.invoke(
            self.context,
            ToolCall(
                "continue",
                "creative.lyrics.continue",
                {
                    "lyrics": lyrics_payload,
                    "additions": {"sec_chorus_1": ["Ngày mai nắng lên"]},
                },
            ),
        )
        lyrics_payload = continued["lyrics"]
        compile_arguments: JsonObject = {
            "document_id": "doc_plan",
            "brief_revision_id": "rev_brief",
            "blueprint_revision_id": "rev_blueprint",
            "lyrics_revision_id": "rev_lyrics",
            "brief": brief_payload,
            "blueprint": blueprint_payload,
            "lyrics": lyrics_payload,
            "operation": "text_to_music",
            "take_count": 2,
            "base_seed": 7,
            "capabilities": {
                "operations": ["text_to_music"],
                "max_duration_seconds": 240.0,
                "supported_languages": ["vi"],
                "supports_vocals": True,
            },
        }
        compiled = self.registry.invoke(
            self.context,
            ToolCall("compile", "creative.plan.compile", compile_arguments),
        )
        plan_payload = compiled["plan"]
        self.assertIsInstance(plan_payload, dict)
        assert isinstance(plan_payload, dict)
        self.assertEqual(2, plan_payload["take_count"])
        review = self.registry.invoke(
            self.context,
            ToolCall("review", "creative.contract.review", compile_arguments),
        )
        approval_view = review["approval_view"]
        self.assertIsInstance(approval_view, str)
        self.assertIn("2 takes", str(approval_view))
        self.assertEqual(
            0,
            len(
                self.repository.get_index(
                    owner_id=self.owner, project_id=self.project_id
                ).selections
            ),
        )
        brief_schema = next(
            schema
            for schema in self.registry.schemas()
            if schema["function"]["name"] == "creative.brief.propose"  # type: ignore[index]
        )
        parameters = brief_schema["function"]["parameters"]  # type: ignore[index]
        self.assertFalse(parameters["additionalProperties"])  # type: ignore[index]
        self.assertIn("concept", parameters["required"])  # type: ignore[index]

    def test_tools_reject_cross_tenant_and_unsafe_identity(self) -> None:
        arguments: JsonObject = {
            "document_id": "doc_brief",
            "concept": "Idea",
            "audience": "general",
            "language": "vi",
            "genres": ["pop"],
            "moods": ["bright"],
            "tempo_bpm": 100,
            "duration_seconds": 90.0,
            "vocal": "clone the voice of X",
        }
        with self.assertRaises(ToolPolicyError):
            self.registry.invoke(
                ToolContext(UserId("usr_bob"), self.project_id, "agent"),
                ToolCall("cross", "creative.brief.propose", arguments),
            )
        arguments["style_description"] = "clone the voice"
        with self.assertRaises(ToolPolicyError):
            self.registry.invoke(
                self.context,
                ToolCall("unsafe", "creative.brief.propose", arguments),
            )

    def test_locked_line_rewrite_fails_without_mutating_payload(self) -> None:
        brief = build_brief(
            DocumentId("doc_brief"),
            BriefRequest(
                concept="Idea",
                audience="general",
                language="vi",
                genres=("pop",),
                moods=("calm",),
                tempo_bpm=90,
                duration_seconds=90.0,
                vocal="solo",
                lyrics_rights=LyricsRights.USER_ORIGINAL,
            ),
        )
        blueprint = plan_blueprint(DocumentId("doc_blueprint"), brief)
        lyrics = self.registry.invoke(
            self.context,
            ToolCall(
                "lyrics_locked",
                "creative.lyrics.propose",
                {
                    "document_id": "doc_lyrics",
                    "brief": to_json_object(brief),
                    "blueprint": to_json_object(blueprint),
                    "candidates": {"sec_verse_1": ["Giữ nguyên câu này"]},
                    "locked_sections": ["sec_verse_1"],
                },
            ),
        )["lyrics"]
        with self.assertRaises(ToolPolicyError):
            self.registry.invoke(
                self.context,
                ToolCall(
                    "rewrite_locked",
                    "creative.lyrics.rewrite",
                    {
                        "lyrics": lyrics,
                        "replacements": {"lin_verse_1_01": "Đổi câu này"},
                    },
                ),
            )


if __name__ == "__main__":
    unittest.main()
