"""VI-11b: VERIFY for the Ops worker. Pure: (open anomalies, this tick's anomalies, now) -> (open, events)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

FMT = "%Y-%m-%dT%H:%M:%SZ"


def _t(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def due_at(now: str, window_ms: int | None) -> str | None:
    if window_ms is None:
        return None
    return (_t(now) + timedelta(milliseconds=window_ms)).strftime(FMT)


def verify(open_: dict[str, dict[str, Any]], anomalies: list[dict[str, Any]], now: str,
           windows: dict[str, int | None]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    cur = {f"{a['rule']}|{a['key']}": a for a in anomalies}
    new: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []

    def ev(name: str, o: dict[str, Any]) -> None:
        events.append({"event": name, "rule": o["rule"], "kind": o["kind"], "key": o["key"], "at": now,
                       "since": o["since"], "rung": o["rung"]})

    for k, old in open_.items():
        o = dict(old)
        if k not in cur:
            ev("cleared", o)
            continue
        if not o["blocked"] and (o["due"] is None or _t(now) >= _t(o["due"])):
            o["fails"] += 1
            if o["fails"] >= 2:
                o["blocked"] = True
                ev("blocked", o)
            else:
                o["rung"] += 1
                o["due"] = due_at(now, windows.get(o["rule"]))
                ev("escalate", o)
        new[k] = o
    for k, a in cur.items():
        if k not in open_:
            new[k] = {"rule": a["rule"], "kind": a["kind"], "key": a["key"], "since": now,
                      "due": due_at(now, windows.get(a["rule"])), "fails": 0, "rung": 0, "blocked": False}
            ev("raised", new[k])
    return new, sorted(events, key=lambda e: (e["rule"], e["key"]))
