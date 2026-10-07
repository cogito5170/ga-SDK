"""VI-11c (baseline acceptance test, VMAUTO GA57; research/VM_INTERIOR_DESIGN.md §4): `ga ops tick`, the Ops worker
in shadow: observe (the hub's shadow.jsonl) -> rule/1 (ga.vm.ops_rules.evaluate) -> Guard (mode "shadow": nothing is
executed) -> VERIFY (ga.vm.ops_verify.verify) -> alerts through ga.watch.tick. 0 model calls, no session, nothing mailed.

ga/vm/ops.py (imports only __future__, argparse, json, sys, datetime, pathlib, typing, ga.watch, ga.vm.ops_rules,
ga.vm.ops_verify):
  tick(ga_dir, now, daily_turns=ops_rules.DAILY_TURNS) -> {"at": now, "mode": "shadow", "anomalies": n,
      "events": ["<event> <rule> <key>", ...], "alerts": number sent, "model_calls": 0}
    rows = JSON-object lines of <ga_dir>/hub/shadow.jsonl (missing file = none, a broken line is skipped)
    anomalies = ops_rules.evaluate(rows, today=now[:10], daily_turns=daily_turns)
    open = "open" of <ga_dir>/vm/ops_state.json (missing = {}); windows = {rule id: action_for(id)["window_ms"]}
    (open, events) = ops_verify.verify(open, anomalies, now, windows); ops_state.json = {"open": open}
    one line per event appended to <ga_dir>/vm/ops.jsonl: the event + "evidence" (the anomaly's, {} when cleared)
      + "guard": ops_rules.guard(action_for(rule), anomaly, "shadow") for raised and escalate, else None
    alerts: raised -> {kind, key, note}; blocked -> {kind: kind + "_blocked", key, note}; given to
      ga.watch.tick(alerts, <ga_dir>/vm/ops_alerts.json, now as a UTC datetime, send) where send(to, text) appends
      {"to": to, "text": text} as one JSON line to <ga_dir>/vm/ops_outbox.jsonl (shadow: the outbox is not mailed).
  main(argv) -> 0: `ga ops tick [--ga-dir .ga] [--now YYYY-MM-DDTHH:MM:SSZ] [--daily-turns N]` prints tick() as JSON.
ga/__main__.py OWN_PARSER gains "ops": "ga.vm.ops:main".
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAY = "2026-10-07"


def O():
    from ga.vm import ops
    return ops


def row(i, hm, decision, asks, error=None, rev=1, sha="abc1234"):
    return {"at": f"{DAY}T{hm}:00Z", "id": i, "rev": rev, "sha": sha, "decision": decision, "asks": asks, "error": error}


ROWS = [row("CMD-A1", "01:00", "SEND_BACK", ["mutations: survived: m2"]),
        row("CMD-B2", "01:05", "SHADOW", ["ga verdict failed: TimeoutExpired"], "verdict:TimeoutExpired", 2),
        row("CMD-C3", "01:10", "SEND_BACK", ["ancestry: not a descendant of the base"], sha="def5678")]


def world(d):
    hub = Path(d) / "hub"
    hub.mkdir(parents=True)
    with open(hub / "shadow.jsonl", "w", encoding="utf-8") as f:
        for r in ROWS:
            f.write(json.dumps(r) + "\n")
        f.write("{broken\n")
    return hub / "shadow.jsonl"


def lines(p):
    return [json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines()] if Path(p).exists() else []


class Tick(unittest.TestCase):
    def test_a_day_of_ticks(self):
        from ga.forms import hard, parse_text, validate
        ops = O()
        with tempfile.TemporaryDirectory() as d:
            shadow = world(d)
            vm = Path(d) / "vm"
            out = ops.tick(d, f"{DAY}T02:00:00Z", daily_turns=3)
            self.assertEqual(out, {"at": f"{DAY}T02:00:00Z", "mode": "shadow", "anomalies": 4, "alerts": 4,
                                   "model_calls": 0,
                                   "events": ["raised O1 2026-10-07", "raised O2 CMD-B2", "raised O4 CMD-A1",
                                              "raised O7 CMD-C3"]})
            rows = lines(vm / "ops.jsonl")
            self.assertEqual([(r["event"], r["rule"], r["guard"]["action"], r["guard"]["decision"]) for r in rows],
                             [("raised", "O1", "alert_ops", "would_do"), ("raised", "O2", "retry_next_rung", "would_do"),
                              ("raised", "O4", "send_back_template", "would_do"),
                              ("raised", "O7", "send_back_template", "would_do")])
            self.assertEqual(rows[2]["evidence"], {"id": "CMD-A1", "rev": 1, "sha": "abc1234",
                                                   "detail": "mutations: survived: m2"})
            self.assertEqual(rows[1]["at"], f"{DAY}T02:00:00Z")
            box = lines(vm / "ops_outbox.jsonl")
            self.assertEqual([b["to"] for b in box], ["baseline-ops"] * 4)
            for b in box:
                head, _ = parse_text(b["text"])
                self.assertEqual((head["schema"], head["kind"]), ("notify/1", "alert"))
                self.assertEqual(hard(validate(head)), [])
            self.assertEqual(sorted(json.loads((vm / "ops_state.json").read_text())["open"]),
                             ["O1|2026-10-07", "O2|CMD-B2", "O4|CMD-A1", "O7|CMD-C3"])

            with open(shadow, "a", encoding="utf-8") as f:
                f.write(json.dumps(row("CMD-A1", "02:05", "ACCEPT", ["all executable checks pass"], rev=2)) + "\n")
            out = ops.tick(d, f"{DAY}T02:10:00Z", daily_turns=3)
            self.assertEqual(out["events"], ["escalate O1 2026-10-07", "escalate O2 CMD-B2", "cleared O4 CMD-A1",
                                             "escalate O7 CMD-C3"])
            self.assertEqual(out["alerts"], 0)
            new = lines(vm / "ops.jsonl")[4:]
            self.assertEqual([(r["event"], r["rung"], r["guard"] and r["guard"]["decision"]) for r in new],
                             [("escalate", 1, "would_do"), ("escalate", 1, "would_do"), ("cleared", 0, None),
                              ("escalate", 1, "would_do")])
            self.assertEqual((new[2]["evidence"], new[2]["since"]), ({}, f"{DAY}T02:00:00Z"))

            out = ops.tick(d, f"{DAY}T02:20:00Z", daily_turns=3)
            self.assertEqual(out["events"], ["blocked O1 2026-10-07", "blocked O2 CMD-B2", "blocked O7 CMD-C3"])
            self.assertEqual(out["alerts"], 3)
            box = lines(vm / "ops_outbox.jsonl")[4:]
            self.assertEqual(sorted(parse_text(b["text"])[0]["id"] for b in box),
                             ["WATCH-base_drift_blocked-CMD-C3", "WATCH-cap_reached_blocked-2026-10-07",
                              "WATCH-decide_no_model_blocked-CMD-B2"])
            out = ops.tick(d, f"{DAY}T02:30:00Z", daily_turns=3)
            self.assertEqual((out["events"], out["alerts"]), ([], 0))
            out = ops.tick(d, "2026-10-08T00:10:00Z", daily_turns=3)
            self.assertEqual(out["events"], ["cleared O1 2026-10-07"])
            self.assertEqual(out["anomalies"], 2)

    def test_empty_ga_dir_is_quiet(self):
        ops = O()
        with tempfile.TemporaryDirectory() as d:
            out = ops.tick(d, f"{DAY}T02:00:00Z")
            self.assertEqual((out["anomalies"], out["events"], out["alerts"], out["model_calls"]), (0, [], 0, 0))
            self.assertEqual(json.loads((Path(d) / "vm" / "ops_state.json").read_text()), {"open": {}})


class Cli(unittest.TestCase):
    def test_ga_ops_tick(self):
        with tempfile.TemporaryDirectory() as d:
            world(d)
            p = subprocess.run([sys.executable, "-m", "ga", "ops", "tick", "--ga-dir", d, "--now", f"{DAY}T02:00:00Z",
                                "--daily-turns", "3"], cwd=ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            out = json.loads(p.stdout)
            self.assertEqual((out["mode"], out["anomalies"], out["alerts"], out["model_calls"]), ("shadow", 4, 4, 0))
            self.assertTrue((Path(d) / "vm" / "ops.jsonl").is_file())

    def test_a_tick_imports_no_model_code(self):
        code = textwrap.dedent("""
            import sys, tempfile
            sys.path.insert(0, %r)
            import tests.test_vi11c_tick as T
            from ga.vm import ops
            with tempfile.TemporaryDirectory() as d:
                T.world(d)
                ops.tick(d, "2026-10-07T02:00:00Z", daily_turns=3)
            bad = sorted(m for m in sys.modules if m.split('.')[:2] in (['ga', 'llm'], ['ga', 'backends'],
                         ['ga', 'gemini'], ['ga', 'act'], ['ga', 'hub']))
            print(bad)
            sys.exit(1 if bad else 0)
        """ % str(ROOT))
        p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                           env=dict(os.environ), timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
