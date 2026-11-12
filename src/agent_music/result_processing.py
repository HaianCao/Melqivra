"""Post-run validation and durable publication before job success."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from agent_music.artifact_store import AtomicArtifactStore
from agent_music.backends.heartmula import HEARTMULA_DESCRIPTOR
from agent_music.config import HarnessSettings
from agent_music.domain import JsonObject, RevisionId, to_json_object
from agent_music.generation import (
    MusicBackendDescriptor,
    request_from_json,
    result_from_json,
)
from agent_music.generation_artifacts import import_generation_batch
from agent_music.models import GpuJob


class JobResultProcessor(Protocol):
    """Transform a runner result before its fenced durable completion."""

    def process(self, job: GpuJob, result: dict[str, Any]) -> dict[str, Any]: ...


class PassthroughResultProcessor:
    """Preserve existing behavior for jobs without post-run publication."""

    def process(self, job: GpuJob, result: dict[str, Any]) -> dict[str, Any]:
        _ = job
        return result


class GenerationResultProcessor:
    """Import successful music output before reporting durable job success."""

    def __init__(
        self,
        *,
        artifact_store: AtomicArtifactStore,
        transfer_root: Path,
        descriptors: tuple[MusicBackendDescriptor, ...],
    ) -> None:
        self._artifact_store = artifact_store
        self._transfer_root = transfer_root.resolve()
        self._descriptors = {descriptor.backend_id: descriptor for descriptor in descriptors}
        if len(self._descriptors) != len(descriptors):
            raise ValueError("Generation backend descriptors must be unique")
        for descriptor in descriptors:
            descriptor.validate()

    def process(self, job: GpuJob, result: dict[str, Any]) -> dict[str, Any]:
        if job.kind != "music.generate":
            return result
        request_payload = _object(job.payload, "generation_request")
        request = request_from_json(request_payload)
        plan_revision_id = RevisionId(_string(job.payload, "plan_revision_id"))
        descriptor = self._descriptors.get(request.backend_id)
        if descriptor is None or descriptor.source_revision != request.backend_revision:
            raise ValueError("Approved generation backend descriptor is unavailable")
        batch = result_from_json(_object(result, "result"))
        transfer_id = _string(result, "_transfer_id")
        transfer_root = (self._transfer_root / transfer_id).resolve()
        if transfer_root.parent != self._transfer_root:
            raise ValueError("Runner transfer path escaped its configured root")
        environment = _json_object(result.get("_environment"), "_environment")
        imported = import_generation_batch(
            request=request,
            result=batch,
            descriptor=descriptor,
            plan_revision_id=plan_revision_id,
            remote_artifact_root=transfer_root,
            artifact_store=self._artifact_store,
            environment=environment,
        )
        return {
            "status": "succeeded",
            "job_id": job.id,
            "kind": job.kind,
            "generation": to_json_object(imported),
            "metrics": batch.metrics,
        }


def build_default_result_processor(settings: HarnessSettings) -> JobResultProcessor:
    """Build the installed generation completion policy without loading model assets."""

    return GenerationResultProcessor(
        artifact_store=AtomicArtifactStore(settings.artifact_root),
        transfer_root=settings.transfer_root,
        descriptors=(HEARTMULA_DESCRIPTOR,),
    )


def _object(payload: Mapping[str, object], name: str) -> JsonObject:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast(JsonObject, value)


def _string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _json_object(value: object, name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast(JsonObject, value)
