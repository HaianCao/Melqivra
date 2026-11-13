"""Bounded Modal wrapper for the shared allowlisted remote worker."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

import modal

REMOTE_WORKER = Path("/opt/melqivra/worker.py")

image = modal.Image.debian_slim(python_version="3.12")
if modal.is_local():
    local_worker = Path(__file__).with_name("worker.py")
    image = image.add_local_file(str(local_worker), remote_path=str(REMOTE_WORKER))
app = modal.App()
GPU_JOB_KINDS = {"music.generate"}
GPU_PROBE_KINDS = {"harness.gpu_healthcheck"}


def _execute_worker(request_json: str) -> dict[str, Any]:
    request = json.loads(request_json)
    if not isinstance(request, dict):
        raise TypeError("Modal request must be a JSON object")
    with tempfile.TemporaryDirectory(prefix="melqivra-modal-worker-") as temporary:
        root = Path(temporary)
        request_path = root / "job.json"
        result_path = root / "result.json"
        request["result_path"] = str(result_path)
        request_path.write_text(
            json.dumps(request, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [
                sys.executable,
                str(REMOTE_WORKER),
                "--request",
                str(request_path),
                "--result",
                str(result_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
        return_code = process.wait()
        if not result_path.is_file():
            raise RuntimeError(f"Remote worker exited {return_code} without a result")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise TypeError("Remote worker result must be a JSON object")
        return cast(dict[str, Any], result)


@app.function(  # type: ignore[untyped-decorator]
    image=image,
    cpu=2,
    memory=1_024,
    max_containers=1,
    scaledown_window=2,
    timeout=300,
)
def run_control_job(request_json: str) -> dict[str, Any]:
    """Execute non-GPU health, echo, or unsupported-kind validation work."""

    return _execute_worker(request_json)


@app.function(  # type: ignore[untyped-decorator]
    image=image,
    gpu="T4",
    cpu=2,
    memory=4_096,
    max_containers=1,
    scaledown_window=2,
    timeout=1_800,
)
def run_gpu_job(request_json: str) -> dict[str, Any]:
    """Execute an allowlisted GPU job on T4 only."""

    return _execute_worker(request_json)


@app.function(  # type: ignore[untyped-decorator]
    image=image,
    gpu="T4",
    cpu=2,
    memory=1_024,
    max_containers=1,
    scaledown_window=2,
    timeout=120,
)
def run_gpu_probe(request_json: str) -> dict[str, Any]:
    """Run the bounded S06 GPU lifecycle healthcheck without model assets."""

    return _execute_worker(request_json)


@app.local_entrypoint()  # type: ignore[untyped-decorator]
def main(request_file: str) -> str:
    """Read private job content from a file rather than CLI arguments."""

    request_json = Path(request_file).read_text(encoding="utf-8")
    request = json.loads(request_json)
    if not isinstance(request, dict):
        raise TypeError("Modal request must be a JSON object")
    kind = request.get("kind")
    if kind in GPU_PROBE_KINDS:
        function = run_gpu_probe
    elif kind in GPU_JOB_KINDS:
        function = run_gpu_job
    else:
        function = run_control_job
    result = function.remote(request_json)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)
