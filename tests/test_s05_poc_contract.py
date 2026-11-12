"""Privacy-safe Colab PoC contract extraction tests."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


def load_extractor() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1] / "scripts" / "research" / "s05_extract_poc_contract.py"
    )
    spec = importlib.util.spec_from_file_location("s05_extract_poc_contract", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load PoC contract extractor")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PocContractTests(unittest.TestCase):
    def test_private_content_is_hashed_and_runtime_mutation_is_explicit(self) -> None:
        extractor = load_extractor()
        source = """
STYLE = "private style"
LYRICS = "private lyrics"
DURATION_SECONDS = 140
SEED = 12345
self.dtype = torch.float32
env = {"ACESTEP_DTYPE": "float32"}
params = GenerationParams(inference_steps=8, shift=3.0, thinking=True)
config = GenerationConfig(batch_size=1, use_random_seed=False)
lm.initialize()
"""
        notebook = {
            "cells": [
                {
                    "cell_type": "code",
                    "source": source.splitlines(keepends=True),
                    "outputs": [
                        {
                            "text": [
                                "llm_initialized=False use_lm=False\n"
                                "pred_latents dtype=torch.float32\nĐã xuất: output.mp3"
                            ]
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "poc.ipynb"
            path.write_text(json.dumps(notebook, ensure_ascii=False), encoding="utf-8")
            result = extractor.extract_contract(path)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("private style", serialized)
        self.assertNotIn("private lyrics", serialized)
        self.assertEqual(140.0, result["generation"]["duration_seconds"])
        self.assertEqual(12345, result["generation"]["seed"])
        self.assertTrue(result["runtime_mutation"]["pre_ampere_float32_source_patch"])
        self.assertTrue(result["generation"]["lm_initialization_called"])
        self.assertFalse(result["generation"]["lm_observed_initialized"])
        self.assertFalse(result["assessment"]["exact_reproduction_ready"])


if __name__ == "__main__":
    unittest.main()
