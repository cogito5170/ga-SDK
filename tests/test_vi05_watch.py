"""VI-05 (baseline acceptance test, DEV-WATCH): the watcher as ga code with no model call.

ga.watch.rules(snapshot, prev, ledger_rows, thresholds, now) -> alerts [{kind, key, note}] sorted by (kind, key):
  ctx_over           a session whose status is not archived and whose bucket is not completed has ctx > ctx_cap
  session_cost_rate  one session's cost_usd rose faster than session_usd_per_h between prev and snapshot
  total_cost_rate    the sessions' summed cost_usd rose faster than total_usd_per_h between prev and snapshot
  snapshot_stale     no snapshot, or snapshot["at"] older than snapshot_max_age_s (rule off when that is None)
  vm_spend_rate      ledger rows' usd in the last hour above vm_usd_per_h
ga.watch.DEFAULTS; ga.watch.load_thresholds(path) merges an Ops-owned JSON file (missing file = DEFAULTS).
ga.watch.tick(alerts, state_path, now, send) mails each alert once per (kind, key) per UTC day as notify/1 kind alert
to baseline-ops (snapshot_stale also to baseline) through send(to, text) and returns the alerts it sent.
"""
import ast
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ga import watch as W
from ga.forms import hard, parse_text, validate

NOW = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def snap(at, sessions):
    return {"schema": "cloud-sessions/1", "at": iso(at), "sessions": sessions}


def s(sid, ctx=1000, cost=0.0, status="idle", bucket="working"):
    return {"id": sid, "ctx": ctx, "cost_usd": cost, "status": status, "bucket": bucket}


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.th = dict(W.DEFAULTS)

    def test_defaults(self):
        self.assertEqual(W.DEFAULTS["ctx_cap"], 150000)
        self.assertEqual(W.DEFAULTS["session_usd_per_h"], 5.0)
        self.assertEqual(W.DEFAULTS["total_usd_per_h"], 12.0)
        self.assertEqual(W.DEFAULTS["snapshot_max_age_s"], 7200)
        self.assertEqual(W.DEFAULTS["vm_usd_per_h"], 6.0)

    def test_quiet_world_has_no_alert(self):
        now = snap(NOW, [s("a", cost=1.0)])
        prev = snap(NOW - timedelta(hours=1), [s("a", cost=0.5)])
        self.assertEqual(W.rules(now, prev, [], self.th, NOW), [])

    def test_ctx_over_only_on_live_sessions(self):
        now = snap(NOW, [s("a", ctx=200000), s("b", ctx=200000, status="archived"),
                         s("c", ctx=200000, bucket="completed"), s("d", ctx=150000)])
        got = W.rules(now, None, [], self.th, NOW)
        self.assertEqual([(a["kind"], a["key"]) for a in got], [("ctx_over", "a")])

    def test_cost_rates(self):
        prev = snap(NOW - timedelta(minutes=30), [s("a", cost=1.0), s("b", cost=1.0), s("c", cost=1.0)])
        now = snap(NOW, [s("a", cost=4.0), s("b", cost=3.0), s("c", cost=3.0)])  # a: 6 USD/h; total: 14 USD/h
        got = [(a["kind"], a["key"]) for a in W.rules(now, prev, [], self.th, NOW)]
        self.assertEqual(got, [("session_cost_rate", "a"), ("total_cost_rate", "total")])

    def test_snapshot_stale_and_missing(self):
        old = snap(NOW - timedelta(hours=3), [s("a")])
        self.assertEqual([a["kind"] for a in W.rules(old, None, [], self.th, NOW)], ["snapshot_stale"])
        self.assertEqual([a["kind"] for a in W.rules(None, None, [], self.th, NOW)], ["snapshot_stale"])
        off = dict(self.th, snapshot_max_age_s=None)
        self.assertEqual(W.rules(None, None, [], off, NOW), [])

    def test_vm_spend_rate_from_the_ledger(self):
        rows = [{"at": iso(NOW - timedelta(minutes=10)), "usd": 4.0, "outcome": "ok"},
                {"at": iso(NOW - timedelta(minutes=20)), "usd": 2.5, "outcome": "ok"},
                {"at": iso(NOW - timedelta(hours=2)), "usd": 50.0, "outcome": "ok"}]
        fresh = snap(NOW, [s("a")])
        self.assertEqual([(a["kind"], a["key"]) for a in W.rules(fresh, None, rows, self.th, NOW)], [("vm_spend_rate", "vm")])
        self.assertEqual(W.rules(fresh, None, rows[1:], self.th, NOW), [])

    def test_deterministic_and_sorted(self):
        now = snap(NOW - timedelta(hours=3), [s("b", ctx=200000), s("a", ctx=200000)])
        a1 = W.rules(now, None, [], self.th, NOW)
        self.assertEqual(a1, W.rules(now, None, [], self.th, NOW))
        self.assertEqual([(a["kind"], a["key"]) for a in a1], sorted((a["kind"], a["key"]) for a in a1))
        for a in a1:
            self.assertEqual(set(a), {"kind", "key", "note"})

    def test_thresholds_file(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(W.load_thresholds(d / "missing.json"), W.DEFAULTS)
        (d / "t.json").write_text(json.dumps({"ctx_cap": 100000}))
        t = W.load_thresholds(d / "t.json")
        self.assertEqual(t["ctx_cap"], 100000)
        self.assertEqual(t["total_usd_per_h"], 12.0)


class TickTest(unittest.TestCase):
    def test_once_per_kind_key_day_and_valid_notify(self):
        state = Path(tempfile.mkdtemp()) / "watch-state.json"
        sent = []
        alerts = [{"kind": "ctx_over", "key": "a", "note": "ctx 200000 > 150000"},
                  {"kind": "snapshot_stale", "key": "snapshot", "note": "no snapshot"}]
        out = W.tick(alerts, state, NOW, lambda to, text: sent.append((to, text)))
        self.assertEqual(out, alerts)
        self.assertEqual(sorted(to for to, _ in sent), ["baseline", "baseline-ops", "baseline-ops"])
        for to, text in sent:
            head, _ = parse_text(text)
            self.assertEqual(hard(validate(head)), [], head)
            self.assertEqual((head["schema"], head["kind"], head["to"]), ("notify/1", "alert", to))
        self.assertEqual(W.tick(alerts, state, NOW + timedelta(hours=1), lambda to, text: sent.append((to, text))), [])
        self.assertEqual(len(sent), 3)  # same day: nothing again
        again = W.tick(alerts[:1], state, NOW + timedelta(days=1), lambda to, text: sent.append((to, text)))
        self.assertEqual(again, alerts[:1])  # next UTC day: once more


class NoModelTest(unittest.TestCase):
    def test_the_watch_path_imports_no_model_code(self):
        src = Path(W.__file__).read_text(encoding="utf-8")
        mods = set()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add(("." * n.level) + (n.module or ""))
        bad = [m for m in mods if any(x in m for x in ("llm", "backends", "adapters", "gemini", "anthropic", "openai"))]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
