"""Run the pinned 0.6B LM contract phase without invoking vocal DiT generation."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import platform
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

SOURCE_REVISION = "dce621408bee8c31b4fcf4811682eb9359e1bc94"


def atomic_write_json(path: Path, value: object) -> None:
    """Atomically write a UTF-8 JSON document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_json(value: object) -> str:
    """Return a stable SHA-256 for a JSON-compatible value."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected a JSON object: {path}")
    return cast(dict[str, Any], value)


def select_fixture(corpus_path: Path, fixture_id: str) -> dict[str, Any]:
    corpus = load_json(corpus_path)
    briefs = corpus.get("briefs")
    if not isinstance(briefs, list):
        raise ValueError("Fixture corpus has no briefs array")
    for brief in briefs:
        if isinstance(brief, dict) and brief.get("id") == fixture_id:
            return cast(dict[str, Any], brief)
    raise ValueError(f"Unknown fixture ID: {fixture_id}")


def fixture_caption(fixture: dict[str, Any]) -> str:
    genres = ", ".join(str(value) for value in fixture["genres"])
    moods = ", ".join(str(value) for value in fixture["moods"])
    return (
        f"{fixture['concept']} Thể loại: {genres}. Cảm xúc: {moods}. "
        f"Phần trình diễn: {fixture['vocal']}."
    )


def contract_fingerprints(metadata: object, audio_codes: object) -> dict[str, object]:
    """Return privacy-safe fingerprints for LM outputs."""

    metadata_present = isinstance(metadata, dict) and bool(metadata)
    audio_codes_present = isinstance(audio_codes, str) and bool(audio_codes.strip())
    return {
        "lm_metadata_present": metadata_present,
        "lm_metadata_sha256": sha256_json(metadata) if metadata_present else None,
        "lm_metadata_fields": sorted(metadata) if isinstance(metadata, dict) else [],
        "lm_audio_codes_present": audio_codes_present,
        "lm_audio_codes_sha256": sha256_json(audio_codes) if audio_codes_present else None,
        "lm_audio_codes_length": len(audio_codes) if isinstance(audio_codes, str) else 0,
    }


def nvidia_environment() -> dict[str, str | int]:
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


def run_contract(args: argparse.Namespace) -> dict[str, object]:
    """Initialize the pinned LM and record its generated conditioning contract."""

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
    lyrics = fixture.get("lyrics")
    if not isinstance(lyrics, str) or not lyrics.strip():
        raise ValueError("LM-sensitive fixture requires non-empty project-authored lyrics")
    caption = fixture_caption(fixture)

    sys.path.insert(0, str(source))
    llm_module = importlib.import_module("acestep.llm_inference")
    torch = importlib.import_module("torch")
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    load_started = time.monotonic()
    handler = llm_module.LLMHandler()
    status, initialized = handler.initialize(
        checkpoint_dir=str(source / "checkpoints"),
        lm_model_path="acestep-5Hz-lm-0.6B",
        backend="vllm",
        device="cuda",
        offload_to_cpu=False,
        dtype=None,
    )
    model_load = time.monotonic() - load_started
    if not initialized:
        raise RuntimeError(f"LM initialization failed: {status}")

    inference_started = time.monotonic()
    result = handler.generate_with_stop_condition(
        caption=caption,
        lyrics=lyrics,
        infer_type="llm_dit",
        temperature=0.85,
        cfg_scale=2.0,
        negative_prompt="NO USER INPUT",
        top_k=None,
        top_p=0.9,
        target_duration=float(args.duration),
        user_metadata={
            "bpm": int(fixture["tempo_bpm"]),
            "duration": int(args.duration),
        },
        use_cot_caption=True,
        use_cot_language=True,
        use_cot_metas=True,
        use_constrained_decoding=True,
        constrained_decoding_debug=False,
        batch_size=1,
        seeds=[int(args.seed)],
        progress=None,
    )
    inference_seconds = time.monotonic() - inference_started
    if not isinstance(result, dict) or not result.get("success", False):
        error = result.get("error", "unknown LM error") if isinstance(result, dict) else result
        raise RuntimeError(f"LM generation failed: {error}")
    fingerprints = contract_fingerprints(result.get("metadata"), result.get("audio_codes"))
    extra_outputs = result.get("extra_outputs", {})
    time_costs = extra_outputs.get("time_costs", {}) if isinstance(extra_outputs, dict) else {}
    contract_valid = bool(
        fingerprints["lm_metadata_present"] and fingerprints["lm_audio_codes_present"]
    )

    return {
        "schema_version": 1,
        "run_id": args.run_id,
        "started_at": started_wall.isoformat(),
        "environment": {
            **nvidia_environment(),
            "cuda": str(torch.version.cuda),
            "python": platform.python_version(),
            "pytorch": str(torch.__version__),
            "provider": args.provider,
            "provider_cli": args.provider_cli,
            "session_name": args.session_name,
        },
        "source": {
            "repository": "https://github.com/ace-step/ACE-Step-1.5.git",
            "revision": actual_revision,
        },
        "request": {
            "fixture_id": args.fixture_id,
            "duration_seconds": float(args.duration),
            "seed": int(args.seed),
            "caption_sha256": sha256_json(caption),
            "lyrics_sha256": sha256_json(lyrics),
        },
        "capabilities": {
            "lm_initialized": True,
            "lm_used": True,
            "contract_valid": contract_valid,
            "failure_class": None if contract_valid else "deterministic_contract_invalid",
            "failure_reason": None
            if contract_valid
            else "LM returned empty metadata and/or audio codes",
            **fingerprints,
        },
        "timings_seconds": {
            "model_load": model_load,
            "inference": inference_seconds,
            "lm_phase1": float(time_costs.get("phase1_time", 0.0))
            if isinstance(time_costs, dict)
            else 0.0,
            "lm_phase2": float(time_costs.get("phase2_time", 0.0))
            if isinstance(time_costs, dict)
            else 0.0,
            "total": time.monotonic() - started,
        },
        "peak_vram_bytes": int(torch.cuda.max_memory_allocated()),
        "result": "passed" if contract_valid else "failed",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--fixture-id", default="vi-020")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=20261111)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--provider", default="colab")
    parser.add_argument("--provider-cli")
    parser.add_argument("--session-name", default="melqivra-t4")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        evidence = run_contract(args)
        atomic_write_json(args.result, evidence)
        print(
            json.dumps(
                {
                    "status": "recorded",
                    "outcome": evidence["result"],
                    "result": str(args.result),
                }
            ),
            flush=True,
        )
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
