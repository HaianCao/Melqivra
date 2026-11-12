"""Bounded Modal T4 wrapper for the shared allowlisted remote worker."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

import modal

ROOT = Path(__file__).resolve().parents[2]
LOCAL_WORKER = ROOT / "scripts" / "colab" / "remote_worker.py"
REMOTE_WORKER = Path("/opt/melqivra/remote_worker.py")

image = modal.Image.debian_slim(python_version="3.12").add_local_file(
    str(LOCAL_WORKER),
    remote_path=str(REMOTE_WORKER),
)
app = modal.App()


@app.function(  # type: ignore[untyped-decorator]
    image=image,
    gpu="T4",
    cpu=2,
    memory=4_096,
    max_containers=1,
    scaledown_window=2,
    timeout=1_800,
)
def run_job(request_json: str) -> dict[str, Any]:
    """Execute one request on T4 and return its typed worker result."""

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


@app.local_entrypoint()  # type: ignore[untyped-decorator]
def main(request_file: str) -> str:
    """Read private job content from a file rather than placing it in CLI arguments."""

    request_json = Path(request_file).read_text(encoding="utf-8")
    result = run_job.remote(request_json)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)
