"""``ga usage`` (CMD-GA31 S5): the tokmon alarms from ga's own L0, without any session API.

Reads every ``run.end`` in ``.ga/telemetry/*.jsonl`` (hub sessions) and ``.ga/nodes/*/telemetry.jsonl`` (nodes),
counts ``peer.message.*``, and compares with the last snapshot (``--state``, default ``.ga/usage.json``):

    ctx     context per call > --ctx-max      context = input + cache_read + cache_creation of a run.end, divided by
                                               its api_calls (else num_turns, else 1)
    burst   cache_read since the last snapshot > --read-max
    growth  the latest context per call minus the one at the last snapshot > --ctx-grow

Defaults are tokmon's (150000 · 20000000 · 50000). A field the runner did not report is not counted as 0 for the
context alarm: a run.end without any input count is skipped.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CTX_MAX, READ_MAX, CTX_GROW = 150_000, 20_000_000, 50_000


def _files(ga_dir: Path) -> dict[str, Path]:
    out = {}
    for f in sorted((ga_dir / "telemetry").glob("*.jsonl")):
        out[f"session:{f.stem}"] = f
    for f in sorted((ga_dir / "nodes").glob("*/telemetry.jsonl")):
        out[f"node:{f.parent.name}"] = f
    return out


def context_per_call(d: dict[str, Any]) -> int | None:
    parts = [d.get(k) for k in ("reported_input_tokens", "reported_cache_read_input_tokens",
                                "reported_cache_creation_input_tokens")]
    if not any(isinstance(p, int) for p in parts):
        return None
    total = sum(p for p in parts if isinstance(p, int))
    calls = d.get("api_calls") if isinstance(d.get("api_calls"), int) and d["api_calls"] > 0 else \
        d.get("num_turns") if isinstance(d.get("num_turns"), int) and d["num_turns"] > 0 else 1
    return -(-total // calls)


def readings(ga_dir: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for name, f in _files(ga_dir).items():
        r = {"runs": 0, "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "ctx_max": None, "ctx_last": None,
             "peer_sent": 0, "peer_received": 0, "peer_bytes": 0, "over": []}
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t, d = ev.get("type"), ev.get("data") or {}
            if t == "run.end":
                r["runs"] += 1
                for k, src in (("input", "reported_input_tokens"), ("output", "reported_output_tokens"),
                               ("cache_read", "reported_cache_read_input_tokens"),
                               ("cache_creation", "reported_cache_creation_input_tokens")):
                    if isinstance(d.get(src), int):
                        r[k] += d[src]
                c = context_per_call(d)
                if c is not None:
                    r["ctx_last"] = c
                    r["ctx_max"] = c if r["ctx_max"] is None else max(r["ctx_max"], c)
                    r["over"].append([ev.get("run_id"), c])
            elif t in ("peer.message.sent", "peer.message.received"):
                r["peer_" + t.rsplit(".", 1)[1]] += 1
                r["peer_bytes"] += int(d.get("bytes") or 0)
        out[name] = r
    return out


def alarms(prev: dict, cur: dict, *, ctx_max: int = CTX_MAX, read_max: int = READ_MAX,
           ctx_grow: int = CTX_GROW) -> list[str]:
    out = []
    for name, r in sorted(cur.items()):
        for run_id, c in r["over"]:
            if c > ctx_max:
                out.append(f"ctx {name} {run_id}: context per call {c} > {ctx_max}")
        p = prev.get(name)
        if p:
            dr = r["cache_read"] - p.get("cache_read", 0)
            if dr > read_max:
                out.append(f"burst {name}: cache_read +{dr} since last snapshot")
            if r["ctx_last"] is not None and p.get("ctx_last") is not None and r["ctx_last"] - p["ctx_last"] > ctx_grow:
                out.append(f"growth {name}: context +{r['ctx_last'] - p['ctx_last']} since last snapshot")
    return out


def run(ga_dir: Path, state: Path | None = None, **limits: int) -> tuple[dict, list[str]]:
    """-> (readings, alarms); writes the snapshot. A ctx alarm is raised once per run (the snapshot keeps what was
    already seen)."""
    state = state or ga_dir / "usage.json"
    try:
        prev = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    cur = readings(ga_dir)
    seen = {name: {x[0] for x in (p.get("over") or [])} for name, p in prev.items()}
    fresh = {n: dict(r, over=[x for x in r["over"] if x[0] not in seen.get(n, set())]) for n, r in cur.items()}
    out = alarms(prev, fresh, **limits)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(cur, sort_keys=True) + "\n", encoding="utf-8")
    return cur, out
