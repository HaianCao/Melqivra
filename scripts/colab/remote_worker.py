"""Self-contained Colab-side harness worker.

The harness uploads one request to /content and executes this file through
the official Colab CLI. Only allowlisted job kinds are accepted.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

REQUEST_PATH = Path("/content/agent_music_job.json")
DEFAULT_RESULT_PATH = Path("/content/agent_music_result.json")


def emit(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)


def gpu_info() -> dict[str, Any]:
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
        from agent_music_worker import generate
    except ImportError as error:
        raise RuntimeError(
            "agent_music_worker is not installed in the Colab runtime. "
            "The GPU harness is healthy, but the ACE-Step generation plugin "
            "must be bootstrapped before music.generate jobs."
        ) from error
    result = generate(job)
    if not isinstance(result, dict):
        raise TypeError("agent_music_worker.generate must return a dict")
    return result


HANDLERS = {
    "harness.healthcheck": handle_healthcheck,
    "harness.echo": handle_echo,
    "music.generate": handle_music_generate,
}


def main() -> int:
    started = time.time()
    job: dict[str, Any] = {}
    result_path = DEFAULT_RESULT_PATH
    try:
        job = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
        configured_result_path = job.get("result_path")
        if configured_result_path:
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
