"""VI-10b: the work-item state machine T1-T11 in shadow (no sessions); every step appends one journal/1 row."""
from __future__ import annotations

import re
from typing import Any

from ..forms import hard, validate
from ..forms.kinds import JOURNAL_TRANSITIONS
from ..net.pool import _cycle, overlap
from .journal import JournalError, fold, inputs_hash

ACTIVE = ("DISPATCHED", "ACTING", "REPORTED", "VERDICT")
DONE = ("INTEGRATED", "DEPLOYED", "OBSERVED")
WOULD_DO = {"T2": "dispatch: admit a session for the item", "T3": "start the session at the base sha",
            "T4": "run the commit gate on the reported sha", "T6'": "dispatch again after the send-back"}
SHA = re.compile(r"^[0-9a-f]{7,40}$")


class Machine:
    def __init__(self, rows: list[dict[str, Any]] = ()):
        self.rows = [dict(r) for r in rows]
        self.state = fold(self.rows)
        self.items: dict[str, dict[str, list[str]]] = {}
        for r in self.rows:
            if r["event"] == "T1" and r["guard"]["ok"]:
                self.items[r["item"]] = dict(r["record"]["work"])

    def step(self, ev: dict[str, Any]) -> dict[str, Any]:
        event, item, at, inputs = ev["event"], ev["item"], ev["at"], dict(ev.get("inputs") or {})
        if event not in JOURNAL_TRANSITIONS:
            raise JournalError(f"unknown event {event}")
        cur = self.state.get(item, "NONE")
        if cur == "NONE" and event != "T1":
            raise JournalError(f"{item} is not planned")
        ok, why, record = self._guard(event, item, cur, inputs)
        to = JOURNAL_TRANSITIONS[event][1] if ok else cur
        row = {"schema": "journal/1", "id": f"J-{len(self.rows) + 1}", "at": at, "item": item, "from_state": cur,
               "to_state": to, "event": event, "guard": {"ok": ok, "why": why}, "inputs_hash": inputs_hash(inputs),
               "record": record}
        probs = hard(validate(row, "journal/1"))
        if probs:
            raise JournalError("; ".join(str(p) for p in probs))
        self.rows.append(row)
        if to != "NONE":
            self.state[item] = to
        if event == "T1" and ok:
            self.items[item] = record["work"]
        return row

    def _guard(self, event: str, item: str, cur: str, inputs: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        want = JOURNAL_TRANSITIONS[event][0]
        if event == "T11":
            if cur == "CANCELLED":
                return False, f"{item} is already CANCELLED", {}
            return True, "cancelled", {"reason": str(inputs.get("reason") or "")}
        if cur != want:
            return False, f"{item} is {cur}, {event} needs {want}", {}
        if event == "T1":
            work = {"after": sorted(inputs.get("after") or []), "files": sorted(inputs.get("files") or [])}
            if not work["files"]:
                return False, "files is empty", {}
            graph = {k: v["after"] for k, v in self.items.items()}
            graph[item] = work["after"]
            if _cycle(graph, item):
                return False, "after has a cycle", {}
            return True, "planned", {"work": work}
        if event in ("T2", "T6'"):
            mine = self.items[item]
            wait = [a for a in mine["after"] if self.state.get(a) not in DONE]
            if wait:
                return False, "after not integrated: " + ", ".join(wait), {}
            others = [o for o, s in self.state.items() if s in ACTIVE and o != item]
            busy = [o for o in others if overlap(mine["files"], self.items.get(o, {}).get("files", []))]
            if busy:
                return False, "files overlap: " + ", ".join(busy), {}
            mc = inputs.get("max_concurrent")
            if mc is not None and len(others) >= mc:
                return False, f"concurrency {len(others)} >= {mc}", {}
            return True, "admitted (shadow)", {"would_do": WOULD_DO[event]}
        if event == "T3":
            return True, "session start (shadow)", {"would_do": WOULD_DO["T3"]}
        if event in ("T4", "T5"):
            sha = inputs.get("sha")
            if not isinstance(sha, str) or not SHA.match(sha):
                return False, "no reported sha", {}
            if event == "T4":
                return True, "reported (shadow)", {"would_do": WOULD_DO["T4"], "sha": sha}
            return True, "verdict started", {"sha": sha}
        d = inputs.get("decision")
        if event == "T6":
            n = sum(1 for r in self.rows if r["item"] == item and r["event"] == "T6" and r["guard"]["ok"])
            cap = inputs.get("sendback_cap")
            if d != "SEND_BACK":
                return False, f"decision is {d}", {}
            if cap is not None and n >= cap:
                return False, f"send-back cap {cap} reached", {}
            return True, "sent back", {"reason": str(inputs.get("reason") or ""), "sendbacks": n + 1}
        if event == "T7":
            if d != "SHADOW":
                return False, f"decision is {d}", {}
            return True, "criterion not executable", {"reason": str(inputs.get("reason") or "")}
        if event == "T8":
            if d != "ACCEPT":
                return False, f"decision is {d}", {}
            if inputs.get("halt") is True:
                return False, "halt", {}
            return True, "accepted (shadow ref only)", {}
        if event == "T9":
            if inputs.get("descendant") is not True:
                return False, "running sha does not contain the item", {}
            return True, "deployed", {"running_sha": str(inputs.get("running_sha") or "")}
        if inputs.get("alarm") is not False:  # T10
            return False, "alarm attributed or not reported", {}
        return True, "observed", {}


def replay(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    m = Machine()
    for e in events:
        m.step(e)
    return m.rows, dict(m.state)
