"""Provider-neutral remote worker file-contract tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_music.config import DEFAULT_REMOTE_WORKER_FILE

WORKER = DEFAULT_REMOTE_WORKER_FILE


class RemoteWorkerTests(unittest.TestCase):
    def _run(self, *, kind: str, payload: dict[str, object]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = root / "request.json"
            result = root / "result.json"
            request.write_text(
                json.dumps({"job_id": "job-1", "kind": kind, "payload": payload}),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(WORKER),
                    "--request",
                    str(request),
                    "--result",
                    str(result),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            value = json.loads(result.read_text(encoding="utf-8"))
            self.assertIsInstance(value, dict)
            return value

    def test_custom_paths_execute_the_shared_allowlist(self) -> None:
        record = self._run(kind="harness.echo", payload={"message": "hello"})
        self.assertEqual("succeeded", record["status"])
        self.assertEqual({"message": "hello"}, record["result"]["echo"])

    def test_healthcheck_succeeds_without_nvidia_smi(self) -> None:
        record = self._run(kind="harness.healthcheck", payload={})
        self.assertEqual("succeeded", record["status"])
        self.assertIsInstance(record["result"]["gpu"]["available"], bool)


if __name__ == "__main__":
    unittest.main()
