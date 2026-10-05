"""ga ask's files (CMD-GA36): one folder, ``$GA_ASK_HOME`` or ``~/.ga-ask``.

    ask.json        optional settings: bridge_config, daily_agy_turns (default 10), solve {backend, model, cap, base_url}
    day.json        today's agy turns (the daily cap counts these)
    ledger.jsonl    one row per run that spent model tokens: run, model ask, solve (``ga ask "오늘 얼마나 썼어?"`` reads it)
    runs/<id>.log   the run log a person reads; ga ui streams it (SSE)
    bridge.pid      a running ``ga bridge`` (``ga ask "멈춰"`` stops it); solve.pid the same for a solve loop
Numbers and labels only in day.json and the ledger: no prompt, answer or token of any kind.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

DAILY_AGY_TURNS = 10
PER_TURN_DEFAULT = 10000   # AGY_FACTS: ~10k input tokens of fixed overhead per agy turn (V0 9,852)
AGV_OVERHEAD = 9852        # AGY_FACTS V0: the measured fixed overhead of one agy turn
SOLVE_DEFAULT = {"backend": "agv", "model": "gpt-oss-120b-medium", "cap": 8, "base_url": None}


def home() -> Path:
    h = Path(os.environ.get("GA_ASK_HOME") or "~/.ga-ask").expanduser()
    h.mkdir(parents=True, exist_ok=True)
    return h


def today(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock()).strftime("%Y-%m-%d")


def settings(h: Path | None = None) -> dict[str, Any]:
    h = h or home()
    f = h / "ask.json"
    raw: dict[str, Any] = {}
    if f.exists():
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            raw = {}
    out = {"bridge_config": os.environ.get("GA_BRIDGE_CONFIG") or "~/agy-bridge.json",
           "daily_agy_turns": DAILY_AGY_TURNS, **raw}
    out["solve"] = {**SOLVE_DEFAULT, **(raw.get("solve") or {})}
    return out


class DayTurns:
    """agy turns spent today against the daily cap (default 10). A new day starts at 0."""

    def __init__(self, h: Path, cap: int = DAILY_AGY_TURNS, clock: Callable[[], float] = time.time):
        self.path, self.cap, self.clock = h / "day.json", int(cap), clock

    def used(self) -> int:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        return int(d.get("agy_turns", 0)) if d.get("date") == today(self.clock) else 0

    def left(self) -> int:
        return max(self.cap - self.used(), 0)

    def add(self, n: int) -> None:
        if n <= 0:
            return
        self.path.write_text(json.dumps({"date": today(self.clock), "agy_turns": self.used() + int(n)}) + "\n",
                             encoding="utf-8")


class Ledger:
    """``ledger.jsonl``: one row per run that spent model tokens."""

    def __init__(self, h: Path, clock: Callable[[], float] = time.time):
        self.path, self.clock = h / "ledger.jsonl", clock

    def add(self, row: dict[str, Any]) -> dict[str, Any]:
        row = {"date": today(self.clock), "at": round(self.clock(), 3), **row}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return row

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def day(self, date: str | None = None) -> dict[str, Any]:
        date = date or today(self.clock)
        rows = [r for r in self.rows() if r.get("date") == date]
        return {"date": date, "runs": len(rows), **{k: sum(r.get(k) or 0 for r in rows)
                                                    for k in ("turns", "input", "output", "seconds", "tool_steps")}}

    def per_turn_input(self, backend: str = "agv") -> tuple[int, str]:
        """The input tokens one turn costs: from the last measured runs (up to 10) when there are any, else ~10k."""
        rows = [r for r in self.rows() if r.get("backend") == backend and r.get("turns") and r.get("input")][-10:]
        if rows:
            return round(sum(r["input"] for r in rows) / sum(r["turns"] for r in rows)), f"measured ({len(rows)} runs)"
        return PER_TURN_DEFAULT, "default (~10k per agy turn)"
