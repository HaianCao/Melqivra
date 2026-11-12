"""Import the existing real HeartMuLa WAVs through durable S05 completion."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agent_music.artifact_store import AtomicArtifactStore  # noqa: E402
from agent_music.backends.heartmula import HEARTMULA_DESCRIPTOR  # noqa: E402
from agent_music.config import HarnessSettings  # noqa: E402
from agent_music.domain import JsonObject, JsonValue  # noqa: E402
from agent_music.generation import (  # noqa: E402
    GeneratedTake,
    GenerationBatchResult,
    request_to_json,
    result_to_json,
)
from agent_music.models import GpuJob, JobState, RunnerState  # noqa: E402
from agent_music.result_processing import GenerationResultProcessor  # noqa: E402
from agent_music.runners.base import EventCallback, GpuRunner  # noqa: E402
from agent_music.s05_preflight import build_probe_request  # noqa: E402
from agent_music.scheduler import GpuHarness  # noqa: E402
from agent_music.store import SqliteJobStore  # noqa: E402


class ExistingBatchRunner(GpuRunner):
    """Return one already-generated batch without allocating compute."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result
        self._state = RunnerState.STOPPED

    @property
    def state(self) -> RunnerState:
        return self._state

    @property
    def session_name(self) -> str | None:
        return None

    async def ensure_ready(self) -> None:
        self._state = RunnerState.READY

    async def run(self, job: GpuJob, on_event: EventCallback) -> dict[str, Any]:
        _ = job
        self._state = RunnerState.BUSY
        await on_event({"type": "stage", "stage": "transfer_complete", "progress": 0.9})
        self._state = RunnerState.READY
        return self._result

    async def cancel(self, job_id: str) -> None:
        _ = job_id

    async def stop(self) -> None:
        self._state = RunnerState.STOPPED


async def import_real_takes(
    *,
    source_root: Path,
    output_root: Path,
    run_record: JsonObject,
    verification_record: JsonObject,
    preferred_take_id: str,
) -> JsonObject:
    """Persist both real WAVs, prove restart retrieval, and record preference."""

    request_id = _string(run_record, "request_id")
    request = build_probe_request(request_id)
    if _string(run_record, "contract_checksum") != request.contract_checksum:
        raise ValueError("Run record contract checksum does not match the pinned probe")

    transfer_id = "transfer_s05_real_heartmula"
    transfer_root = output_root / "transfers" / transfer_id
    runner_result = _prepare_runner_result(
        source_root=source_root,
        transfer_root=transfer_root,
        transfer_id=transfer_id,
        request_id=request_id,
        run_record=run_record,
        verification_record=verification_record,
        contract_checksum=request.contract_checksum,
    )
    settings = HarnessSettings(
        database_path=output_root / "harness.db",
        artifact_root=output_root / "artifacts",
        transfer_root=output_root / "transfers",
        runner="fake",
        scheduler_poll_seconds=0.01,
        idle_timeout_seconds=1.0,
    )
    harness = GpuHarness(
        settings,
        runner=ExistingBatchRunner(runner_result),
        result_processor=GenerationResultProcessor(
            artifact_store=AtomicArtifactStore(settings.artifact_root),
            transfer_root=settings.transfer_root,
            descriptors=(HEARTMULA_DESCRIPTOR,),
        ),
    )
    await harness.start()
    try:
        job = harness.enqueue(
            user_id="s05-real-take-import",
            kind="music.generate",
            payload={
                "generation_request": request_to_json(request),
                "plan_revision_id": "rev_s05_operator_approved_probe",
            },
            idempotency_key=f"s05-real-import:{request.contract_checksum}",
        )
        await _wait_until(
            lambda: (
                harness.store.get(job.id).state
                in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
            )
        )
        completed = harness.store.get(job.id)
        if completed.state is not JobState.SUCCEEDED or completed.result is None:
            raise RuntimeError(completed.error or "Real-take import did not succeed")
        duplicate = harness.enqueue(
            user_id="s05-real-take-import",
            kind="music.generate",
            payload={
                "generation_request": request_to_json(request),
                "plan_revision_id": "rev_s05_operator_approved_probe",
            },
            idempotency_key=f"s05-real-import:{request.contract_checksum}",
        )
        if duplicate.id != job.id:
            raise RuntimeError("Idempotent retry created a duplicate durable job")
        durable_result = completed.result
    finally:
        await harness.close()

    reopened = SqliteJobStore(settings.database_path)
    try:
        recovered = reopened.get(job.id)
    finally:
        reopened.close()
    if recovered.result != durable_result:
        raise RuntimeError("Durable generation result changed after database restart")
    serialized = json.dumps(durable_result, ensure_ascii=False, sort_keys=True)
    if "Morning light" in serialized or "dream-pop" in serialized:
        raise RuntimeError("Private creative text leaked into the durable result")

    generation = _object(durable_result, "generation")
    raw_takes = generation.get("takes")
    if not isinstance(raw_takes, list):
        raise ValueError("Durable generation takes are unavailable")
    takes: list[JsonValue] = []
    preferred_persisted_take_id: str | None = None
    for item in raw_takes:
        if not isinstance(item, dict):
            raise ValueError("Durable generation take is invalid")
        manifest = _object(item, "manifest")
        settings_value = _object(manifest, "settings")
        backend_take_id = _string(settings_value, "backend_take_id")
        audio_artifact = _object(item, "audio_artifact")
        relative_path = _string(audio_artifact, "relative_path")
        artifact_path = settings.artifact_root / relative_path
        digest = _sha256(artifact_path)
        if digest != _string(audio_artifact, "sha256"):
            raise RuntimeError("Persisted artifact checksum changed")
        persisted_take_id = _string(_object(item, "take"), "id")
        if backend_take_id == preferred_take_id:
            preferred_persisted_take_id = persisted_take_id
        takes.append(
            {
                "backend_take_id": backend_take_id,
                "persisted_take_id": persisted_take_id,
                "audio_sha256": digest,
                "audio_size_bytes": artifact_path.stat().st_size,
                "artifact_relative_path": relative_path,
            }
        )
    if preferred_persisted_take_id is None:
        raise ValueError("Preferred take is not present in the imported batch")

    return {
        "schema_version": 1,
        "recorded_at": "2026-11-13",
        "status": "succeeded",
        "gpu_allocated": False,
        "request_id": request.request_id,
        "contract_checksum": request.contract_checksum,
        "job_id": job.id,
        "job_state": recovered.state.value,
        "batch_id": _string(generation, "batch_id"),
        "restart_verified": True,
        "idempotent_retry_verified": True,
        "private_creative_text_persisted": False,
        "takes": takes,
        "user_preference": {
            "backend_take_id": preferred_take_id,
            "persisted_take_id": preferred_persisted_take_id,
            "subjective_music_quality": "good",
        },
    }


def _prepare_runner_result(
    *,
    source_root: Path,
    transfer_root: Path,
    transfer_id: str,
    request_id: str,
    run_record: JsonObject,
    verification_record: JsonObject,
    contract_checksum: str,
) -> dict[str, Any]:
    transfer_root.mkdir(parents=True)
    verification_by_id = {
        _string(item, "take_id"): item for item in _object_list(verification_record, "takes")
    }
    generated: list[GeneratedTake] = []
    for item in _object_list(run_record, "takes"):
        take_id = _string(item, "take_id")
        verification = verification_by_id.get(take_id)
        if verification is None:
            raise ValueError(f"Missing local verification for {take_id}")
        relative_path = _string(item, "relative_path")
        source = (source_root / relative_path).resolve()
        if not source.is_relative_to(source_root.resolve()):
            raise ValueError("Source artifact escaped its configured root")
        destination = (transfer_root / relative_path).resolve()
        if not destination.is_relative_to(transfer_root.resolve()):
            raise ValueError("Transfer artifact escaped its configured root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if _sha256(destination) != _string(item, "sha256"):
            raise ValueError("Source artifact does not match the real T4 record")
        validation = _object(verification, "validation")
        generated.append(
            GeneratedTake(
                take_id=take_id,
                seed=_integer(item, "seed"),
                relative_path=relative_path,
                media_type=_string(item, "media_type"),
                size_bytes=_integer(item, "size_bytes"),
                sha256=_string(item, "sha256"),
                duration_seconds=_number(item, "duration_seconds"),
                sample_rate_hz=_integer(item, "sample_rate_hz"),
                channels=_integer(item, "channels"),
                validation=validation,
            )
        )
    result = GenerationBatchResult(
        backend_id=_string(run_record, "backend_id"),
        backend_revision=_string(run_record, "backend_revision"),
        contract_checksum=contract_checksum,
        takes=tuple(generated),
        metrics={"source": "existing_real_t4_batch", "request_id": request_id},
    )
    return {
        "status": "succeeded",
        "result": result_to_json(result),
        "_transfer_id": transfer_id,
        "_environment": {
            "provider": _string(run_record, "provider"),
            "gpu": _string(run_record, "gpu"),
            "source_evidence": "docs/evidence/S05/heartmula-t4-run.json",
        },
    }


async def _wait_until(predicate: Callable[[], bool], timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Durable import did not reach a terminal state")
        await asyncio.sleep(0.01)


def _load_json(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return cast(JsonObject, value)


def _write_json_atomic(path: Path, value: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _object(payload: Mapping[str, object], name: str) -> JsonObject:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast(JsonObject, value)


def _object_list(payload: Mapping[str, object], name: str) -> tuple[JsonObject, ...]:
    value = payload.get(name)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{name} must be an object array")
    return tuple(cast(JsonObject, item) for item in value)


def _string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _integer(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return value


def _number(payload: Mapping[str, object], name: str) -> float:
    value = payload.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    return float(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run-record", type=Path, required=True)
    parser.add_argument("--verification-record", type=Path, required=True)
    parser.add_argument("--preferred-take-id", default="take_b")
    parser.add_argument("--evidence-output", type=Path, required=True)
    args = parser.parse_args()
    if args.output_root.exists():
        raise FileExistsError(f"Refusing to overwrite persistence evidence: {args.output_root}")
    args.output_root.mkdir(parents=True)
    evidence = asyncio.run(
        import_real_takes(
            source_root=args.source_root,
            output_root=args.output_root,
            run_record=_load_json(args.run_record),
            verification_record=_load_json(args.verification_record),
            preferred_take_id=args.preferred_take_id,
        )
    )
    _write_json_atomic(args.evidence_output, evidence)
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
