"""Real S07 multi-process API/worker recovery demo contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, cast

POSTGRES_DSN = os.getenv("MELQIVRA_TEST_POSTGRES_DSN", "")
S3_ENDPOINT = os.getenv("MELQIVRA_TEST_S3_ENDPOINT", "")
ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "scripts" / "s07_service_recovery_demo.py"
EVIDENCE = ROOT / "docs" / "evidence" / "S07" / "service-recovery-demo-results.json"


class S07ServiceRecoveryEvidenceTests(unittest.TestCase):
    def test_committed_demo_evidence_records_exact_recovery_contract(self) -> None:
        result = cast(dict[str, Any], json.loads(EVIDENCE.read_text(encoding="utf-8")))
        self.assertEqual("passed", result["status"])
        self.assertEqual(49, result["service_contract"]["tests_passed"])
        self.assertEqual(
            ["lease_expired", "succeeded"],
            result["result"]["recovered_attempt_outcomes"],
        )
        self.assertEqual(1, result["result"]["max_concurrent_attempts"])
        self.assertTrue(result["result"]["tenant_job_isolation"])
        self.assertTrue(result["result"]["tenant_artifact_isolation"])
        self.assertEqual(0, result["cleanup"]["containers_after"])
        self.assertEqual(0, result["cleanup"]["volumes_after"])


@unittest.skipUnless(
    POSTGRES_DSN and S3_ENDPOINT,
    "PostgreSQL and S3-compatible test services are not configured",
)
class S07ServiceRecoveryTests(unittest.TestCase):
    def test_multi_process_restart_reconnect_and_isolation_demo(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(DEMO)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = cast(dict[str, Any], json.loads(completed.stdout))
        self.assertEqual("passed", result["status"])
        self.assertTrue(result["api_worker_separated"])
        self.assertTrue(result["pre_worker_jobs_queued"])
        self.assertTrue(result["completion_order_preserved"])
        self.assertEqual(1, result["max_concurrent_attempts"])
        self.assertEqual(
            ["lease_expired", "succeeded"],
            result["recovered_attempt_outcomes"],
        )
        self.assertEqual([1, 1, 1], result["successful_attempts_per_job"])
        self.assertTrue(result["sse_sequences_contiguous_after_cursor"])
        self.assertIn("job.requeued", result["sse_resumed_event_types"])
        self.assertIn("job.succeeded", result["sse_resumed_event_types"])
        self.assertTrue(result["tenant_job_isolation"])
        self.assertTrue(result["tenant_artifact_isolation"])
        self.assertEqual(0, result["active_capacity_slots_after"])
        self.assertEqual(0, result["nonterminal_jobs_after"])
        self.assertEqual(0, result["child_processes_after"])


if __name__ == "__main__":
    unittest.main()
