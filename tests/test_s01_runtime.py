"""Tests for the reproducible S01 Colab runtime tooling."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "colab" / "s01_runtime.py"


def load_runtime_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("s01_runtime", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load S01 runtime module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class S01RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = load_runtime_module()

    def test_allow_patterns_limit_primary_repository_to_registered_families(self) -> None:
        assets = [
            {"path": "acestep-v15-turbo/model.safetensors"},
            {"path": "vae/diffusion_pytorch_model.safetensors"},
            {"path": "Qwen3-Embedding-0.6B/model.safetensors"},
        ]
        self.assertEqual(
            ["Qwen3-Embedding-0.6B/**", "acestep-v15-turbo/**", "vae/**"],
            self.runtime._allow_patterns(assets),
        )

    def test_root_asset_downloads_the_complete_small_lm_repository(self) -> None:
        self.assertIsNone(self.runtime._allow_patterns([{"path": "model.safetensors"}]))

    def test_registered_asset_verification_rejects_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint_root = Path(directory)
            model = checkpoint_root / "model.safetensors"
            model.write_bytes(b"not-the-registered-model")
            registry: dict[str, Any] = {
                "assets": [
                    {
                        "id": "candidate",
                        "repository": "ACE-Step/Ace-Step1.5",
                        "revision": "a" * 40,
                        "path": "model.safetensors",
                        "size_bytes": model.stat().st_size,
                        "sha256": "0" * 64,
                    }
                ]
            }
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                self.runtime.verify_registered_assets(registry, checkpoint_root)

    def test_registered_asset_verification_records_exact_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint_root = Path(directory)
            model = checkpoint_root / "model.safetensors"
            payload = b"pinned-model-fixture"
            model.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            registry: dict[str, Any] = {
                "assets": [
                    {
                        "id": "candidate",
                        "repository": "ACE-Step/Ace-Step1.5",
                        "revision": "a" * 40,
                        "path": "model.safetensors",
                        "size_bytes": len(payload),
                        "sha256": digest,
                    }
                ]
            }
            verified = self.runtime.verify_registered_assets(registry, checkpoint_root)
            self.assertEqual(digest, verified[0]["sha256"])

    def test_atomic_json_replaces_existing_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text("stale", encoding="utf-8")
            self.runtime.atomic_write_json(path, {"status": "succeeded"})
            self.assertEqual({"status": "succeeded"}, json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
