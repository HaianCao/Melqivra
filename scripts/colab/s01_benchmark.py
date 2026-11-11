"""Run one pinned ACE-Step S01 T4 benchmark and validate its WAV artifact."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

SOURCE_REVISION = "dce621408bee8c31b4fcf4811682eb9359e1bc94"
DEFAULT_SOURCE = Path("/content/melqivra-s01/ACE-Step-1.5")
DEFAULT_OUTPUT = Path("/content/melqivra-s01/benchmark")


def atomic_write_json(path: Path, value: object) -> None:
    """Atomically write a UTF-8 JSON document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 digest of a file."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected a JSON object: {path}")
    return cast(dict[str, Any], value)


def select_fixture(corpus_path: Path, fixture_id: str) -> dict[str, Any]:
    """Select one project-authored fixture by its stable ID."""

    corpus = load_json(corpus_path)
    briefs = corpus.get("briefs")
    if not isinstance(briefs, list):
        raise ValueError("Fixture corpus has no briefs array")
    for brief in briefs:
        if isinstance(brief, dict) and brief.get("id") == fixture_id:
            return cast(dict[str, Any], brief)
    raise ValueError(f"Unknown fixture ID: {fixture_id}")


def fixture_caption(fixture: dict[str, Any]) -> str:
    """Build a deterministic caption from non-copyrighted fixture fields."""

    genres = ", ".join(str(value) for value in fixture["genres"])
    moods = ", ".join(str(value) for value in fixture["moods"])
    return (
        f"{fixture['concept']} Thể loại: {genres}. Cảm xúc: {moods}. "
        f"Phần trình diễn: {fixture['vocal']}."
    )


def fixture_generation_inputs(fixture: dict[str, Any]) -> tuple[str, str, bool]:
    """Resolve deterministic caption, lyrics, and instrumental mode for a fixture."""

    caption = fixture_caption(fixture)
    lyrics = fixture.get("lyrics")
    if lyrics is None:
        return caption, "[Instrumental]", True
    if not isinstance(lyrics, str) or not lyrics.strip():
        raise ValueError("Fixture lyrics must be a non-empty string when provided")
    return caption, lyrics, False


def sha256_json(value: object) -> str:
    """Hash one JSON-compatible value with a stable UTF-8 representation."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def lm_conditioning_evidence(result: Any, *, lm_requested: bool) -> dict[str, object]:
    """Record privacy-safe fingerprints of LM metadata and generated audio codes."""

    extra_outputs = result.extra_outputs
    if not isinstance(extra_outputs, dict):
        raise TypeError("Generation extra_outputs must be an object")
    metadata = extra_outputs.get("lm_metadata")

    audios = result.audios
    if not isinstance(audios, list) or not audios or not isinstance(audios[0], dict):
        raise RuntimeError("Generation returned no audio parameters")
    params = audios[0].get("params")
    if not isinstance(params, dict):
        raise RuntimeError("Generation returned invalid audio parameters")
    audio_codes = params.get("audio_codes")
    audio_codes_present = isinstance(audio_codes, str) and bool(audio_codes.strip())
    metadata_present = isinstance(metadata, dict) and bool(metadata)

    if lm_requested and not audio_codes_present:
        raise RuntimeError("LM ran without returning generated audio codes")

    return {
        "lm_metadata_present": metadata_present,
        "lm_metadata_sha256": sha256_json(metadata) if metadata_present else None,
        "lm_metadata_fields": sorted(metadata) if isinstance(metadata, dict) else [],
        "lm_audio_codes_present": audio_codes_present,
        "lm_audio_codes_sha256": sha256_json(audio_codes) if audio_codes_present else None,
        "lm_audio_codes_length": len(audio_codes) if isinstance(audio_codes, str) else 0,
    }


def _parse_ffmpeg_loudness(stderr: str) -> tuple[float, float]:
    integrated_matches = re.findall(r"\bI:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+LUFS", stderr)
    peak_matches = re.findall(r"\bPeak:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+dBFS", stderr)
    if not integrated_matches or not peak_matches:
        raise ValueError("ffmpeg ebur128 output did not contain integrated LUFS and true peak")
    integrated = float(integrated_matches[-1])
    peak = float(peak_matches[-1])
    if not math.isfinite(integrated) or not math.isfinite(peak):
        raise ValueError("Audio loudness metrics are not finite")
    return integrated, peak


def analyze_audio(path: Path) -> dict[str, object]:
    """Validate audio samples and measure loudness with ffmpeg ebur128."""

    soundfile = importlib.import_module("soundfile")
    numpy = importlib.import_module("numpy")
    samples, sample_rate = soundfile.read(str(path), always_2d=True, dtype="float64")
    finite_samples = bool(numpy.isfinite(samples).all())
    absolute_peak = float(numpy.max(numpy.abs(samples))) if samples.size else 0.0
    loudness = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-filter_complex",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    integrated_lufs, true_peak_dbfs = _parse_ffmpeg_loudness(loudness.stderr)
    duration_seconds = float(len(samples) / int(sample_rate))
    return {
        "duration_seconds": duration_seconds,
        "sample_rate_hz": int(sample_rate),
        "channels": int(samples.shape[1]),
        "finite_samples": finite_samples,
        "silent": absolute_peak < 0.0001,
        "clipped": absolute_peak >= 0.999969,
        "true_peak_dbfs": true_peak_dbfs,
        "integrated_lufs": integrated_lufs,
    }


def _nvidia_environment() -> dict[str, str | int]:
    completed = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    name, memory_mib, driver = [part.strip() for part in completed.stdout.split(",")]
    return {
        "gpu_name": name,
        "gpu_memory_bytes": int(memory_mib) * 1024 * 1024,
        "driver": driver,
    }


def _audio_path(result: Any) -> Path:
    audios = result.audios
    if not isinstance(audios, list) or not audios:
        raise RuntimeError("Generation returned no audio artifacts")
    first = audios[0]
    if not isinstance(first, dict) or not first.get("path"):
        raise RuntimeError("Generation returned an audio without a path")
    path = Path(str(first["path"]))
    if not path.is_file():
        raise FileNotFoundError(f"Generated audio is missing: {path}")
    return path


def sanitize_runtime_environment() -> None:
    """Remove notebook-only settings that break the isolated locked runtime."""

    os.environ.pop("MPLBACKEND", None)


def end_to_end_total(runtime_seconds: float, args: argparse.Namespace) -> float:
    """Include earlier provision, runtime setup, and cache population phases."""

    return (
        runtime_seconds
        + float(args.provision_seconds)
        + float(args.runtime_setup_seconds)
        + float(args.model_download_seconds)
    )


def run_benchmark(args: argparse.Namespace) -> dict[str, object]:
    """Load pinned models, generate one sample, and return schema-ready evidence."""

    sanitize_runtime_environment()
    started_wall = datetime.now(UTC)
    started = time.monotonic()
    source = args.source.resolve()
    actual_revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    if actual_revision != SOURCE_REVISION:
        raise RuntimeError(f"Unexpected source revision: {actual_revision}")
    fixture = select_fixture(args.fixtures, args.fixture_id)
    caption, lyrics, instrumental = fixture_generation_inputs(fixture)
    cache_manifest = load_json(args.cache_manifest)
    if cache_manifest.get("status") != "succeeded":
        raise RuntimeError("Model cache manifest is not successful")

    sys.path.insert(0, str(source))
    handler_module = importlib.import_module("acestep.handler")
    llm_module = importlib.import_module("acestep.llm_inference")
    inference_module = importlib.import_module("acestep.inference")
    torch = importlib.import_module("torch")

    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    load_started = time.monotonic()
    dit_handler = handler_module.AceStepHandler()
    status, initialized = dit_handler.initialize_service(
        project_root=str(source),
        config_path="acestep-v15-turbo",
        device="cuda",
        offload_to_cpu=False,
    )
    if not initialized:
        raise RuntimeError(f"DiT initialization failed: {status}")

    llm_handler: Any = None
    lm_initialized = False
    if args.lm:
        llm_handler = llm_module.LLMHandler()
        llm_status, lm_initialized = llm_handler.initialize(
            checkpoint_dir=str(source / "checkpoints"),
            lm_model_path="acestep-5Hz-lm-0.6B",
            backend="vllm",
            device="cuda",
            offload_to_cpu=False,
            dtype=None,
        )
        if not lm_initialized:
            raise RuntimeError(f"LM initialization failed: {llm_status}")
    model_load = time.monotonic() - load_started

    params = inference_module.GenerationParams(
        task_type="text2music",
        caption=caption,
        lyrics=lyrics,
        instrumental=instrumental,
        vocal_language="vi",
        bpm=int(fixture["tempo_bpm"]),
        duration=float(args.duration),
        inference_steps=8,
        shift=3.0,
        guidance_scale=1.0,
        seed=int(args.seed),
        thinking=bool(args.lm),
        use_cot_metas=bool(args.lm),
        use_cot_caption=bool(args.lm),
        use_cot_language=bool(args.lm),
    )
    config = inference_module.GenerationConfig(
        batch_size=1,
        use_random_seed=False,
        seeds=[int(args.seed)],
        audio_format="wav",
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    inference_started = time.monotonic()
    result = inference_module.generate_music(
        dit_handler,
        llm_handler,
        params=params,
        config=config,
        save_dir=str(args.output_dir),
    )
    inference_seconds = time.monotonic() - inference_started
    if not result.success:
        raise RuntimeError(f"Generation failed: {result.status_message}")
    audio_path = _audio_path(result)
    audio_validation = analyze_audio(audio_path)
    if not audio_validation["finite_samples"]:
        raise RuntimeError("Generated audio contains NaN or infinite samples")
    if audio_validation["silent"]:
        raise RuntimeError("Generated audio is silent")
    measured_duration = audio_validation["duration_seconds"]
    if not isinstance(measured_duration, (int, float)):
        raise TypeError("Measured audio duration is not numeric")
    duration_error = abs(float(measured_duration) - float(args.duration))
    if duration_error > 1.0:
        raise RuntimeError(f"Generated duration differs by {duration_error:.3f} seconds")

    time_costs = result.extra_outputs.get("time_costs", {})
    lm_used = bool(
        args.lm
        and lm_initialized
        and isinstance(time_costs, dict)
        and any(
            str(key).startswith("lm_") and isinstance(value, (int, float)) and value > 0
            for key, value in time_costs.items()
        )
    )
    if args.lm and not lm_used:
        raise RuntimeError("LM was requested and initialized but no LM timing proves it was used")
    conditioning = lm_conditioning_evidence(result, lm_requested=bool(args.lm))

    gpu = _nvidia_environment()
    total = time.monotonic() - started
    return {
        "schema_version": 1,
        "run_id": args.run_id,
        "started_at": started_wall.isoformat(),
        "environment": {
            **gpu,
            "cuda": str(torch.version.cuda),
            "python": platform.python_version(),
            "pytorch": str(torch.__version__),
            "colab_cli": args.colab_cli,
            "session_name": args.session_name,
        },
        "source": {
            "repository": "https://github.com/ace-step/ACE-Step-1.5.git",
            "revision": actual_revision,
        },
        "assets": [
            {
                "id": asset["id"],
                "repository": asset["repository"],
                "revision": asset["revision"],
                "sha256": asset["sha256"],
            }
            for asset in cache_manifest["verified_assets"]
        ],
        "request": {
            "fixture_id": args.fixture_id,
            "task_type": "text2music",
            "duration_seconds": float(args.duration),
            "seed": int(args.seed),
            "diffusion_steps": 8,
            "shift": 3.0,
            "batch_size": 1,
            "precision": "upstream_default",
            "lm_requested": bool(args.lm),
            "instrumental": instrumental,
            "caption_sha256": sha256_json(caption),
            "lyrics_sha256": sha256_json(lyrics),
        },
        "capabilities": {
            "lm_initialized": lm_initialized,
            "lm_used": lm_used,
            "task_supported": True,
            "failure_reason": None,
            **conditioning,
        },
        "timings_seconds": {
            "provision": float(args.provision_seconds),
            "runtime_setup": float(args.runtime_setup_seconds),
            "model_download_or_copy": float(args.model_download_seconds),
            "model_load": model_load,
            "inference": inference_seconds,
            "decode": float(time_costs.get("dit_vae_decode_time", 0.0))
            if isinstance(time_costs, dict)
            else 0.0,
            "transfer": 0.0,
            "total": end_to_end_total(total, args),
        },
        "peak_vram_bytes": int(torch.cuda.max_memory_allocated()),
        "audio_validation": audio_validation,
        "artifacts": [
            {
                "path": audio_path.name,
                "media_type": "audio/wav",
                "size_bytes": audio_path.stat().st_size,
                "sha256": sha256_file(audio_path),
            }
        ],
        "result": "passed",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--fixture-id", default="vi-014")
    parser.add_argument("--cache-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=20261111)
    parser.add_argument("--lm", action="store_true")
    parser.add_argument("--session-name", default="melqivra-t4")
    parser.add_argument("--colab-cli", default="0.8.0")
    parser.add_argument("--provision-seconds", type=float, default=0.0)
    parser.add_argument("--runtime-setup-seconds", type=float, default=0.0)
    parser.add_argument("--model-download-seconds", type=float, default=0.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        evidence = run_benchmark(args)
        atomic_write_json(args.result, evidence)
        print(json.dumps({"status": "passed", "result": str(args.result)}), flush=True)
        return 0
    except Exception as error:
        atomic_write_json(
            args.result.with_suffix(".error.json"),
            {"status": "failed", "error": f"{type(error).__name__}: {error}"},
        )
        print(json.dumps({"status": "failed", "error": str(error)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
