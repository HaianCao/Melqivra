"""Run the CPU-only S04 Vietnamese creative-contract demonstration."""

from __future__ import annotations

import json

from agent_music.creative import (
    BackendCapabilities,
    BriefRequest,
    LyricsRights,
    build_brief,
    compile_generation_plan,
    constrained_rewrite,
    create_lyrics,
    critique_lyrics,
    plan_blueprint,
    render_approval_view,
)
from agent_music.domain import DocumentId, RevisionId, SectionId


def main() -> None:
    brief = build_brief(
        DocumentId("doc_demo_brief"),
        BriefRequest(
            concept="Tìm lại bình yên sau mùa mưa",
            audience="young adults",
            language="vi",
            genres=("acoustic ballad",),
            moods=("hopeful", "intimate"),
            tempo_bpm=82,
            duration_seconds=180.0,
            vocal="warm alto",
            lyrics_rights=LyricsRights.GENERATED_ORIGINAL,
            style_description="organic acoustic pop, intimate dynamics",
        ),
    )
    blueprint = plan_blueprint(DocumentId("doc_demo_blueprint"), brief)
    lyrics = create_lyrics(
        DocumentId("doc_demo_lyrics"),
        brief,
        blueprint,
        {
            SectionId("sec_verse_1"): (
                "Mưa nghiêng qua hiên nhà",
                "Ta nghe tim mình xa",
            ),
            SectionId("sec_chorus_1"): (
                "Tìm bình yên trong đêm",
                "Giữ bàn tay qua ngày",
            ),
        },
        locked_sections=frozenset({SectionId("sec_verse_1")}),
    )
    chorus_line = next(line for line in lyrics.lines if not line.locked)
    revised = constrained_rewrite(
        lyrics,
        {chorus_line.id: "Tìm bình yên trong tim"},
    )
    warnings = critique_lyrics(revised)
    compiled = compile_generation_plan(
        DocumentId("doc_demo_plan"),
        brief_revision_id=RevisionId("rev_demo_brief"),
        blueprint_revision_id=RevisionId("rev_demo_blueprint"),
        lyrics_revision_id=RevisionId("rev_demo_lyrics"),
        brief=brief,
        blueprint=blueprint,
        operation="text_to_music",
        take_count=3,
        base_seed=20261112,
        capabilities=BackendCapabilities(
            frozenset({"text_to_music"}),
            240.0,
            frozenset({"vi", "en"}),
        ),
    )
    approval = render_approval_view(
        brief,
        blueprint,
        revised,
        compiled,
        warnings=warnings,
    )
    print(
        json.dumps(
            {
                "approval_characters": len(approval),
                "locked_lines": sum(line.locked for line in revised.lines),
                "lyric_lines": len(revised.lines),
                "section_count": len(blueprint.sections),
                "status": "ok",
                "take_count": compiled.plan.take_count,
                "variation_seeds": [item.seed for item in compiled.variations],
                "warning_count": len(warnings),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
