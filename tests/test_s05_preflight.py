"""CPU tests for the bounded S05 HeartMuLa cache contract."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_music.s05_preflight import (
    ExpectedCacheFile,
    build_probe_request,
    validate_cache,
    write_manifest_atomic,
)


class S05PreflightTests(unittest.TestCase):
    def test_probe_is_small_product_valid_and_has_no_hidden_variation(self) -> None:
        request = build_probe_request("heartmula_probe_a")
        request.validate()
        self.assertEqual(20.0, request.duration_seconds)
        self.assertEqual(2, len(request.takes))
        self.assertEqual({2026111201, 2026111202}, {take.seed for take in request.takes})
        self.assertEqual(request.takes[0].settings, request.takes[1].settings)

    def test_exact_cache_files_and_atomic_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "model.bin"
            artifact.write_bytes(b"pinned-model")
            expected = (ExpectedCacheFile("model.bin", 12),)

            manifest = validate_cache(root, verify_hashes=False, expected_files=expected)
            manifest_path = root / "manifest.json"
            write_manifest_atomic(manifest_path, manifest)

            self.assertEqual(12, manifest["total_bytes"])
            self.assertEqual(manifest, json.loads(manifest_path.read_text(encoding="utf-8")))

    def test_missing_size_mismatch_and_path_escape_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "model.bin").write_bytes(b"wrong")
            with self.assertRaisesRegex(ValueError, "size mismatch"):
                validate_cache(
                    root,
                    verify_hashes=False,
                    expected_files=(ExpectedCacheFile("model.bin", 99),),
                )
            with self.assertRaises(FileNotFoundError):
                validate_cache(
                    root,
                    verify_hashes=False,
                    expected_files=(ExpectedCacheFile("missing.bin", 1),),
                )
            with self.assertRaisesRegex(ValueError, "escaped"):
                validate_cache(
                    root,
                    verify_hashes=False,
                    expected_files=(ExpectedCacheFile("../escape.bin", 1),),
                )


if __name__ == "__main__":
    unittest.main()
