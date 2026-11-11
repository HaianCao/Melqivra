"""CPU-safe tests for the S01 benchmark evidence helpers."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "colab" / "s01_benchmark.py"


def load_benchmark_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("s01_benchmark", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load S01 benchmark module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class S01BenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.benchmark = load_benchmark_module()

    def test_fixture_selection_and_caption_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixtures.json"
            path.write_text(
                json.dumps(
                    {
                        "briefs": [
                            {
                                "id": "vi-test",
                                "concept": "Khái niệm gốc.",
                                "genres": ["folk", "ambient"],
                                "moods": ["calm"],
                                "vocal": "instrumental",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            fixture = self.benchmark.select_fixture(path, "vi-test")
            caption = self.benchmark.fixture_caption(fixture)
            self.assertIn("Khái niệm gốc", caption)
            self.assertIn("folk, ambient", caption)

    def test_unknown_fixture_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixtures.json"
            path.write_text('{"briefs": []}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unknown fixture ID"):
                self.benchmark.select_fixture(path, "missing")

    def test_fixture_generation_inputs_preserve_project_authored_lyrics(self) -> None:
        fixture = {
            "concept": "Một lời hẹn qua hai múi giờ.",
            "genres": ["pop"],
            "moods": ["hopeful"],
            "vocal": "original duet",
            "lyrics": "[Verse]\nMình gửi nhau một câu chào",
        }
        caption, lyrics, instrumental = self.benchmark.fixture_generation_inputs(fixture)
        self.assertIn("Một lời hẹn", caption)
        self.assertEqual(fixture["lyrics"], lyrics)
        self.assertFalse(instrumental)

    def test_fixture_generation_inputs_default_to_instrumental(self) -> None:
        fixture = {
            "concept": "Nhạc nền buổi chiều.",
            "genres": ["lo-fi"],
            "moods": ["focused"],
            "vocal": "no lead vocal",
        }
        _, lyrics, instrumental = self.benchmark.fixture_generation_inputs(fixture)
        self.assertEqual("[Instrumental]", lyrics)
        self.assertTrue(instrumental)

    def test_lm_conditioning_evidence_hashes_metadata_and_audio_codes(self) -> None:
        result = type(
            "Result",
            (),
            {
                "extra_outputs": {"lm_metadata": {"caption": "rewritten"}},
                "audios": [{"params": {"audio_codes": "<|audio_code_1|>"}}],
            },
        )()
        evidence = self.benchmark.lm_conditioning_evidence(result, lm_requested=True)
        self.assertTrue(evidence["lm_metadata_present"])
        self.assertTrue(evidence["lm_audio_codes_present"])
        self.assertRegex(str(evidence["lm_metadata_sha256"]), r"^[0-9a-f]{64}$")
        self.assertRegex(str(evidence["lm_audio_codes_sha256"]), r"^[0-9a-f]{64}$")

    def test_lm_conditioning_evidence_rejects_missing_codes(self) -> None:
        result = type(
            "Result",
            (),
            {"extra_outputs": {"lm_metadata": {}}, "audios": [{"params": {}}]},
        )()
        with self.assertRaisesRegex(RuntimeError, "without returning generated audio codes"):
            self.benchmark.lm_conditioning_evidence(result, lm_requested=True)

    def test_ffmpeg_loudness_parser_uses_summary_values(self) -> None:
        stderr = "I: -70.0 LUFS\nPeak: -20.0 dBFS\nI: -16.4 LUFS\nPeak: -1.2 dBFS"
        self.assertEqual((-16.4, -1.2), self.benchmark._parse_ffmpeg_loudness(stderr))

    def test_ffmpeg_loudness_parser_rejects_infinite_silence_metrics(self) -> None:
        with self.assertRaisesRegex(ValueError, "not finite"):
            self.benchmark._parse_ffmpeg_loudness("I: -inf LUFS\nPeak: -inf dBFS")

    def test_runtime_environment_removes_notebook_only_matplotlib_backend(self) -> None:
        previous = os.environ.get("MPLBACKEND")
        try:
            os.environ["MPLBACKEND"] = "module://matplotlib_inline.backend_inline"
            self.benchmark.sanitize_runtime_environment()
            self.assertNotIn("MPLBACKEND", os.environ)
        finally:
            if previous is not None:
                os.environ["MPLBACKEND"] = previous

    def test_precision_override_is_explicit_and_reversible(self) -> None:
        previous = os.environ.get("ACESTEP_DTYPE")
        try:
            self.benchmark.configure_precision("float32")
            self.assertEqual("float32", os.environ["ACESTEP_DTYPE"])
            self.benchmark.configure_precision("upstream_default")
            self.assertNotIn("ACESTEP_DTYPE", os.environ)
        finally:
            if previous is None:
                os.environ.pop("ACESTEP_DTYPE", None)
            else:
                os.environ["ACESTEP_DTYPE"] = previous

    def test_explicit_precision_rejects_silent_fallback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "was not honored"):
            self.benchmark.validate_observed_precision("float32", "torch.float16")
        self.assertEqual(
            "float16",
            self.benchmark.validate_observed_precision("upstream_default", "torch.float16"),
        )

    def test_end_to_end_total_includes_prior_phases(self) -> None:
        args = Namespace(
            provision_seconds=4.0,
            runtime_setup_seconds=10.0,
            model_download_seconds=20.0,
        )
        self.assertEqual(40.0, self.benchmark.end_to_end_total(6.0, args))

    def test_parser_records_provider_without_relabeling_modal_as_colab(self) -> None:
        parser = self.benchmark.build_parser()
        args = parser.parse_args(
            [
                "--fixtures",
                "fixtures.json",
                "--cache-manifest",
                "cache.json",
                "--result",
                "result.json",
                "--run-id",
                "modal-test",
                "--provider",
                "modal",
                "--provider-cli",
                "1.6.3",
                "--colab-cli",
                "not-applicable",
            ]
        )
        self.assertEqual("modal", args.provider)
        self.assertEqual("1.6.3", args.provider_cli)
        self.assertEqual("not-applicable", args.colab_cli)


if __name__ == "__main__":
    unittest.main()
