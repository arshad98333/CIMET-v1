from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2] / "test"


def test_dir() -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    return ROOT


def write_test_artifact(name: str, payload: Any) -> Path:
    path = test_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    elif isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=True), encoding="utf-8")
    return path


def append_jsonl(name: str, payload: dict[str, Any]) -> Path:
    path = test_dir() / name
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str, ensure_ascii=True) + "\n")
    return path
