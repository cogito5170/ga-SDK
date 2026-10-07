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


def load_thresholds(path):
    if path is None:
        return dict(DEFAULTS)
    p = Path(path)
    if not p.exists():
        return dict(DEFAULTS)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**DEFAULTS, **data}
    except Exception:
        return dict(DEFAULTS)


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _parse_time(ts):
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    if isinstance(ts, str):
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return ts


def _get_cost(s):
    c = _get(s, "cost")
    if c is None:
        c = _get(s, "cost_usd")
    if c is None:
        c = _get(s, "usd")
    if c is None:
        return 0.0
    return float(c)


def rules(snapshot, prev, ledger_rows, thresholds, now):
    now_dt = _parse_time(now)
    alerts = []

    snap_max_age = thresholds.get("snapshot_max_age_s")
    if snap_max_age is not None:
        if snapshot is None:
            alerts.append({
                "kind": "snapshot_stale",
                "key": "snapshot",
                "note": f"Snapshot missing (max age {snap_max_age}s)",
            })
        else:
            snap_at = _parse_time(_get(snapshot, "at"))
            if snap_at is not None and (now_dt - snap_at).total_seconds() > snap_max_age:
                age_s = (now_dt - snap_at).total_seconds()
                alerts.append({
                    "kind": "snapshot_stale",
                    "key": "snapshot",
                    "note": f"Snapshot age {age_s:.1f}s > max age {snap_max_age}s",
                })

    if snapshot is not None:
        sessions = _get(snapshot, "sessions") or []
        ctx_cap = thresholds.get("ctx_cap")
        if ctx_cap is not None:
            for s in sessions:
                status = _get(s, "status")
                bucket = _get(s, "bucket")
                if status != "archived" and bucket != "completed":
                    ctx = _get(s, "ctx", 0)
                    if ctx > ctx_cap:
                        sid = _get(s, "id")
                        if sid is None:
                            sid = _get(s, "session_id")
                        alerts.append({
                            "kind": "ctx_over",
                            "key": str(sid),
                            "note": f"Session {sid} ctx {ctx} > cap {ctx_cap}",
                        })

        if prev is not None:
            snap_at = _parse_time(_get(snapshot, "at"))
            prev_at = _parse_time(_get(prev, "at"))
            if snap_at is not None and prev_at is not None:
                hours = (snap_at - prev_at).total_seconds() / 3600.0
                if hours > 0:
                    prev_sessions = _get(prev, "sessions") or []
                    prev_by_id = {}
                    for ps in prev_sessions:
                        psid = _get(ps, "id")
                        if psid is None:
                            psid = _get(ps, "session_id")
                        if psid is not None:
                            prev_by_id[str(psid)] = ps

                    session_usd_per_h = thresholds.get("session_usd_per_h")
                    for s in sessions:
                        sid = _get(s, "id")
                        if sid is None:
                            sid = _get(s, "session_id")
                        str_sid = str(sid) if sid is not None else None
                        if str_sid in prev_by_id:
                            ps = prev_by_id[str_sid]
                            c_now = _get_cost(s)
                            c_prev = _get_cost(ps)
                            rate = (c_now - c_prev) / hours
                            if session_usd_per_h is not None and rate > session_usd_per_h:
                                alerts.append({
                                    "kind": "session_cost_rate",
                                    "key": str_sid,
                                    "note": f"Session {str_sid} cost rate {rate:.2f} > limit {session_usd_per_h}",
                                })

                    total_usd_per_h = thresholds.get("total_usd_per_h")
                    if total_usd_per_h is not None:
                        sum_now = sum(_get_cost(s) for s in sessions)
                        sum_prev = sum(_get_cost(ps) for ps in prev_sessions)
                        tot_rate = (sum_now - sum_prev) / hours
                        if tot_rate > total_usd_per_h:
                            alerts.append({
                                "kind": "total_cost_rate",
                                "key": "total",
                                "note": f"Total cost rate {tot_rate:.2f} > limit {total_usd_per_h}",
                            })

    if ledger_rows:
        vm_usd_per_h = thresholds.get("vm_usd_per_h")
        if vm_usd_per_h is not None:
            one_hour_ago = now_dt - timedelta(hours=1)
            vm_sum = 0.0
            for row in ledger_rows:
                row_at = _parse_time(_get(row, "at"))
                if row_at is not None and one_hour_ago < row_at <= now_dt:
                    usd = _get(row, "usd", 0.0)
                    vm_sum += float(usd or 0.0)
            if vm_sum > vm_usd_per_h:
                alerts.append({
                    "kind": "vm_spend_rate",
                    "key": "vm",
                    "note": f"VM spend rate {vm_sum:.2f} > limit {vm_usd_per_h}",
                })

    return sorted(alerts, key=lambda a: (a["kind"], a["key"]))


def tick(alerts, state_path, now, send):
    now_dt = _parse_time(now)
    day_str = now_dt.strftime("%Y-%m-%d")
    sp = Path(state_path)
    state = {}
    if sp.exists():
        try:
            state = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    sent_dict = state.get("sent", {})
    if not isinstance(sent_dict, dict):
        sent_dict = {}

    sent_alerts = []
    for alert in alerts:
        kind = alert["kind"]
        key = str(alert["key"])
        note = str(alert.get("note", ""))
        state_key = f"{kind}|{key}"
        if sent_dict.get(state_key) == day_str:
            continue

        recipients = ["baseline-ops"]
        if kind == "snapshot_stale":
            recipients.append("baseline")

        for to in recipients:
            msg_payload = {
                "schema": "notify/1",
                "to": to,
                "kind": "alert",
                "ref": "https://github.com/cogito5170/baseline/blob/claude/gracious-meitner-vp49xe/ops/hub/watch_thresholds.json",
                "id": f"WATCH-{kind}-{key}",
                "note": note[:280],
            }
            send(to, dump_text(msg_payload))

        sent_dict[state_key] = day_str
        sent_alerts.append(alert)

    state["sent"] = sent_dict
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(state), encoding="utf-8")

    return sent_alerts
