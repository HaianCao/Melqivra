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
            if "lyrics" in brief:
                self.assertIsInstance(brief["lyrics"], str)
                self.assertTrue(brief["lyrics"].strip())

        self.assertGreaterEqual(sum("lyrics" in brief for brief in briefs), 1)

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

    def test_recorded_t4_benchmarks_have_complete_passing_evidence(self) -> None:
        schema = load_json(EVIDENCE_ROOT / "benchmark-run.schema.json")
        required = set(schema["required"])
        for name in (
            "benchmark-dit-only-fresh.json",
            "benchmark-dit-only-warm.json",
            "benchmark-lm-0.6b.json",
            "benchmark-modal-colab-reproduction.json",
        ):
            benchmark = load_json(EVIDENCE_ROOT / name)
            self.assertTrue(required.issubset(benchmark), name)
            self.assertEqual("passed", benchmark["result"], name)
            self.assertEqual("Tesla T4", benchmark["environment"]["gpu_name"])
            self.assertTrue(benchmark["audio_validation"]["finite_samples"])
            self.assertFalse(benchmark["audio_validation"]["silent"])
            self.assertFalse(benchmark["audio_validation"]["clipped"])
            self.assertGreater(benchmark["peak_vram_bytes"], 0)
            self.assertGreaterEqual(
                benchmark["timings_seconds"]["total"],
                benchmark["timings_seconds"]["model_load"]
                + benchmark["timings_seconds"]["inference"],
            )

    def test_modal_reproduction_matches_colab_contract_and_records_transfer(self) -> None:
        colab = load_json(EVIDENCE_ROOT / "benchmark-dit-only-fresh.json")
        modal = load_json(EVIDENCE_ROOT / "benchmark-modal-colab-reproduction.json")
        verification = load_json(EVIDENCE_ROOT / "modal-colab-reproduction-verification.json")

        self.assertEqual(colab["source"], modal["source"])
        self.assertEqual(colab["assets"], modal["assets"])
        for key in (
            "fixture_id",
            "task_type",
            "duration_seconds",
            "seed",
            "diffusion_steps",
            "shift",
            "batch_size",
            "precision",
            "lm_requested",
        ):
            self.assertEqual(colab["request"][key], modal["request"][key], key)
        for key in (
            "duration_seconds",
            "sample_rate_hz",
            "channels",
            "finite_samples",
            "silent",
            "clipped",
            "true_peak_dbfs",
            "integrated_lufs",
        ):
            self.assertEqual(colab["audio_validation"][key], modal["audio_validation"][key], key)
        self.assertNotEqual(colab["artifacts"][0]["sha256"], modal["artifacts"][0]["sha256"])
        self.assertEqual("passed", verification["status"])
        self.assertTrue(verification["exact_request_match"])
        self.assertTrue(verification["exact_source_and_assets_match"])
        self.assertTrue(verification["technical_audio_contract_match"])
        self.assertFalse(verification["artifact_checksum_match"])
        self.assertEqual(
            modal["artifacts"][0]["sha256"],
            verification["modal_artifact"]["sha256"],
        )
        self.assertGreater(verification["transfer"]["seconds"], 0)
        self.assertGreater(verification["transfer"]["bytes_per_second"], 0)

    def test_warm_cache_and_fixed_seed_observations_are_recorded(self) -> None:
        fresh = load_json(EVIDENCE_ROOT / "benchmark-dit-only-fresh.json")
        warm = load_json(EVIDENCE_ROOT / "benchmark-dit-only-warm.json")
        self.assertLess(
            warm["timings_seconds"]["model_load"], fresh["timings_seconds"]["model_load"]
        )
        self.assertEqual(fresh["artifacts"][0]["sha256"], warm["artifacts"][0]["sha256"])

    def test_lm_run_proves_initialization_and_execution(self) -> None:
        dit = load_json(EVIDENCE_ROOT / "benchmark-dit-only-warm.json")
        lm = load_json(EVIDENCE_ROOT / "benchmark-lm-0.6b.json")
        self.assertTrue(lm["request"]["lm_requested"])
        self.assertTrue(lm["capabilities"]["lm_initialized"])
        self.assertTrue(lm["capabilities"]["lm_used"])
        self.assertGreater(lm["peak_vram_bytes"], dit["peak_vram_bytes"])
        self.assertEqual(dit["artifacts"][0]["sha256"], lm["artifacts"][0]["sha256"])

    def test_cache_manifest_matches_the_registered_assets(self) -> None:
        registry = load_json(EVIDENCE_ROOT / "model-registry.json")
        manifest = load_json(EVIDENCE_ROOT / "cache-manifest.json")
        expected = {
            asset["id"]: (asset["size_bytes"], asset["sha256"]) for asset in registry["assets"]
        }
        actual = {
            asset["id"]: (asset["size_bytes"], asset["sha256"])
            for asset in manifest["verified_assets"]
        }
        self.assertEqual(expected, actual)
        self.assertFalse(manifest["persistent_drive_copy"])

    def test_drive_cache_evidence_proves_population_restore_and_rejection(self) -> None:
        registry = load_json(EVIDENCE_ROOT / "model-registry.json")
        evidence = load_json(EVIDENCE_ROOT / "drive-cache-evidence.json")
        registered_bytes = sum(asset["size_bytes"] for asset in registry["assets"])

        self.assertEqual(registered_bytes, evidence["registered_bytes"])
        self.assertEqual(len(registry["assets"]), evidence["registered_asset_count"])
        population = evidence["atomic_population"]
        self.assertEqual("populated", population["population_status"])
        self.assertGreater(population["persistent_copy_seconds"], 0)
        self.assertEqual(registered_bytes, population["verified_bytes"])

        restore = evidence["cold_restore"]
        self.assertEqual("restored", restore["restore_status"])
        self.assertGreater(restore["transfer_seconds"], 0)
        self.assertEqual(registered_bytes, restore["verified_bytes"])

        probes = evidence["rejection_probes"]
        self.assertFalse(probes["authoritative_cache_modified"])
        self.assertTrue(probes["temporary_fixture_removed"])
        self.assertIn("stale", probes["stale"]["message"])
        self.assertIn("Size mismatch", probes["corrupt"]["message"])
        self.assertEqual(0, evidence["session_cleanup"]["active_sessions_after"])

    def test_license_review_rejects_default_backend_and_weight_redistribution(self) -> None:
        evidence = load_json(EVIDENCE_ROOT / "license-review.json")
        decision = evidence["decision"]
        self.assertEqual("rejected", decision["ace_step_default_backend"])
        self.assertEqual("not_approved", decision["redistribution"])
        components = {component["id"]: component for component in evidence["components"]}
        self.assertEqual("MIT", components["ace-step-source"]["declared_license"])
        self.assertEqual("Apache-2.0", components["qwen3-0.6b-base"]["declared_license"])
        self.assertRegex(components["qwen3-0.6b-base"]["evidence_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
