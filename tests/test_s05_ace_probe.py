"""CPU-only guards for the bounded private ACE-Step T4 experiment."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

from agent_music.s05_ace_probe import (
    ACE_FLOAT32_REVISION,
    MODAL_PRICE_CHECKED_AT,
    MODAL_PRICING_SOURCE,
    PROBE_MAX_FUNCTION_SECONDS,
    decode_exact_utf8,
    estimated_max_metered_usd,
    validate_probe_authorization,
)

ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "scripts" / "research" / "s05_prepare_ace_reference.py"


def load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class S05AceProbeTests(unittest.TestCase):
    def test_private_input_decode_preserves_exact_windows_newlines_for_hashing(self) -> None:
        payload = b'{\r\n  "caption": "private"\r\n}\r\n'
        decoded, sha256 = decode_exact_utf8(payload)
        self.assertEqual(payload, decoded.encode("utf-8"))
        self.assertEqual(
            "b9d98f322e410daf06afcc1aa7820a7ddee56153dd7a43271013821c8ab80410",
            sha256,
        )

    def test_reference_command_is_audio_only_small_and_bounded(self) -> None:
        prepare = load_module(PREPARE, "s05_prepare_ace_reference")
        command = prepare.ffmpeg_command(
            Path("private video.mp4"),
            Path("reference.wav"),
            start=0.0,
            duration=20.0,
        )
        self.assertIn("-vn", command)
        self.assertEqual("1", command[command.index("-ac") + 1])
        self.assertEqual("48000", command[command.index("-ar") + 1])
        self.assertEqual("20.0", command[command.index("-t") + 1])
        with self.assertRaisesRegex(ValueError, "under 60 seconds"):
            prepare.ffmpeg_command(Path("x"), Path("y"), start=0.0, duration=60.0)

    def test_gpu_authorization_requires_consent_cost_and_exact_hashes(self) -> None:
        authorization = {
            "gpu_execution_approved": True,
            "reference_media_reuse_approved": True,
            "provider_price_prechecked": True,
            "approved_max_metered_usd": 0.2,
            "provider": "modal",
            "provider_price_source": MODAL_PRICING_SOURCE,
            "provider_price_checked_at": MODAL_PRICE_CHECKED_AT,
            "max_function_seconds": PROBE_MAX_FUNCTION_SECONDS,
            "estimated_max_metered_usd": estimated_max_metered_usd(),
            "source_revision": ACE_FLOAT32_REVISION,
            "reference_sha256": "a" * 64,
            "private_inputs_sha256": "b" * 64,
        }
        validate_probe_authorization(
            authorization,
            reference_sha256="a" * 64,
            private_inputs_sha256="b" * 64,
        )
        authorization["reference_media_reuse_approved"] = False
        with self.assertRaisesRegex(ValueError, "approvals are required"):
            validate_probe_authorization(
                authorization,
                reference_sha256="a" * 64,
                private_inputs_sha256="b" * 64,
            )

    def test_gpu_authorization_rejects_cost_ceiling_below_worst_case_estimate(self) -> None:
        authorization = {
            "gpu_execution_approved": True,
            "reference_media_reuse_approved": True,
            "provider_price_prechecked": True,
            "approved_max_metered_usd": estimated_max_metered_usd() - 0.001,
            "provider": "modal",
            "provider_price_source": MODAL_PRICING_SOURCE,
            "provider_price_checked_at": MODAL_PRICE_CHECKED_AT,
            "max_function_seconds": PROBE_MAX_FUNCTION_SECONDS,
            "estimated_max_metered_usd": estimated_max_metered_usd(),
            "source_revision": ACE_FLOAT32_REVISION,
            "reference_sha256": "a" * 64,
            "private_inputs_sha256": "b" * 64,
        }
        with self.assertRaisesRegex(ValueError, "below the bounded price estimate"):
            validate_probe_authorization(
                authorization,
                reference_sha256="a" * 64,
                private_inputs_sha256="b" * 64,
            )

    def test_gpu_authorization_rejects_stale_price_contract(self) -> None:
        authorization = {
            "gpu_execution_approved": True,
            "reference_media_reuse_approved": True,
            "provider_price_prechecked": True,
            "approved_max_metered_usd": 0.2,
            "provider": "modal",
            "provider_price_source": MODAL_PRICING_SOURCE,
            "provider_price_checked_at": "2026-11-12",
            "max_function_seconds": PROBE_MAX_FUNCTION_SECONDS,
            "estimated_max_metered_usd": estimated_max_metered_usd(),
            "source_revision": ACE_FLOAT32_REVISION,
            "reference_sha256": "a" * 64,
            "private_inputs_sha256": "b" * 64,
        }
        with self.assertRaisesRegex(ValueError, "price-check date"):
            validate_probe_authorization(
                authorization,
                reference_sha256="a" * 64,
                private_inputs_sha256="b" * 64,
            )

    def test_notebook_private_values_are_not_returned_in_safe_manifest_code(self) -> None:
        prepare = load_module(PREPARE, "s05_prepare_ace_reference_private")
        notebook = {
            "cells": [
                {
                    "cell_type": "code",
                    "source": ['STYLE = "private caption"\n', 'LYRICS = "private lyrics"\n'],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "poc.ipynb"
            path.write_text(json.dumps(notebook), encoding="utf-8")
            self.assertEqual(
                ("private caption", "private lyrics"),
                prepare.extract_private_inputs(path),
            )


if __name__ == "__main__":
    unittest.main()
