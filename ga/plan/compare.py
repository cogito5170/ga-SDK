"""``ga plan compare <draft.md> <final.md>`` (CMD-GA40 S3): how far baseline's final directive moved from the draft.

Field-level: goal (same / changed), scope and done_when by id (added / removed / changed text), refs (added /
removed). Score = unchanged units / all units, one unit each for the goal, every scope id, every done_when id and
every ref in either file: 1.0 means the final is the draft. Reads two files; writes nothing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..forms import FormError, parse_text


def head_of(path: Path) -> dict[str, Any]:
    head, _ = parse_text(Path(path).read_text(encoding="utf-8"))
    return head


def _by_id(head: dict[str, Any], key: str) -> dict[str, str]:
    return {str(i.get("id")): str(i.get("text", "")) for i in head.get(key) or [] if isinstance(i, dict)}


def _ws(s: Any) -> str:
    return " ".join(str(s or "").split())


def compare(draft: dict[str, Any], final: dict[str, Any]) -> dict[str, Any]:
    changes: list[dict[str, str]] = []
    units = same = 1
    if _ws(draft.get("goal")) != _ws(final.get("goal")):
        changes.append({"field": "goal", "change": "changed"})
        same -= 1
    for key in ("scope", "done_when"):
        a, b = _by_id(draft, key), _by_id(final, key)
        for i in sorted(set(a) | set(b)):
            units += 1
            if i not in b:
                changes.append({"field": key, "id": i, "change": "removed"})
            elif i not in a:
                changes.append({"field": key, "id": i, "change": "added"})
            elif _ws(a[i]) != _ws(b[i]):
                changes.append({"field": key, "id": i, "change": "changed"})
            else:
                same += 1
    ra, rb = [_ws(r) for r in draft.get("refs") or []], [_ws(r) for r in final.get("refs") or []]
    for r in sorted(set(ra) | set(rb)):
        units += 1
        if r not in rb:
            changes.append({"field": "refs", "ref": r[:120], "change": "removed"})
        elif r not in ra:
            changes.append({"field": "refs", "ref": r[:120], "change": "added"})
        else:
            same += 1
    return {"schema": "plan-compare/1", "draft": draft.get("id"), "final": final.get("id"), "changes": changes,
            "units": units, "unchanged": same, "score": round(same / units, 3)}


def compare_files(draft: Path, final: Path) -> dict[str, Any]:
    return compare(head_of(draft), head_of(final))


__all__ = ["FormError", "compare", "compare_files", "head_of"]
