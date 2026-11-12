"""Local verification for exact Colab-to-Modal S01 PoC reproduction."""

from __future__ import annotations

import hashlib
import math
import wave
from array import array
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class ReproductionMismatchError(RuntimeError):
    """The Modal result does not reproduce the pinned Colab technical contract."""


class CapabilityArtifactError(RuntimeError):
    """A capability probe artifact does not prove the requested behavior."""


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


def compare_reference_artifact(*, source: Path, generated: Path) -> dict[str, object]:
    """Verify reference-conditioned output is valid and not a copied input."""

    source_metadata = wav_metadata(source)
    generated_metadata = wav_metadata(generated)
    for key in ("sample_rate_hz", "channels", "sample_width_bytes"):
        if source_metadata[key] != generated_metadata[key]:
            raise CapabilityArtifactError(f"Reference output metadata mismatch: {key}")
    if (
        abs(
            float(source_metadata["duration_seconds"])
            - float(generated_metadata["duration_seconds"])
        )
        > 0.01
    ):
        raise CapabilityArtifactError("Reference output duration mismatch")
    source_sha256 = sha256_file(source)
    generated_sha256 = sha256_file(generated)
    if source_sha256 == generated_sha256:
        raise CapabilityArtifactError("Reference output is byte-identical to its input")
    return {
        "status": "passed",
        "input_sha256": source_sha256,
        "output_sha256": generated_sha256,
        "output_differs_from_input": True,
        "wav_metadata": generated_metadata,
    }


def compare_repaint_artifact(
    *,
    source: Path,
    generated: Path,
    repaint_start_seconds: float,
    repaint_end_seconds: float,
    boundary_margin_seconds: float = 1.0,
) -> dict[str, object]:
    """Measure whether a repaint changes its target while preserving surrounding audio."""

    if not 0 <= repaint_start_seconds < repaint_end_seconds:
        raise ValueError("Invalid repaint range")
    if boundary_margin_seconds < 0:
        raise ValueError("Boundary margin must be non-negative")
    source_samples, metadata = _pcm16_samples(source)
    generated_samples, generated_metadata = _pcm16_samples(generated)
    if metadata != generated_metadata:
        raise CapabilityArtifactError("Repaint output WAV metadata differs from source")
    if len(source_samples) != len(generated_samples):
        raise CapabilityArtifactError("Repaint output sample count differs from source")
    duration = float(metadata["duration_seconds"])
    if repaint_end_seconds > duration:
        raise CapabilityArtifactError("Repaint range exceeds source duration")

    channels = int(metadata["channels"])
    sample_rate = int(metadata["sample_rate_hz"])
    inner_start = min(repaint_end_seconds, repaint_start_seconds + boundary_margin_seconds)
    inner_end = max(repaint_start_seconds, repaint_end_seconds - boundary_margin_seconds)
    outside_left_end = max(0.0, repaint_start_seconds - boundary_margin_seconds)
    outside_right_start = min(duration, repaint_end_seconds + boundary_margin_seconds)
    inside = _sample_metrics(
        source_samples,
        generated_samples,
        int(inner_start * sample_rate) * channels,
        int(inner_end * sample_rate) * channels,
    )
    outside_ranges = (
        (0, int(outside_left_end * sample_rate) * channels),
        (int(outside_right_start * sample_rate) * channels, len(source_samples)),
    )
    outside = _combined_sample_metrics(source_samples, generated_samples, outside_ranges)
    if sha256_file(source) == sha256_file(generated):
        raise CapabilityArtifactError("Repaint output is byte-identical to its source")
    if inside["changed_fraction"] < 0.01:
        raise CapabilityArtifactError("Repaint target range did not materially change")
    if outside["correlation"] < 0.90:
        raise CapabilityArtifactError("Repaint did not preserve surrounding audio")
    if inside["mean_absolute_delta"] <= outside["mean_absolute_delta"]:
        raise CapabilityArtifactError("Repaint changes were not localized to the target range")
    return {
        "status": "passed",
        "range_seconds": {
            "start": repaint_start_seconds,
            "end": repaint_end_seconds,
            "boundary_margin": boundary_margin_seconds,
        },
        "inside_range": inside,
        "outside_range": outside,
        "localized_change_observed": True,
        "surrounding_audio_preserved": True,
        "source_sha256": sha256_file(source),
        "output_sha256": sha256_file(generated),
        "wav_metadata": generated_metadata,
    }


def _pcm16_samples(path: Path) -> tuple[array[int], dict[str, int | float]]:
    with wave.open(str(path), "rb") as stream:
        if stream.getsampwidth() != 2 or stream.getcomptype() != "NONE":
            raise CapabilityArtifactError("Capability verifier requires PCM16 WAV")
        frames = stream.getnframes()
        sample_rate = stream.getframerate()
        channels = stream.getnchannels()
        samples = array("h")
        samples.frombytes(stream.readframes(frames))
    metadata = {
        "frames": frames,
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "sample_width_bytes": 2,
        "duration_seconds": frames / sample_rate,
    }
    return samples, metadata


def _sample_metrics(
    source: array[int], generated: array[int], start: int, end: int
) -> dict[str, float]:
    if end <= start:
        raise CapabilityArtifactError("Capability comparison range is empty")
    count = end - start
    absolute_delta = 0
    changed = 0
    source_sum = 0.0
    generated_sum = 0.0
    source_square = 0.0
    generated_square = 0.0
    cross = 0.0
    for index in range(start, end):
        source_value = int(source[index])
        generated_value = int(generated[index])
        delta = abs(source_value - generated_value)
        absolute_delta += delta
        changed += delta != 0
        source_sum += source_value
        generated_sum += generated_value
        source_square += source_value * source_value
        generated_square += generated_value * generated_value
        cross += source_value * generated_value
    source_variance = source_square - source_sum * source_sum / count
    generated_variance = generated_square - generated_sum * generated_sum / count
    denominator = math.sqrt(max(0.0, source_variance * generated_variance))
    correlation = (
        (cross - source_sum * generated_sum / count) / denominator if denominator > 0 else 1.0
    )
    return {
        "sample_count": float(count),
        "changed_fraction": changed / count,
        "mean_absolute_delta": absolute_delta / count,
        "correlation": correlation,
    }


def _combined_sample_metrics(
    source: array[int],
    generated: array[int],
    ranges: tuple[tuple[int, int], ...],
) -> dict[str, float]:
    segments = [
        _sample_metrics(source, generated, start, end) for start, end in ranges if end > start
    ]
    if not segments:
        raise CapabilityArtifactError("No surrounding audio remains for verification")
    total = sum(segment["sample_count"] for segment in segments)
    return {
        "sample_count": total,
        "changed_fraction": sum(
            segment["changed_fraction"] * segment["sample_count"] for segment in segments
        )
        / total,
        "mean_absolute_delta": sum(
            segment["mean_absolute_delta"] * segment["sample_count"] for segment in segments
        )
        / total,
        "correlation": sum(segment["correlation"] * segment["sample_count"] for segment in segments)
        / total,
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
