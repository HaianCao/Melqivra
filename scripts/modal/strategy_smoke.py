"""Run one real Modal T4 healthcheck through the durable runner strategy."""

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
from agent_music.models import JobState  # noqa: E402
from agent_music.scheduler import GpuHarness  # noqa: E402

TERMINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


async def run_smoke(*, modal_executable: str, timeout_seconds: float) -> dict[str, object]:
    """Submit a healthcheck and return a sanitized durable-job summary."""

    with tempfile.TemporaryDirectory(prefix="melqivra-modal-strategy-") as temporary:
        root = Path(temporary)
        settings = HarnessSettings(
            database_path=root / "harness.db",
            artifact_root=root / "artifacts",
            runner="modal",
            idle_timeout_seconds=2.0,
            scheduler_poll_seconds=0.1,
            job_timeout_seconds=timeout_seconds,
            modal_executable=modal_executable,
            modal_app_name="melqivra-strategy-smoke",
            modal_runner_file=ROOT / "scripts" / "modal" / "runner_app.py",
            modal_command_timeout_seconds=timeout_seconds,
        )
        harness = GpuHarness(settings)
        await harness.start()
        started = time.monotonic()
        try:
            job = harness.enqueue(
                user_id="strategy-smoke",
                kind="harness.healthcheck",
                payload={},
                idempotency_key="modal-strategy-smoke-v1",
            )
            deadline = started + timeout_seconds
            while time.monotonic() < deadline:
                current = harness.store.get(job.id)
                if current.state in TERMINAL_STATES:
                    return {
                        "status": current.state.value,
                        "duration_seconds": time.monotonic() - started,
                        "runner": settings.runner,
                        "app_name": settings.modal_app_name,
                        "result": current.result,
                        "error": current.error,
                    }
                await asyncio.sleep(0.1)
            raise TimeoutError("Modal strategy smoke job did not reach a terminal state")
        finally:
            await harness.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modal-executable", default="modal")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    args = parser.parse_args()
    result = asyncio.run(
        run_smoke(
            modal_executable=args.modal_executable,
            timeout_seconds=args.timeout_seconds,
        )
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == JobState.SUCCEEDED.value else 1


if __name__ == "__main__":
    raise SystemExit(main())
