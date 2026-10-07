"""VI-12 (baseline acceptance test, VMAUTO observe; research/VM_INTERIOR_DESIGN.md §4.4, §15 r2): DORA metrics and SLO
checks computed by code from the journal (journal/1 rows, VI-10) and the Ops rows (ga/vm/ops.jsonl, VI-11c). 0 model
calls. STRUCTURE ONLY: the SLO set and targets are the owner's (slo.json, §13 Q4); without slo.json the metrics are
computed and no target is checked. No console panel.

ga/vm/dora.py (imports only __future__, argparse, json, math, re, statistics, sys, datetime, pathlib, typing):
  FMT = "%Y-%m-%dT%H:%M:%SZ";  SAMPLED = ("lead_time_s", "deploy_lag_s", "verdict_latency_s", "time_to_restore_s")
  RATES = ("change_failure_rate", "send_back_rate")
  stats(values) -> {"n", "median" (statistics.median), "p90" (nearest rank: sorted[ceil(0.9 n) - 1])}; n 0 -> None, None
  Only journal rows with guard.ok count. Per item, in journal order:
    lead_time_s        t(T8) - t(first T2)              (INTEGRATED - DISPATCHED)
    deploy_lag_s       t(T9) - t(first T8)              (DEPLOYED - INTEGRATED)
    verdict_latency_s  t(T6 | T7 | T8) - t(the latest T4 not yet paired)   (VERDICT end - REPORTED)
    time_to_restore_s  ops rows: a "cleared" row's at - the at of the open "raised" row of the same (rule, key)
    each sample is kept with its end time (the later of the two times).
  deploys: each T9 row (ok) of an item; it failed when that item has a T10 row with guard.ok false (alarm in the window)
  report(journal, ops, slo, now) -> {"at": now, "model_calls": 0,
      "deployment_frequency": {KST day (UTC+9) of each T9: count}, sorted by day,
      <each SAMPLED metric>: stats(all its samples),
      "change_failure_rate": failed deploys / deploys (None without deploys),
      "send_back_rate": T6 / (T6 + T7 + T8) (None without verdicts),
      "open_alarms": raised rows - cleared rows,
      "slo": None when slo is None (no target checked; no "slo_problems" key); else "slo_problems":
      slo_problems(slo) and "slo": None if there are problems, else one check per SLO in order}
  slo (slo.json) is a list of {name: str, metric: one of SAMPLED + RATES, objective: number, target: fraction in (0, 1]
    (required for SAMPLED metrics only), window: seconds (int > 0) or "<n>h" / "<n>d"}; slo_problems(slo) -> list[str],
    a non-list is a problem.
  check over the samples / deploys whose end time t satisfies now - window < t <= now:
    SAMPLED: {name, metric, objective, target, n, good (value <= objective), value: good / n, met: value >= target}
    change_failure_rate: {name, metric, objective, target, n: deploys, value: failed / n, met: value <= objective}
    send_back_rate: over the whole journal (no window): value = send_back_rate, met: value <= objective, n verdicts
    n == 0 -> value None and met None (missing data never counts as good).
  main(argv) -> 0: python -m ga.vm.dora [--journal .ga/vm/journal.jsonl] [--ops .ga/vm/ops.jsonl]
    [--slo .ga/vm/slo.json] [--now T] prints report() as JSON; a missing journal / ops file is empty; a missing slo
    file means slo None.
"""
import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHA = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"


def D():
    from ga.vm import dora
    return dora


def e(event, item, at, **inputs):
    ev = {"event": event, "item": item, "at": at if "T" in at else f"2026-10-07T{at}:00Z"}
    if inputs:
        ev["inputs"] = inputs
    return ev


def plan(item, f):
    return e("T1", item, "00:00", files=[f])


# A: accepted, deployed, observed. B: one send-back, accepted, deployed (KST next day), alarm in the window.
# C: shadowed. D: refused rows only after T1 (a T2 refused by overlap with A does not count).
EVENTS = [plan("CMD-A1", "a.py"), plan("CMD-B2", "b.py"), plan("CMD-C3", "c.py"), plan("CMD-D4", "a.py"),
          e("T2", "CMD-C3", "00:02"), e("T2", "CMD-B2", "00:05"), e("T2", "CMD-A1", "00:10"),
          e("T2", "CMD-D4", "00:10"),
          e("T3", "CMD-A1", "00:11"), e("T3", "CMD-B2", "00:06"), e("T3", "CMD-C3", "00:03"),
          e("T4", "CMD-C3", "00:12", sha=SHA), e("T5", "CMD-C3", "00:13", sha=SHA),
          e("T4", "CMD-B2", "00:15", sha=SHA), e("T5", "CMD-B2", "00:16", sha=SHA),
          e("T4", "CMD-A1", "00:20", sha=SHA), e("T5", "CMD-A1", "00:21", sha=SHA),
          e("T7", "CMD-C3", "00:22", decision="SHADOW"),
          e("T6", "CMD-B2", "00:25", decision="SEND_BACK"),
          e("T8", "CMD-A1", "00:30", decision="ACCEPT"),
          e("T6'", "CMD-B2", "00:26"), e("T3", "CMD-B2", "00:27"),
          e("T4", "CMD-B2", "00:40", sha=SHA), e("T5", "CMD-B2", "00:41", sha=SHA),
          e("T8", "CMD-B2", "00:55", decision="ACCEPT"),
          e("T9", "CMD-A1", "01:00", descendant=True), e("T10", "CMD-A1", "02:00", alarm=False),
          e("T9", "CMD-B2", "15:30", descendant=True), e("T10", "CMD-B2", "16:30", alarm=True)]
OPS = [{"at": "2026-10-07T01:00:00Z", "event": "raised", "rule": "O4", "key": "CMD-X1"},
       {"at": "2026-10-07T01:10:00Z", "event": "escalate", "rule": "O4", "key": "CMD-X1"},
       {"at": "2026-10-07T01:30:00Z", "event": "cleared", "rule": "O4", "key": "CMD-X1"},
       {"at": "2026-10-07T02:00:00Z", "event": "raised", "rule": "O1", "key": "2026-10-07"}]
NOW = "2026-10-08T00:00:00Z"


def journal():
    from ga.vm.machine import replay
    rows, state = replay(EVENTS)
    assert state["CMD-A1"] == "OBSERVED" and state["CMD-B2"] == "DEPLOYED" and state["CMD-C3"] == "SHADOW", state
    assert state["CMD-D4"] == "PLANNED", state
    return rows


class Metrics(unittest.TestCase):
    def test_stats(self):
        d = D()
        self.assertEqual(d.stats([]), {"n": 0, "median": None, "p90": None})
        self.assertEqual(d.stats([3, 1, 2]), {"n": 3, "median": 2, "p90": 3})
        self.assertEqual(d.stats(list(range(1, 11))), {"n": 10, "median": 5.5, "p90": 9})

    def test_report_without_slo_computes_everything_and_checks_no_target(self):
        r = D().report(journal(), OPS, None, NOW)
        self.assertEqual(r["at"], NOW)
        self.assertEqual(r["model_calls"], 0)
        self.assertIsNone(r["slo"])
        self.assertNotIn("slo_problems", r)
        self.assertEqual(r["deployment_frequency"], {"2026-10-07": 1, "2026-10-08": 1})
        self.assertEqual(r["lead_time_s"], {"n": 2, "median": 2100.0, "p90": 3000.0})
        self.assertEqual(r["deploy_lag_s"], {"n": 2, "median": 27150.0, "p90": 52500.0})
        self.assertEqual(r["verdict_latency_s"], {"n": 4, "median": 600.0, "p90": 900.0})
        self.assertEqual(r["time_to_restore_s"], {"n": 1, "median": 1800.0, "p90": 1800.0})
        self.assertEqual(r["change_failure_rate"], 0.5)
        self.assertEqual(r["send_back_rate"], 0.25)
        self.assertEqual(r["open_alarms"], 1)

    def test_empty_journal(self):
        r = D().report([], [], None, NOW)
        self.assertEqual((r["deployment_frequency"], r["change_failure_rate"], r["send_back_rate"], r["open_alarms"]),
                         ({}, None, None, 0))
        self.assertEqual(r["lead_time_s"], {"n": 0, "median": None, "p90": None})


class Slo(unittest.TestCase):
    SLO = [{"name": "verdict", "metric": "verdict_latency_s", "objective": 700, "target": 0.9, "window": "24h"},
           {"name": "lead", "metric": "lead_time_s", "objective": 7200, "target": 0.8, "window": "7d"},
           {"name": "cfr", "metric": "change_failure_rate", "objective": 0.2, "window": 604800},
           {"name": "restore", "metric": "time_to_restore_s", "objective": 3600, "target": 0.9, "window": "1h"},
           {"name": "sendback", "metric": "send_back_rate", "objective": 0.3, "window": "7d"}]

    def test_checks_in_order(self):
        r = D().report(journal(), OPS, self.SLO, NOW)
        self.assertEqual(r["slo_problems"], [])
        got = [(c["name"], c["n"], c["value"], c["met"]) for c in r["slo"]]
        self.assertEqual(got, [("verdict", 4, 0.75, False), ("lead", 2, 1.0, True), ("cfr", 2, 0.5, False),
                               ("restore", 0, None, None), ("sendback", 4, 0.25, True)])
        self.assertEqual((r["slo"][0]["good"], r["slo"][0]["objective"], r["slo"][0]["target"]), (3, 700, 0.9))

    def test_window_excludes_old_samples(self):
        late = "2026-10-08T01:00:00Z"  # the 24 h window now starts after every verdict of 10-07 00:22..00:55
        r = D().report(journal(), OPS, self.SLO[:1], late)
        self.assertEqual((r["slo"][0]["n"], r["slo"][0]["value"], r["slo"][0]["met"]), (0, None, None))

    def test_bad_slo_is_reported_not_checked(self):
        d = D()
        for bad in ({"name": "x"}, [{"name": "x", "metric": "nope", "objective": 1, "window": "1h"}],
                    [{"name": "x", "metric": "lead_time_s", "objective": 1, "target": 1.5, "window": "1h"}],
                    [{"name": "x", "metric": "lead_time_s", "objective": "1", "target": 0.5, "window": "1h"}],
                    [{"name": "x", "metric": "lead_time_s", "objective": 1, "target": 0.5, "window": "1w"}]):
            r = d.report(journal(), OPS, bad, NOW)
            self.assertNotEqual(r["slo_problems"], [], bad)
            self.assertIsNone(r["slo"])
            self.assertNotEqual(d.slo_problems(bad), [])
        self.assertEqual(d.slo_problems(self.SLO), [])


class Cli(unittest.TestCase):
    def test_main_reads_optional_files(self):
        from ga.vm import journal as J
        with tempfile.TemporaryDirectory() as d:
            jp = Path(d) / "journal.jsonl"
            for r in journal():
                J.append(jp, r)
            args = [sys.executable, "-m", "ga.vm.dora", "--journal", str(jp), "--ops", str(Path(d) / "none.jsonl"),
                    "--slo", str(Path(d) / "slo.json"), "--now", NOW]
            p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            out = json.loads(p.stdout)
            self.assertEqual((out["slo"], out["change_failure_rate"], out["open_alarms"]), (None, 0.5, 0))
            (Path(d) / "slo.json").write_text(json.dumps(Slo.SLO[2:3]), encoding="utf-8")
            p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual([c["met"] for c in json.loads(p.stdout)["slo"]], [False])

    def test_imports(self):
        tree = ast.parse((ROOT / "ga" / "vm" / "dora.py").read_text(encoding="utf-8"))
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                names.add("." * n.level + (n.module or ""))
        self.assertLessEqual(names, {"__future__", "argparse", "json", "math", "re", "statistics", "sys", "datetime",
                                     "pathlib", "typing"})


if __name__ == "__main__":
    unittest.main()
