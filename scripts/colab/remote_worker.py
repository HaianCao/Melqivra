"""Self-contained remote harness worker.

Colab uploads and executes this file directly. Modal packages and invokes the
same file inside its T4 function. Only allowlisted job kinds are accepted.
"""

from __future__ import annotations

import argparse
import importlib
import json
import platform
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

REQUEST_PATH = Path("/content/agent_music_job.json")
DEFAULT_RESULT_PATH = Path("/content/agent_music_result.json")


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
            "The GPU harness is healthy, but the ACE-Step generation plugin "
            "must be bootstrapped before music.generate jobs."
        ) from error
    generate = cast(Callable[[dict[str, Any]], object], worker_module.generate)
    result = generate(job)
    if not isinstance(result, dict):
        raise TypeError("agent_music_worker.generate must return a dict")
    return result


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
        payload = handler(job)
        result = {
            "status": "succeeded",
            "job_id": job.get("job_id"),
            "kind": kind,
            "duration_seconds": time.time() - started,
            "result": payload,
            "artifacts": [],
        }
        emit({"type": "completed", "progress": 1.0})
        exit_code = 0
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
