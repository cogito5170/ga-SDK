"""Where a checked task/1 goes next, by kind (CMD-GA37 rev 2 S6). Code only.

    answer       ends at intake: the spec's answer (with its cites) is the outcome           -> "answered"
    decide       a decision row in the state store, with the open work it affects             -> "decided"
    investigate  open work in the state, then the planner (a stub until GA40: paused)         -> "paused"
    change       the same as investigate

Every kind also stores the options it offered (the next '1번' refers to them) and the questions for a person.
"""
from __future__ import annotations

from typing import Any, Callable

from ..forms.task import NEEDS, WORK_KINDS
from .state import State

PLANNER_CALLS: list[str] = []  # task ids the stub was handed (tests read it)


def planner_stub(spec: dict[str, Any]) -> dict[str, Any]:
    """GA40 does not exist yet: the task waits for it."""
    PLANNER_CALLS.append(spec["id"])
    return {"status": "paused", "next": "planner"}


def route(spec: dict[str, Any], state: State,
          planner: Callable[[dict[str, Any]], dict[str, Any]] = planner_stub) -> dict[str, Any]:
    tid, kind = spec["id"], spec["kind"]
    if spec.get("options"):
        state.set_options(tid, spec["options"])
    asked = [q for q in spec.get("questions", []) if q.get("needs") in NEEDS]
    if asked:
        state.add_requests(tid, asked)
    if kind in WORK_KINDS:
        state.add_work(tid, spec["goal"], kind)
        return dict(planner(spec), outcome="paused")
    if kind == "decide":
        d = spec["decision"]
        did = state.add_decision(d["text"], d.get("affects", []), tid)
        return {"status": "done", "next": None, "outcome": "decided", "decision": did,
                "affects": list(d.get("affects", []))}
    return {"status": "done", "next": None, "outcome": "answered", "answer": spec["answer"]}


__all__ = ["route", "planner_stub", "PLANNER_CALLS"]
