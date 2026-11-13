"""Self-contained allowlisted worker shared by remote runner providers."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import platform
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, cast

REQUEST_PATH = Path("/content/agent_music_job.json")
DEFAULT_RESULT_PATH = Path("/content/agent_music_result.json")
ALLOWED_CANCEL_ROOT = Path("/content/agent_music_control")
ALLOWED_DRIVE_ROOT = Path("/content/drive")
CACHE_DESCRIPTOR = ".melqivra-cache.json"


class RemoteJobCancelled(RuntimeError):
    """The durable control plane requested cancellation."""


def emit(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)


def gpu_info() -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return {
            "available": False,
            "nvidia_smi": "",
            "error": "nvidia-smi executable not found",
        }
    return {
        "available": result.returncode == 0,
        "nvidia_smi": result.stdout.strip(),
        "error": result.stderr.strip() or None,
    }


def handle_healthcheck(job: dict[str, Any]) -> dict[str, Any]:
    _ = job
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "gpu": gpu_info(),
    }


def handle_echo(job: dict[str, Any]) -> dict[str, Any]:
    return {"echo": job.get("payload", {})}


def handle_music_generate(job: dict[str, Any]) -> dict[str, Any]:
    """Call the separately installed music worker implementation."""

    try:
        worker_module = importlib.import_module("agent_music_worker")
    except ImportError as error:
        raise RuntimeError(
            "agent_music_worker is not installed in the remote runtime. "
            "The GPU harness is healthy, but the music-generation plugin "
            "must be bootstrapped before music.generate jobs."
        ) from error
    generate = cast(Callable[[dict[str, Any]], object], worker_module.generate)
    result = generate(job)
    if not isinstance(result, dict):
        raise TypeError("agent_music_worker.generate must return a dict")
    return result


def cancellation_path(job: dict[str, Any]) -> Path | None:
    """Return the allowlisted cancellation marker path from a trusted request."""

    control = job.get("control")
    if control is None:
        return None
    if not isinstance(control, dict) or not isinstance(control.get("cancel_path"), str):
        raise TypeError("Remote job control must declare a cancel_path string")
    path = Path(str(control["cancel_path"]))
    try:
        path.relative_to(ALLOWED_CANCEL_ROOT)
    except ValueError:
        raise ValueError("Remote cancellation path escaped its allowlisted root") from None
    return path


def raise_if_cancelled(job: dict[str, Any]) -> None:
    """Consume a matching marker and stop before publishing success."""

    path = cancellation_path(job)
    if path is None or not path.is_file():
        return
    marker = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(marker, dict) or marker.get("job_id") != job.get("job_id"):
        raise ValueError("Remote cancellation marker does not match the job")
    path.unlink()
    raise RemoteJobCancelled("Remote job cancellation requested")


def verify_cache(job: dict[str, Any]) -> dict[str, Any] | None:
    """Verify one pinned Drive cache contract before model work."""

    contract = job.get("cache_contract")
    if contract is None:
        return None
    if not isinstance(contract, dict):
        raise TypeError("Remote cache contract must be an object")
    cache_key = contract.get("cache_key")
    source_revision = contract.get("source_revision")
    root_text = contract.get("root")
    assets = contract.get("assets")
    if (
        not isinstance(cache_key, str)
        or not cache_key
        or not isinstance(source_revision, str)
        or not source_revision
        or not isinstance(root_text, str)
        or not isinstance(assets, list)
        or not assets
    ):
        raise ValueError("Remote cache contract metadata is invalid")
    root = Path(root_text)
    try:
        root.relative_to(ALLOWED_DRIVE_ROOT)
    except ValueError:
        raise ValueError("Remote cache root escaped Google Drive") from None

    normalized: list[dict[str, object]] = []
    expected_descriptor_assets: list[dict[str, object]] = []
    observed_paths: set[str] = set()
    total_bytes = 0
    started = time.monotonic()
    for raw in assets:
        if not isinstance(raw, dict):
            raise ValueError("Remote cache asset is invalid")
        asset_id = raw.get("id")
        relative_text = raw.get("relative_path")
        size_bytes = raw.get("size_bytes")
        checksum = raw.get("sha256")
        if (
            not isinstance(asset_id, str)
            or not asset_id
            or not isinstance(relative_text, str)
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
            or not isinstance(checksum, str)
            or len(checksum) != 64
        ):
            raise ValueError("Remote cache asset metadata is invalid")
        try:
            int(checksum, 16)
        except ValueError:
            raise ValueError("Remote cache asset checksum is invalid") from None
        relative = PurePosixPath(relative_text)
        if (
            relative.is_absolute()
            or "\\" in relative_text
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative_text in observed_paths
        ):
            raise ValueError("Remote cache asset path is unsafe or duplicated")
        observed_paths.add(relative_text)
        path = root.joinpath(*relative.parts)
        if not path.is_file():
            raise FileNotFoundError(f"Remote cache asset is missing: {asset_id}")
        observed_size = path.stat().st_size
        if observed_size != size_bytes:
            raise ValueError(
                f"Remote cache size mismatch for {asset_id}: {observed_size} != {size_bytes}"
            )
        hasher = hashlib.sha256()
        with path.open("rb") as asset_file:
            while chunk := asset_file.read(1024 * 1024):
                hasher.update(chunk)
        if hasher.hexdigest() != checksum.lower():
            raise ValueError(f"Remote cache checksum mismatch for {asset_id}")
        normalized.append(
            {
                "id": asset_id,
                "relative_path": relative_text,
                "size_bytes": size_bytes,
                "sha256": checksum.lower(),
            }
        )
        expected_descriptor_assets.append(
            {"id": asset_id, "size_bytes": size_bytes, "sha256": checksum.lower()}
        )
        total_bytes += size_bytes

    descriptor_path = root / CACHE_DESCRIPTOR
    if not descriptor_path.is_file():
        raise FileNotFoundError("Remote cache descriptor is missing")
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    expected_descriptor = {
        "schema_version": 1,
        "cache_key": cache_key,
        "source_revision": source_revision,
        "verified_assets": expected_descriptor_assets,
    }
    if descriptor != expected_descriptor:
        raise ValueError("Remote cache descriptor is stale or incomplete")
    return {
        "status": "verified",
        "cache_key": cache_key,
        "source_revision": source_revision,
        "asset_count": len(normalized),
        "verified_bytes": total_bytes,
        "verification_seconds": time.monotonic() - started,
    }


HANDLERS = {
    "harness.healthcheck": handle_healthcheck,
    "harness.echo": handle_echo,
    "music.generate": handle_music_generate,
}


def build_parser() -> argparse.ArgumentParser:
    """Build provider-neutral file-path arguments with Colab-compatible defaults."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, default=REQUEST_PATH)
    parser.add_argument("--result", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    started = time.time()
    job: dict[str, Any] = {}
    result: dict[str, Any]
    args = build_parser().parse_args(argv)
    result_path = args.result or DEFAULT_RESULT_PATH
    try:
        job = json.loads(args.request.read_text(encoding="utf-8"))
        configured_result_path = job.get("result_path")
        if args.result is None and configured_result_path:
            result_path = Path(str(configured_result_path))
        kind = str(job.get("kind", ""))
        handler = HANDLERS.get(kind)
        if handler is None:
            raise ValueError(f"Unsupported remote job kind: {kind}")
        emit({"type": "stage", "stage": "remote_started", "progress": 0.1})
        raise_if_cancelled(job)
        cache_metrics = verify_cache(job)
        payload = handler(job)
        raise_if_cancelled(job)
        result = {
            "status": "succeeded",
            "job_id": job.get("job_id"),
            "kind": kind,
            "duration_seconds": time.time() - started,
            "result": payload,
            "artifacts": [],
        }
        if cache_metrics is not None:
            result["cache"] = cache_metrics
        emit({"type": "completed", "progress": 1.0})
        exit_code = 0
    except RemoteJobCancelled as error:
        result = {
            "status": "cancelled",
            "job_id": job.get("job_id"),
            "error": str(error),
            "duration_seconds": time.time() - started,
        }
        emit({"type": "cancelled", "error": result["error"]})
        exit_code = 2
    except Exception as error:
        result = {
            "status": "failed",
            "job_id": job.get("job_id"),
            "error": f"{type(error).__name__}: {error}",
            "traceback": traceback.format_exc(limit=20),
            "duration_seconds": time.time() - started,
        }
        emit({"type": "failed", "error": result["error"]})
        exit_code = 1
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
