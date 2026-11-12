"""Modal runner state, transport, and failure-contract tests."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_music.config import HarnessSettings
from agent_music.models import GpuJob, JobState, RunnerState
from agent_music.runners.modal import ModalRunner
from agent_music.runners.modal_cli import CommandResult, LineCallback


class FakeModalClient:
    def __init__(self, *, remote_status: str = "succeeded") -> None:
        self.remote_status = remote_status
        self.commands: list[tuple[str, ...]] = []
        self.cancelled = False

    def token_info_argv(self) -> list[str]:
        return ["modal", "token", "info"]

    def run_job_argv(
        self,
        runner_file: Path,
        request_path: Path,
        result_path: Path,
    ) -> list[str]:
        return [
            "modal",
            "run",
            "--write-result",
            str(result_path),
            str(runner_file),
            "--request-file",
            str(request_path),
        ]

    def stop_argv(self) -> list[str]:
        return ["modal", "app", "stop", "--yes", "melqivra-gpu-runner"]

    async def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        on_stdout: LineCallback | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        _ = check, environment
        self.commands.append(tuple(argv))
        if len(argv) > 1 and argv[1] == "run":
            result_path = Path(argv[argv.index("--write-result") + 1])
            request_path = Path(argv[argv.index("--request-file") + 1])
            request_text = await asyncio.to_thread(request_path.read_text, encoding="utf-8")
            request = json.loads(request_text)
            if on_stdout is not None:
                await on_stdout('{"type":"stage","stage":"remote_started"}')
                await on_stdout("ignored provider log")
            result_text = json.dumps(
                {
                    "status": self.remote_status,
                    "job_id": request["job_id"],
                    "result": {"echo": request["payload"]},
                    "error": "deterministic failure" if self.remote_status != "succeeded" else None,
                }
            )
            await asyncio.to_thread(result_path.write_text, result_text, encoding="utf-8")
        return CommandResult(returncode=0, stdout="", stderr="")

    async def cancel_current(self) -> None:
        self.cancelled = True


class ModalRunnerTests(unittest.IsolatedAsyncioTestCase):
    def _job(self) -> GpuJob:
        return GpuJob(
            id="job-1",
            user_id="user-1",
            kind="harness.echo",
            payload={"message": "private"},
            state=JobState.RUNNING,
            created_at=1.0,
            updated_at=1.0,
            lease_token=1,
        )

    async def test_runner_uses_file_transport_and_structured_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner_file = Path(temporary) / "remote_worker.py"
            runner_file.write_text("# fixture\n", encoding="utf-8")
            client = FakeModalClient()
            runner = ModalRunner(
                HarnessSettings(modal_runner_file=runner_file),
                client=client,
            )
            events: list[dict[str, Any]] = []

            async def capture(event: dict[str, Any]) -> None:
                events.append(event)

            await runner.ensure_ready()
            result = await runner.run(self._job(), capture)

            self.assertEqual(RunnerState.READY, runner.state)
            self.assertIsNone(runner.session_name)
            self.assertEqual("succeeded", result["status"])
            self.assertEqual("submitting", events[0]["stage"])
            self.assertEqual("remote_started", events[1]["stage"])
            command_text = " ".join(part for command in client.commands for part in command)
            self.assertNotIn("private", command_text)

    async def test_remote_failure_and_stop_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner_file = Path(temporary) / "remote_worker.py"
            runner_file.write_text("# fixture\n", encoding="utf-8")
            client = FakeModalClient(remote_status="failed")
            runner = ModalRunner(
                HarnessSettings(modal_runner_file=runner_file),
                client=client,
            )
            await runner.ensure_ready()

            with self.assertRaisesRegex(RuntimeError, "deterministic failure"):
                await runner.run(self._job(), lambda _event: _completed())
            await runner.stop()

            self.assertEqual(RunnerState.STOPPED, runner.state)
            self.assertTrue(client.cancelled)
            self.assertIn(("modal", "app", "stop", "--yes", "melqivra-gpu-runner"), client.commands)


async def _completed() -> None:
    return None


if __name__ == "__main__":
    unittest.main()
