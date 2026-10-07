"""VI-11b (baseline acceptance test, VMAUTO GA57; research/VM_INTERIOR_DESIGN.md §4.3, GA_ENGINE_OPS §3 VERIFY):
VERIFY closes every Ops action: the postcondition (the anomaly is no longer raised) is checked inside window_ms; a
failure re-enters one ladder rung up; the same failure twice is BLOCKED and reported once, with no endless retry.
Pure code, 0 model calls.

ga/vm/ops_verify.py (imports only __future__, datetime, typing):
  FMT = "%Y-%m-%dT%H:%M:%SZ" (every time is a UTC string in this format)
  due_at(now, window_ms) -> None when window_ms is None (= checked at the next tick), else now + window_ms in FMT
  verify(open_, anomalies, now, windows) -> (open, events); never mutates its inputs.
    open_: {"<rule>|<key>": {rule, kind, key, since, due, fails, rung, blocked}};  anomalies: ga.vm.ops_rules.evaluate
    rows {rule, kind, key, evidence};  windows: {rule id: window_ms or None} (a missing rule id = None).
    for each open key not in anomalies: event "cleared"; the key leaves open.
    for each open key still in anomalies, when not blocked and (due is None or now >= due): fails += 1;
      fails >= 2 -> blocked = True, event "blocked";  else rung += 1, due = due_at(now, windows[rule]), event "escalate".
      (blocked, or due not reached: no event.) The key stays open.
    for each anomaly not open: open[key] = {rule, kind, key, since: now, due: due_at(now, windows[rule]), fails: 0,
      rung: 0, blocked: False}, event "raised".
    every event = {event, rule, kind, key, at: now, since, rung} (rung after the change); events sorted by (rule, key).
"""
import ast
import copy
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def V():
    from ga.vm import ops_verify
    return ops_verify


def an(rule, key, kind="k"):
    return {"rule": rule, "kind": kind, "key": key, "evidence": {}}


def t(hm):
    return f"2026-10-07T{hm}:00Z"


class Verify(unittest.TestCase):
    def test_due_at(self):
        v = V()
        self.assertEqual(v.FMT, "%Y-%m-%dT%H:%M:%SZ")
        self.assertIsNone(v.due_at(t("01:00"), None))
        self.assertEqual(v.due_at(t("01:00"), 90_000), "2026-10-07T01:01:30Z")
        self.assertEqual(v.due_at("2026-10-07T23:59:00Z", 120_000), "2026-10-08T00:01:00Z")

    def test_raise_escalate_block_clear(self):
        v = V()
        a = [an("O4", "CMD-A1", "mut_survived")]
        o, ev = v.verify({}, a, t("01:00"), {})
        self.assertEqual(ev, [{"event": "raised", "rule": "O4", "kind": "mut_survived", "key": "CMD-A1",
                               "at": t("01:00"), "since": t("01:00"), "rung": 0}])
        self.assertEqual(o, {"O4|CMD-A1": {"rule": "O4", "kind": "mut_survived", "key": "CMD-A1", "since": t("01:00"),
                                           "due": None, "fails": 0, "rung": 0, "blocked": False}})
        o, ev = v.verify(o, a, t("01:10"), {})
        self.assertEqual([(e["event"], e["rung"], e["since"], e["at"]) for e in ev],
                         [("escalate", 1, t("01:00"), t("01:10"))])
        self.assertEqual((o["O4|CMD-A1"]["fails"], o["O4|CMD-A1"]["rung"]), (1, 1))
        o, ev = v.verify(o, a, t("01:20"), {})
        self.assertEqual([(e["event"], e["rung"]) for e in ev], [("blocked", 1)])
        self.assertTrue(o["O4|CMD-A1"]["blocked"])
        for hm in ("01:30", "02:30"):
            o, ev = v.verify(o, a, t(hm), {})
            self.assertEqual(ev, [])
        o, ev = v.verify(o, [], t("03:00"), {})
        self.assertEqual(ev, [{"event": "cleared", "rule": "O4", "kind": "mut_survived", "key": "CMD-A1",
                               "at": t("03:00"), "since": t("01:00"), "rung": 1}])
        self.assertEqual(o, {})

    def test_window_ms_waits_for_the_due_time(self):
        v = V()
        w = {"O2": 600_000}
        a = [an("O2", "CMD-B2")]
        o, ev = v.verify({}, a, t("01:00"), w)
        self.assertEqual(o["O2|CMD-B2"]["due"], t("01:10"))
        o2, ev = v.verify(o, a, t("01:09"), w)
        self.assertEqual((ev, o2), ([], o))
        o, ev = v.verify(o, a, t("01:10"), w)
        self.assertEqual([e["event"] for e in ev], ["escalate"])
        self.assertEqual(o["O2|CMD-B2"]["due"], t("01:20"))
        o, ev = v.verify(o, [], t("01:15"), w)
        self.assertEqual([(e["event"], e["rung"]) for e in ev], [("cleared", 1)])

    def test_several_keys_sorted_and_inputs_unchanged(self):
        v = V()
        open_ = {"O7|CMD-C3": {"rule": "O7", "kind": "base_drift", "key": "CMD-C3", "since": t("00:00"), "due": None,
                               "fails": 0, "rung": 0, "blocked": False},
                 "O1|2026-10-07": {"rule": "O1", "kind": "cap_reached", "key": "2026-10-07", "since": t("00:00"),
                                   "due": None, "fails": 0, "rung": 0, "blocked": False}}
        anomalies = [an("O7", "CMD-C3"), an("O2", "CMD-B2"), an("O2", "CMD-A1")]
        keep = copy.deepcopy((open_, anomalies))
        o, ev = v.verify(open_, anomalies, t("01:00"), {"O2": None})
        self.assertEqual(copy.deepcopy((open_, anomalies)), keep)
        self.assertEqual([(e["event"], e["rule"], e["key"]) for e in ev],
                         [("cleared", "O1", "2026-10-07"), ("raised", "O2", "CMD-A1"), ("raised", "O2", "CMD-B2"),
                          ("escalate", "O7", "CMD-C3")])
        self.assertEqual(sorted(o), ["O2|CMD-A1", "O2|CMD-B2", "O7|CMD-C3"])

    def test_imports(self):
        tree = ast.parse((ROOT / "ga" / "vm" / "ops_verify.py").read_text(encoding="utf-8"))
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                names.add("." * n.level + (n.module or ""))
        self.assertLessEqual(names, {"__future__", "datetime", "typing"})


if __name__ == "__main__":
    unittest.main()
