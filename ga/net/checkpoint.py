"""A budget stop is a checkpoint, not a failure (CMD-GA31 S4, NETWORK.md 8).

The runner reads the budget hook's log (``ga-budget.jsonl``, written by ga/adapters/budget_hook.py) for the records
the turn added; an enforced ``checkpoint`` stage at or over the hard budget (rlo.ctxbudget.decide) sets
``TurnResult.stop = budget_checkpoint``. The fixed instructions ask the model, on a budget notice, to end with report/2 (handled status ``paused``: the report/2 contract has no
``partial``) plus a state block. ga writes the state, keeps the directive open and continues it on the next step with a
new fresh turn. ``Continuation.after`` decides:

    continue          a checkpoint with a state block whose (state hash, branch head) differs from the last checkpoint's
    needs_judgement   two continuations in a row with the same state hash and branch head (no progress); or a second
                      stop with no state block (the first one continues once from the branch commits and the last state)
    budget            the directive's runs budget is spent (continuations count against it)
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

BUDGET_CHECKPOINT = "budget_checkpoint"


def log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def budget_stop(path: Path, offset: int = 0) -> str:
    """``budget_checkpoint`` when a record the turn appended (after ``offset`` bytes) is an enforced stop at the hard
    budget, else "" (a shadow record or a warn never stops a turn)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in tail.splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(r, dict) and r.get("stage") == "checkpoint" and r.get("enforced") is True
                and isinstance(r.get("ctx"), int) and isinstance(r.get("hard"), int) and r["ctx"] >= r["hard"]):
            return BUDGET_CHECKPOINT
    return ""


def state_hash(text: str | None) -> str | None:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text is not None else None


class Continuation:
    """Per open directive (or node task): the checkpoints so far. Stored as a dict in the node's run.json."""

    def __init__(self, d: dict[str, Any] | None = None):
        d = d or {}
        self.keys: list[list] = d.get("keys", [])
        self.no_state_used: bool = d.get("no_state_used", False)
        self.continuations: int = d.get("continuations", 0)
        self.status: str = d.get("status", "open")

    def after(self, stop: str, state_text: str | None, head: str | None, *, runs_used: int,
              runs_budget: int | None) -> str:
        """-> "normal" (no checkpoint) | "continue" | "needs_judgement" | "budget"."""
        if stop != BUDGET_CHECKPOINT:
            return "normal"
        if runs_budget is not None and runs_used >= runs_budget:
            self.status = "budget"
            return "budget"
        if state_text is None:
            if self.no_state_used:
                self.status = "needs_judgement"
                return "needs_judgement"
            self.no_state_used = True
            self.continuations += 1
            return "continue"
        key = [state_hash(state_text), head]
        if self.keys and self.keys[-1] == key:
            self.status = "needs_judgement"
            return "needs_judgement"
        self.keys.append(key)
        self.continuations += 1
        return "continue"

    def to_dict(self) -> dict[str, Any]:
        return {"keys": self.keys, "no_state_used": self.no_state_used, "continuations": self.continuations,
                "status": self.status}


CHECKPOINT_INSTRUCTION = ("- If a context-budget notice says to stop: stop working and end the answer as below anyway, "
                          "report/2 handled status `paused`, and a state block saying what is done and the next step. "
                          "ga keeps the work open and continues it in a new turn from that state.")
