from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from ga.forms import dump_text

DEFAULTS = {
    "ctx_cap": 150000,
    "session_usd_per_h": 5.0,
    "total_usd_per_h": 12.0,
    "snapshot_max_age_s": 7200,
    "vm_usd_per_h": 6.0,
}


def _parse_time(t):
    if isinstance(t, str):
        return datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if isinstance(t, datetime) and t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def load_thresholds(path):
    if path is None:
        return dict(DEFAULTS)
    p = Path(path)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {**DEFAULTS, **data}
        except Exception:
            return dict(DEFAULTS)
    return dict(DEFAULTS)


def rules(snapshot, prev, ledger_rows, thresholds, now):
    thresholds = {**DEFAULTS, **(thresholds or {})}
    now_t = _parse_time(now)
    alerts = []

    max_age = thresholds.get("snapshot_max_age_s")
    if snapshot is None:
        if max_age is not None:
            alerts.append({"kind": "snapshot_stale", "key": "snapshot", "note": "Snapshot is missing"})
    else:
        snap_at = _parse_time(_get(snapshot, "at"))
        if max_age is not None and (now_t - snap_at).total_seconds() > max_age:
            age_s = int((now_t - snap_at).total_seconds())
            alerts.append(
                {
                    "kind": "snapshot_stale",
                    "key": "snapshot",
                    "note": f"Snapshot age {age_s}s exceeds {max_age}s",
                }
            )

        raw_sessions = _get(snapshot, "sessions", [])
        if isinstance(raw_sessions, dict):
            sessions_list = list(raw_sessions.values())
        else:
            sessions_list = list(raw_sessions) if raw_sessions else []

        ctx_cap = thresholds.get("ctx_cap", 150000)
        for s in sessions_list:
            status = _get(s, "status")
            bucket = _get(s, "bucket")
            if status != "archived" and bucket != "completed":
                ctx = _get(s, "ctx", 0) or 0
                if ctx > ctx_cap:
                    sid = str(_get(s, "id"))
                    alerts.append(
                        {
                            "kind": "ctx_over",
                            "key": sid,
                            "note": f"Session {sid} ctx {ctx} > {ctx_cap}",
                        }
                    )

        if prev is not None:
            prev_at = _parse_time(_get(prev, "at"))
            hours = (snap_at - prev_at).total_seconds() / 3600.0
            if hours > 0:
                raw_prev_sessions = _get(prev, "sessions", [])
                if isinstance(raw_prev_sessions, dict):
                    prev_sessions_list = list(raw_prev_sessions.values())
                else:
                    prev_sessions_list = list(raw_prev_sessions) if raw_prev_sessions else []

                prev_by_id = {
                    str(_get(s, "id")): s
                    for s in prev_sessions_list
                    if _get(s, "id") is not None
                }
                snap_by_id = {
                    str(_get(s, "id")): s
                    for s in sessions_list
                    if _get(s, "id") is not None
                }

                sess_cap = thresholds.get("session_usd_per_h", 5.0)
                for sid, s_now in snap_by_id.items():
                    if sid in prev_by_id:
                        s_prev = prev_by_id[sid]
                        cost_now = float(_get(s_now, "cost", _get(s_now, "cost_usd", _get(s_now, "usd", 0.0))) or 0.0)
                        cost_prev = float(_get(s_prev, "cost", _get(s_prev, "cost_usd", _get(s_prev, "usd", 0.0))) or 0.0)
                        rate = (cost_now - cost_prev) / hours
                        if rate > sess_cap:
                            alerts.append(
                                {
                                    "kind": "session_cost_rate",
                                    "key": sid,
                                    "note": f"Session {sid} spend rate ${rate:.2f}/h > ${sess_cap:.2f}/h",
                                }
                            )

                total_cap = thresholds.get("total_usd_per_h", 12.0)
                sum_now = sum(
                    float(_get(s, "cost", _get(s, "cost_usd", _get(s, "usd", 0.0))) or 0.0)
                    for s in sessions_list
                )
                sum_prev = sum(
                    float(_get(s, "cost", _get(s, "cost_usd", _get(s, "usd", 0.0))) or 0.0)
                    for s in prev_sessions_list
                )
                total_rate = (sum_now - sum_prev) / hours
                if total_rate > total_cap:
                    alerts.append(
                        {
                            "kind": "total_cost_rate",
                            "key": "total",
                            "note": f"Total spend rate ${total_rate:.2f}/h > ${total_cap:.2f}/h",
                        }
                    )

    if ledger_rows:
        vm_cap = thresholds.get("vm_usd_per_h", 6.0)
        vm_sum = 0.0
        cutoff = now_t - timedelta(hours=1)
        for row in ledger_rows:
            row_at = _parse_time(_get(row, "at"))
            if cutoff < row_at <= now_t:
                vm_sum += float(_get(row, "usd", 0.0) or 0.0)
        if vm_sum > vm_cap:
            alerts.append(
                {
                    "kind": "vm_spend_rate",
                    "key": "vm",
                    "note": f"VM spend rate ${vm_sum:.2f}/h > ${vm_cap:.2f}/h",
                }
            )

    return sorted(alerts, key=lambda a: (a["kind"], a["key"]))


def tick(alerts, state_path, now, send):
    state_file = Path(state_path)
    state = {}
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    sent_map = state.setdefault("sent", {})
    now_t = _parse_time(now)
    day = now_t.date().isoformat()

    sent_alerts = []
    for alert in alerts:
        kind = alert["kind"]
        key = str(alert["key"])
        note = alert.get("note", "")
        state_key = f"{kind}|{key}"

        if sent_map.get(state_key) == day:
            continue

        recipients = ["baseline-ops"] + (["baseline"] if kind == "snapshot_stale" else [])
        for to in recipients:
            payload = {
                "schema": "notify/1",
                "to": to,
                "kind": "alert",
                "ref": "https://github.com/cogito5170/baseline/blob/claude/gracious-meitner-vp49xe/ops/hub/watch_thresholds.json",
                "id": f"WATCH-{kind}-{key}",
                "note": note[:280],
            }
            send(to, dump_text(payload))

        sent_map[state_key] = day
        sent_alerts.append(alert)

    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return sent_alerts
