"""Safe bounded service observability contracts."""

from __future__ import annotations

import unittest

from agent_music.observability import (
    InMemoryServiceTelemetry,
    allowed_route_classes,
    classify_http_route,
    resolve_trace_context,
)


class ServiceObservabilityTests(unittest.TestCase):
    def test_route_labels_have_fixed_cardinality_and_hide_object_ids(self) -> None:
        cases = {
            "/healthz": "health",
            "/v1/gpu/jobs": "gpu_jobs_collection",
            "/v1/gpu/jobs/job_private": "gpu_job_item",
            "/v1/gpu/jobs/job_private/events": "gpu_job_events",
            "/v1/artifacts/art_private": "artifact_item",
            "/v1/unknown/private": "unknown",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                observed = classify_http_route(path)
                self.assertEqual(expected, observed)
                self.assertIn(observed, allowed_route_classes())
                self.assertNotIn("private", observed)

    def test_trace_context_accepts_only_strict_nonzero_trace_ids(self) -> None:
        supplied_trace = "1" * 32
        observed = resolve_trace_context(f"00-{supplied_trace}-{'2' * 16}-00")
        self.assertEqual(supplied_trace, observed.trace_id)
        self.assertRegex(observed.traceparent, rf"^00-{supplied_trace}-[0-9a-f]{{16}}-00$")

        invalid = resolve_trace_context("00-unsafe")
        zero = resolve_trace_context(f"00-{'0' * 32}-{'2' * 16}-01")
        zero_parent = resolve_trace_context(f"00-{supplied_trace}-{'0' * 16}-01")
        self.assertNotEqual("0" * 32, zero.trace_id)
        self.assertNotEqual(supplied_trace, zero_parent.trace_id)
        self.assertNotEqual("unsafe", invalid.trace_id)

    def test_metrics_are_content_free_copied_and_validate_inputs(self) -> None:
        telemetry = InMemoryServiceTelemetry(wall_clock=lambda: 100.0)
        telemetry.request_started()
        telemetry.request_finished(
            route_class="gpu_job_item",
            status_code=429,
            duration_milliseconds=12.3456,
            rate_limited=True,
        )
        snapshot = telemetry.snapshot()

        self.assertEqual(0, snapshot.active_requests)
        self.assertEqual(1, snapshot.requests_total)
        self.assertEqual(1, snapshot.rate_limited_total)
        self.assertEqual({"4xx": 1}, snapshot.responses_by_status_class)
        self.assertEqual(12.346, snapshot.routes["gpu_job_item"].duration_milliseconds_total)
        self.assertNotIn("user", repr(snapshot))

        snapshot.responses_by_status_class["5xx"] = 99
        self.assertNotIn("5xx", telemetry.snapshot().responses_by_status_class)
        with self.assertRaises(ValueError):
            telemetry.request_finished(
                route_class="private-job-id",
                status_code=200,
                duration_milliseconds=1,
                rate_limited=False,
            )


if __name__ == "__main__":
    unittest.main()
