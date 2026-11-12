"""Independently verify downloaded S05 HeartMuLa WAVs against the run record."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from agent_music.audio_validation import validate_pcm16_wav
from agent_music.s05_preflight import sha256_file


def load_json(path: Path) -> dict[str, Any]:
    """Load one JSON object."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return cast(dict[str, Any], value)


def verify_probe(
    *,
    record: dict[str, Any],
    artifact_root: Path,
) -> dict[str, Any]:
    """Verify size, checksum, and PCM WAV properties for every recorded take."""

    expected_duration = record.get("request", {}).get("duration_seconds")
    if not isinstance(expected_duration, (int, float)) or isinstance(expected_duration, bool):
        raise TypeError("Run record duration is invalid")
    takes = record.get("takes")
    if not isinstance(takes, list) or len(takes) != 2:
        raise ValueError("Run record must contain exactly two takes")
    resolved_root = artifact_root.resolve()
    verified: list[dict[str, Any]] = []
    observed_checksums: set[str] = set()
    observed_seeds: set[int] = set()
    for raw_take in takes:
        if not isinstance(raw_take, dict):
            raise TypeError("Run take must be an object")
        take = cast(dict[str, Any], raw_take)
        relative_path = take.get("relative_path")
        if not isinstance(relative_path, str):
            raise TypeError("Run take path is invalid")
        path = (resolved_root / relative_path).resolve()
        if resolved_root not in path.parents:
            raise ValueError("Run take escaped the artifact root")
        expected_size = take.get("size_bytes")
        expected_sha256 = take.get("sha256")
        seed = take.get("seed")
        if not isinstance(expected_size, int) or isinstance(expected_size, bool):
            raise TypeError("Run take size is invalid")
        if not isinstance(expected_sha256, str):
            raise TypeError("Run take checksum is invalid")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise TypeError("Run take seed is invalid")
        observed_sha256 = sha256_file(path)
        if path.stat().st_size != expected_size or observed_sha256 != expected_sha256:
            raise ValueError(f"Artifact integrity mismatch: {relative_path}")
        validation = validate_pcm16_wav(
            path,
            expected_duration_seconds=float(expected_duration),
        )
        if validation["passed"] is not True:
            raise ValueError(f"Artifact WAV validation failed: {relative_path}")
        if validation["sample_rate_hz"] != take.get("sample_rate_hz"):
            raise ValueError(f"Artifact sample rate mismatch: {relative_path}")
        if validation["channels"] != take.get("channels"):
            raise ValueError(f"Artifact channel mismatch: {relative_path}")
        observed_checksums.add(observed_sha256)
        observed_seeds.add(seed)
        verified.append(
            {
                "take_id": take.get("take_id"),
                "relative_path": relative_path,
                "sha256": observed_sha256,
                "size_bytes": expected_size,
                "validation": validation,
            }
        )
    if len(observed_checksums) != 2 or len(observed_seeds) != 2:
        raise ValueError("Downloaded takes are not distinct")
    return {
        "schema_version": 1,
        "request_id": record.get("request_id"),
        "passed": True,
        "takes": verified,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify_probe(
        record=load_json(args.record),
        artifact_root=args.artifact_root,
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
