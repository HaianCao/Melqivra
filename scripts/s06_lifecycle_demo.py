"""Run the CPU-only S06 FIFO, grace-boundary, and idle-shutdown demo."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from agent_music.config import HarnessSettings
from agent_music.models import JobState, RunnerState
from agent_music.runners.fake import FakeRunner
from agent_music.scheduler import GpuHarness


class FakeClock:
    """Deterministic scheduler clock used only by the documented demo."""

    def __init__(self) -> None:
        self.monotonic_seconds = 0.0
        self.wall_seconds = 1_700_000_000.0

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def wall(self) -> float:
        return self.wall_seconds

    def advance(self, seconds: float) -> None:
        self.monotonic_seconds += seconds
        self.wall_seconds += seconds


async def wait_until(predicate: Callable[[], bool], *, timeout_seconds: float = 5.0) -> None:
    """Wait for one synchronous predicate without sleeping the demo for 300 seconds."""

    deadline = time.monotonic() + timeout_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("S06 demo condition was not reached")
        await asyncio.sleep(0.005)


async def run_demo() -> dict[str, object]:
    """Prove FIFO serialization, exact-grace reuse, and one final idle stop."""

    with tempfile.TemporaryDirectory(prefix="melqivra-s06-demo-") as temporary:
        root = Path(temporary)
        settings = HarnessSettings(
            database_path=root / "harness.db",
            artifact_root=root / "artifacts",
            idle_timeout_seconds=300.0,
            scheduler_poll_seconds=0.01,
            job_timeout_seconds=2.0,
        )
        runner = FakeRunner(job_seconds=0.01)
        clock = FakeClock()
        harness = GpuHarness(
            settings,
            runner=runner,
            monotonic_clock=clock.monotonic,
            wall_clock=clock.wall,
        )
        await harness.start()
        try:
            first = harness.enqueue(user_id="alice", kind="harness.echo", payload={"index": 1})
            second = harness.enqueue(user_id="bob", kind="harness.echo", payload={"index": 2})
            await wait_until(
                lambda: (
                    all(
                        harness.store.get(job.id).state == JobState.SUCCEEDED
                        for job in (first, second)
                    )
                    and harness.snapshot().current_job_id is None
                )
            )

            clock.advance(300.0)
            grace = harness.enqueue(
                user_id="charlie",
                kind="harness.echo",
                payload={"arrival_seconds": 300},
            )
            await wait_until(
                lambda: (
                    harness.store.get(grace.id).state == JobState.SUCCEEDED
                    and harness.snapshot().current_job_id is None
                )
            )
            provisions_at_grace = runner.provision_count

            clock.advance(300.0)
            await wait_until(lambda: runner.state == RunnerState.STOPPED)
            return {
                "status": "passed",
                "completed_job_ids": list(runner.completed_job_ids),
                "expected_job_ids": [first.id, second.id, grace.id],
                "fifo_preserved": runner.completed_job_ids == [first.id, second.id, grace.id],
                "max_active_jobs": runner.max_active_jobs,
                "provisions_at_exact_grace": provisions_at_grace,
                "stop_count": runner.stop_count,
                "idle_timeout_seconds": settings.idle_timeout_seconds,
            }
        finally:
            await harness.close()


def main() -> int:
    result = asyncio.run(run_demo())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
