"""Regression checks for tracked S05 probe evidence."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, cast

from agent_music.s05_preflight import (
    EXPECTED_CACHE_FILES,
    HEARTMULA_DIRECT_DEPENDENCIES,
    HEARTMULA_PIPELINE_SHA256,
    HEARTMULA_SOURCE_REVISION,
    HEARTMULA_VOLUME_NAME,
    build_probe_request,
)

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "S05" / "heartmula-probe-contract.json"


class S05EvidenceTests(unittest.TestCase):
    def test_probe_contract_matches_executable_pins_and_cost_controls(self) -> None:
        evidence = cast(dict[str, Any], json.loads(EVIDENCE.read_text(encoding="utf-8")))
        request = build_probe_request("heartmula_probe_evidence")

        self.assertEqual(HEARTMULA_SOURCE_REVISION, evidence["source"]["revision"])
        self.assertEqual(HEARTMULA_PIPELINE_SHA256, evidence["source"]["pipeline_sha256"])
        self.assertEqual(HEARTMULA_VOLUME_NAME, evidence["cache"]["volume"])
        self.assertEqual(
            sum(item.size_bytes for item in EXPECTED_CACHE_FILES),
            evidence["cache"]["expected_bytes"],
        )
        self.assertEqual(
            list(HEARTMULA_DIRECT_DEPENDENCIES),
            evidence["runtime"]["direct_dependencies"],
        )
        self.assertFalse(evidence["operator_authorization"]["automatic_retry"])
        self.assertEqual(1, evidence["probe"]["max_containers"])
        self.assertEqual(2, evidence["probe"]["scaledown_window_seconds"])
        self.assertEqual(len(request.takes), evidence["probe"]["take_count"])
        self.assertEqual(request.duration_seconds, evidence["probe"]["duration_seconds_per_take"])


if __name__ == "__main__":
    unittest.main()
