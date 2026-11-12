"""S04 creative brief, blueprint, lyrics, safety, and planning tests."""

from __future__ import annotations

import json
import unicodedata
import unittest
from dataclasses import replace
from pathlib import Path

from agent_music.creative import (
    BackendCapabilities,
    BriefRequest,
    CopyrightSafetyError,
    CreativePlanningError,
    IdentitySafetyError,
    LockedContentError,
    LyricsRights,
    UnsupportedCapabilityError,
    WarningCode,
    build_brief,
    compile_generation_plan,
    constrained_rewrite,
    continue_lyrics,
    create_lyrics,
    create_lyrics_from_user_text,
    critique_lyrics,
    normalize_style,
    plan_blueprint,
    render_approval_view,
    restructure_lyrics,
    revise_blueprint,
    validate_blueprint,
)
from agent_music.domain import DocumentId, RevisionId, SectionId


def _request(**overrides: object) -> BriefRequest:
    values: dict[str, object] = {
        "concept": "Tìm lại bình yên sau một mùa mưa",
        "audience": "young adults",
        "language": "vi",
        "genres": ("acoustic ballad",),
        "moods": ("hopeful", "intimate"),
        "tempo_bpm": 82,
        "duration_seconds": 180.0,
        "vocal": "warm alto",
        "style_description": "organic acoustic pop, intimate dynamics",
    }
    values.update(overrides)
    return BriefRequest(**values)  # type: ignore[arg-type]


class CreativePlanningTests(unittest.TestCase):
    def test_golden_idea_to_blueprint_and_lock_safe_climax_revision(self) -> None:
        brief = build_brief(DocumentId("doc_brief"), _request())
        blueprint = plan_blueprint(DocumentId("doc_blueprint"), brief)
        golden = json.loads(
            (Path(__file__).parent / "fixtures" / "s04_golden_blueprint.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(golden["section_ids"], [str(item.id) for item in blueprint.sections])
        self.assertEqual(
            golden["durations"], [item.duration_seconds for item in blueprint.sections]
        )
        self.assertEqual(
            golden["climax"], str(next(item.id for item in blueprint.sections if item.is_climax))
        )
        self.assertEqual(golden["instrumentation"], list(blueprint.sections[0].instrumentation))

        verse = next(item for item in blueprint.sections if item.id == SectionId("sec_verse_1"))
        bridge = next(item for item in blueprint.sections if item.id == SectionId("sec_bridge"))
        final = next(
            item for item in blueprint.sections if item.id == SectionId("sec_final_chorus")
        )
        revised = revise_blueprint(
            blueprint,
            {
                bridge.id: replace(bridge, energy=1.0, is_climax=True),
                final.id: replace(final, energy=0.82, is_climax=False),
            },
            locked_sections=frozenset({verse.id}),
        )
        self.assertEqual(verse, next(item for item in revised.sections if item.id == verse.id))
        self.assertEqual(bridge.id, next(item.id for item in revised.sections if item.is_climax))
        with self.assertRaises(LockedContentError):
            revise_blueprint(
                blueprint,
                {verse.id: replace(verse, energy=0.9)},
                locked_sections=frozenset({verse.id}),
            )

    def test_generate_continue_restructure_critique_and_constrained_rewrite(self) -> None:
        brief = build_brief(DocumentId("doc_brief"), _request())
        blueprint = plan_blueprint(DocumentId("doc_blueprint"), brief)
        lyrics = create_lyrics(
            DocumentId("doc_lyrics"),
            brief,
            blueprint,
            {
                SectionId("sec_verse_1"): (
                    "Mưa nghiêng qua hiên nhà",
                    "Em nghe tim mình xa",
                ),
                SectionId("sec_chorus_1"): (
                    "Tìm bình yên trong đêm tonight",
                    "Giữ bàn tay qua ngày",
                ),
            },
            locked_sections=frozenset({SectionId("sec_verse_1")}),
        )
        original_ids = tuple(line.id for line in lyrics.lines)
        continued = continue_lyrics(
            lyrics,
            {SectionId("sec_chorus_1"): ("Ngày mai nắng lên rồi",)},
        )
        self.assertEqual(original_ids, tuple(line.id for line in continued.lines[:4]))
        restructured = restructure_lyrics(
            continued,
            (SectionId("sec_chorus_1"), SectionId("sec_verse_1")),
        )
        self.assertEqual(SectionId("sec_chorus_1"), restructured.lines[0].section_id)
        unlocked = next(line for line in lyrics.lines if not line.locked)
        rewritten = constrained_rewrite(lyrics, {unlocked.id: "Ta giữ nhau qua ngày"})
        self.assertEqual(
            "Ta giữ nhau qua ngày",
            next(line.text for line in rewritten.lines if line.id == unlocked.id),
        )
        locked = next(line for line in lyrics.lines if line.locked)
        with self.assertRaises(LockedContentError):
            constrained_rewrite(lyrics, {locked.id: "Không được đổi"})
        with self.assertRaises(CopyrightSafetyError):
            constrained_rewrite(
                replace(lyrics, source_rights=LyricsRights.UNKNOWN.value),
                {unlocked.id: "Không có quyền"},
            )
        warning_codes = {warning.code for warning in critique_lyrics(lyrics)}
        self.assertIn(WarningCode.MIXED_LANGUAGE, warning_codes)
        self.assertIn(WarningCode.RHYME_MISMATCH, warning_codes)

    def test_user_lyrics_require_rights_and_preserve_unicode(self) -> None:
        with self.assertRaises(CopyrightSafetyError):
            build_brief(
                DocumentId("doc_brief"),
                _request(user_lyrics="Lời của tôi", lyrics_rights=LyricsRights.UNKNOWN),
            )
        source = "Nắng lên bên thềm\nMình đi qua đêm"
        brief = build_brief(
            DocumentId("doc_brief"),
            _request(user_lyrics=source, lyrics_rights=LyricsRights.USER_ORIGINAL),
        )
        blueprint = plan_blueprint(DocumentId("doc_blueprint"), brief)
        lyrics = create_lyrics_from_user_text(
            DocumentId("doc_lyrics"),
            brief,
            blueprint,
            locked_sections=frozenset({SectionId("sec_verse_1")}),
        )
        self.assertEqual(2, len(lyrics.lines))
        self.assertTrue(all(unicodedata.is_normalized("NFC", line.text) for line in lyrics.lines))
        self.assertTrue(lyrics.lines[0].locked)

    def test_style_identity_and_named_imitation_are_safe(self) -> None:
        normalized = normalize_style(
            "theo phong cách của Singer Name, acoustic pop with brushed drums",
            protected_identities=("Singer Name",),
        )
        self.assertNotIn("Singer Name", normalized.normalized)
        self.assertIn("acoustic pop", normalized.normalized)
        self.assertEqual(WarningCode.STYLE_IDENTITY_REMOVED, normalized.warnings[0].code)
        for request in ("clone the voice", "giọng của ca sĩ X"):
            with self.subTest(request=request), self.assertRaises(IdentitySafetyError):
                normalize_style(request)
        with self.assertRaises(IdentitySafetyError):
            normalize_style("in the style of Artist")

    def test_duration_and_capability_validation_are_explicit(self) -> None:
        brief = build_brief(DocumentId("doc_brief"), _request())
        blueprint = plan_blueprint(DocumentId("doc_blueprint"), brief)
        broken = replace(
            blueprint,
            sections=(
                replace(blueprint.sections[0], duration_seconds=1.0),
                *blueprint.sections[1:],
            ),
        )
        with self.assertRaisesRegex(CreativePlanningError, "duration"):
            validate_blueprint(broken, target_duration=180.0)
        capabilities = BackendCapabilities(
            frozenset({"text_to_music"}),
            240.0,
            frozenset({"vi", "en"}),
        )
        with self.assertRaises(UnsupportedCapabilityError):
            compile_generation_plan(
                DocumentId("doc_plan"),
                brief_revision_id=RevisionId("rev_brief"),
                blueprint_revision_id=RevisionId("rev_blueprint"),
                lyrics_revision_id=RevisionId("rev_lyrics"),
                brief=brief,
                blueprint=blueprint,
                operation="repaint",
                take_count=2,
                base_seed=42,
                capabilities=capabilities,
            )
        compiled = compile_generation_plan(
            DocumentId("doc_plan"),
            brief_revision_id=RevisionId("rev_brief"),
            blueprint_revision_id=RevisionId("rev_blueprint"),
            lyrics_revision_id=RevisionId("rev_lyrics"),
            brief=brief,
            blueprint=blueprint,
            operation="text_to_music",
            take_count=4,
            base_seed=42,
            capabilities=capabilities,
        )
        self.assertEqual((42, 7961, 15880, 23799), tuple(item.seed for item in compiled.variations))
        approval = render_approval_view(brief, blueprint, None, compiled)
        self.assertIn("4 takes", approval)
        self.assertIn("Final Chorus", approval)


if __name__ == "__main__":
    unittest.main()
