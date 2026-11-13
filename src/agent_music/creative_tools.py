"""Authenticated S04 creative capabilities exposed to the planner registry."""

from __future__ import annotations

from collections.abc import Mapping

from agent_music.agent_harness import ToolContext, ToolDefinition, ToolHandler, ToolRegistry
from agent_music.creative import (
    BackendCapabilities,
    BriefRequest,
    CompiledPlan,
    LyricsRights,
    blueprint_from_json,
    brief_from_json,
    build_brief,
    compile_generation_plan,
    constrained_rewrite,
    continue_lyrics,
    create_lyrics,
    critique_lyrics,
    lyrics_from_json,
    plan_blueprint,
    render_approval_view,
    restructure_lyrics,
)
from agent_music.domain import (
    DocumentId,
    JsonObject,
    JsonValue,
    LyricLineId,
    RevisionId,
    SectionId,
    to_json_object,
)
from agent_music.project_store import ProjectRepository


class CreativeToolService:
    """Register pure, validated creative proposals behind project authorization."""

    def __init__(self, repository: ProjectRepository) -> None:
        self._repository = repository

    def register(self, registry: ToolRegistry) -> None:
        document: JsonObject = {"type": "object"}
        string_list: JsonObject = {"type": "array", "items": {"type": "string"}}
        self._register(
            registry,
            "creative.brief.propose",
            "Validate a structured creative brief.",
            {
                "document_id": {"type": "string"},
                "concept": {"type": "string"},
                "audience": {"type": "string"},
                "language": {"type": "string"},
                "genres": string_list,
                "moods": string_list,
                "tempo_bpm": {"type": ["integer", "null"]},
                "duration_seconds": {"type": "number"},
                "vocal": {"type": "string"},
                "references": string_list,
                "exclusions": string_list,
                "user_lyrics": {"type": ["string", "null"]},
                "lyrics_rights": {"type": "string"},
                "style_description": {"type": "string"},
                "protected_identities": string_list,
            },
            (
                "document_id",
                "concept",
                "audience",
                "language",
                "genres",
                "moods",
                "duration_seconds",
                "vocal",
            ),
            self._brief,
        )
        self._register(
            registry,
            "creative.blueprint.propose",
            "Plan a duration-balanced blueprint.",
            {"document_id": {"type": "string"}, "brief": document},
            ("document_id", "brief"),
            self._blueprint,
        )
        self._register(
            registry,
            "creative.lyrics.propose",
            "Structure proposed lyrics with stable IDs.",
            {
                "document_id": {"type": "string"},
                "brief": document,
                "blueprint": document,
                "candidates": document,
                "locked_sections": string_list,
            },
            ("document_id", "brief", "blueprint", "candidates"),
            self._lyrics,
        )
        for name, description, payload_name, handler in (
            (
                "creative.lyrics.continue",
                "Append lines while preserving stable IDs.",
                "additions",
                self._continue,
            ),
            (
                "creative.lyrics.rewrite",
                "Rewrite only unlocked lyric lines.",
                "replacements",
                self._rewrite,
            ),
        ):
            self._register(
                registry,
                name,
                description,
                {"lyrics": document, payload_name: document},
                ("lyrics", payload_name),
                handler,
            )
        self._register(
            registry,
            "creative.lyrics.restructure",
            "Reorder lyric sections without mutation.",
            {"lyrics": document, "section_order": string_list},
            ("lyrics", "section_order"),
            self._restructure,
        )
        self._register(
            registry,
            "creative.lyrics.critique",
            "Analyze lyric singability and language warnings.",
            {"lyrics": document},
            ("lyrics",),
            self._critique,
        )
        compile_properties: JsonObject = {
            "document_id": {"type": "string"},
            "brief_revision_id": {"type": "string"},
            "blueprint_revision_id": {"type": "string"},
            "lyrics_revision_id": {"type": ["string", "null"]},
            "brief": document,
            "blueprint": document,
            "lyrics": {"type": ["object", "null"]},
            "operation": {"type": "string"},
            "take_count": {"type": "integer", "minimum": 2, "maximum": 4},
            "base_seed": {"type": "integer"},
            "capabilities": document,
        }
        compile_required = (
            "document_id",
            "brief_revision_id",
            "blueprint_revision_id",
            "brief",
            "blueprint",
            "operation",
            "take_count",
            "base_seed",
            "capabilities",
        )
        self._register(
            registry,
            "creative.plan.compile",
            "Compile a capability-aware take strategy.",
            compile_properties,
            compile_required,
            self._compile,
        )
        self._register(
            registry,
            "creative.contract.review",
            "Render the full contract approval view.",
            compile_properties,
            compile_required,
            self._review,
        )

    def _register(
        self,
        registry: ToolRegistry,
        name: str,
        description: str,
        properties: JsonObject,
        required: tuple[str, ...],
        handler: ToolHandler,
    ) -> None:
        registry.register(
            ToolDefinition(
                name,
                description,
                _object_schema(properties, required),
                handler,
                self._owns_project,
            )
        )

    def _owns_project(self, context: ToolContext, arguments: JsonObject) -> bool:
        try:
            self._repository.get_project(
                owner_id=context.user_id,
                project_id=context.project_id,
            )
        except KeyError:
            return False
        return True

    def _brief(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        request = BriefRequest(
            concept=_string(arguments, "concept"),
            audience=_string(arguments, "audience"),
            language=_string(arguments, "language"),
            genres=_strings(arguments, "genres"),
            moods=_strings(arguments, "moods"),
            tempo_bpm=_optional_int(arguments, "tempo_bpm"),
            duration_seconds=_number(arguments, "duration_seconds"),
            vocal=_string(arguments, "vocal"),
            references=_strings(arguments, "references", required=False),
            exclusions=_strings(arguments, "exclusions", required=False),
            user_lyrics=_optional_string(arguments, "user_lyrics"),
            lyrics_rights=LyricsRights(str(arguments.get("lyrics_rights", "unknown"))),
            style_description=_optional_string(arguments, "style_description") or "",
            protected_identities=_strings(arguments, "protected_identities", required=False),
        )
        brief = build_brief(DocumentId(_string(arguments, "document_id")), request)
        return {"brief": to_json_object(brief)}

    def _blueprint(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        brief = brief_from_json(_object(arguments, "brief"))
        blueprint = plan_blueprint(
            DocumentId(_string(arguments, "document_id")),
            brief,
        )
        return {"blueprint": to_json_object(blueprint)}

    def _lyrics(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        brief = brief_from_json(_object(arguments, "brief"))
        blueprint = blueprint_from_json(_object(arguments, "blueprint"))
        raw_candidates = _object(arguments, "candidates")
        candidates: dict[SectionId, tuple[str, ...]] = {}
        for key, value in raw_candidates.items():
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise TypeError("candidate lines must be string lists")
            candidates[SectionId(key)] = tuple(item for item in value if isinstance(item, str))
        locked = frozenset(
            SectionId(value) for value in _strings(arguments, "locked_sections", required=False)
        )
        lyrics = create_lyrics(
            DocumentId(_string(arguments, "document_id")),
            brief,
            blueprint,
            candidates,
            locked_sections=locked,
        )
        return {"lyrics": to_json_object(lyrics)}

    def _continue(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        lyrics = lyrics_from_json(_object(arguments, "lyrics"))
        raw_additions = _object(arguments, "additions")
        additions: dict[SectionId, tuple[str, ...]] = {}
        for key, value in raw_additions.items():
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise TypeError("addition lines must be string lists")
            additions[SectionId(key)] = tuple(item for item in value if isinstance(item, str))
        return {"lyrics": to_json_object(continue_lyrics(lyrics, additions))}

    def _rewrite(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        lyrics = lyrics_from_json(_object(arguments, "lyrics"))
        raw_replacements = _object(arguments, "replacements")
        replacements = {
            LyricLineId(key): _mapping_string(raw_replacements, key) for key in raw_replacements
        }
        return {"lyrics": to_json_object(constrained_rewrite(lyrics, replacements))}

    def _restructure(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        lyrics = lyrics_from_json(_object(arguments, "lyrics"))
        order = tuple(SectionId(value) for value in _strings(arguments, "section_order"))
        return {"lyrics": to_json_object(restructure_lyrics(lyrics, order))}

    def _critique(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        lyrics = lyrics_from_json(_object(arguments, "lyrics"))
        return {"warnings": [to_json_object(warning) for warning in critique_lyrics(lyrics)]}

    def _compile(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        compiled = self._compile_value(arguments)
        return {
            "plan": to_json_object(compiled.plan),
            "variations": [to_json_object(item) for item in compiled.variations],
            "warnings": [to_json_object(item) for item in compiled.warnings],
        }

    def _review(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        brief = brief_from_json(_object(arguments, "brief"))
        blueprint = blueprint_from_json(_object(arguments, "blueprint"))
        lyrics_payload = arguments.get("lyrics")
        lyrics = lyrics_from_json(lyrics_payload) if isinstance(lyrics_payload, dict) else None
        compiled = self._compile_value(arguments)
        warnings = critique_lyrics(lyrics) if lyrics is not None else ()
        return {
            "approval_view": render_approval_view(
                brief,
                blueprint,
                lyrics,
                compiled,
                warnings=warnings,
            ),
            "warning_count": len(warnings),
        }

    @staticmethod
    def _compile_value(arguments: JsonObject) -> CompiledPlan:
        brief = brief_from_json(_object(arguments, "brief"))
        blueprint = blueprint_from_json(_object(arguments, "blueprint"))
        capabilities_payload = _object(arguments, "capabilities")
        capabilities = BackendCapabilities(
            operations=frozenset(_strings(capabilities_payload, "operations")),
            max_duration_seconds=_number(capabilities_payload, "max_duration_seconds"),
            supported_languages=frozenset(_strings(capabilities_payload, "supported_languages")),
            supports_vocals=_boolean(capabilities_payload, "supports_vocals", True),
        )
        return compile_generation_plan(
            DocumentId(_string(arguments, "document_id")),
            brief_revision_id=RevisionId(_string(arguments, "brief_revision_id")),
            blueprint_revision_id=RevisionId(_string(arguments, "blueprint_revision_id")),
            lyrics_revision_id=(
                RevisionId(value)
                if (value := _optional_string(arguments, "lyrics_revision_id"))
                else None
            ),
            brief=brief,
            blueprint=blueprint,
            operation=_string(arguments, "operation"),
            take_count=_int(arguments, "take_count"),
            base_seed=_int(arguments, "base_seed"),
            capabilities=capabilities,
        )


def _object_schema(properties: JsonObject, required: tuple[str, ...] = ()) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _object(payload: Mapping[str, JsonValue], name: str) -> JsonObject:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _string(payload: Mapping[str, JsonValue], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{name} must be a non-empty string")
    return value


def _mapping_string(payload: Mapping[str, JsonValue], name: str) -> str:
    return _string(payload, name)


def _optional_string(payload: Mapping[str, JsonValue], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string or null")
    return value


def _strings(
    payload: Mapping[str, JsonValue], name: str, *, required: bool = True
) -> tuple[str, ...]:
    value = payload.get(name)
    if value is None and not required:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{name} must be a string list")
    return tuple(item for item in value if isinstance(item, str))


def _number(payload: Mapping[str, JsonValue], name: str) -> float:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be numeric")
    return float(value)


def _int(payload: Mapping[str, JsonValue], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _optional_int(payload: Mapping[str, JsonValue], name: str) -> int | None:
    value = payload.get(name)
    if value is None:
        return None
    return _int(payload, name)


def _boolean(payload: Mapping[str, JsonValue], name: str, default: bool) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be boolean")
    return value
