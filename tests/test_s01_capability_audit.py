"""Pinned ACE-Step capability-matrix audit tests."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "colab" / "s01_capability_audit.py"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("s01_capability_audit", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load capability audit")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CapabilityAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit = load_module()

    def test_turbo_matrix_parses_supported_and_unsupported_operations(self) -> None:
        readme = """
| DiT Model | Refer audio | Text2Music | Cover | Repaint | Extract | Lego | Complete |
|---|---|---|---|---|---|---|---|
| `acestep-v15-turbo` | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
"""
        capabilities = self.audit.parse_model_capabilities(readme)
        self.assertTrue(capabilities["reference_audio"])
        self.assertTrue(capabilities["repaint"])
        self.assertFalse(capabilities["complete"])
        self.assertFalse(capabilities["extend"])

    def test_unsupported_extend_fails_before_gpu_scheduling(self) -> None:
        with self.assertRaisesRegex(
            self.audit.UnsupportedCapabilityError,
            "no GPU job was scheduled",
        ):
            self.audit.require_capability({"extend": False}, "extend")


if __name__ == "__main__":
    unittest.main()
