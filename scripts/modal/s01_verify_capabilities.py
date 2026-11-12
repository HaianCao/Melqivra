"""Verify downloaded S01 reference, repaint, OOM, and recovery evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from agent_music.s01_reproduction import (
    compare_reference_artifact,
    compare_repaint_artifact,
    sha256_file,
)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return cast(dict[str, Any], value)


def verify_record_artifact(record: dict[str, Any], artifact: Path) -> None:
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise RuntimeError("Expected exactly one recorded artifact")
    expected = artifacts[0]
    if not isinstance(expected, dict):
        raise RuntimeError("Invalid artifact record")
    if artifact.stat().st_size != expected.get("size_bytes"):
        raise RuntimeError(f"Artifact size mismatch: {artifact.name}")
    if sha256_file(artifact) != expected.get("sha256"):
        raise RuntimeError(f"Artifact checksum mismatch: {artifact.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capability-audit", type=Path, required=True)
    parser.add_argument("--source-artifact", type=Path, required=True)
    parser.add_argument("--reference-record", type=Path, required=True)
    parser.add_argument("--reference-artifact", type=Path, required=True)
    parser.add_argument("--repaint-record", type=Path, required=True)
    parser.add_argument("--repaint-artifact", type=Path, required=True)
    parser.add_argument("--oom-record", type=Path, required=True)
    parser.add_argument("--recovery-record", type=Path, required=True)
    parser.add_argument("--recovery-artifact", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()

    capability_audit = load_json(args.capability_audit)
    reference_record = load_json(args.reference_record)
    repaint_record = load_json(args.repaint_record)
    oom_record = load_json(args.oom_record)
    recovery_record = load_json(args.recovery_record)
    for record, artifact in (
        (reference_record, args.reference_artifact),
        (repaint_record, args.repaint_artifact),
        (recovery_record, args.recovery_artifact),
    ):
        verify_record_artifact(record, artifact)

    source_sha256 = sha256_file(args.source_artifact)
    if reference_record["request"].get("reference_audio_sha256") != source_sha256:
        raise RuntimeError("Reference request does not identify the source artifact")
    repaint_request = repaint_record["request"]
    if repaint_request.get("source_audio_sha256") != source_sha256:
        raise RuntimeError("Repaint request does not identify the source artifact")
    if oom_record.get("result") != "expected_failure":
        raise RuntimeError("OOM probe did not record its expected failure")
    failure = oom_record.get("failure")
    if not isinstance(failure, dict) or failure.get("class") != "cuda_out_of_memory":
        raise RuntimeError("OOM probe has the wrong failure classification")
    if failure.get("false_success") is not False or oom_record.get("artifacts") != []:
        raise RuntimeError("OOM probe recorded a false success or partial artifact")
    if recovery_record.get("result") != "passed":
        raise RuntimeError("Post-OOM recovery generation did not pass")

    reference = compare_reference_artifact(
        source=args.source_artifact,
        generated=args.reference_artifact,
    )
    repaint = compare_repaint_artifact(
        source=args.source_artifact,
        generated=args.repaint_artifact,
        repaint_start_seconds=float(repaint_request["repainting_start_seconds"]),
        repaint_end_seconds=float(repaint_request["repainting_end_seconds"]),
    )
    result = {
        "schema_version": 1,
        "status": "passed",
        "capability_audit": capability_audit,
        "reference": reference,
        "repaint": repaint,
        "oom": {
            "classified": True,
            "failure_class": "cuda_out_of_memory",
            "partial_artifacts": [],
            "cleanup": oom_record["memory"],
        },
        "recovery": {
            "passed": True,
            "artifact_sha256": sha256_file(args.recovery_artifact),
            "audio_validation": recovery_record["audio_validation"],
        },
    }
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
