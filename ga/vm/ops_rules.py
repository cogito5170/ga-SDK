"""VI-11a: the Ops worker's rule/1 table (O1 O2 O4 O7), action-spec/1 table and Guard. Pure code, 0 model calls."""
from __future__ import annotations

import re
from typing import Any

RISKS = ("low", "medium", "high")
DAILY_TURNS = 20  # = ga.hub.DAILY_TURNS, the shadow hub's default daily_turns
RULES = [
    {"schema": "rule/1", "id": "O1", "kind": "cap_reached", "source": "shadow", "action": "alert_ops"},
    {"schema": "rule/1", "id": "O2", "kind": "decide_no_model", "source": "shadow", "action": "retry_next_rung"},
    {"schema": "rule/1", "id": "O4", "kind": "mut_survived", "source": "shadow", "action": "send_back_template"},
    {"schema": "rule/1", "id": "O7", "kind": "base_drift", "source": "shadow", "action": "send_back_template"},
]
ACTIONS = [
    {"schema": "action-spec/1", "name": "alert_ops", "risk": "low", "preconditions": ["day"],
     "postcondition": "the anomaly is not raised at a later tick", "window_ms": None},
    {"schema": "action-spec/1", "name": "retry_next_rung", "risk": "low", "preconditions": ["id", "error"],
     "postcondition": "the anomaly is not raised at a later tick", "window_ms": None},
    {"schema": "action-spec/1", "name": "send_back_template", "risk": "low", "preconditions": ["id", "sha", "detail"],
     "postcondition": "the anomaly is not raised at a later tick", "window_ms": None},
    {"schema": "action-spec/1", "name": "raise_cap_once", "risk": "high", "preconditions": ["day"],
     "postcondition": "the anomaly is not raised at a later tick", "window_ms": None},
]


def table_problems(rules: list[dict[str, Any]], actions: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    names = [a.get("name") for a in actions]
    for a in actions:
        n = a.get("name")
        if a.get("schema") != "action-spec/1" or not isinstance(n, str) or not n or names.count(n) > 1:
            out.append(f"action {n!r}: schema action-spec/1 and a unique name")
        if a.get("risk") not in RISKS:
            out.append(f"action {n!r}: risk must be one of {', '.join(RISKS)}")
        if not isinstance(a.get("preconditions"), list) or not all(isinstance(p, str) for p in a["preconditions"]):
            out.append(f"action {n!r}: preconditions must be a list of strings")
        if not isinstance(a.get("postcondition"), str) or not a["postcondition"].strip():
            out.append(f"action {n!r}: postcondition must be a non-empty string")
        w = a.get("window_ms")
        if w is not None and (not isinstance(w, int) or isinstance(w, bool) or w < 0):
            out.append(f"action {n!r}: window_ms must be null or an integer >= 0")
    ids = [r.get("id") for r in rules]
    for r in rules:
        i = r.get("id")
        if r.get("schema") != "rule/1" or not isinstance(i, str) or not re.match(r"^O\d+$", i) or ids.count(i) > 1:
            out.append(f"rule {i!r}: schema rule/1 and a unique id O<n>")
        if not isinstance(r.get("kind"), str) or not r["kind"]:
            out.append(f"rule {i!r}: kind must be a non-empty string")
        if r.get("action") not in names:
            out.append(f"rule {i!r}: action {r.get('action')!r} is not in the action table")
    return out


def action_for(rule_id: str) -> dict[str, Any]:
    name = next(r["action"] for r in RULES if r["id"] == rule_id)
    return next(a for a in ACTIONS if a["name"] == name)


def classify(error: str) -> str:
    e = error.lower()
    if "timeout" in e or "timed out" in e:
        return "timeout"
    if any(w in e for w in ("form", "parse", "json")):
        return "form"
    return "backend"


def evaluate(rows: list[dict[str, Any]], *, today: str, daily_turns: int = DAILY_TURNS) -> list[dict[str, Any]]:
    kinds = {r["id"]: r["kind"] for r in RULES}
    out: list[dict[str, Any]] = []

    def add(rule: str, key: str, evidence: dict[str, Any]) -> None:
        out.append({"rule": rule, "kind": kinds[rule], "key": key, "evidence": evidence})

    n = sum(1 for r in rows if str(r.get("at") or "").startswith(today))
    if n >= daily_turns:
        add("O1", today, {"day": today, "rows": n, "daily_turns": daily_turns})
    last: dict[str, dict[str, Any]] = {}
    for r in rows:
        if isinstance(r.get("id"), str) and r["id"]:
            last[r["id"]] = r
    for i, r in last.items():
        d, err = r.get("decision"), str(r.get("error") or "")
        first = str((r.get("asks") or [""])[0])
        if err and d in ("ASK_HUMAN", "SHADOW"):
            add("O2", i, {"id": i, "rev": r.get("rev"), "error": err, "class": classify(err)})
        elif d == "SEND_BACK" and first.startswith("mutations:"):
            add("O4", i, {"id": i, "rev": r.get("rev"), "sha": r.get("sha"), "detail": first})
        elif d == "SEND_BACK" and first.startswith("ancestry:"):
            add("O7", i, {"id": i, "rev": r.get("rev"), "sha": r.get("sha"), "detail": first})
    return sorted(out, key=lambda a: (a["rule"], a["key"]))


def guard(spec: dict[str, Any], anomaly: dict[str, Any], mode: str = "shadow") -> dict[str, str]:
    if mode not in ("shadow", "enforce"):
        raise ValueError(f"guard mode {mode!r} is not shadow or enforce")
    ev = anomaly.get("evidence") or {}
    g = {"action": spec["name"], "risk": spec["risk"]}
    miss = [p for p in spec["preconditions"] if ev.get(p) in (None, "")]
    if miss:
        return {**g, "decision": "refused", "why": "precondition missing: " + ", ".join(miss)}
    if spec["risk"] == "high":
        return {**g, "decision": "blocked", "why": "high risk: status/1 blocker to the user via baseline"}
    if mode == "shadow":
        return {**g, "decision": "would_do", "why": f"shadow: {spec['risk']} risk is never executed"}
    if spec["risk"] == "low":
        return {**g, "decision": "execute", "why": "low risk"}
    return {**g, "decision": "execute_report", "why": "medium risk: execute and report"}
