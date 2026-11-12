"""Submit one bounded HeartMuLa vocal batch through the durable Modal strategy."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agent_music.config import HarnessSettings  # noqa: E402
from agent_music.generation import request_to_json  # noqa: E402
from agent_music.models import JobState  # noqa: E402
from agent_music.s05_preflight import build_probe_request  # noqa: E402
from agent_music.scheduler import GpuHarness  # noqa: E402

TERMINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
RUNNER_FILE = ROOT / "scripts" / "modal" / "s05_heartmula_app.py"


async def run_probe(
    *,
    run_id: str,
    modal_executable: str,
    timeout_seconds: float,
) -> dict[str, object]:
    """Queue exactly one two-take job and return its authoritative durable state."""

    request = build_probe_request(run_id)
    request.validate()
    with tempfile.TemporaryDirectory(prefix="melqivra-s05-probe-") as temporary:
        root = Path(temporary)
        settings = HarnessSettings(
            database_path=root / "harness.db",
            artifact_root=root / "artifacts",
            runner="modal",
            idle_timeout_seconds=2.0,
            scheduler_poll_seconds=0.1,
            job_timeout_seconds=timeout_seconds,
            modal_executable=modal_executable,
            modal_app_name="melqivra-s05-heartmula-probe",
            modal_runner_file=RUNNER_FILE,
            modal_remote_output_root="/cache/runs",
            modal_command_timeout_seconds=timeout_seconds,
        )
        harness = GpuHarness(settings)
        await harness.start()
        started = time.monotonic()
        try:
            job = harness.enqueue(
                user_id="s05-operator-approved-probe",
                kind="music.generate",
                payload={"generation_request": request_to_json(request)},
                idempotency_key=f"s05-heartmula:{run_id}",
            )
            deadline = started + timeout_seconds + 30.0
            while time.monotonic() < deadline:
                current = harness.store.get(job.id)
                if current.state in TERMINAL_STATES:
                    return {
                        "status": current.state.value,
                        "duration_seconds": time.monotonic() - started,
                        "runner": settings.runner,
                        "app_name": settings.modal_app_name,
                        "request_id": request.request_id,
                        "contract_checksum": request.contract_checksum,
                        "result": current.result,
                        "error": current.error,
                    }
                await asyncio.sleep(0.2)
            await harness.cancel(job.id)
            return {
                "status": "observer_timeout",
                "duration_seconds": time.monotonic() - started,
                "runner": settings.runner,
                "app_name": settings.modal_app_name,
                "request_id": request.request_id,
                "contract_checksum": request.contract_checksum,
                "result": None,
                "error": "S05 HeartMuLa job did not reach a terminal state",
            }
        finally:
            await harness.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--modal-executable", default="modal")
    parser.add_argument("--timeout-seconds", type=float, default=1_800.0)
    args = parser.parse_args()
    result = asyncio.run(
        run_probe(
            run_id=args.run_id,
            modal_executable=args.modal_executable,
            timeout_seconds=args.timeout_seconds,
        )
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == JobState.SUCCEEDED.value else 1


if __name__ == "__main__":
    raise SystemExit(main())
