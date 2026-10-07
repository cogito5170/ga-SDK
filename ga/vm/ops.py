"""VI-11c: `ga ops tick`, the Ops worker in shadow: observe -> rule/1 -> Guard -> VERIFY -> alert. 0 model calls,
no session, nothing mailed: alerts go to <ga>/vm/ops_outbox.jsonl through ga.watch.tick (dedup per kind, key, day)."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import watch
from . import ops_rules as R
from .ops_verify import FMT, verify


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def tick(ga_dir: str | Path, now: str, daily_turns: int = R.DAILY_TURNS) -> dict[str, Any]:
    ga = Path(ga_dir)
    vm = ga / "vm"
    vm.mkdir(parents=True, exist_ok=True)
    anomalies = R.evaluate(_rows(ga / "hub" / "shadow.jsonl"), today=now[:10], daily_turns=daily_turns)
    sp = vm / "ops_state.json"
    st = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    windows = {r["id"]: R.action_for(r["id"])["window_ms"] for r in R.RULES}
    open_, events = verify(st.get("open", {}), anomalies, now, windows)
    cur = {(a["rule"], a["key"]): a for a in anomalies}
    rows, alerts = [], []
    for e in events:
        a = cur.get((e["rule"], e["key"]))
        g = R.guard(R.action_for(e["rule"]), a, "shadow") if e["event"] in ("raised", "escalate") else None
        rows.append({**e, "evidence": (a or {}).get("evidence", {}), "guard": g})
        if e["event"] in ("raised", "blocked"):
            kind = e["kind"] if e["event"] == "raised" else e["kind"] + "_blocked"
            what = f"{g['decision']} {g['action']}" if g else "VERIFY failed twice"
            alerts.append({"kind": kind, "key": e["key"], "note": f"{e['rule']} {e['kind']} {e['key']}: {e['event']}; {what}"})
    with open(vm / "ops.jsonl", "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")

    def send(to: str, text: str) -> None:
        with open(vm / "ops_outbox.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"to": to, "text": text}, ensure_ascii=False) + "\n")

    sent = watch.tick(alerts, vm / "ops_alerts.json", datetime.strptime(now, FMT).replace(tzinfo=timezone.utc), send)
    sp.write_text(json.dumps({"open": open_}, sort_keys=True), encoding="utf-8")
    return {"at": now, "mode": "shadow", "anomalies": len(anomalies), "events": [f"{e['event']} {e['rule']} {e['key']}"
            for e in events], "alerts": len(sent), "model_calls": 0}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ga ops", description="the Ops worker in shadow (VI-11): 0 model calls")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tick", help="one pass: observe, rules, Guard, VERIFY, alerts (shadow)")
    p.add_argument("--ga-dir", default=".ga")
    p.add_argument("--now", default=None, help="YYYY-MM-DDTHH:MM:SSZ (default: the clock)")
    p.add_argument("--daily-turns", type=int, default=R.DAILY_TURNS)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    now = a.now or datetime.now(timezone.utc).strftime(FMT)
    print(json.dumps(tick(a.ga_dir, now, a.daily_turns), ensure_ascii=False))
    return 0
