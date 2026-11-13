"""Concrete allowlisted S02 project tools for the planner harness."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import cast

from agent_music.agent_harness import ToolContext, ToolDefinition, ToolHandler, ToolRegistry
from agent_music.domain import DocumentType, JsonObject, JsonValue, RevisionId
from agent_music.project_store import ProjectRepository

ReadCollection = Callable[[ToolContext], tuple[JsonObject, ...]]


class ProjectToolService:
    """Bind typed planner capabilities to tenant-scoped project services."""

    def __init__(
        self,
        repository: ProjectRepository,
        *,
        read_takes: ReadCollection | None = None,
        read_jobs: ReadCollection | None = None,
    ) -> None:
        self._repository = repository
        self._read_takes = read_takes or (lambda context: ())
        self._read_jobs = read_jobs or (lambda context: ())

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            ToolDefinition(
                "project.read",
                "Read the current project and revision pointers.",
                _object_schema({}),
                self._project_read,
                self._owns_project,
            )
        )
        registry.register(
            ToolDefinition(
                "revision.read",
                "Read one immutable revision by ID.",
                _object_schema({"revision_id": {"type": "string"}}, ("revision_id",)),
                self._revision_read,
                self._owns_project,
            )
        )
        for document_type in (
            DocumentType.BRIEF,
            DocumentType.LYRICS,
            DocumentType.BLUEPRINT,
            DocumentType.GENERATION_PLAN,
        ):
            name = document_type.value
            registry.register(
                ToolDefinition(
                    f"{name}.read",
                    f"Read the working {name} revision.",
                    _object_schema({}),
                    self._document_reader(document_type),
                    self._owns_project,
                )
            )
            registry.register(
                ToolDefinition(
                    f"{name}.edit",
                    f"Append an immutable {name} revision.",
                    _object_schema(
                        {
                            "expected_parent_id": {"type": ["string", "null"]},
                            "reason": {"type": "string"},
                            "payload": {"type": "object"},
                        },
                        ("expected_parent_id", "reason", "payload"),
                    ),
                    self._document_editor(document_type),
                    self._owns_project,
                )
            )
        registry.register(
            ToolDefinition(
                "revision.accept",
                "Accept a reviewed working revision.",
                _object_schema(
                    {
                        "document_type": {"type": "string"},
                        "revision_id": {"type": "string"},
                        "expected_working_id": {"type": "string"},
                    },
                    ("document_type", "revision_id", "expected_working_id"),
                ),
                self._revision_accept,
                self._owns_project,
                approval_action="revision.accept",
            )
        )
        registry.register(
            ToolDefinition(
                "revision.restore",
                "Restore old content as a new immutable revision.",
                _object_schema(
                    {
                        "source_revision_id": {"type": "string"},
                        "expected_working_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    ("source_revision_id", "expected_working_id", "reason"),
                ),
                self._revision_restore,
                self._owns_project,
                approval_action="revision.restore",
            )
        )
        for tool_name, reader in (("takes.list", self._read_takes), ("jobs.list", self._read_jobs)):
            registry.register(
                ToolDefinition(
                    tool_name,
                    f"Read project {tool_name.split('.')[0]}.",
                    _object_schema({}),
                    self._collection_reader(reader),
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

    def _project_read(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        project = self._repository.get_project(
            owner_id=context.user_id, project_id=context.project_id
        )
        index = self._repository.get_index(owner_id=context.user_id, project_id=context.project_id)
        return {
            "project_id": str(project.id),
            "title": project.title,
            "selections": [
                {
                    "document_type": item.document_type.value,
                    "working_revision_id": (
                        str(item.working_revision_id) if item.working_revision_id else None
                    ),
                    "accepted_revision_id": (
                        str(item.accepted_revision_id) if item.accepted_revision_id else None
                    ),
                }
                for item in index.selections
            ],
        }

    def _revision_read(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        revision_id = RevisionId(_required_string(arguments, "revision_id"))
        revision = self._repository.get_revision(
            owner_id=context.user_id,
            project_id=context.project_id,
            revision_id=revision_id,
        )
        return self._revision_result(context, revision.id)

    @staticmethod
    def _collection_reader(reader: ReadCollection) -> ToolHandler:
        def read(context: ToolContext, arguments: JsonObject) -> JsonObject:
            return {"items": list(reader(context))}

        return read

    def _document_reader(self, document_type: DocumentType) -> ToolHandler:
        def read(context: ToolContext, arguments: JsonObject) -> JsonObject:
            selection = self._selection(context, document_type)
            if selection is None:
                return {"revision_id": None, "payload": None}
            return self._revision_result(context, selection)

        return read

    def _document_editor(self, document_type: DocumentType) -> ToolHandler:
        def edit(context: ToolContext, arguments: JsonObject) -> JsonObject:
            expected_raw = arguments.get("expected_parent_id")
            if expected_raw is not None and not isinstance(expected_raw, str):
                raise TypeError("expected_parent_id must be a string or null")
            payload = arguments.get("payload")
            if not isinstance(payload, dict):
                raise TypeError("payload must be an object")
            revision = self._repository.commit_revision(
                owner_id=context.user_id,
                project_id=context.project_id,
                document_type=document_type,
                payload=cast(Mapping[str, JsonValue], payload),
                expected_working_revision_id=(
                    RevisionId(expected_raw) if isinstance(expected_raw, str) else None
                ),
                actor=context.actor,
                reason=_required_string(arguments, "reason"),
            )
            return self._revision_result(context, revision.id)

        return edit

    def _revision_accept(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        document_type = DocumentType(_required_string(arguments, "document_type"))
        revision_id = RevisionId(_required_string(arguments, "revision_id"))
        self._repository.accept_revision(
            owner_id=context.user_id,
            project_id=context.project_id,
            document_type=document_type,
            revision_id=revision_id,
            expected_working_revision_id=RevisionId(
                _required_string(arguments, "expected_working_id")
            ),
        )
        return {"revision_id": str(revision_id), "accepted": True}

    def _revision_restore(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        source = self._repository.get_revision(
            owner_id=context.user_id,
            project_id=context.project_id,
            revision_id=RevisionId(_required_string(arguments, "source_revision_id")),
        )
        restored = self._repository.commit_revision(
            owner_id=context.user_id,
            project_id=context.project_id,
            document_type=source.document_type,
            payload=self._repository.revision_payload(source),
            expected_working_revision_id=RevisionId(
                _required_string(arguments, "expected_working_id")
            ),
            actor=context.actor,
            reason=_required_string(arguments, "reason"),
        )
        return self._revision_result(context, restored.id)

    def _selection(self, context: ToolContext, document_type: DocumentType) -> RevisionId | None:
        index = self._repository.get_index(owner_id=context.user_id, project_id=context.project_id)
        for selection in index.selections:
            if selection.document_type is document_type:
                return selection.working_revision_id
        return None

    def _revision_result(self, context: ToolContext, revision_id: RevisionId) -> JsonObject:
        revision = self._repository.get_revision(
            owner_id=context.user_id,
            project_id=context.project_id,
            revision_id=revision_id,
        )
        return {
            "revision_id": str(revision.id),
            "document_type": revision.document_type.value,
            "parent_revision_id": (
                str(revision.parent_revision_id) if revision.parent_revision_id else None
            ),
            "payload": self._repository.revision_payload(revision),
            "checksum": revision.checksum,
        }


def _required_string(arguments: JsonObject, name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{name} must be a non-empty string")
    return value


def _object_schema(properties: JsonObject, required: tuple[str, ...] = ()) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }
