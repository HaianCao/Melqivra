"""Independently verify one downloaded ACE reference-probe artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from agent_music.audio_validation import validate_pcm16_wav
from agent_music.s05_ace_probe import ACE_FLOAT32_REVISION
from agent_music.s05_preflight import sha256_file


def load_json(path: Path) -> dict[str, Any]:
    """Load one JSON object."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return cast(dict[str, Any], value)


def verify_ace_reference_probe(
    *,
    record: dict[str, Any],
    audio_path: Path,
    expected_run_id: str,
) -> dict[str, Any]:
    """Verify exact pins, integrity, and PCM properties without private text."""

    if record.get("result") != "passed" or record.get("run_id") != expected_run_id:
        raise ValueError("ACE probe record did not pass for the expected run")
    source = record.get("source")
    environment = record.get("environment")
    request = record.get("request")
    artifacts = record.get("artifacts")
    if not all(isinstance(value, dict) for value in (source, environment, request)):
        raise TypeError("ACE probe record has invalid source, environment, or request")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise ValueError("ACE probe record must declare exactly one artifact")
    artifact = artifacts[0]
    if not isinstance(artifact, dict):
        raise TypeError("ACE probe artifact must be an object")
    source_value = cast(dict[str, Any], source)
    environment_value = cast(dict[str, Any], environment)
    request_value = cast(dict[str, Any], request)
    artifact_value = cast(dict[str, Any], artifact)
    if source_value.get("revision") != ACE_FLOAT32_REVISION:
        raise ValueError("ACE probe source revision mismatch")
    if (
        environment_value.get("provider") != "modal"
        or environment_value.get("gpu_name") != "Tesla T4"
    ):
        raise ValueError("ACE probe provider or GPU mismatch")
    if (
        request_value.get("precision") != "float32"
        or request_value.get("observed_precision") != "float32"
    ):
        raise ValueError("ACE probe did not honor float32 precision")
    if request_value.get("seed") != 12345 or request_value.get("vocal_language") != "vi":
        raise ValueError("ACE probe seed or language mismatch")
    duration = request_value.get("duration_seconds")
    expected_size = artifact_value.get("size_bytes")
    expected_sha256 = artifact_value.get("sha256")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        raise TypeError("ACE probe duration is invalid")
    if not isinstance(expected_size, int) or isinstance(expected_size, bool):
        raise TypeError("ACE probe artifact size is invalid")
    if not isinstance(expected_sha256, str):
        raise TypeError("ACE probe artifact checksum is invalid")
    observed_sha256 = sha256_file(audio_path)
    if audio_path.stat().st_size != expected_size or observed_sha256 != expected_sha256:
        raise ValueError("ACE probe artifact integrity mismatch")
    validation = validate_pcm16_wav(
        audio_path,
        expected_duration_seconds=float(duration),
    )
    if validation["passed"] is not True:
        raise ValueError("ACE probe artifact WAV validation failed")
    return {
        "schema_version": 1,
        "run_id": expected_run_id,
        "passed": True,
        "source_revision": source_value["revision"],
        "provider": environment_value["provider"],
        "gpu_name": environment_value["gpu_name"],
        "requested_precision": request_value["precision"],
        "observed_precision": request_value["observed_precision"],
        "reference_audio_sha256": request_value.get("reference_audio_sha256"),
        "artifact": {
            "sha256": observed_sha256,
            "size_bytes": expected_size,
            "validation": validation,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify_ace_reference_probe(
        record=load_json(args.record),
        audio_path=args.audio,
        expected_run_id=args.run_id,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
