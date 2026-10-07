"""The call ledger (R1): ~/.ga/llm/ledger/<UTC date>.jsonl, one row per call, and the window folds over it."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def append(self, row: dict[str, Any], at: datetime | None = None) -> None:
        at = at or now_utc()
        row = {"at": iso(at), **row}
        self.root.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")
        fd = os.open(self.root / f"{at:%Y-%m-%d}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def rows(self, since: datetime | None = None, now: datetime | None = None) -> Iterator[dict[str, Any]]:
        """Rows with at >= since (all rows when None), oldest first. A bad line is skipped."""
        if not self.root.is_dir():
            return
        floor = iso(since) if since else ""
        for f in sorted(self.root.glob("*.jsonl")):
            if since and f.stem < f"{since:%Y-%m-%d}":
                continue
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and str(r.get("at", "")) >= floor:
                    yield r

    def spend(self, purposes: set[str], window_s: int | None, now: datetime | None = None,
              item_id: str | None = None) -> float:
        """Actual USD of the rows of these purposes inside the rolling window (None = all time); item_id narrows."""
        now = now or now_utc()
        since = now - timedelta(seconds=window_s) if window_s else None
        total = 0.0
        for r in self.rows(since):
            if r.get("purpose") in purposes and (item_id is None or r.get("item_id") == item_id):
                try:
                    total += float(r.get("usd") or 0.0)
                except (TypeError, ValueError):
                    pass
        return total

    def purposes(self, window_s: int, now: datetime | None = None) -> set[str]:
        now = now or now_utc()
        return {str(r.get("purpose")) for r in self.rows(now - timedelta(seconds=window_s))}
