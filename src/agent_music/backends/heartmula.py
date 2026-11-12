"""Pinned HeartMuLa candidate adapter; not accepted before real T4 evidence."""

from __future__ import annotations

import hashlib
import importlib
import shutil
import subprocess
from pathlib import Path
from typing import Any, Protocol, TypedDict, cast

from agent_music.audio_validation import validate_pcm16_wav
from agent_music.domain import JsonObject, JsonValue
from agent_music.generation import (
    GeneratedTake,
    GenerationBatchResult,
    GenerationContractError,
    MusicBackendDescriptor,
    MusicGenerationRequest,
    ProgressCallback,
)

HEARTMULA_SOURCE_REVISION = "5858ca8f1ffe58d62be7c007ea55f1033c5ca456"
HEARTMULA_MODEL_ID = (
    "HeartMuLa/HeartMuLa-oss-3B-happy-new-year@00a79ac9477f64593eba2ca96a25807cb2f55be6"
)
HEARTCODEC_MODEL_ID = "HeartMuLa/HeartCodec-oss-20260123@f889dab0532cfa4bf459f2a3367eb6d346b8eeda"

HEARTMULA_DESCRIPTOR = MusicBackendDescriptor(
    backend_id="heartmula_3b",
    source_revision=HEARTMULA_SOURCE_REVISION,
    model_ids=(HEARTMULA_MODEL_ID, HEARTCODEC_MODEL_ID),
    license_ids=("Apache-2.0",),
    operations=frozenset({"text2music"}),
    supported_languages=frozenset({"zh", "en", "ja", "ko", "es"}),
    supports_vocals=True,
    supports_instrumental=False,
    min_duration_seconds=20.0,
    max_duration_seconds=240.0,
)


class HeartMuLaEngine(Protocol):
    """Narrow external-runtime interface used by the product adapter."""

    def generate(
        self,
        *,
        lyrics: str,
        tags: str,
        output_path: Path,
        duration_seconds: float,
        seed: int,
        topk: int,
        temperature: float,
        cfg_scale: float,
    ) -> JsonObject: ...


class HeartMuLaSettings(TypedDict):
    topk: int
    temperature: float
    cfg_scale: float


class HeartLibEngine:
    """Lazy import wrapper around the exact upstream HeartMuLa pipeline."""

    def __init__(self, model_root: Path) -> None:
        self._model_root = model_root
        self._pipeline: Any | None = None

    def generate(
        self,
        *,
        lyrics: str,
        tags: str,
        output_path: Path,
        duration_seconds: float,
        seed: int,
        topk: int,
        temperature: float,
        cfg_scale: float,
    ) -> JsonObject:
        torch = importlib.import_module("torch")
        if not cast(bool, torch.cuda.is_available()):
            raise RuntimeError("HeartMuLa requires an available CUDA device")
        gpu_name = str(torch.cuda.get_device_name(0))
        if "T4" not in gpu_name:
            raise RuntimeError(f"Expected Tesla T4, received {gpu_name}")
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        pipeline = self._get_pipeline(torch)
        raw_path = output_path.with_suffix(".raw.wav")
        pipeline(
            {"lyrics": lyrics, "tags": tags},
            max_audio_length_ms=round(duration_seconds * 1000),
            save_path=str(raw_path),
            topk=topk,
            temperature=temperature,
            cfg_scale=cfg_scale,
        )
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg is required to normalize HeartMuLa output")
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(raw_path),
                "-c:a",
                "pcm_s16le",
                str(output_path),
            ],
            check=True,
        )
        raw_path.unlink(missing_ok=True)
        return {
            "gpu_name": gpu_name,
            "peak_vram_bytes": int(torch.cuda.max_memory_allocated(0)),
        }

    def _get_pipeline(self, torch: Any) -> Any:
        if self._pipeline is None:
            heartlib = importlib.import_module("heartlib")
            pipeline_type = heartlib.HeartMuLaGenPipeline
            self._pipeline = pipeline_type.from_pretrained(
                str(self._model_root),
                device={"mula": torch.device("cuda"), "codec": torch.device("cuda")},
                dtype={"mula": torch.float16, "codec": torch.float32},
                version="3B",
                lazy_load=True,
            )
        return self._pipeline


class HeartMuLaBackend:
    """Translate an approved vocal contract into exact HeartMuLa calls."""

    def __init__(self, engine: HeartMuLaEngine, output_root: Path) -> None:
        self._engine = engine
        self._output_root = output_root.resolve()

    @property
    def descriptor(self) -> MusicBackendDescriptor:
        return HEARTMULA_DESCRIPTOR

    def generate(
        self,
        request: MusicGenerationRequest,
        on_progress: ProgressCallback,
    ) -> GenerationBatchResult:
        job_root = (self._output_root / request.request_id).resolve()
        if self._output_root not in job_root.parents:
            raise GenerationContractError("Generation output escaped the configured root")
        job_root.mkdir(parents=True, exist_ok=False)
        outputs: list[GeneratedTake] = []
        engine_metrics: list[JsonValue] = []
        for index, take in enumerate(request.takes):
            settings = _heartmula_settings(take.settings)
            output_path = job_root / f"{take.take_id}.wav"
            on_progress(
                {
                    "type": "stage",
                    "stage": "inference",
                    "take_id": take.take_id,
                    "progress": 0.1 + 0.7 * (index / len(request.takes)),
                }
            )
            metrics = self._engine.generate(
                lyrics=request.lyrics or "",
                tags=request.prompt,
                output_path=output_path,
                duration_seconds=request.duration_seconds,
                seed=take.seed,
                topk=settings["topk"],
                temperature=settings["temperature"],
                cfg_scale=settings["cfg_scale"],
            )
            validation = validate_pcm16_wav(
                output_path,
                expected_duration_seconds=request.duration_seconds,
            )
            if validation["passed"] is not True:
                raise GenerationContractError(f"HeartMuLa take failed validation: {take.take_id}")
            outputs.append(
                GeneratedTake(
                    take.take_id,
                    take.seed,
                    f"{request.request_id}/{output_path.name}",
                    "audio/wav",
                    output_path.stat().st_size,
                    _sha256(output_path),
                    _json_float(validation, "duration_seconds"),
                    _json_int(validation, "sample_rate_hz"),
                    _json_int(validation, "channels"),
                    validation,
                )
            )
            engine_metrics.append(metrics)
        return GenerationBatchResult(
            self.descriptor.backend_id,
            self.descriptor.source_revision,
            request.contract_checksum,
            tuple(outputs),
            {
                "take_metrics": engine_metrics,
                "translation": {
                    "tags": "approved prompt passed unchanged",
                    "lyrics": "approved lyrics passed unchanged",
                    "precision": {"music_model": "float16", "codec": "float32"},
                    "lazy_load": True,
                },
            },
        )


def _heartmula_settings(settings: JsonObject) -> HeartMuLaSettings:
    allowed = {"topk", "temperature", "cfg_scale"}
    if set(settings) != allowed:
        raise GenerationContractError(
            "HeartMuLa settings must explicitly pin topk/temperature/cfg_scale"
        )
    topk = settings["topk"]
    temperature = settings["temperature"]
    cfg_scale = settings["cfg_scale"]
    if not isinstance(topk, int) or isinstance(topk, bool) or not 1 <= topk <= 1000:
        raise GenerationContractError("HeartMuLa topk is invalid")
    if (
        not isinstance(temperature, (int, float))
        or isinstance(temperature, bool)
        or float(temperature) <= 0
    ):
        raise GenerationContractError("HeartMuLa temperature is invalid")
    if (
        not isinstance(cfg_scale, (int, float))
        or isinstance(cfg_scale, bool)
        or float(cfg_scale) <= 0
    ):
        raise GenerationContractError("HeartMuLa cfg_scale is invalid")
    return {
        "topk": topk,
        "temperature": float(temperature),
        "cfg_scale": float(cfg_scale),
    }


def _json_float(payload: JsonObject, name: str) -> float:
    value = payload.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise GenerationContractError(f"Validation field {name} is invalid")
    return float(value)


def _json_int(payload: JsonObject, name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise GenerationContractError(f"Validation field {name} is invalid")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
