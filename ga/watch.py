"""DEV-WATCH: deterministic watcher rules and once-per-day alert mailing (no model call)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ga.forms import dump_text

DEFAULTS = {
    "ctx_cap": 150000,
    "session_usd_per_h": 5.0,
    "total_usd_per_h": 12.0,
    "snapshot_max_age_s": 7200,
    "vm_usd_per_h": 6.0,
}

REF = "https://github.com/cogito5170/baseline/blob/claude/gracious-meitner-vp49xe/ops/hub/watch_thresholds.json"


def load_thresholds(path):
    p = Path(path)
    if not p.exists():
        return dict(DEFAULTS)
    return {**DEFAULTS, **json.loads(p.read_text(encoding="utf-8"))}


def _t(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def rules(snapshot, prev, ledger_rows, thresholds, now):
    alerts = []

    def add(kind, key, note):
        alerts.append({"kind": kind, "key": key, "note": note})

    max_age = thresholds.get("snapshot_max_age_s")
    if snapshot is None:
        if max_age is not None:
            add("snapshot_stale", "snapshot", "no snapshot")
    else:
        snap_at = _t(snapshot["at"])
        if max_age is not None and now - snap_at > timedelta(seconds=max_age):
            age = int((now - snap_at).total_seconds())
            add("snapshot_stale", "snapshot", f"snapshot age {age}s > {max_age}s")
        sessions = snapshot.get("sessions", [])
        cap = thresholds["ctx_cap"]
        for se in sessions:
            if se.get("status") == "archived" or se.get("bucket") == "completed":
                continue
            if se.get("ctx", 0) > cap:
                add("ctx_over", se["id"], f"ctx {se['ctx']} > {cap}")
        if prev is not None:
            hours = (snap_at - _t(prev["at"])).total_seconds() / 3600
            if hours > 0:
                before = {x["id"]: x.get("cost_usd", 0.0) for x in prev.get("sessions", [])}
                limit = thresholds["session_usd_per_h"]
                for se in sessions:
                    if se["id"] in before:
                        rate = (se.get("cost_usd", 0.0) - before[se["id"]]) / hours
                        if rate > limit:
                            add("session_cost_rate", se["id"], f"cost rate {rate:.2f} USD/h > {limit}")
                sum_now = sum(x.get("cost_usd", 0.0) for x in sessions)
                sum_prev = sum(before.values())
                rate = (sum_now - sum_prev) / hours
                limit = thresholds["total_usd_per_h"]
                if rate > limit:
                    add("total_cost_rate", "total", f"total cost rate {rate:.2f} USD/h > {limit}")

    spent = 0.0
    for row in ledger_rows or []:
        at = _t(row["at"])
        if now - timedelta(hours=1) < at <= now:
            spent += row.get("usd", 0.0)
    if spent > thresholds["vm_usd_per_h"]:
        add("vm_spend_rate", "vm", f"vm spend {spent:.2f} USD in the last hour > {thresholds['vm_usd_per_h']}")

    return sorted(alerts, key=lambda a: (a["kind"], a["key"]))


def tick(alerts, state_path, now, send):
    state_path = Path(state_path)
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        state = {}
    sent = state.setdefault("sent", {})
    day = now.astimezone(timezone.utc).date().isoformat()
    out = []
    for a in alerts:
        kind, key, note = a["kind"], a["key"], a["note"]
        k = f"{kind}|{key}"
        if sent.get(k) == day:
            continue
        for to in ["baseline-ops"] + (["baseline"] if kind == "snapshot_stale" else []):
            send(to, dump_text({
                "schema": "notify/1",
                "to": to,
                "kind": "alert",
                "ref": REF,
                "id": f"WATCH-{kind}-{key}",
                "note": note[:280],
            }))
        sent[k] = day
        out.append(a)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    return out
