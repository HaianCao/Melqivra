"""Verify and record a downloaded Modal reproduction of the Colab PoC."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from agent_music.s01_reproduction import compare_reproduction


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return cast(dict[str, Any], value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modal-record", type=Path, required=True)
    parser.add_argument("--colab-record", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--transfer-seconds", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare_reproduction(
        modal_record=_load(args.modal_record),
        colab_record=_load(args.colab_record),
        local_artifact=args.artifact,
        transfer_seconds=args.transfer_seconds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
