"""Immutable import of validated remote generation batches."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from agent_music.artifact_service import ArtifactPublisher
from agent_music.artifact_store import ArtifactWrite
from agent_music.audio_validation import validate_pcm16_wav
from agent_music.domain import (
    Artifact,
    ArtifactId,
    GenerationManifest,
    JsonObject,
    RevisionId,
    Take,
    TakeId,
    UserId,
    to_json_object,
)
from agent_music.generation import (
    GenerationBatchResult,
    GenerationContractError,
    MusicBackendDescriptor,
    MusicGenerationRequest,
    validate_generation_result,
)


@dataclass(frozen=True, slots=True)
class ImportedTake:
    """One persisted take with its audio and reproducibility manifest."""

    take: Take
    audio_artifact: Artifact
    manifest_artifact: Artifact
    manifest: GenerationManifest


@dataclass(frozen=True, slots=True)
class ImportedGenerationBatch:
    """Complete immutable persistence result for one remote generation batch."""

    batch_id: str
    contract_checksum: str
    takes: tuple[ImportedTake, ...]


@dataclass(frozen=True, slots=True)
class _PreparedTake:
    take_id: TakeId
    audio_id: ArtifactId
    manifest_id: ArtifactId
    audio_content: bytes
    manifest_content: bytes
    manifest: GenerationManifest


def import_generation_batch(
    *,
    request: MusicGenerationRequest,
    result: GenerationBatchResult,
    descriptor: MusicBackendDescriptor,
    owner_id: UserId,
    plan_revision_id: RevisionId,
    remote_artifact_root: Path,
    artifact_publisher: ArtifactPublisher,
    environment: JsonObject,
) -> ImportedGenerationBatch:
    """Verify and atomically persist every take and manifest in one batch."""

    validate_generation_result(descriptor, request, result)
    if plan_revision_id not in request.source_revision_ids:
        raise GenerationContractError("Plan revision is not pinned by the request")
    root = remote_artifact_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)

    request_specs = {take.take_id: take for take in request.takes}
    prepared: list[_PreparedTake] = []
    for generated in result.takes:
        source = (root / generated.relative_path).resolve()
        if not source.is_relative_to(root) or source == root:
            raise GenerationContractError("Generated artifact escaped the transfer root")
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != generated.size_bytes or digest != generated.sha256:
            raise GenerationContractError("Transferred artifact failed integrity verification")
        validation = validate_pcm16_wav(
            source,
            expected_duration_seconds=request.duration_seconds,
        )
        if validation["passed"] is not True:
            raise GenerationContractError("Transferred artifact failed audio validation")
        if (
            validation["sample_rate_hz"] != generated.sample_rate_hz
            or validation["channels"] != generated.channels
        ):
            raise GenerationContractError("Transferred artifact audio metadata changed")

        stable = _stable_suffix(request.request_id, generated.take_id)
        take_id = TakeId(f"take_{stable}")
        audio_id = ArtifactId(f"art_{stable}_wav")
        manifest_id = ArtifactId(f"art_{stable}_manifest")
        spec = request_specs[generated.take_id]
        manifest = GenerationManifest(
            project_id=request.project_id,
            request_id=request.request_id,
            contract_checksum=request.contract_checksum,
            take_id=take_id,
            artifact_id=audio_id,
            artifact_sha256=digest,
            source_revision_ids=request.source_revision_ids,
            backend=descriptor.backend_id,
            backend_revision=descriptor.source_revision,
            model_ids=descriptor.model_ids,
            settings={
                "operation": request.operation,
                "language": request.language,
                "vocal_mode": request.vocal_mode.value,
                "duration_seconds": request.duration_seconds,
                "backend_take_id": generated.take_id,
                "variation_label": spec.label,
                "variation": spec.settings,
            },
            seed=generated.seed,
            audio=validation,
            environment=environment,
            license_provenance=descriptor.license_ids,
        )
        manifest_content = (
            json.dumps(
                to_json_object(manifest),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        prepared.append(
            _PreparedTake(
                take_id,
                audio_id,
                manifest_id,
                content,
                manifest_content,
                manifest,
            )
        )

    batch_id = f"batch_{_stable_suffix(request.request_id, request.contract_checksum)}"
    writes = tuple(
        write
        for item in prepared
        for write in (
            ArtifactWrite(
                item.audio_id,
                descriptor.output_media_type,
                item.audio_content,
                request.source_revision_ids,
            ),
            ArtifactWrite(
                item.manifest_id,
                "application/json",
                item.manifest_content,
                request.source_revision_ids,
            ),
        )
    )
    artifacts = artifact_publisher.publish_batch(
        owner_id=owner_id,
        project_id=request.project_id,
        batch_id=batch_id,
        writes=writes,
    )
    artifacts_by_id = {artifact.id: artifact for artifact in artifacts}
    imported = tuple(
        ImportedTake(
            take=Take(
                id=item.take_id,
                project_id=request.project_id,
                plan_revision_id=plan_revision_id,
                artifact_ids=(item.audio_id, item.manifest_id),
                seed=item.manifest.seed,
            ),
            audio_artifact=artifacts_by_id[item.audio_id],
            manifest_artifact=artifacts_by_id[item.manifest_id],
            manifest=item.manifest,
        )
        for item in prepared
    )
    return ImportedGenerationBatch(batch_id, request.contract_checksum, imported)


def _stable_suffix(*values: str) -> str:
    digest = hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()
    return digest[:24]
