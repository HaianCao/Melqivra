"""S06 bounded real-provider authorization tests."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from typing import Any, cast

from agent_music.s06_probe import (
    MODAL_PRICE_CHECKED_AT,
    MODAL_PRICING_SOURCE,
    PROBE_GPU_CALLS,
    PROBE_MAX_FUNCTION_SECONDS,
    estimated_max_metered_usd,
    validate_probe_authorization,
)

ROOT = Path(__file__).resolve().parents[1]
MODAL_APP = ROOT / "src" / "agent_music" / "remote" / "modal_app.py"
PROBE_PLAN = ROOT / "docs" / "evidence" / "S06" / "modal-lifecycle-probe-plan.json"
PROBE_EVIDENCE = ROOT / "docs" / "evidence" / "S06" / "modal-lifecycle-evidence.json"
PROBE_VERIFICATION = ROOT / "docs" / "evidence" / "S06" / "modal-lifecycle-verification.json"


def authorization(*, ceiling: float) -> dict[str, object]:
    return {
        "gpu_execution_approved": True,
        "provider_price_prechecked": True,
        "approved_max_metered_usd": ceiling,
        "provider": "modal",
        "provider_price_source": MODAL_PRICING_SOURCE,
        "provider_price_checked_at": MODAL_PRICE_CHECKED_AT,
        "probe_gpu_calls": PROBE_GPU_CALLS,
        "max_function_seconds": PROBE_MAX_FUNCTION_SECONDS,
        "estimated_max_metered_usd": estimated_max_metered_usd(),
        "run_id": "s06-modal-lifecycle-20261113-a",
    }


class S06ProbeTests(unittest.TestCase):
    def test_price_bound_is_fixed_and_authorization_accepts_exact_contract(self) -> None:
        self.assertEqual(0.069271, estimated_max_metered_usd())
        validate_probe_authorization(
            authorization(ceiling=0.08),
            expected_run_id="s06-modal-lifecycle-20261113-a",
        )

    def test_authorization_rejects_missing_approval_or_insufficient_ceiling(self) -> None:
        missing = authorization(ceiling=0.08)
        missing["gpu_execution_approved"] = False
        with self.assertRaisesRegex(ValueError, "GPU execution approval"):
            validate_probe_authorization(
                missing,
                expected_run_id="s06-modal-lifecycle-20261113-a",
            )

        with self.assertRaisesRegex(ValueError, "below the bounded estimate"):
            validate_probe_authorization(
                authorization(ceiling=0.06),
                expected_run_id="s06-modal-lifecycle-20261113-a",
            )

    def test_tracked_plan_matches_code_and_routes_only_the_probe_to_t4(self) -> None:
        plan = cast(dict[str, Any], json.loads(PROBE_PLAN.read_text(encoding="utf-8")))
        self.assertEqual(PROBE_GPU_CALLS, plan["probe_gpu_calls"])
        self.assertEqual(PROBE_MAX_FUNCTION_SECONDS, plan["max_function_seconds"])
        self.assertEqual(estimated_max_metered_usd(), plan["estimated_max_metered_usd"])
        self.assertEqual("executed_passed", plan["status"])
        self.assertTrue(plan["authorization"]["gpu_execution_approved"])
        self.assertEqual(0.08, plan["authorization"]["approved_max_metered_usd"])

        source = MODAL_APP.read_text(encoding="utf-8")
        self.assertIn('GPU_PROBE_KINDS = {"harness.gpu_healthcheck"}', source)
        self.assertIn("def run_gpu_probe", source)
        self.assertIn("timeout=120", source)
        self.assertIn("function = run_gpu_probe", source)

    def test_real_t4_evidence_passes_exact_lifecycle_contract(self) -> None:
        evidence = cast(
            dict[str, Any],
            json.loads(PROBE_EVIDENCE.read_text(encoding="utf-8")),
        )
        verification = cast(
            dict[str, Any],
            json.loads(PROBE_VERIFICATION.read_text(encoding="utf-8")),
        )
        self.assertEqual("passed", evidence["status"])
        self.assertEqual("s06-modal-lifecycle-20261113-a", evidence["run_id"])
        calls = cast(list[dict[str, Any]], evidence["calls"])
        self.assertEqual(["cold", "warm", "recovered"], [call["readiness_mode"] for call in calls])
        self.assertTrue(all("T4" in call["gpu"] for call in calls))
        self.assertTrue(all(call["total_seconds"] >= 0 for call in calls))
        self.assertTrue(evidence["cleanup"]["app_stop_completed"])
        self.assertEqual("reclaimed", evidence["cleanup"]["runner_state"])
        self.assertAlmostEqual(
            calls[0]["total_seconds"] - calls[1]["total_seconds"],
            evidence["warm_total_gain_seconds"],
        )

        self.assertEqual("passed", verification["status"])
        self.assertEqual(
            hashlib.sha256(PROBE_EVIDENCE.read_bytes()).hexdigest(),
            verification["evidence_sha256"],
        )
        self.assertEqual(3, verification["gpu_call_count"])
        self.assertEqual(0, verification["active_apps_after"])
        self.assertEqual(0, verification["active_containers_after"])
        self.assertTrue(verification["cleanup_passed"])
        self.assertLess(
            verification["billing"]["observed_metered_usd"],
            verification["price_contract"]["approved_max_metered_usd"],
        )


if __name__ == "__main__":
    unittest.main()
