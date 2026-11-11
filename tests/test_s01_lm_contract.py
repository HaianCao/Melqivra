"""CPU-safe tests for the S01 LM contract probe."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "colab" / "s01_lm_contract.py"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("s01_lm_contract", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load S01 LM contract module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class S01LmContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_module()

    def test_contract_fingerprints_require_metadata_and_codes(self) -> None:
        evidence = self.contract.contract_fingerprints(
            {"caption": "rewritten", "bpm": 104},
            "<|audio_code_1|>",
        )
        self.assertEqual(["bpm", "caption"], evidence["lm_metadata_fields"])
        self.assertRegex(str(evidence["lm_metadata_sha256"]), r"^[0-9a-f]{64}$")
        self.assertRegex(str(evidence["lm_audio_codes_sha256"]), r"^[0-9a-f]{64}$")

    def test_contract_fingerprints_record_empty_outputs(self) -> None:
        evidence = self.contract.contract_fingerprints({}, "")
        self.assertFalse(evidence["lm_metadata_present"])
        self.assertFalse(evidence["lm_audio_codes_present"])
        self.assertIsNone(evidence["lm_metadata_sha256"])
        self.assertIsNone(evidence["lm_audio_codes_sha256"])

    def test_atomic_json_replaces_existing_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text("stale", encoding="utf-8")
            self.contract.atomic_write_json(path, {"result": "passed"})
            self.assertIn('"passed"', path.read_text(encoding="utf-8"))

    def test_parser_accepts_provider_metadata(self) -> None:
        args = self.contract.build_parser().parse_args(
            [
                "--source",
                "source",
                "--fixtures",
                "fixtures.json",
                "--run-id",
                "modal-contract",
                "--result",
                "result.json",
                "--provider",
                "modal",
                "--provider-cli",
                "1.6.3",
                "--session-name",
                "melqivra-s01-modal-t4",
            ]
        )
        self.assertEqual("modal", args.provider)
        self.assertEqual("1.6.3", args.provider_cli)
        self.assertEqual("melqivra-s01-modal-t4", args.session_name)


if __name__ == "__main__":
    unittest.main()
