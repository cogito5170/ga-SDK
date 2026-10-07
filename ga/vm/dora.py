"""VI-12: DORA metrics and SLO checks computed by code from the journal (journal/1) and the Ops rows (ops.jsonl).
0 model calls. The SLO targets are the owner's (slo.json); without one, the metrics are computed and no target is
checked. python -m ga.vm.dora [--journal J] [--ops O] [--slo S] [--now T] prints the report as JSON."""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

FMT = "%Y-%m-%dT%H:%M:%SZ"
KST = timedelta(hours=9)
SAMPLED = ("lead_time_s", "deploy_lag_s", "verdict_latency_s", "time_to_restore_s")
RATES = ("change_failure_rate", "send_back_rate")


def _t(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def stats(values: list[float]) -> dict[str, Any]:
    v = sorted(values)
    if not v:
        return {"n": 0, "median": None, "p90": None}
    return {"n": len(v), "median": statistics.median(v), "p90": v[math.ceil(0.9 * len(v)) - 1]}


def samples(journal: list[dict[str, Any]], ops: list[dict[str, Any]]) -> dict[str, list[tuple[str, float]]]:
    """metric -> [(end time, seconds)] in journal / ops order."""
    out: dict[str, list[tuple[str, float]]] = {m: [] for m in SAMPLED}
    first: dict[tuple[str, str], str] = {}
    reported: dict[str, str] = {}
    for r in journal:
        if not r["guard"]["ok"]:
            continue
        i, ev, at = r["item"], r["event"], r["at"]
        first.setdefault((i, ev), at)
        if ev == "T4":
            reported[i] = at
        elif ev in ("T6", "T7", "T8") and i in reported:
            out["verdict_latency_s"].append((at, (_t(at) - _t(reported.pop(i))).total_seconds()))
        if ev == "T8" and (i, "T2") in first:
            out["lead_time_s"].append((at, (_t(at) - _t(first[(i, "T2")])).total_seconds()))
        if ev == "T9" and (i, "T8") in first:
            out["deploy_lag_s"].append((at, (_t(at) - _t(first[(i, "T8")])).total_seconds()))
    raised: dict[tuple[str, str], str] = {}
    for r in ops:
        k = (r.get("rule"), r.get("key"))
        if r.get("event") == "raised":
            raised[k] = r["at"]
        elif r.get("event") == "cleared" and k in raised:
            out["time_to_restore_s"].append((r["at"], (_t(r["at"]) - _t(raised.pop(k))).total_seconds()))
    return out


def deploys(journal: list[dict[str, Any]]) -> list[tuple[str, bool]]:
    """(T9 time, failed) per deployed item; failed = a refused T10 row of that item (alarm in the window)."""
    failed = {r["item"] for r in journal if r["event"] == "T10" and not r["guard"]["ok"]}
    return [(r["at"], r["item"] in failed) for r in journal if r["event"] == "T9" and r["guard"]["ok"]]


def window_s(w: Any) -> int | None:
    if isinstance(w, int) and not isinstance(w, bool) and w > 0:
        return w
    m = re.match(r"^(\d+)([hd])$", str(w))
    return int(m[1]) * (3600 if m[2] == "h" else 86400) if m and int(m[1]) > 0 else None


def slo_problems(slo: Any) -> list[str]:
    if not isinstance(slo, list):
        return ["slo must be a list of {name, metric, objective, target, window}"]
    out = []
    for i, s in enumerate(slo):
        if not isinstance(s, dict) or not isinstance(s.get("name"), str) or s.get("metric") not in SAMPLED + RATES:
            out.append(f"slo[{i}]: name and a metric of {', '.join(SAMPLED + RATES)}")
            continue
        if not isinstance(s.get("objective"), (int, float)) or isinstance(s.get("objective"), bool):
            out.append(f"slo[{i}]: objective must be a number")
        t = s.get("target")
        if s["metric"] in SAMPLED and (not isinstance(t, (int, float)) or isinstance(t, bool) or not 0 < t <= 1):
            out.append(f"slo[{i}]: target must be a fraction in (0, 1]")
        if window_s(s.get("window")) is None:
            out.append(f"slo[{i}]: window must be seconds or '<n>h' / '<n>d'")
    return out


def check(s: dict[str, Any], smp: dict[str, list[tuple[str, float]]], dep: list[tuple[str, bool]],
          sb: tuple[int, int], now: str) -> dict[str, Any]:
    lo = _t(now) - timedelta(seconds=window_s(s["window"]))
    inw = lambda at: lo < _t(at) <= _t(now)  # noqa: E731
    row = {"name": s["name"], "metric": s["metric"], "objective": s["objective"], "target": s.get("target")}
    if s["metric"] in SAMPLED:
        v = [x for at, x in smp[s["metric"]] if inw(at)]
        good = sum(1 for x in v if x <= s["objective"])
        frac = good / len(v) if v else None
        return {**row, "n": len(v), "good": good, "value": frac, "met": None if frac is None else frac >= s["target"]}
    if s["metric"] == "change_failure_rate":
        d = [f for at, f in dep if inw(at)]
        rate = sum(d) / len(d) if d else None
        return {**row, "n": len(d), "value": rate, "met": None if rate is None else rate <= s["objective"]}
    rate = sb[0] / sb[1] if sb[1] else None
    return {**row, "n": sb[1], "value": rate, "met": None if rate is None else rate <= s["objective"]}


def report(journal: list[dict[str, Any]], ops: list[dict[str, Any]], slo: Any, now: str) -> dict[str, Any]:
    smp = samples(journal, ops)
    dep = deploys(journal)
    per_day: dict[str, int] = {}
    for at, _ in dep:
        d = (_t(at) + KST).date().isoformat()
        per_day[d] = per_day.get(d, 0) + 1
    ok = [r["event"] for r in journal if r["guard"]["ok"]]
    sb = (ok.count("T6"), ok.count("T6") + ok.count("T7") + ok.count("T8"))
    out: dict[str, Any] = {"at": now, "model_calls": 0, "deployment_frequency": dict(sorted(per_day.items())),
                           **{m: stats([x for _, x in smp[m]]) for m in SAMPLED},
                           "change_failure_rate": sum(f for _, f in dep) / len(dep) if dep else None,
                           "send_back_rate": sb[0] / sb[1] if sb[1] else None,
                           "open_alarms": sum(1 for r in ops if r.get("event") == "raised")
                           - sum(1 for r in ops if r.get("event") == "cleared")}
    if slo is None:
        out["slo"] = None
        return out
    probs = slo_problems(slo)
    out["slo_problems"] = probs
    out["slo"] = None if probs else [check(s, smp, dep, sb, now) for s in slo]
    return out


def _jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ga.vm.dora", description="DORA / SLO report from the journal (VI-12)")
    ap.add_argument("--journal", default=".ga/vm/journal.jsonl")
    ap.add_argument("--ops", default=".ga/vm/ops.jsonl")
    ap.add_argument("--slo", default=".ga/vm/slo.json", help="optional; absent = no target is checked")
    ap.add_argument("--now", default=None)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    slo = json.loads(Path(a.slo).read_text(encoding="utf-8")) if Path(a.slo).is_file() else None
    now = a.now or datetime.now(timezone.utc).strftime(FMT)
    print(json.dumps(report(_jsonl(Path(a.journal)), _jsonl(Path(a.ops)), slo, now), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
