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
RUNTIME_EVIDENCE = ROOT / "docs" / "evidence" / "S05" / "heartmula-runtime-preflight.json"
T4_RUN = ROOT / "docs" / "evidence" / "S05" / "heartmula-t4-run.json"
T4_VERIFICATION = ROOT / "docs" / "evidence" / "S05" / "heartmula-t4-verification.json"
USER_REVIEW = ROOT / "docs" / "evidence" / "S05" / "heartmula-user-review.json"
DURABLE_IMPORT = ROOT / "docs" / "evidence" / "S05" / "heartmula-durable-import.json"
POC_CONTRACT = ROOT / "docs" / "evidence" / "S05" / "ace-step-colab-poc-contract.json"
ACE_PROBE = ROOT / "docs" / "evidence" / "S05" / "ace-step-vietnamese-reference-probe-contract.json"
ACE_REFERENCE_PREFLIGHT = ROOT / "docs" / "evidence" / "S05" / "ace-step-reference-preflight.json"
MODAL_APP = ROOT / "scripts" / "modal" / "s05_heartmula_app.py"


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
        self.assertEqual("3.11", evidence["runtime"]["python"])
        self.assertFalse(evidence["operator_authorization"]["automatic_retry"])
        self.assertEqual(1, evidence["probe"]["max_containers"])
        self.assertEqual(2, evidence["probe"]["scaledown_window_seconds"])
        self.assertEqual(len(request.takes), evidence["probe"]["take_count"])
        self.assertEqual(request.duration_seconds, evidence["probe"]["duration_seconds_per_take"])

    def test_modal_app_bootstraps_product_imports_before_loading_contracts(self) -> None:
        source = MODAL_APP.read_text(encoding="utf-8")
        bootstrap = source.index('sys.path.insert(0, str(local_root / "src"))')
        product_import = source.index("from agent_music.backends.heartmula import")
        self.assertLess(bootstrap, product_import)

    def test_cpu_runtime_and_cache_pass_before_gpu(self) -> None:
        evidence = cast(
            dict[str, Any],
            json.loads(RUNTIME_EVIDENCE.read_text(encoding="utf-8")),
        )
        self.assertEqual("cache_ready_gpu_not_run", evidence["status"])
        self.assertEqual("passed", evidence["image_contract"]["result"])
        self.assertEqual(
            sum(item.size_bytes for item in EXPECTED_CACHE_FILES),
            evidence["cache"]["total_bytes"],
        )
        self.assertTrue(evidence["cache"]["verified_hashes"])
        self.assertEqual(6, len(evidence["cache"]["weight_sha256"]))
        self.assertEqual(0, evidence["cleanup"]["active_apps"])
        self.assertEqual(0, evidence["cleanup"]["active_containers"])
        self.assertEqual(0.0, evidence["billing_before_gpu"]["billed_usd"])

    def test_t4_run_and_downloaded_artifacts_pass_without_overclaiming(self) -> None:
        run = cast(dict[str, Any], json.loads(T4_RUN.read_text(encoding="utf-8")))
        verification = cast(
            dict[str, Any],
            json.loads(T4_VERIFICATION.read_text(encoding="utf-8")),
        )
        self.assertEqual("succeeded", run["status"])
        self.assertEqual("Tesla T4", run["gpu"])
        self.assertEqual(2, len(run["takes"]))
        self.assertTrue(verification["passed"])
        self.assertEqual(
            {take["sha256"] for take in run["takes"]},
            {take["sha256"] for take in verification["takes"]},
        )
        self.assertEqual("narrow", run["assessment"]["vram_safety_margin"])
        self.assertEqual("not_yet_reviewed", run["assessment"]["subjective_music_quality"])
        self.assertEqual("not_claimed", run["assessment"]["instrumental_capability"])
        self.assertTrue(run["cleanup"]["app_stopped"])
        self.assertEqual(0, run["cleanup"]["active_containers"])
        self.assertEqual(0.0, run["billing_after_gpu"]["billed_usd"])

    def test_user_preference_and_real_take_restart_persistence_are_recorded(self) -> None:
        review = cast(dict[str, Any], json.loads(USER_REVIEW.read_text(encoding="utf-8")))
        imported = cast(
            dict[str, Any],
            json.loads(DURABLE_IMPORT.read_text(encoding="utf-8")),
        )
        self.assertEqual("good", review["subjective_music_quality"])
        self.assertEqual("take_b", review["preferred_backend_take_id"])
        self.assertTrue(review["backend_decision"]["additional_backend_options_requested"])
        self.assertFalse(review["backend_decision"]["automatic_fallback_allowed"])
        self.assertEqual("succeeded", imported["status"])
        self.assertFalse(imported["gpu_allocated"])
        self.assertTrue(imported["restart_verified"])
        self.assertTrue(imported["idempotent_retry_verified"])
        self.assertFalse(imported["private_creative_text_persisted"])
        self.assertEqual(2, len(imported["takes"]))
        self.assertEqual("take_b", imported["user_preference"]["backend_take_id"])
        preferred = next(take for take in imported["takes"] if take["backend_take_id"] == "take_b")
        self.assertEqual(
            review["preferred_take_sha256"],
            preferred["audio_sha256"],
        )

    def test_old_poc_is_private_and_next_gpu_probe_is_not_authorized(self) -> None:
        poc = cast(dict[str, Any], json.loads(POC_CONTRACT.read_text(encoding="utf-8")))
        probe = cast(dict[str, Any], json.loads(ACE_PROBE.read_text(encoding="utf-8")))
        reference = cast(
            dict[str, Any],
            json.loads(ACE_REFERENCE_PREFLIGHT.read_text(encoding="utf-8")),
        )
        self.assertFalse(poc["assessment"]["exact_reproduction_ready"])
        self.assertEqual(140.0, poc["generation"]["duration_seconds"])
        self.assertEqual(12345, poc["generation"]["seed"])
        self.assertTrue(poc["generation"]["lm_initialization_called"])
        self.assertFalse(poc["generation"]["lm_observed_initialized"])
        self.assertFalse(poc["generation"]["lm_used"])
        self.assertTrue(poc["runtime_mutation"]["pre_ampere_float32_source_patch"])
        self.assertNotIn("lyrics", poc["private_inputs"])
        self.assertEqual("prepared_not_authorized", probe["status"])
        self.assertFalse(probe["authorization"]["gpu_execution_approved"])
        self.assertFalse(probe["authorization"]["reference_media_reuse_approved"])
        self.assertIsNone(probe["cost_control"]["approved_max_metered_usd"])
        self.assertTrue(probe["cost_control"]["execution_must_remain_blocked_while_null"])
        self.assertFalse(probe["probe"]["automatic_retry"])
        self.assertFalse(probe["probe"]["provider_fallback"])
        self.assertFalse(probe["probe"]["initialize_lm_for_probe"])
        self.assertEqual("private_preflight_ready_gpu_blocked", reference["status"])
        self.assertFalse(reference["gpu_allocated"])
        self.assertFalse(reference["authorization"]["allowed_for_gpu"])
        self.assertEqual(
            probe["reference_media"]["prepared_reference_sha256"],
            reference["reference_audio"]["sha256"],
        )
        self.assertLess(reference["reference_audio"]["size_bytes"], 10_000_000)


if __name__ == "__main__":
    unittest.main()
