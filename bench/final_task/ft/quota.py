"""Quota arithmetic and final-task-metrics/1 (FINAL_TASK sections 3, 5.3). Prices come from ga/backends/catalog.py."""
from __future__ import annotations

from typing import Any

from ga.backends.catalog import CATALOG

DISAGREE = 0.05  # claude -p total_cost_usd vs our arithmetic: flag a call that differs by more than 5%


def prices(model: str) -> tuple[float, float]:
    """(p_in, p_out) USD per token for a claude_cli catalog model."""
    for e in CATALOG["claude_cli"]:
        if e["model"] == model and e["price"]:
            return e["price"]["in"] / 1e6, e["price"]["out"] / 1e6
    raise KeyError(f"no price for {model}")


def usage_parts(u: dict[str, Any]) -> dict[str, int]:
    det = u.get("output_tokens_details") or {}
    return {"input": int(u.get("input_tokens") or 0), "cache_creation": int(u.get("cache_creation_input_tokens") or 0),
            "cache_read": int(u.get("cache_read_input_tokens") or 0), "output": int(u.get("output_tokens") or 0),
            "thinking": int(det.get("thinking_tokens") or 0)}


def quota_usd(model: str, u: dict[str, Any]) -> float:
    """input x p_in + cache_creation x 1.25 p_in + cache_read x 0.1 p_in + output x p_out (FINAL_TASK 5.3)."""
    p_in, p_out = prices(model)
    p = usage_parts(u)
    return p["input"] * p_in + p["cache_creation"] * 1.25 * p_in + p["cache_read"] * 0.1 * p_in + p["output"] * p_out


def call_input(u: dict[str, Any]) -> int:
    p = usage_parts(u)
    return p["input"] + p["cache_creation"] + p["cache_read"]


def disagrees(ours: float, theirs: float | None) -> bool:
    if theirs is None:
        return False
    return abs(ours - theirs) > DISAGREE * max(abs(theirs), 1e-12)


def metrics(model: str, calls: list[dict[str, Any]], *, correct: bool, tool_calls: int, peer_messages: int,
            ctx: dict[str, Any], repeated: int, unknown_correct: bool | None) -> dict[str, Any]:
    """One run's final-task-metrics/1 values. ``calls``: [{usage, total_cost_usd}] of every claude -p call of the run."""
    inp = {"input": 0, "cache_creation": 0, "cache_read": 0}
    out = think = 0
    q_calls: list[float] = []
    flags: list[int] = []
    max_in = 0
    for i, c in enumerate(calls):
        p = usage_parts(c["usage"])
        for k in inp:
            inp[k] += p[k]
        out += p["output"]
        think += p["thinking"]
        q = quota_usd(model, c["usage"])
        q_calls.append(q)
        max_in = max(max_in, call_input(c["usage"]))
        if disagrees(q, c.get("total_cost_usd")):
            flags.append(i)
    q = sum(q_calls)
    input_tokens = sum(inp.values())
    return {"input_tokens": input_tokens, "input_parts": inp, "output_tokens": out, "thinking_tokens": think,
            "total_tokens": input_tokens + out, "llm_calls": len(calls), "tool_calls": tool_calls,
            "peer_messages": peer_messages, **ctx, "repeated_information": repeated,
            "result_correct": bool(correct), "unknown_correct": unknown_correct,
            "quota_usd": q, "quota_hte": q / 1e-6, "max_call_input": max_in,
            "max_call_share": (max(q_calls) / q) if q else 0.0,
            "quota_per_correct": (q if correct else None),
            "total_cost_usd_sum": sum((c.get("total_cost_usd") or 0.0) for c in calls),
            "cost_disagree_calls": flags}
