"""Framework-independent song-project domain contracts."""

from __future__ import annotations

import dataclasses
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import NewType, TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

UserId = NewType("UserId", str)
ProjectId = NewType("ProjectId", str)
DocumentId = NewType("DocumentId", str)
SectionId = NewType("SectionId", str)
LyricLineId = NewType("LyricLineId", str)
RevisionId = NewType("RevisionId", str)
TakeId = NewType("TakeId", str)
ArtifactId = NewType("ArtifactId", str)
ApprovalId = NewType("ApprovalId", str)
JobId = NewType("JobId", str)

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{2,127}$")


def new_identifier(prefix: str) -> str:
    """Create a sortable-safe opaque identifier with a stable type prefix."""

    validate_identifier(prefix)
    return f"{prefix}_{uuid.uuid4().hex}"


def validate_identifier(value: str) -> str:
    """Reject identifiers that could become ambiguous paths or SQL selectors."""

    if not _IDENTIFIER.fullmatch(value) or ".." in value:
        raise ValueError(f"Invalid identifier: {value!r}")
    return value


class DocumentType(StrEnum):
    """Immutable document families stored in a song project."""

    BRIEF = "brief"
    BLUEPRINT = "blueprint"
    LYRICS = "lyrics"
    GENERATION_PLAN = "generation_plan"


class ApprovalState(StrEnum):
    """State of a consequential action proposal."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class User:
    """Project owner identity at the domain boundary."""

    id: UserId
    display_name: str


@dataclass(frozen=True, slots=True)
class Project:
    """Durable song-project root."""

    id: ProjectId
    owner_id: UserId
    title: str
    created_at: float


@dataclass(frozen=True, slots=True)
class Brief:
    """Structured user intent independent of planner chat history."""

    id: DocumentId
    concept: str
    audience: str
    language: str
    genres: tuple[str, ...]
    moods: tuple[str, ...]
    tempo_bpm: int | None
    duration_seconds: float | None
    vocal: str
    references: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    user_lyrics: str | None = None


@dataclass(frozen=True, slots=True)
class Section:
    """Stable arrangement section."""

    id: SectionId
    name: str
    duration_seconds: float
    energy: float
    narrative_role: str
    instrumentation: tuple[str, ...]
    delivery: str
    transition: str


@dataclass(frozen=True, slots=True)
class Blueprint:
    """Ordered song arrangement with stable section identifiers."""

    id: DocumentId
    sections: tuple[Section, ...]


@dataclass(frozen=True, slots=True)
class LyricLine:
    """One stable, lockable lyric line."""

    id: LyricLineId
    section_id: SectionId
    text: str
    locked: bool = False


@dataclass(frozen=True, slots=True)
class Lyrics:
    """Versioned lyric document."""

    id: DocumentId
    language: str
    lines: tuple[LyricLine, ...]


@dataclass(frozen=True, slots=True)
class GenerationPlan:
    """Approved revision pins and backend-neutral generation settings."""

    id: DocumentId
    brief_revision_id: RevisionId
    blueprint_revision_id: RevisionId
    lyrics_revision_id: RevisionId | None
    operation: str
    requested_capabilities: tuple[str, ...]
    take_count: int
    settings: JsonObject


@dataclass(frozen=True, slots=True)
class Artifact:
    """Checksummed immutable artifact metadata."""

    id: ArtifactId
    project_id: ProjectId
    media_type: str
    size_bytes: int
    sha256: str
    relative_path: str
    source_revision_ids: tuple[RevisionId, ...]


@dataclass(frozen=True, slots=True)
class Take:
    """Immutable generated take and lineage."""

    id: TakeId
    project_id: ProjectId
    plan_revision_id: RevisionId
    artifact_ids: tuple[ArtifactId, ...]
    seed: int | None
    parent_take_id: TakeId | None = None


@dataclass(frozen=True, slots=True)
class Approval:
    """Review decision for a consequential action."""

    id: ApprovalId
    project_id: ProjectId
    revision_id: RevisionId
    action: str
    state: ApprovalState
    actor: str
    created_at: float
    decided_at: float | None = None


@dataclass(frozen=True, slots=True)
class JobRef:
    """Provider-neutral reference from a project to durable work."""

    id: JobId
    project_id: ProjectId
    operation: str


@dataclass(frozen=True, slots=True)
class Revision:
    """Immutable project document revision."""

    id: RevisionId
    project_id: ProjectId
    document_type: DocumentType
    parent_revision_id: RevisionId | None
    payload_json: str
    checksum: str
    actor: str
    reason: str
    created_at: float


@dataclass(frozen=True, slots=True)
class RevisionSelection:
    """Working and accepted pointers for one document family."""

    document_type: DocumentType
    working_revision_id: RevisionId | None
    accepted_revision_id: RevisionId | None


@dataclass(frozen=True, slots=True)
class ProjectIndex:
    """Current project pointers without mutating historical revisions."""

    project_id: ProjectId
    selections: tuple[RevisionSelection, ...]


@dataclass(frozen=True, slots=True)
class GenerationManifest:
    """Reproducibility contract for a generated artifact."""

    project_id: ProjectId
    source_revision_ids: tuple[RevisionId, ...]
    backend: str
    backend_revision: str
    model_ids: tuple[str, ...]
    settings: JsonObject
    seed: int | None
    environment: JsonObject
    license_provenance: tuple[str, ...]


def to_json_object(value: object) -> JsonObject:
    """Convert one domain dataclass to a JSON-compatible object."""

    if not dataclasses.is_dataclass(value) or isinstance(value, type):
        raise TypeError("Expected a dataclass instance")
    normalized = _normalize_json(dataclasses.asdict(value))
    if not isinstance(normalized, dict):
        raise TypeError("Dataclass did not normalize to an object")
    return normalized


def _normalize_json(value: object) -> JsonValue:
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, tuple | list):
        return [_normalize_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _normalize_json(item) for key, item in value.items()}
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")
