"""Validation for the S01 provenance and legal benchmark fixtures."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = ROOT / "docs" / "evidence" / "S01"


def load_json(path: Path) -> dict[str, Any]:
    """Load one evidence JSON object."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return value


class S01EvidenceTests(unittest.TestCase):
    def test_model_registry_has_immutable_revisions_and_checksums(self) -> None:
        registry = load_json(EVIDENCE_ROOT / "model-registry.json")
        backend = registry["backend"]
        self.assertRegex(backend["source_revision"], r"^[0-9a-f]{40}$")
        self.assertEqual("MIT", backend["source_license"])

        assets = registry["assets"]
        self.assertGreaterEqual(len(assets), 4)
        self.assertEqual(len(assets), len({asset["id"] for asset in assets}))
        for asset in assets:
            self.assertRegex(asset["revision"], r"^[0-9a-f]{40}$")
            self.assertRegex(asset["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(asset["size_bytes"], 0)
            self.assertEqual("not_bundled", asset["redistribution"])
            self.assertTrue(asset["license_source"].startswith("https://"))

    def test_vietnamese_fixture_corpus_has_required_coverage(self) -> None:
        corpus = load_json(EVIDENCE_ROOT / "vietnamese-briefs.json")
        briefs = corpus["briefs"]
        self.assertGreaterEqual(len(briefs), 20)
        self.assertEqual(len(briefs), len({brief["id"] for brief in briefs}))
        self.assertGreaterEqual(len({genre for brief in briefs for genre in brief["genres"]}), 12)
        self.assertEqual({"north", "central", "south"}, {b["region"] for b in briefs} - {"mixed"})
        self.assertGreaterEqual(sum(brief["code_switch"] for brief in briefs), 2)

        for brief in briefs:
            self.assertEqual("vi", brief["language"])
            self.assertGreaterEqual(brief["tempo_bpm"], 40)
            self.assertLessEqual(brief["tempo_bpm"], 220)
            self.assertGreaterEqual(brief["duration_seconds"], 10)
            self.assertLessEqual(brief["duration_seconds"], 600)
            self.assertGreaterEqual(len(brief["structure"]), 4)
            self.assertIn("no living-artist imitation", brief["constraints"])

    def test_benchmark_schema_requires_safety_critical_evidence(self) -> None:
        schema = load_json(EVIDENCE_ROOT / "benchmark-run.schema.json")
        required = set(schema["required"])
        self.assertTrue(
            {
                "environment",
                "assets",
                "capabilities",
                "timings_seconds",
                "peak_vram_bytes",
                "audio_validation",
                "artifacts",
                "result",
            }.issubset(required)
        )


if __name__ == "__main__":
    unittest.main()
