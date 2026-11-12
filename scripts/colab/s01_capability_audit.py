"""Audit the pinned ACE-Step model matrix and reject unsupported operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

SOURCE_REVISION = "dce621408bee8c31b4fcf4811682eb9359e1bc94"
MODEL_ID = "acestep-v15-turbo"
CAPABILITY_COLUMNS = {
    "reference_audio": "Refer audio",
    "text2music": "Text2Music",
    "cover": "Cover",
    "repaint": "Repaint",
    "extract": "Extract",
    "lego": "Lego",
    "complete": "Complete",
}


class UnsupportedCapabilityError(RuntimeError):
    """The pinned model does not advertise the requested operation."""


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return one immutable file digest."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def parse_model_capabilities(readme: str, model_id: str = MODEL_ID) -> dict[str, bool]:
    """Parse one DiT model row from the upstream Markdown capability matrix."""

    lines = readme.splitlines()
    for index, line in enumerate(lines):
        if "| DiT Model |" not in line:
            continue
        headers = [cell.strip() for cell in line.strip().strip("|").split("|")]
        for row in lines[index + 2 :]:
            if not row.lstrip().startswith("|"):
                break
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            if not cells or cells[0].strip("`") != model_id:
                continue
            if len(cells) != len(headers):
                raise ValueError(f"Malformed capability row for {model_id}")
            values = dict(zip(headers, cells, strict=True))
            capabilities = {
                key: values[column] == "✅" for key, column in CAPABILITY_COLUMNS.items()
            }
            capabilities["extend"] = capabilities["complete"]
            return capabilities
    raise ValueError(f"Capability matrix has no row for {model_id}")


def require_capability(capabilities: dict[str, bool], operation: str) -> None:
    """Fail before GPU scheduling when an operation is not advertised."""

    if operation not in capabilities:
        raise UnsupportedCapabilityError(f"Unknown capability: {operation}")
    if not capabilities[operation]:
        raise UnsupportedCapabilityError(
            f"{MODEL_ID} does not support {operation}; no GPU job was scheduled"
        )


def audit_source(source: Path) -> dict[str, object]:
    """Verify the source pin, file hashes, and selected model capabilities."""

    source = source.resolve()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    if revision != SOURCE_REVISION:
        raise RuntimeError(f"Unexpected source revision: {revision}")
    readme = source / "README.md"
    inference = source / "acestep" / "inference.py"
    capabilities = parse_model_capabilities(readme.read_text(encoding="utf-8"))
    extend_rejection = ""
    try:
        require_capability(capabilities, "extend")
    except UnsupportedCapabilityError as error:
        extend_rejection = str(error)
    else:
        raise RuntimeError("Expected turbo extend capability to be unsupported")
    return {
        "schema_version": 1,
        "source": {
            "repository": "https://github.com/ace-step/ACE-Step-1.5.git",
            "revision": revision,
        },
        "model_id": MODEL_ID,
        "source_files": {
            "README.md": sha256_file(readme),
            "acestep/inference.py": sha256_file(inference),
        },
        "capabilities": capabilities,
        "unsupported_preflight": {
            "operation": "extend",
            "gpu_scheduled": False,
            "message": extend_rejection,
        },
        "result": "passed",
    }


def atomic_write_json(path: Path, value: object) -> None:
    """Atomically write one UTF-8 JSON record."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    atomic_write_json(args.result, audit_source(args.source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
