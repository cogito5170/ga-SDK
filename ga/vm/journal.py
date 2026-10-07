"""VI-10a: journal/1 rows on disk and the work-item states as fold(journal). No model, no session."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..forms import hard, validate


class JournalError(ValueError):
    pass


def inputs_hash(inputs: Any) -> str:
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def _check(row: Any, where: str) -> None:
    probs = hard(validate(row, "journal/1"))
    if probs:
        raise JournalError(f"{where}: " + "; ".join(str(p) for p in probs))


def fold(rows: list[dict[str, Any]]) -> dict[str, str]:
    state: dict[str, str] = {}
    for i, row in enumerate(rows, 1):
        _check(row, f"row {i}")
        if row["id"] != f"J-{i}":
            raise JournalError(f"row {i}: id {row['id']} is not J-{i}")
        cur = state.get(row["item"], "NONE")
        if row["from_state"] != cur:
            raise JournalError(f"{row['id']}: {row['item']} is {cur}, the row says {row['from_state']}")
        state[row["item"]] = row["to_state"]
    return {k: v for k, v in sorted(state.items()) if v != "NONE"}


def dumps(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


def append(path: str | Path, row: dict[str, Any]) -> None:
    _check(row, "append")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(dumps(row))


def read(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
