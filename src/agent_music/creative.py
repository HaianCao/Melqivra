"""CPU-only creative planning, lyrics, safety, and generation contracts."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import pairwise

from agent_music.domain import (
    Blueprint,
    Brief,
    DocumentId,
    DocumentType,
    GenerationPlan,
    JsonObject,
    LyricLine,
    LyricLineId,
    Lyrics,
    ProjectId,
    RevisionId,
    Section,
    SectionId,
    UserId,
    validate_identifier,
)
from agent_music.project_store import SqliteProjectRepository, canonical_json


class CreativePlanningError(ValueError):
    """A proposed creative contract violates a reviewable domain invariant."""


class UnsupportedCapabilityError(CreativePlanningError):
    """The selected backend cannot honor the requested generation contract."""


class IdentitySafetyError(CreativePlanningError):
    """A request asks for identity imitation or unverified voice use."""


class CopyrightSafetyError(CreativePlanningError):
    """Lyrics rights are unknown or the requested transformation is not allowed."""


class LockedContentError(CreativePlanningError):
    """A rewrite attempted to modify a locked line or section."""


class LyricsRights(StrEnum):
    GENERATED_ORIGINAL = "generated_original"
    USER_ORIGINAL = "user_original"
    LICENSED = "licensed"
    PUBLIC_DOMAIN = "public_domain"
    UNKNOWN = "unknown"


class WarningCode(StrEnum):
    LINE_TOO_SHORT = "line_too_short"
    LINE_TOO_LONG = "line_too_long"
    REPEATED_TONE = "repeated_tone"
    RHYME_MISMATCH = "rhyme_mismatch"
    MIXED_LANGUAGE = "mixed_language"
    UNNORMALIZED_UNICODE = "unnormalized_unicode"
    STYLE_IDENTITY_REMOVED = "style_identity_removed"


@dataclass(frozen=True, slots=True)
class CreativeWarning:
    code: WarningCode
    message: str
    line_ids: tuple[LyricLineId, ...] = ()


@dataclass(frozen=True, slots=True)
class BriefRequest:
    concept: str
    audience: str
    language: str
    genres: tuple[str, ...]
    moods: tuple[str, ...]
    tempo_bpm: int | None
    duration_seconds: float
    vocal: str
    references: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    user_lyrics: str | None = None
    lyrics_rights: LyricsRights = LyricsRights.UNKNOWN
    style_description: str = ""
    protected_identities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StyleNormalization:
    normalized: str
    removed_identities: tuple[str, ...]
    warnings: tuple[CreativeWarning, ...]


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    operations: frozenset[str]
    max_duration_seconds: float
    supported_languages: frozenset[str]
    supports_vocals: bool = True
    supports_instrumental: bool = True


@dataclass(frozen=True, slots=True)
class TakeVariation:
    index: int
    seed: int
    label: str
    settings: JsonObject


@dataclass(frozen=True, slots=True)
class CompiledPlan:
    plan: GenerationPlan
    variations: tuple[TakeVariation, ...]
    warnings: tuple[CreativeWarning, ...]


@dataclass(frozen=True, slots=True)
class ApprovedGenerationContract:
    project_id: ProjectId
    brief_revision_id: RevisionId
    blueprint_revision_id: RevisionId
    lyrics_revision_id: RevisionId | None
    plan_revision_id: RevisionId
    checksum: str


_VOICE_IDENTITY = re.compile(
    r"\b(clone|copy|replicate|imitate)\s+(the\s+)?voice\b|"
    r"\b(giả|nhái|sao chép)\s+giọng\b|\bgiọng\s+(của|giống)\b",
    re.IGNORECASE,
)
_STYLE_PREFIX = re.compile(
    r"\b(in the style of|sound exactly like|giống hệt|theo phong cách của)\b",
    re.IGNORECASE,
)
_ENGLISH_MARKERS = frozenset(
    {"baby", "dream", "forever", "love", "night", "tonight", "yeah", "you"}
)
_VIETNAMESE_MARKS = frozenset("ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ")
_TONE_GROUPS: tuple[tuple[str, frozenset[str]], ...] = (
    ("sac", frozenset("áấắéếíóốớúứý")),
    ("huyen", frozenset("àầằèềìòồờùừỳ")),
    ("hoi", frozenset("ảẩẳẻểỉỏổởủửỷ")),
    ("nga", frozenset("ãẫẵẽễĩõỗỡũữỹ")),
    ("nang", frozenset("ạậặẹệịọộợụựỵ")),
)


def build_brief(document_id: DocumentId, request: BriefRequest) -> Brief:
    """Validate user intent and produce a structured, rights-aware brief."""

    validate_identifier(str(document_id))
    concept = _required_text(request.concept, "concept")
    audience = _required_text(request.audience, "audience")
    language = _required_text(request.language, "language").lower()
    if not request.genres or not request.moods:
        raise CreativePlanningError("At least one genre and mood are required")
    if request.tempo_bpm is not None and not 40 <= request.tempo_bpm <= 240:
        raise CreativePlanningError("Tempo must be between 40 and 240 BPM")
    if not 20 <= request.duration_seconds <= 900:
        raise CreativePlanningError("Duration must be between 20 and 900 seconds")
    if request.user_lyrics and request.lyrics_rights is LyricsRights.UNKNOWN:
        raise CopyrightSafetyError("User lyrics require an explicit rights declaration")
    if _VOICE_IDENTITY.search(request.vocal):
        raise IdentitySafetyError("Voice identity imitation is disabled")
    style = normalize_style(
        request.style_description,
        protected_identities=request.protected_identities,
    )
    return Brief(
        id=document_id,
        concept=concept,
        audience=audience,
        language=language,
        genres=tuple(_clean_values(request.genres, "genres")),
        moods=tuple(_clean_values(request.moods, "moods")),
        tempo_bpm=request.tempo_bpm,
        duration_seconds=float(request.duration_seconds),
        vocal=_required_text(request.vocal, "vocal"),
        references=tuple(_clean_values(request.references, "references")),
        exclusions=tuple(_clean_values(request.exclusions, "exclusions")),
        user_lyrics=(
            unicodedata.normalize("NFC", request.user_lyrics.strip())
            if request.user_lyrics
            else None
        ),
        lyrics_rights=request.lyrics_rights.value,
        style_description=style.normalized,
    )


def normalize_style(
    description: str, *, protected_identities: Sequence[str] = ()
) -> StyleNormalization:
    """Remove named-identity imitation while preserving explicit musical traits."""

    normalized = " ".join(description.split())
    if not normalized:
        return StyleNormalization("", (), ())
    if _VOICE_IDENTITY.search(normalized):
        raise IdentitySafetyError("Voice identity imitation is disabled")
    removed: list[str] = []
    for identity in sorted(protected_identities, key=str.casefold):
        if identity.casefold() in normalized.casefold():
            pattern = re.compile(re.escape(identity), re.IGNORECASE)
            normalized = pattern.sub("", normalized)
            removed.append(identity)
    if _STYLE_PREFIX.search(normalized):
        parts = normalized.split(",", 1)
        if len(parts) == 1:
            raise IdentitySafetyError("Named style imitation needs trait-based description")
        normalized = parts[1]
    normalized = " ".join(normalized.strip(" ,-:").split())
    if removed and not normalized:
        raise IdentitySafetyError("No identity-independent style traits remain")
    warnings = (
        (
            CreativeWarning(
                WarningCode.STYLE_IDENTITY_REMOVED,
                "Named identity was removed; only reviewable musical traits remain.",
            ),
        )
        if removed
        else ()
    )
    return StyleNormalization(normalized, tuple(removed), warnings)


def plan_blueprint(document_id: DocumentId, brief: Brief) -> Blueprint:
    """Create a deterministic duration-balanced arrangement from a valid brief."""

    validate_identifier(str(document_id))
    if brief.duration_seconds is None:
        raise CreativePlanningError("Blueprint planning requires a target duration")
    duration = brief.duration_seconds
    layout: tuple[tuple[str, float, float, str, bool], ...]
    if duration <= 75:
        layout = (
            ("intro", 0.10, 0.25, "establish", False),
            ("verse_1", 0.30, 0.50, "setup", False),
            ("chorus", 0.35, 0.85, "hook", True),
            ("outro", 0.25, 0.35, "resolve", False),
        )
    else:
        layout = (
            ("intro", 0.08, 0.20, "establish", False),
            ("verse_1", 0.17, 0.45, "setup", False),
            ("pre_chorus", 0.09, 0.65, "build", False),
            ("chorus_1", 0.18, 0.82, "hook", False),
            ("verse_2", 0.16, 0.55, "develop", False),
            ("bridge", 0.12, 0.72, "turn", False),
            ("final_chorus", 0.15, 1.00, "climax", True),
            ("outro", 0.05, 0.30, "resolve", False),
        )
    instrumentation = _instrumentation(brief.genres)
    sections: list[Section] = []
    allocated = 0.0
    for index, (name, ratio, energy, role, climax) in enumerate(layout):
        section_duration = (
            round(duration - allocated, 3)
            if index == len(layout) - 1
            else round(duration * ratio, 3)
        )
        allocated += section_duration
        sections.append(
            Section(
                id=SectionId(f"sec_{name}"),
                name=name.replace("_", " ").title(),
                duration_seconds=section_duration,
                energy=energy,
                narrative_role=role,
                instrumentation=instrumentation,
                delivery=brief.vocal,
                transition="cut" if name == "outro" else "build" if role == "build" else "flow",
                is_climax=climax,
            )
        )
    blueprint = Blueprint(document_id, tuple(sections))
    validate_blueprint(blueprint, target_duration=duration)
    return blueprint


def validate_blueprint(
    blueprint: Blueprint, *, target_duration: float, tolerance_seconds: float = 0.05
) -> None:
    """Validate section identity, duration, energy, and a single explicit climax."""

    if not blueprint.sections:
        raise CreativePlanningError("Blueprint requires at least one section")
    identifiers = [section.id for section in blueprint.sections]
    if len(set(identifiers)) != len(identifiers):
        raise CreativePlanningError("Section IDs must be unique")
    if any(section.duration_seconds <= 0 for section in blueprint.sections):
        raise CreativePlanningError("Section durations must be positive")
    if any(not 0 <= section.energy <= 1 for section in blueprint.sections):
        raise CreativePlanningError("Section energy must be between zero and one")
    total = sum(section.duration_seconds for section in blueprint.sections)
    if abs(total - target_duration) > tolerance_seconds:
        raise CreativePlanningError("Blueprint duration does not match the brief")
    if sum(section.is_climax for section in blueprint.sections) != 1:
        raise CreativePlanningError("Blueprint requires exactly one climax")


def revise_blueprint(
    blueprint: Blueprint,
    replacements: Mapping[SectionId, Section],
    *,
    locked_sections: frozenset[SectionId] = frozenset(),
) -> Blueprint:
    """Replace selected sections while preserving IDs and all locked sections."""

    known = {section.id for section in blueprint.sections}
    if set(replacements) - known:
        raise CreativePlanningError("Blueprint revision references an unknown section")
    revised: list[Section] = []
    for section in blueprint.sections:
        replacement = replacements.get(section.id)
        if replacement is None:
            revised.append(section)
            continue
        if replacement.id != section.id:
            raise CreativePlanningError("Blueprint revisions must preserve section IDs")
        if section.id in locked_sections and replacement != section:
            raise LockedContentError(f"Locked section cannot change: {section.id}")
        revised.append(replacement)
    result = replace(blueprint, sections=tuple(revised))
    validate_blueprint(
        result,
        target_duration=sum(section.duration_seconds for section in blueprint.sections),
    )
    return result


def create_lyrics(
    document_id: DocumentId,
    brief: Brief,
    blueprint: Blueprint,
    candidates: Mapping[SectionId, Sequence[str]],
    *,
    locked_sections: frozenset[SectionId] = frozenset(),
) -> Lyrics:
    """Validate planner-proposed lines and assign stable section/line identifiers."""

    validate_identifier(str(document_id))
    section_ids = {section.id for section in blueprint.sections}
    unknown = set(candidates) - section_ids
    if unknown:
        raise CreativePlanningError("Lyrics reference an unknown section")
    lines: list[LyricLine] = []
    for section in blueprint.sections:
        for index, text in enumerate(candidates.get(section.id, ()), start=1):
            normalized = unicodedata.normalize("NFC", _required_text(text, "lyric line"))
            line_id = LyricLineId(f"lin_{str(section.id)[4:]}_{index:02d}")
            validate_identifier(str(line_id))
            lines.append(
                LyricLine(
                    id=line_id,
                    section_id=section.id,
                    text=normalized,
                    locked=section.id in locked_sections,
                )
            )
    if brief.vocal.casefold() != "instrumental" and not lines:
        raise CreativePlanningError("Vocal briefs require at least one lyric line")
    source_rights = (
        brief.lyrics_rights
        if brief.user_lyrics is not None
        else LyricsRights.GENERATED_ORIGINAL.value
    )
    return Lyrics(document_id, brief.language, tuple(lines), source_rights)


def create_lyrics_from_user_text(
    document_id: DocumentId,
    brief: Brief,
    blueprint: Blueprint,
    *,
    locked_sections: frozenset[SectionId] = frozenset(),
) -> Lyrics:
    """Split authorized user text across vocal sections without rewriting it."""

    if not brief.user_lyrics:
        raise CreativePlanningError("Brief has no user lyrics")
    if brief.lyrics_rights == LyricsRights.UNKNOWN.value:
        raise CopyrightSafetyError("User lyrics rights are unknown")
    sections = tuple(
        section.id
        for section in blueprint.sections
        if section.narrative_role not in {"establish", "resolve"}
    )
    if not sections:
        raise CreativePlanningError("Blueprint has no vocal sections")
    candidates: defaultdict[SectionId, list[str]] = defaultdict(list)
    source_lines = [line.strip() for line in brief.user_lyrics.splitlines() if line.strip()]
    if not source_lines:
        raise CreativePlanningError("User lyrics contain no non-empty lines")
    for index, text in enumerate(source_lines):
        candidates[sections[index % len(sections)]].append(text)
    return create_lyrics(
        document_id,
        brief,
        blueprint,
        candidates,
        locked_sections=locked_sections,
    )


def continue_lyrics(lyrics: Lyrics, additions: Mapping[SectionId, Sequence[str]]) -> Lyrics:
    """Append new lines while preserving every existing stable line ID."""

    _ensure_lyrics_rights(lyrics)
    existing_counts: defaultdict[SectionId, int] = defaultdict(int)
    for line in lyrics.lines:
        existing_counts[line.section_id] += 1
    appended = list(lyrics.lines)
    for section_id in sorted(additions, key=str):
        for text in additions[section_id]:
            existing_counts[section_id] += 1
            line_id = LyricLineId(f"lin_{str(section_id)[4:]}_{existing_counts[section_id]:02d}")
            appended.append(
                LyricLine(
                    line_id,
                    section_id,
                    unicodedata.normalize("NFC", _required_text(text, "lyric line")),
                )
            )
    return replace(lyrics, lines=tuple(appended))


def restructure_lyrics(lyrics: Lyrics, section_order: Sequence[SectionId]) -> Lyrics:
    """Reorder sections without changing line IDs, text, or lock state."""

    _ensure_lyrics_rights(lyrics)
    existing_sections = {line.section_id for line in lyrics.lines}
    if set(section_order) != existing_sections or len(section_order) != len(existing_sections):
        raise CreativePlanningError("Section order must contain each lyric section exactly once")
    rank = {section_id: index for index, section_id in enumerate(section_order)}
    ordered = tuple(sorted(lyrics.lines, key=lambda line: rank[line.section_id]))
    return replace(lyrics, lines=ordered)


def constrained_rewrite(lyrics: Lyrics, replacements: Mapping[LyricLineId, str]) -> Lyrics:
    """Apply line replacements while making locked content invariant."""

    _ensure_lyrics_rights(lyrics)
    known = {line.id for line in lyrics.lines}
    if set(replacements) - known:
        raise CreativePlanningError("Rewrite references an unknown lyric line")
    rewritten: list[LyricLine] = []
    for line in lyrics.lines:
        replacement = replacements.get(line.id)
        if replacement is None:
            rewritten.append(line)
            continue
        normalized = unicodedata.normalize("NFC", _required_text(replacement, "replacement"))
        if line.locked and normalized != line.text:
            raise LockedContentError(f"Locked line cannot change: {line.id}")
        rewritten.append(replace(line, text=normalized))
    return replace(lyrics, lines=tuple(rewritten))


def critique_lyrics(lyrics: Lyrics) -> tuple[CreativeWarning, ...]:
    """Return deterministic Vietnamese singability, tone, rhyme, and Unicode warnings."""

    warnings: list[CreativeWarning] = []
    section_lines: defaultdict[SectionId, list[LyricLine]] = defaultdict(list)
    for line in lyrics.lines:
        section_lines[line.section_id].append(line)
        if unicodedata.normalize("NFC", line.text) != line.text:
            warnings.append(
                CreativeWarning(
                    WarningCode.UNNORMALIZED_UNICODE,
                    "Line is not NFC-normalized.",
                    (line.id,),
                )
            )
        syllables = _tokens(line.text)
        if len(syllables) < 3:
            warnings.append(
                CreativeWarning(
                    WarningCode.LINE_TOO_SHORT,
                    "Vietnamese lyric line has fewer than three syllables.",
                    (line.id,),
                )
            )
        if len(syllables) > 12 or len(line.text) > 72:
            warnings.append(
                CreativeWarning(
                    WarningCode.LINE_TOO_LONG,
                    "Line may need splitting for singable phrasing.",
                    (line.id,),
                )
            )
        tones = [_tone(token) for token in syllables]
        if any(
            tones[index : index + 4].count(tones[index]) == 4
            for index in range(max(0, len(tones) - 3))
        ):
            warnings.append(
                CreativeWarning(
                    WarningCode.REPEATED_TONE,
                    "Four adjacent syllables share one tone contour.",
                    (line.id,),
                )
            )
        lowered = {token.casefold().strip(".,!?;:'\"") for token in syllables}
        has_vietnamese = any(character in _VIETNAMESE_MARKS for character in line.text.casefold())
        if has_vietnamese and lowered & _ENGLISH_MARKERS:
            warnings.append(
                CreativeWarning(
                    WarningCode.MIXED_LANGUAGE,
                    "Mixed Vietnamese/English line needs pronunciation review.",
                    (line.id,),
                )
            )
    for lines in section_lines.values():
        for first, second in pairwise(lines):
            if _rhyme_key(first.text) != _rhyme_key(second.text):
                warnings.append(
                    CreativeWarning(
                        WarningCode.RHYME_MISMATCH,
                        "Adjacent line endings do not share a simple rhyme nucleus.",
                        (first.id, second.id),
                    )
                )
    return tuple(warnings)


def compile_generation_plan(
    document_id: DocumentId,
    *,
    brief_revision_id: RevisionId,
    blueprint_revision_id: RevisionId,
    lyrics_revision_id: RevisionId | None,
    brief: Brief,
    blueprint: Blueprint,
    operation: str,
    take_count: int,
    base_seed: int,
    capabilities: BackendCapabilities,
) -> CompiledPlan:
    """Compile an explicit 2-4 take strategy without hidden fallback."""

    validate_identifier(str(document_id))
    if operation not in capabilities.operations:
        raise UnsupportedCapabilityError(f"Unsupported operation: {operation}")
    if not 2 <= take_count <= 4:
        raise CreativePlanningError("Take count must be between two and four")
    duration = brief.duration_seconds
    if duration is None or duration > capabilities.max_duration_seconds:
        raise UnsupportedCapabilityError("Requested duration exceeds backend capability")
    instrumental = brief.vocal.casefold() == "instrumental"
    if instrumental and not capabilities.supports_instrumental:
        raise UnsupportedCapabilityError("Backend does not support instrumental generation")
    if not instrumental and brief.language not in capabilities.supported_languages:
        raise UnsupportedCapabilityError("Requested language is unsupported")
    if not instrumental and not capabilities.supports_vocals:
        raise UnsupportedCapabilityError("Backend does not support vocals")
    validate_blueprint(blueprint, target_duration=duration)
    variations = tuple(
        TakeVariation(
            index=index + 1,
            seed=base_seed + index * 7919,
            label=("faithful", "balanced", "expressive", "alternate")[index],
            settings={
                "variation": ("faithful", "balanced", "expressive", "alternate")[index],
                "guidance_scale": (1.0, 1.05, 0.95, 1.1)[index],
            },
        )
        for index in range(take_count)
    )
    requested = [operation]
    if not instrumental:
        requested.append(f"language:{brief.language}")
        requested.append("vocals")
    settings: JsonObject = {
        "duration_seconds": duration,
        "tempo_bpm": brief.tempo_bpm,
        "section_ids": [str(section.id) for section in blueprint.sections],
        "variation_seeds": [variation.seed for variation in variations],
        "variation_labels": [variation.label for variation in variations],
    }
    plan = GenerationPlan(
        id=document_id,
        brief_revision_id=brief_revision_id,
        blueprint_revision_id=blueprint_revision_id,
        lyrics_revision_id=lyrics_revision_id,
        operation=operation,
        requested_capabilities=tuple(requested),
        take_count=take_count,
        settings=settings,
    )
    return CompiledPlan(plan, variations, ())


def render_approval_view(
    brief: Brief,
    blueprint: Blueprint,
    lyrics: Lyrics | None,
    compiled: CompiledPlan,
    *,
    warnings: Sequence[CreativeWarning] = (),
) -> str:
    """Render the complete generation contract for human review."""

    sections = " → ".join(
        f"{section.name} ({section.duration_seconds:g}s{' climax' if section.is_climax else ''})"
        for section in blueprint.sections
    )
    lyric_summary = "instrumental"
    if lyrics is not None:
        locked = sum(line.locked for line in lyrics.lines)
        lyric_summary = f"{len(lyrics.lines)} lines, {locked} locked"
    variation_summary = ", ".join(
        f"{variation.label}: seed {variation.seed}" for variation in compiled.variations
    )
    warning_summary = (
        "none" if not warnings else "; ".join(warning.code.value for warning in warnings)
    )
    return "\n".join(
        (
            f"Concept: {brief.concept}",
            f"Audience/language: {brief.audience} / {brief.language}",
            "Style: "
            f"{', '.join(brief.genres)}; {', '.join(brief.moods)}; "
            f"{brief.style_description}",
            "Tempo/duration/vocal: "
            f"{brief.tempo_bpm or 'open'} BPM / {brief.duration_seconds:g}s / "
            f"{brief.vocal}",
            f"Sections: {sections}",
            f"Lyrics: {lyric_summary}",
            f"Lyrics rights: {brief.lyrics_rights}",
            f"References: {', '.join(brief.references) or 'none'}",
            f"Exclusions: {', '.join(brief.exclusions) or 'none'}",
            f"Generation: {compiled.plan.operation}; {compiled.plan.take_count} takes",
            f"Variations: {variation_summary}",
            f"Warnings: {warning_summary}",
        )
    )


def pin_accepted_contract(
    repository: SqliteProjectRepository,
    *,
    owner_id: UserId,
    project_id: ProjectId,
) -> ApprovedGenerationContract:
    """Resolve and checksum one fully accepted immutable generation contract."""

    index = repository.get_index(owner_id=owner_id, project_id=project_id)
    accepted = {
        selection.document_type: selection.accepted_revision_id
        for selection in index.selections
        if selection.accepted_revision_id is not None
    }
    for required in (DocumentType.BRIEF, DocumentType.BLUEPRINT, DocumentType.GENERATION_PLAN):
        if required not in accepted:
            raise CreativePlanningError(f"Missing accepted {required.value} revision")
    brief_id = accepted[DocumentType.BRIEF]
    blueprint_id = accepted[DocumentType.BLUEPRINT]
    plan_id = accepted[DocumentType.GENERATION_PLAN]
    lyrics_id = accepted.get(DocumentType.LYRICS)
    plan_revision = repository.get_revision(
        owner_id=owner_id, project_id=project_id, revision_id=plan_id
    )
    payload = repository.revision_payload(plan_revision)
    if payload.get("brief_revision_id") != str(brief_id):
        raise CreativePlanningError("Generation plan does not pin the accepted brief")
    if payload.get("blueprint_revision_id") != str(blueprint_id):
        raise CreativePlanningError("Generation plan does not pin the accepted blueprint")
    expected_lyrics = str(lyrics_id) if lyrics_id else None
    if payload.get("lyrics_revision_id") != expected_lyrics:
        raise CreativePlanningError("Generation plan does not pin the accepted lyrics")
    revision_ids = (brief_id, blueprint_id, *(tuple([lyrics_id]) if lyrics_id else ()), plan_id)
    revisions = [
        repository.get_revision(owner_id=owner_id, project_id=project_id, revision_id=item)
        for item in revision_ids
    ]
    checksum_payload: JsonObject = {
        "project_id": str(project_id),
        "revisions": [
            {"id": str(revision.id), "checksum": revision.checksum} for revision in revisions
        ],
    }
    checksum = hashlib.sha256(canonical_json(checksum_payload).encode("utf-8")).hexdigest()
    return ApprovedGenerationContract(
        project_id,
        brief_id,
        blueprint_id,
        lyrics_id,
        plan_id,
        checksum,
    )


def brief_from_json(payload: JsonObject) -> Brief:
    """Decode and validate a canonical brief tool payload."""

    document_id = DocumentId(_json_string(payload, "id"))
    validate_identifier(str(document_id))
    rights = LyricsRights(_json_string(payload, "lyrics_rights"))
    return build_brief(
        document_id,
        BriefRequest(
            concept=_json_string(payload, "concept"),
            audience=_json_string(payload, "audience"),
            language=_json_string(payload, "language"),
            genres=_json_strings(payload, "genres"),
            moods=_json_strings(payload, "moods"),
            tempo_bpm=_json_optional_int(payload, "tempo_bpm"),
            duration_seconds=_json_number(payload, "duration_seconds"),
            vocal=_json_string(payload, "vocal"),
            references=_json_strings(payload, "references"),
            exclusions=_json_strings(payload, "exclusions"),
            user_lyrics=_json_optional_string(payload, "user_lyrics"),
            lyrics_rights=rights,
            style_description=_json_optional_string(payload, "style_description") or "",
        ),
    )


def blueprint_from_json(payload: JsonObject) -> Blueprint:
    """Decode a canonical blueprint and validate all section fields."""

    document_id = DocumentId(_json_string(payload, "id"))
    validate_identifier(str(document_id))
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list):
        raise CreativePlanningError("sections must be a list")
    sections: list[Section] = []
    for raw in raw_sections:
        if not isinstance(raw, dict):
            raise CreativePlanningError("section must be an object")
        section_id = SectionId(_json_string(raw, "id"))
        validate_identifier(str(section_id))
        climax = raw.get("is_climax", False)
        if not isinstance(climax, bool):
            raise CreativePlanningError("is_climax must be boolean")
        sections.append(
            Section(
                id=section_id,
                name=_json_string(raw, "name"),
                duration_seconds=_json_number(raw, "duration_seconds"),
                energy=_json_number(raw, "energy"),
                narrative_role=_json_string(raw, "narrative_role"),
                instrumentation=_json_strings(raw, "instrumentation"),
                delivery=_json_string(raw, "delivery"),
                transition=_json_string(raw, "transition"),
                is_climax=climax,
            )
        )
    blueprint = Blueprint(document_id, tuple(sections))
    validate_blueprint(
        blueprint,
        target_duration=sum(section.duration_seconds for section in sections),
    )
    return blueprint


def lyrics_from_json(payload: JsonObject) -> Lyrics:
    """Decode canonical lyrics while preserving stable IDs and locks."""

    document_id = DocumentId(_json_string(payload, "id"))
    validate_identifier(str(document_id))
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list):
        raise CreativePlanningError("lines must be a list")
    lines: list[LyricLine] = []
    seen: set[LyricLineId] = set()
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise CreativePlanningError("lyric line must be an object")
        line_id = LyricLineId(_json_string(raw, "id"))
        section_id = SectionId(_json_string(raw, "section_id"))
        validate_identifier(str(line_id))
        validate_identifier(str(section_id))
        if line_id in seen:
            raise CreativePlanningError("Lyric line IDs must be unique")
        seen.add(line_id)
        locked = raw.get("locked", False)
        if not isinstance(locked, bool):
            raise CreativePlanningError("locked must be boolean")
        lines.append(
            LyricLine(
                line_id,
                section_id,
                _json_string(raw, "text"),
                locked,
            )
        )
    return Lyrics(
        document_id,
        _json_string(payload, "language"),
        tuple(lines),
        _json_string(payload, "source_rights"),
    )


def _required_text(value: str, name: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise CreativePlanningError(f"{name} must not be empty")
    return cleaned


def _ensure_lyrics_rights(lyrics: Lyrics) -> None:
    try:
        rights = LyricsRights(lyrics.source_rights)
    except ValueError as error:
        raise CopyrightSafetyError("Lyrics rights declaration is invalid") from error
    if rights is LyricsRights.UNKNOWN:
        raise CopyrightSafetyError("Lyrics transformation requires declared rights")


def _json_string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CreativePlanningError(f"{name} must be a non-empty string")
    return value


def _json_optional_string(payload: Mapping[str, object], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CreativePlanningError(f"{name} must be a string or null")
    return value


def _json_strings(payload: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = payload.get(name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CreativePlanningError(f"{name} must be a string list")
    return tuple(value)


def _json_number(payload: Mapping[str, object], name: str) -> float:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CreativePlanningError(f"{name} must be numeric")
    return float(value)


def _json_optional_int(payload: Mapping[str, object], name: str) -> int | None:
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CreativePlanningError(f"{name} must be an integer or null")
    return value


def _clean_values(values: Sequence[str], name: str) -> list[str]:
    cleaned = [_required_text(value, name) for value in values]
    return list(dict.fromkeys(cleaned))


def _instrumentation(genres: Sequence[str]) -> tuple[str, ...]:
    joined = " ".join(genres).casefold()
    if any(value in joined for value in ("electronic", "edm", "synth")):
        return ("drums", "synth bass", "pads", "lead synth")
    if any(value in joined for value in ("rock", "punk", "metal")):
        return ("drums", "bass", "electric guitar", "rhythm guitar")
    if any(value in joined for value in ("ballad", "acoustic", "folk")):
        return ("piano", "acoustic guitar", "bass", "strings")
    return ("drums", "bass", "keys", "texture")


def _tokens(text: str) -> list[str]:
    return [token for token in re.split(r"\s+", text.strip()) if token]


def _tone(token: str) -> str:
    lowered = unicodedata.normalize("NFC", token.casefold())
    for name, characters in _TONE_GROUPS:
        if any(character in characters for character in lowered):
            return name
    return "ngang"


def _rhyme_key(text: str) -> str:
    tokens = _tokens(text)
    if not tokens:
        return ""
    final = unicodedata.normalize("NFD", tokens[-1].casefold())
    base = "".join(character for character in final if not unicodedata.combining(character))
    base = re.sub(r"[^a-z]", "", base)
    match = re.search(r"[aeiouy]+[^aeiouy]*$", base)
    return match.group(0) if match else base[-2:]
