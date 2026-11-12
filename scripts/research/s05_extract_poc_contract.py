"""Extract a privacy-safe contract summary from the ignored Colab PoC notebook."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def extract_contract(notebook_path: Path) -> dict[str, Any]:
    """Return settings and hashes without exposing prompt, lyrics, or media names."""

    raw = notebook_path.read_bytes()
    notebook = json.loads(raw)
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise ValueError("Invalid notebook document")
    code_sources: list[str] = []
    all_outputs: list[str] = []
    private_values: dict[str, str] = {}
    for raw_cell in notebook["cells"]:
        if not isinstance(raw_cell, dict):
            continue
        source_value = raw_cell.get("source", [])
        source = "".join(source_value) if isinstance(source_value, list) else str(source_value)
        if raw_cell.get("cell_type") == "code":
            code_sources.append(source)
            try:
                tree = ast.parse(source)
            except SyntaxError:
                tree = None
            if tree is not None:
                for node in tree.body:
                    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                        continue
                    target = node.targets[0]
                    if not isinstance(target, ast.Name) or target.id not in {"STYLE", "LYRICS"}:
                        continue
                    value = ast.literal_eval(node.value)
                    if isinstance(value, str):
                        private_values[target.id] = value
        outputs = raw_cell.get("outputs", [])
        if isinstance(outputs, list):
            all_outputs.extend(json.dumps(item, ensure_ascii=False) for item in outputs)

    code = "\n".join(code_sources)
    output_text = "\n".join(all_outputs)
    duration = _number_assignment(code, "DURATION_SECONDS")
    seed = int(_number_assignment(code, "SEED"))
    return {
        "schema_version": 1,
        "notebook": {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "source_checkout": "unpinned_depth_1_clone",
        },
        "private_inputs": {
            "style_sha256": _private_hash(private_values, "STYLE"),
            "style_characters": len(private_values.get("STYLE", "")),
            "lyrics_sha256": _private_hash(private_values, "LYRICS"),
            "lyrics_characters": len(private_values.get("LYRICS", "")),
            "reference_audio": "one uploaded audio extracted from a 60-second video",
            "reference_consent_required_before_reuse": True,
        },
        "generation": {
            "operation": "reference_conditioned_text2music",
            "model": "acestep-v15-turbo",
            "lm_model": "acestep-5Hz-lm-0.6B",
            "lm_initialization_called": "lm.initialize(" in code,
            "lm_observed_initialized": "llm_initialized=True" in output_text,
            "lm_used": False,
            "vocal_language": "vi",
            "duration_seconds": duration,
            "seed": seed,
            "inference_steps": _keyword_number(code, "inference_steps"),
            "shift": _keyword_number(code, "shift"),
            "thinking": "thinking=True" in code,
            "batch_size": int(_keyword_number(code, "batch_size")),
            "use_random_seed": "use_random_seed=False" not in code,
            "audio_format": "mp3",
        },
        "runtime_mutation": {
            "pre_ampere_float32_source_patch": (
                "self.dtype = torch.float32" in code and '"ACESTEP_DTYPE": "float32"' in code
            ),
            "clean_upstream_revision": False,
        },
        "observed_output": {
            "float32_latents": "dtype=torch.float32" in output_text,
            "final_export_reported": "Đã xuất:" in output_text,
        },
        "assessment": {
            "exact_reproduction_ready": False,
            "reason": (
                "The notebook used an unpinned clone, an in-place source patch, and private "
                "reference audio; it is evidence of a successful PoC, not a reproducible pin."
            ),
        },
    }


def _private_hash(values: dict[str, str], name: str) -> str | None:
    value = values.get(name)
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None


def _number_assignment(source: str, name: str) -> float:
    match = re.search(rf"(?m)^\s*{re.escape(name)}\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*$", source)
    if match is None:
        raise ValueError(f"Missing notebook assignment: {name}")
    return float(match.group(1))


def _keyword_number(source: str, name: str) -> float:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*([0-9]+(?:\.[0-9]+)?)", source)
    if match is None:
        raise ValueError(f"Missing generation setting: {name}")
    return float(match.group(1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notebook", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    contract = extract_contract(args.notebook)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(contract, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
