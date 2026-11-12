"""Provider-neutral remote worker file-contract tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts" / "colab" / "remote_worker.py"


class RemoteWorkerTests(unittest.TestCase):
    def test_custom_paths_execute_the_shared_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = root / "request.json"
            result = root / "result.json"
            request.write_text(
                json.dumps(
                    {
                        "job_id": "job-1",
                        "kind": "harness.echo",
                        "payload": {"message": "hello"},
                    }
                ),
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
            record = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual("succeeded", record["status"])
            self.assertEqual({"message": "hello"}, record["result"]["echo"])


if __name__ == "__main__":
    unittest.main()
