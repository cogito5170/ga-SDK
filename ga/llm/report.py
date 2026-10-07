"""``ga llm report --hour``: a fold over the ledger -> one JSON object (Ops mails or stores it hourly)."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import policy as pol
from .gateway import GatewayConfig, sdk_sha
from .ledger import Ledger, iso, now_utc


def build(cfg: GatewayConfig, window_s: int = 3600) -> dict[str, Any]:
    policy = pol.load(cfg.policy_path)
    ledger = Ledger(Path(cfg.home) / "ledger")
    now = now_utc()
    rows = list(ledger.rows(now - timedelta(seconds=window_s)))
    seen = {str(r.get("purpose")) for r in rows}
    caps: dict[str, Any] = {}
    for cap in pol.CAP_NAMES:
        limit = policy.caps.get(cap)
        if cap == pol.BUILD_ITEM:
            per_item: dict[str, float] = {}
            for r in ledger.rows():
                if r.get("purpose") == "build":
                    k = str(r.get("item_id"))
                    per_item[k] = per_item.get(k, 0.0) + float(r.get("usd") or 0.0)
            caps[cap] = {"limit": limit, "max_item_spend": round(max(per_item.values(), default=0.0), 6),
                         "refusals": sum(1 for r in rows if (r.get("reason") or {}).get("cap") == cap)}
            continue
        purposes = policy.purposes_of(cap, seen | set(pol.DEFAULT_APPLIES))
        wins = policy.window_s(cap)
        caps[cap] = {"spend": round(ledger.spend(purposes, wins, now), 6), "limit": limit, "window_s": wins,
                     "refusals": sum(1 for r in rows if (r.get("reason") or {}).get("cap") == cap)}
    calls = [r for r in rows if r.get("outcome") in ("ok", "error")]
    cached = sum(1 for r in rows if r.get("outcome") == "cached")
    pairs = [(float(r["estimate_usd"]), float(r.get("usd") or 0.0)) for r in calls
             if r.get("outcome") == "ok" and r.get("estimate_usd") and float(r.get("usd") or 0.0) > 0]
    err = (sum(abs(e - a) / a for e, a in pairs) / len(pairs)) if pairs else None
    return {"at": iso(now), "window_s": window_s, "caps": caps, "calls": len(calls),
            "refused": sum(1 for r in rows if r.get("outcome") == "refused"), "cached": cached,
            "cached_share": round(cached / (cached + len(calls)), 4) if (cached + len(calls)) else 0.0,
            "estimate_vs_actual_error": None if err is None else round(err, 4),
            "ga_sdk_sha": cfg.sdk_sha or sdk_sha(), "policy_sha256": policy.sha256, "policy_ok": policy.ok}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ga llm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("report", help="fold over the ledger into one JSON object")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--hour", action="store_true", help="the last 60 minutes (default)")
    g.add_argument("--day", action="store_true", help="the last 24 hours")
    p.add_argument("--policy"), p.add_argument("--home")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    cfg = GatewayConfig(policy_path=a.policy or "", home=a.home or "")
    print(json.dumps(build(cfg, 86400 if a.day else 3600), ensure_ascii=False, sort_keys=True))
    return 0
