"""Local verification for exact Colab-to-Modal S01 PoC reproduction."""

from __future__ import annotations

import hashlib
import wave
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class ReproductionMismatchError(RuntimeError):
    """The Modal result does not reproduce the pinned Colab technical contract."""


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash an artifact without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def wav_metadata(path: Path) -> dict[str, int | float]:
    """Read the portable PCM fields needed for transfer verification."""

    with wave.open(str(path), "rb") as stream:
        frames = stream.getnframes()
        sample_rate = stream.getframerate()
        channels = stream.getnchannels()
        sample_width = stream.getsampwidth()
    return {
        "frames": frames,
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "duration_seconds": frames / sample_rate,
    }


def compare_reproduction(
    *,
    modal_record: Mapping[str, Any],
    colab_record: Mapping[str, Any],
    local_artifact: Path,
    transfer_seconds: float,
) -> dict[str, object]:
    """Verify exact inputs and a near-identical validated WAV output contract."""

    if transfer_seconds < 0:
        raise ValueError("transfer_seconds must be non-negative")
    if modal_record.get("result") != "passed":
        raise ReproductionMismatchError("Modal benchmark did not pass")
    for key in (
        "fixture_id",
        "task_type",
        "duration_seconds",
        "seed",
        "diffusion_steps",
        "shift",
        "batch_size",
        "precision",
        "lm_requested",
    ):
        if _nested(modal_record, "request", key) != _nested(colab_record, "request", key):
            raise ReproductionMismatchError(f"Request mismatch: {key}")
    if modal_record.get("source") != colab_record.get("source"):
        raise ReproductionMismatchError("Source revision mismatch")
    modal_assets = _asset_contract(modal_record)
    colab_assets = _asset_contract(colab_record)
    if modal_assets != colab_assets:
        raise ReproductionMismatchError("Model asset contract mismatch")

    artifacts = modal_record.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise ReproductionMismatchError("Expected one Modal WAV artifact")
    artifact = artifacts[0]
    if not isinstance(artifact, dict):
        raise ReproductionMismatchError("Invalid Modal artifact record")
    local_size = local_artifact.stat().st_size
    local_sha256 = sha256_file(local_artifact)
    if local_size != artifact.get("size_bytes") or local_sha256 != artifact.get("sha256"):
        raise ReproductionMismatchError("Downloaded artifact failed size/checksum verification")
    metadata = wav_metadata(local_artifact)
    validation = modal_record.get("audio_validation")
    if not isinstance(validation, dict):
        raise ReproductionMismatchError("Modal audio validation is missing")
    if metadata["sample_rate_hz"] != validation.get("sample_rate_hz"):
        raise ReproductionMismatchError("Downloaded sample rate mismatch")
    if metadata["channels"] != validation.get("channels"):
        raise ReproductionMismatchError("Downloaded channel count mismatch")
    if abs(float(metadata["duration_seconds"]) - float(validation["duration_seconds"])) > 0.01:
        raise ReproductionMismatchError("Downloaded duration mismatch")

    colab_validation = colab_record.get("audio_validation")
    if not isinstance(colab_validation, dict):
        raise ReproductionMismatchError("Colab validation is missing")
    technical_match = (
        validation.get("sample_rate_hz") == colab_validation.get("sample_rate_hz")
        and validation.get("channels") == colab_validation.get("channels")
        and abs(float(validation["duration_seconds"]) - float(colab_validation["duration_seconds"]))
        <= 0.01
        and validation.get("finite_samples") is True
        and validation.get("silent") is False
        and validation.get("clipped") is False
    )
    if not technical_match:
        raise ReproductionMismatchError("Modal WAV does not match the Colab technical contract")
    colab_artifact = colab_record["artifacts"][0]
    checksum_match = isinstance(colab_artifact, dict) and local_sha256 == colab_artifact.get(
        "sha256"
    )
    return {
        "status": "passed",
        "exact_request_match": True,
        "exact_source_and_assets_match": True,
        "technical_audio_contract_match": True,
        "artifact_checksum_match": checksum_match,
        "modal_artifact": {
            "path": local_artifact.name,
            "size_bytes": local_size,
            "sha256": local_sha256,
        },
        "wav_metadata": metadata,
        "transfer": {
            "seconds": transfer_seconds,
            "bytes_per_second": (local_size / transfer_seconds if transfer_seconds > 0 else None),
        },
        "comparison_note": (
            "byte-identical fixed-seed artifact"
            if checksum_match
            else "different checksum but matching validated technical contract"
        ),
    }


def _nested(record: Mapping[str, Any], group: str, key: str) -> object:
    value = record.get(group)
    if not isinstance(value, dict):
        raise ReproductionMismatchError(f"Missing record group: {group}")
    return value.get(key)


def _asset_contract(record: Mapping[str, Any]) -> tuple[tuple[str, str, str, str], ...]:
    assets = record.get("assets")
    if not isinstance(assets, list):
        raise ReproductionMismatchError("Missing model assets")
    result: list[tuple[str, str, str, str]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReproductionMismatchError("Invalid model asset")
        result.append(
            (
                str(asset.get("id")),
                str(asset.get("repository")),
                str(asset.get("revision")),
                str(asset.get("sha256")),
            )
        )
    return tuple(result)
