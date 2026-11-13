"""Run bounded cold, warm, and recovery T4 healthchecks through durable jobs."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any, TypedDict, cast

from agent_music.config import DEFAULT_MODAL_RUNNER_FILE, HarnessSettings
from agent_music.models import GpuJob, JobState
from agent_music.s06_probe import (
    MODAL_PRICE_CHECKED_AT,
    MODAL_PRICING_SOURCE,
    PROBE_GPU_CALLS,
    PROBE_MAX_FUNCTION_SECONDS,
    estimated_max_metered_usd,
    validate_probe_authorization,
)
from agent_music.scheduler import GpuHarness

TERMINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


class ProbeCall(TypedDict):
    job_id: str
    readiness_mode: str
    gpu: str
    remote_duration_seconds: float
    execution_seconds: float
    total_seconds: float
    idle_before_job_seconds: float


def load_object(path: Path) -> dict[str, Any]:
    """Load one JSON object from a local authorization file."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected a JSON object: {path}")
    return cast(dict[str, Any], value)


async def wait_for_terminal(
    harness: GpuHarness,
    job: GpuJob,
    *,
    timeout_seconds: float,
) -> GpuJob:
    """Return one durable terminal job or cancel it after the observer bound."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        current = harness.store.get(job.id)
        if current.state in TERMINAL_STATES:
            return current
        await asyncio.sleep(0.1)
    await harness.cancel(job.id)
    raise TimeoutError(f"S06 lifecycle probe job timed out: {job.id}")


def _metric(lifecycle: dict[str, Any], name: str) -> float:
    value = lifecycle.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"S06 lifecycle metric is invalid: {name}")
    return float(value)


def summarize(job: GpuJob, *, expected_mode: str) -> ProbeCall:
    """Validate and sanitize one successful T4 healthcheck record."""

    if job.state != JobState.SUCCEEDED or not isinstance(job.result, dict):
        raise RuntimeError(f"S06 lifecycle probe failed: {job.error or job.state.value}")
    result = job.result.get("result")
    lifecycle = job.result.get("_lifecycle_metrics")
    if not isinstance(result, dict) or not isinstance(lifecycle, dict):
        raise ValueError("S06 lifecycle probe result is missing metrics")
    gpu = result.get("gpu")
    readiness = lifecycle.get("readiness")
    if not isinstance(gpu, dict) or gpu.get("available") is not True:
        raise RuntimeError("S06 lifecycle probe did not observe an NVIDIA GPU")
    gpu_text = gpu.get("nvidia_smi")
    if not isinstance(gpu_text, str) or "T4" not in gpu_text:
        raise RuntimeError("S06 lifecycle probe did not receive a Tesla T4")
    if not isinstance(readiness, dict) or readiness.get("mode") != expected_mode:
        raise ValueError(f"Expected {expected_mode} readiness metrics")
    return {
        "job_id": job.id,
        "readiness_mode": expected_mode,
        "gpu": gpu_text,
        "remote_duration_seconds": _metric(lifecycle, "remote_duration_seconds"),
        "execution_seconds": _metric(lifecycle, "execution_seconds"),
        "total_seconds": _metric(lifecycle, "total_seconds"),
        "idle_before_job_seconds": _metric(lifecycle, "idle_before_job_seconds"),
    }


async def run_probe(
    *,
    authorization: dict[str, Any],
    run_id: str,
    modal_executable: str,
    observer_timeout_seconds: float,
) -> dict[str, object]:
    """Execute three sequential durable jobs under the validated cost contract."""

    validate_probe_authorization(authorization, expected_run_id=run_id)
    with tempfile.TemporaryDirectory(prefix="melqivra-s06-modal-") as temporary:
        root = Path(temporary)
        settings = HarnessSettings(
            database_path=root / "harness.db",
            artifact_root=root / "artifacts",
            runner="modal",
            idle_timeout_seconds=300.0,
            scheduler_poll_seconds=0.1,
            job_timeout_seconds=observer_timeout_seconds,
            modal_executable=modal_executable,
            modal_app_name="melqivra-s06-lifecycle-probe",
            modal_runner_file=DEFAULT_MODAL_RUNNER_FILE,
            modal_command_timeout_seconds=observer_timeout_seconds,
        )
        harness = GpuHarness(settings)
        calls: list[ProbeCall] = []
        cleanup_state = "unknown"
        await harness.start()
        try:
            for index, mode in enumerate(("cold", "warm"), start=1):
                job = harness.enqueue(
                    user_id="s06-lifecycle-probe",
                    kind="harness.gpu_healthcheck",
                    payload={"sequence": index},
                    idempotency_key=f"{run_id}:{mode}",
                )
                terminal = await wait_for_terminal(
                    harness,
                    job,
                    timeout_seconds=observer_timeout_seconds + 30.0,
                )
                calls.append(summarize(terminal, expected_mode=mode))

            await harness.runner.stop()
            recovery = harness.enqueue(
                user_id="s06-lifecycle-probe",
                kind="harness.gpu_healthcheck",
                payload={"sequence": 3},
                idempotency_key=f"{run_id}:recovered",
            )
            terminal = await wait_for_terminal(
                harness,
                recovery,
                timeout_seconds=observer_timeout_seconds + 30.0,
            )
            calls.append(summarize(terminal, expected_mode="recovered"))
        finally:
            await harness.close()
            cleanup_state = harness.runner.state.value

    cold_total = float(calls[0]["total_seconds"])
    warm_total = float(calls[1]["total_seconds"])
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": "passed",
        "provider": "modal",
        "price_contract": {
            "source": MODAL_PRICING_SOURCE,
            "checked_at": MODAL_PRICE_CHECKED_AT,
            "gpu_calls": PROBE_GPU_CALLS,
            "max_function_seconds": PROBE_MAX_FUNCTION_SECONDS,
            "estimated_max_metered_usd": estimated_max_metered_usd(),
            "approved_max_metered_usd": authorization["approved_max_metered_usd"],
        },
        "calls": calls,
        "warm_total_gain_seconds": cold_total - warm_total,
        "cleanup": {
            "app_stop_completed": cleanup_state == "reclaimed",
            "runner_state": cleanup_state,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--modal-executable", default="modal")
    parser.add_argument("--observer-timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()
    result = asyncio.run(
        run_probe(
            authorization=load_object(args.authorization),
            run_id=args.run_id,
            modal_executable=args.modal_executable,
            observer_timeout_seconds=args.observer_timeout_seconds,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
