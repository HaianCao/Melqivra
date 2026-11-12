"""Backend-neutral contracts for approved music generation work."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from agent_music.domain import JsonObject, ProjectId, RevisionId, validate_identifier

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GenerationContractError(ValueError):
    """An approved generation request violates a product invariant."""


class UnsupportedGenerationError(GenerationContractError):
    """The selected backend cannot execute the requested contract."""


class VocalMode(StrEnum):
    """Whether a take contains a sung vocal performance."""

    VOCAL = "vocal"
    INSTRUMENTAL = "instrumental"


@dataclass(frozen=True, slots=True)
class MusicBackendDescriptor:
    """Immutable capability and provenance declaration for one backend pin."""

    backend_id: str
    source_revision: str
    model_ids: tuple[str, ...]
    license_ids: tuple[str, ...]
    operations: frozenset[str]
    supported_languages: frozenset[str]
    supports_vocals: bool
    supports_instrumental: bool
    min_duration_seconds: float
    max_duration_seconds: float
    output_media_type: str = "audio/wav"

    def validate(self) -> None:
        validate_identifier(self.backend_id)
        if not self.source_revision.strip():
            raise GenerationContractError("Backend source revision is required")
        if not self.model_ids or not all(item.strip() for item in self.model_ids):
            raise GenerationContractError("Backend model IDs are required")
        if not self.license_ids or not all(item.strip() for item in self.license_ids):
            raise GenerationContractError("Backend license provenance is required")
        if not self.operations:
            raise GenerationContractError("Backend must declare at least one operation")
        if not self.supported_languages:
            raise GenerationContractError("Backend must declare verified languages")
        if self.min_duration_seconds <= 0:
            raise GenerationContractError("Minimum duration must be positive")
        if self.max_duration_seconds < self.min_duration_seconds:
            raise GenerationContractError("Backend duration bounds are invalid")
        if not self.output_media_type.startswith("audio/"):
            raise GenerationContractError("Backend output must be an audio media type")


@dataclass(frozen=True, slots=True)
class TakeSpec:
    """One explicit take variation within an approved batch."""

    take_id: str
    label: str
    seed: int
    settings: JsonObject

    def validate(self) -> None:
        validate_identifier(self.take_id)
        if not self.label.strip():
            raise GenerationContractError("Take label is required")
        if self.seed < 0:
            raise GenerationContractError("Take seed must be non-negative")


@dataclass(frozen=True, slots=True)
class MusicGenerationRequest:
    """Exact approved contract sent across the durable GPU job boundary."""

    schema_version: int
    request_id: str
    project_id: ProjectId
    contract_checksum: str
    source_revision_ids: tuple[RevisionId, ...]
    backend_id: str
    backend_revision: str
    operation: str
    language: str
    vocal_mode: VocalMode
    prompt: str
    lyrics: str | None
    duration_seconds: float
    takes: tuple[TakeSpec, ...]

    def validate(self) -> None:
        if self.schema_version != 1:
            raise GenerationContractError("Unsupported generation request schema")
        validate_identifier(self.request_id)
        validate_identifier(str(self.project_id))
        validate_identifier(self.backend_id)
        if not _SHA256.fullmatch(self.contract_checksum):
            raise GenerationContractError("Approved contract checksum must be SHA-256")
        if not self.source_revision_ids:
            raise GenerationContractError("Generation request must pin source revisions")
        for revision_id in self.source_revision_ids:
            validate_identifier(str(revision_id))
        if not self.backend_revision.strip():
            raise GenerationContractError("Backend revision pin is required")
        if not self.operation.strip():
            raise GenerationContractError("Generation operation is required")
        if not self.language.strip():
            raise GenerationContractError("Generation language is required")
        if not self.prompt.strip():
            raise GenerationContractError("Backend-neutral prompt is required")
        if not 2 <= len(self.takes) <= 4:
            raise GenerationContractError("Generation requires two to four takes")
        if self.duration_seconds <= 0:
            raise GenerationContractError("Generation duration must be positive")
        if self.vocal_mode is VocalMode.VOCAL and not (self.lyrics or "").strip():
            raise GenerationContractError("Vocal generation requires approved lyrics")
        if self.vocal_mode is VocalMode.INSTRUMENTAL and self.lyrics is not None:
            raise GenerationContractError("Instrumental generation must not hide lyrics")
        take_ids: set[str] = set()
        seeds: set[int] = set()
        for take in self.takes:
            take.validate()
            if take.take_id in take_ids:
                raise GenerationContractError("Take IDs must be unique")
            if take.seed in seeds:
                raise GenerationContractError("Take seeds must be unique")
            take_ids.add(take.take_id)
            seeds.add(take.seed)


@dataclass(frozen=True, slots=True)
class GeneratedTake:
    """Validated backend output metadata before durable artifact import."""

    take_id: str
    seed: int
    relative_path: str
    media_type: str
    size_bytes: int
    sha256: str
    duration_seconds: float
    sample_rate_hz: int
    channels: int
    validation: JsonObject

    def validate(self) -> None:
        validate_identifier(self.take_id)
        if self.seed < 0:
            raise GenerationContractError("Generated take seed must be non-negative")
        path = self.relative_path.replace("\\", "/")
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise GenerationContractError("Generated take path must stay relative")
        if not self.media_type.startswith("audio/"):
            raise GenerationContractError("Generated take must be audio")
        if self.size_bytes <= 0 or not _SHA256.fullmatch(self.sha256):
            raise GenerationContractError("Generated take integrity metadata is invalid")
        if self.duration_seconds <= 0 or self.sample_rate_hz <= 0 or self.channels <= 0:
            raise GenerationContractError("Generated take audio metadata is invalid")
        if self.validation.get("passed") is not True:
            raise GenerationContractError("Generated take failed audio validation")


@dataclass(frozen=True, slots=True)
class GenerationBatchResult:
    """Complete, all-or-nothing result for one approved multi-take request."""

    backend_id: str
    backend_revision: str
    contract_checksum: str
    takes: tuple[GeneratedTake, ...]
    metrics: JsonObject


ProgressCallback = Callable[[JsonObject], None]


class MusicGenerationBackend(Protocol):
    """Interchangeable model backend behind the remote worker boundary."""

    @property
    def descriptor(self) -> MusicBackendDescriptor: ...

    def generate(
        self,
        request: MusicGenerationRequest,
        on_progress: ProgressCallback,
    ) -> GenerationBatchResult: ...


def request_to_json(request: MusicGenerationRequest) -> JsonObject:
    """Serialize one validated request for the provider-neutral worker."""

    request.validate()
    return {
        "schema_version": request.schema_version,
        "request_id": request.request_id,
        "project_id": str(request.project_id),
        "contract_checksum": request.contract_checksum,
        "source_revision_ids": [str(item) for item in request.source_revision_ids],
        "backend_id": request.backend_id,
        "backend_revision": request.backend_revision,
        "operation": request.operation,
        "language": request.language,
        "vocal_mode": request.vocal_mode.value,
        "prompt": request.prompt,
        "lyrics": request.lyrics,
        "duration_seconds": request.duration_seconds,
        "takes": [
            {
                "take_id": take.take_id,
                "label": take.label,
                "seed": take.seed,
                "settings": take.settings,
            }
            for take in request.takes
        ],
    }


def request_from_json(payload: JsonObject) -> MusicGenerationRequest:
    """Decode and validate an untrusted remote generation request."""

    request = MusicGenerationRequest(
        schema_version=_integer(payload, "schema_version"),
        request_id=_string(payload, "request_id"),
        project_id=ProjectId(_string(payload, "project_id")),
        contract_checksum=_string(payload, "contract_checksum"),
        source_revision_ids=tuple(
            RevisionId(item) for item in _string_list(payload, "source_revision_ids")
        ),
        backend_id=_string(payload, "backend_id"),
        backend_revision=_string(payload, "backend_revision"),
        operation=_string(payload, "operation"),
        language=_string(payload, "language"),
        vocal_mode=VocalMode(_string(payload, "vocal_mode")),
        prompt=_string(payload, "prompt"),
        lyrics=_optional_string(payload, "lyrics"),
        duration_seconds=_number(payload, "duration_seconds"),
        takes=tuple(_take_from_json(item) for item in _object_list(payload, "takes")),
    )
    request.validate()
    return request


class MusicBackendRegistry:
    """Explicit backend registry with no implicit model or revision fallback."""

    def __init__(self) -> None:
        self._backends: dict[str, MusicGenerationBackend] = {}

    def register(self, backend: MusicGenerationBackend) -> None:
        descriptor = backend.descriptor
        descriptor.validate()
        if descriptor.backend_id in self._backends:
            raise ValueError(f"Duplicate music backend: {descriptor.backend_id}")
        self._backends[descriptor.backend_id] = backend

    def resolve(self, backend_id: str, backend_revision: str) -> MusicGenerationBackend:
        backend = self._backends.get(backend_id)
        if backend is None:
            raise UnsupportedGenerationError(f"Unknown music backend: {backend_id}")
        if backend.descriptor.source_revision != backend_revision:
            raise UnsupportedGenerationError("Requested backend revision is unavailable")
        return backend


def execute_generation(
    backend: MusicGenerationBackend,
    request: MusicGenerationRequest,
    on_progress: ProgressCallback,
) -> GenerationBatchResult:
    """Validate, execute, and verify one all-or-nothing generation batch."""

    request.validate()
    descriptor = backend.descriptor
    descriptor.validate()
    if descriptor.backend_id != request.backend_id:
        raise UnsupportedGenerationError("Request selected a different backend")
    if descriptor.source_revision != request.backend_revision:
        raise UnsupportedGenerationError("Request selected a different backend revision")
    if request.operation not in descriptor.operations:
        raise UnsupportedGenerationError("Backend does not support the requested operation")
    if request.language.casefold() not in {
        language.casefold() for language in descriptor.supported_languages
    }:
        raise UnsupportedGenerationError("Backend has not verified the requested language")
    if request.vocal_mode is VocalMode.VOCAL and not descriptor.supports_vocals:
        raise UnsupportedGenerationError("Backend does not support vocals")
    if request.vocal_mode is VocalMode.INSTRUMENTAL and not descriptor.supports_instrumental:
        raise UnsupportedGenerationError("Backend does not support instrumental generation")
    if (
        not descriptor.min_duration_seconds
        <= request.duration_seconds
        <= (descriptor.max_duration_seconds)
    ):
        raise UnsupportedGenerationError("Backend does not support the requested duration")

    on_progress({"type": "stage", "stage": "contract_validated", "progress": 0.05})
    result = backend.generate(request, on_progress)
    if result.backend_id != request.backend_id:
        raise GenerationContractError("Backend result identity changed")
    if result.backend_revision != request.backend_revision:
        raise GenerationContractError("Backend result revision changed")
    if result.contract_checksum != request.contract_checksum:
        raise GenerationContractError("Backend result contract checksum changed")
    if len(result.takes) != len(request.takes):
        raise GenerationContractError("Backend returned an incomplete take batch")
    expected = {take.take_id: take.seed for take in request.takes}
    observed: set[str] = set()
    for take in result.takes:
        take.validate()
        if take.take_id in observed or expected.get(take.take_id) != take.seed:
            raise GenerationContractError("Backend returned unexpected take identity")
        if take.media_type != descriptor.output_media_type:
            raise GenerationContractError("Backend returned an unexpected audio format")
        observed.add(take.take_id)
    on_progress({"type": "stage", "stage": "batch_validated", "progress": 1.0})
    return result


def result_to_json(result: GenerationBatchResult) -> JsonObject:
    """Serialize a fully validated backend result for the remote worker."""

    return {
        "backend_id": result.backend_id,
        "backend_revision": result.backend_revision,
        "contract_checksum": result.contract_checksum,
        "takes": [
            {
                "take_id": take.take_id,
                "seed": take.seed,
                "relative_path": take.relative_path,
                "media_type": take.media_type,
                "size_bytes": take.size_bytes,
                "sha256": take.sha256,
                "duration_seconds": take.duration_seconds,
                "sample_rate_hz": take.sample_rate_hz,
                "channels": take.channels,
                "validation": take.validation,
            }
            for take in result.takes
        ],
        "artifacts": [
            {
                "relative_path": take.relative_path,
                "media_type": take.media_type,
                "size_bytes": take.size_bytes,
                "sha256": take.sha256,
            }
            for take in result.takes
        ],
        "metrics": result.metrics,
    }


def _take_from_json(payload: JsonObject) -> TakeSpec:
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise GenerationContractError("Take settings must be an object")
    return TakeSpec(
        _string(payload, "take_id"),
        _string(payload, "label"),
        _integer(payload, "seed"),
        settings,
    )


def _string(payload: JsonObject, name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise GenerationContractError(f"{name} must be a non-empty string")
    return value


def _optional_string(payload: JsonObject, name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise GenerationContractError(f"{name} must be a string or null")
    return value


def _integer(payload: JsonObject, name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise GenerationContractError(f"{name} must be an integer")
    return value


def _number(payload: JsonObject, name: str) -> float:
    value = payload.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise GenerationContractError(f"{name} must be a number")
    return float(value)


def _string_list(payload: JsonObject, name: str) -> tuple[str, ...]:
    value = payload.get(name)
    if not isinstance(value, list):
        raise GenerationContractError(f"{name} must be a string array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise GenerationContractError(f"{name} must be a string array")
        result.append(item)
    return tuple(result)


def _object_list(payload: JsonObject, name: str) -> tuple[JsonObject, ...]:
    value = payload.get(name)
    if not isinstance(value, list):
        raise GenerationContractError(f"{name} must be an object array")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            raise GenerationContractError(f"{name} must be an object array")
        result.append(item)
    return tuple(result)
