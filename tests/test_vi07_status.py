"""VI-07 (baseline acceptance test): the hourly gateway summary as a status/1-ready object.
ga.llm.report.status(report) turns one `ga llm report` object into {schema, id, items, blockers}; the CLI prints it with
--status. 0 model calls."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from ga.llm import report as R

REPORT = {"at": "2026-10-07T03:00:00Z", "window_s": 3600, "calls": 4, "refused": 1, "cached": 0, "cached_share": 0.0,
          "estimate_vs_actual_error": None, "ga_sdk_sha": "19dc227", "policy_sha256": "ab", "policy_ok": True,
          "caps": {"vm_total_usd_per_h": {"spend": 5.0, "limit": 6.0, "window_s": 3600, "refusals": 0},
                   "vm_total_usd_per_day": {"spend": 31.0, "limit": 30.0, "window_s": 86400, "refusals": 1},
                   "vm_baselines_usd_per_h": {"spend": 0.1, "limit": 2.0, "window_s": 3600, "refusals": 0},
                   "vm_coordination_dev_plus_ops_usd_per_h": {"spend": 0.0, "limit": None, "window_s": 3600,
                                                              "refusals": 0},
                   "build_per_item_usd": {"limit": {"default": 3, "hard": 6}, "max_item_spend": 0.5, "refusals": 0}}}


class StatusTest(unittest.TestCase):
    def test_shape_is_status1(self):
        s = R.status(REPORT)
        self.assertEqual(s["schema"], "status/1")
        self.assertEqual(s["id"], "LLM-2026100703")
        self.assertIsInstance(s["items"], list)
        for it in s["items"]:
            self.assertEqual(set(it), {"id", "state", "note"})
            self.assertIn(it["state"], ("ok", "near", "over"))
        self.assertIsInstance(s["blockers"], list)

    def test_states_by_spend_against_limit(self):
        states = {it["id"]: it["state"] for it in R.status(REPORT)["items"]}
        self.assertEqual(states["vm_total_usd_per_h"], "near")      # 5.0 of 6.0: >= 80 %
        self.assertEqual(states["vm_total_usd_per_day"], "over")    # 31 of 30
        self.assertEqual(states["vm_baselines_usd_per_h"], "ok")
        self.assertNotIn("vm_coordination_dev_plus_ops_usd_per_h", states)  # no limit: not an item
        self.assertNotIn("build_per_item_usd", states)  # per-item caps are not a spend rate

    def test_an_over_cap_and_a_bad_policy_are_blockers(self):
        b = R.status(REPORT)["blockers"]
        self.assertEqual([x["kind"] for x in b], ["budget"])
        self.assertIn("vm_total_usd_per_day", b[0]["what"])
        bad = dict(REPORT, policy_ok=False)
        self.assertIn("budget", [x["kind"] for x in R.status(bad)["blockers"]])
        self.assertTrue(any("policy" in x["what"] for x in R.status(bad)["blockers"]))

    def test_deterministic(self):
        self.assertEqual(json.dumps(R.status(REPORT), sort_keys=True), json.dumps(R.status(REPORT), sort_keys=True))

    def test_cli_status_flag(self):
        d = tempfile.mkdtemp()
        pol = os.path.join(d, "policy.json")
        with open(pol, "w") as f:
            json.dump({"vm_budget": {"caps": {"vm_total_usd_per_h": 6.0}}}, f)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(R.main(["report", "--hour", "--status", "--policy", pol, "--home", os.path.join(d, "h")]), 0)
        s = json.loads(out.getvalue())
        self.assertEqual(s["schema"], "status/1")
        self.assertEqual([it["id"] for it in s["items"]], ["vm_total_usd_per_h"])
        self.assertEqual(s["items"][0]["state"], "ok")


if __name__ == "__main__":
    unittest.main()
