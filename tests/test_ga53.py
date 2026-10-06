"""CMD-GA53: served rung, ledger window, starting timeout, shadow gate guard (offline)."""
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from ga.act.route import LEDGER_MIN, LEDGER_WINDOW, from_ledger
from ga.bridge import _served_model, record_served
from ga.console import services as SV
from ga.hub import shadow_compare

LAD = ["small", "mid", "big"]


def won(rung, b="B"):
    return {"bucket": b, "success": True, "rungs": [rung]}


class Served(unittest.TestCase):
    def test_last_turn_that_names_one_wins(self):
        turns = [{"served": ["gpt-oss"]}, {"served": ["gemini"]}, {}]
        self.assertEqual(_served_model(turns), "gemini")
        with tempfile.TemporaryDirectory() as d:
            sf = Path(d) / "served.json"
            run = {"events": [{"event": "turn", "served": ["gpt-oss"]}, {"event": "turn", "served": ["gemini"]}]}
            self.assertEqual(record_served({"served_file": str(sf)}, run, now=lambda: "T"), "gemini")
            self.assertEqual(json.loads(sf.read_text())["model"], "gemini")


class Ledger(unittest.TestCase):
    def test_window_forgets_old_climbs(self):
        rows = [won("big")] + [won("small")] * 6
        self.assertEqual(from_ledger(rows, "B", LAD)["start"], "small")

    def test_window_only_high_rung(self):
        rows = [won("small")] * 5 + [won("big")] * LEDGER_WINDOW
        self.assertEqual(from_ledger(rows, "B", LAD)["start"], "big")

    def test_no_rung_has_enough_takes_highest_in_window(self):
        rows = [won("small"), won("mid"), won("mid")]
        self.assertEqual(from_ledger(rows, "B", LAD)["start"], "mid")
        self.assertEqual(LEDGER_WINDOW, 2 * LEDGER_MIN)

    def test_too_few_rows(self):
        self.assertIsNone(from_ledger([won("small")] * (LEDGER_MIN - 1), "B", LAD))


class Starting(unittest.TestCase):
    def svc(self, spec):
        self.now = 1000.0
        sv = SV.Services({"x": spec}, clock=lambda: self.now, health=lambda u: False)
        s = sv.svc["x"]
        s.state, s.proc, s.started_at = "starting", SimpleNamespace(poll=lambda: None, pid=1), self.now
        return sv, s

    def test_health_service_times_out(self):
        sv, s = self.svc({"argv": ["x"], "health_url": "http://127.0.0.1:1/"})
        self.now += 30
        sv.poll_health()
        self.assertEqual(s.state, "starting")
        self.now += 31
        sv.poll_health()
        self.assertEqual(s.state, "failed")
        self.assertTrue(any("still starting" in r["text"] for r in s.lines))

    def test_ready_line_only_service_times_out_with_spec_timeout(self):
        sv, s = self.svc({"argv": ["x"], "ready_line": "ready", "start_timeout_s": 5})
        self.now += 6
        sv.poll_health()
        self.assertEqual(s.state, "failed")


def rows0(n):
    return ([{"id": f"C{i}", "rev": 1, "decision": "ACCEPT"} for i in range(n)],
            [{"id": f"C{i}", "rev": 1, "at": f"2026-10-05T00:00:{i:02d}Z", "decision": "ACCEPT", "error": None}
             for i in range(n)])


class Gate(unittest.TestCase):
    def test_nothing_compared_is_not_ok(self):
        o = shadow_compare([], [])
        self.assertEqual(o["compared"], 0)
        self.assertFalse(o["gate_ok"])
        with mock.patch("ga.hub.GATE_N", 0):  # run >= 0 alone would pass an empty comparison
            self.assertFalse(shadow_compare([], [])["gate_ok"])
            self.assertTrue(shadow_compare(*rows0(1))["gate_ok"])


if __name__ == "__main__":
    unittest.main()
