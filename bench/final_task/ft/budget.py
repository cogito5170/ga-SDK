"""The caps (rev 2): 130 ``claude -p`` calls in this rev and $35 cumulative over all revs, counted by claude -p's own
total_cost_usd (quota_usd when a call has none), from the ledger (one JSONL row per call, persisted, so a later session sees
what was spent; rows without ``rev`` are rev 1). Enforced before every run (worst case) and before every call."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_CALLS = 130
USD_CAP = 35.0


class CapReached(RuntimeError):
    pass


class Budget:
    def __init__(self, ledger: Path | None, max_calls: int = MAX_CALLS, usd_cap: float = USD_CAP, rev: int = 2):
        self.ledger, self.max_calls, self.usd_cap, self.rev = ledger, max_calls, usd_cap, rev
        self.rows: list[dict[str, Any]] = []
        if ledger and ledger.exists():
            self.rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x.strip()]

    @property
    def calls(self) -> int:
        """claude -p calls made in this rev (earlier revs only count towards the dollar cap)."""
        return sum(1 for r in self.rows if r.get("rev", 1) == self.rev)

    @property
    def usd(self) -> float:
        """Cumulative over all revs, by claude -p's total_cost_usd."""
        return sum((r.get("total_cost_usd") or r["quota_usd"]) for r in self.rows)

    def can_start(self, run_max_calls: int, run_est_usd: float) -> bool:
        """A run may start only if even its worst case stays within both caps."""
        return self.calls + run_max_calls <= self.max_calls and self.usd + run_est_usd <= self.usd_cap

    def before_call(self, est_usd: float = 0.0) -> None:
        if self.calls + 1 > self.max_calls:
            raise CapReached(f"call cap {self.max_calls} reached")
        if self.usd + est_usd > self.usd_cap:
            raise CapReached(f"usd cap {self.usd_cap} would be passed")

    def record(self, row: dict[str, Any]) -> None:
        row = {**row, "rev": self.rev}
        self.rows.append(row)
        if self.ledger:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
