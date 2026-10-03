"""CMD-GA19: METHOD rev 17 (BD-177) and ga_rlo GR1 requests 1 · 2.

D1 na with a reason leaves the report/2 floor, na without one is not met · D2 an operator guard's state lines in
turns[].guards[].state, labels and values only · D3 ga prompt without a guidance key is a config error (exit 2).
"""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ga.__main__ import main
from ga.forms import FormError
from ga.hub import guard_summary
from ga.prompts import hub_prompt, worker_prompt
from ga import config as gacfg

import test_rev15
import test_rev16
from test_rev15 import rlo_guard
from test_rev16 import r2
from world import directive, proposal


class NaRuleTest(test_rev16.MachineRulesTest):
    """The rev 16 floor tests run again here as they are (inherited); the na cases are added."""

    def items(self, w, d2_items):
        head = r2("A", "CMD-A1", 1, [], self.commits())
        head["items"] = d2_items
        return self.post(w, head)

    def test_na_with_a_reason_is_left_out_of_the_floor(self):
        w = self.world()
        res = self.items(w, [{"id": "D1", "state": "met", "evidence": ["x.txt"]},
                             {"id": "D2", "state": "na", "evidence": ["no tests in this repo"]}])
        self.assertEqual((res.integrated, res.verdict["class"]), ({"alpha": self.sha}, "success"))
        self.assertIn("report/2 A: na with a reason, left out of the floor: D2", res.verdict["evidence"]["notes"])
        self.assertFalse(any("not met" in n for n in res.verdict["evidence"]["notes"]))

    def test_na_without_a_reason_is_not_met(self):
        for ev in (None, []):
            with self.subTest(evidence=ev):
                w = self.world()
                item = {"id": "D2", "state": "na"}
                if ev is not None:
                    item["evidence"] = ev
                res = self.items(w, [{"id": "D1", "state": "met", "evidence": ["x.txt"]}, item])
                self.assertEqual((res.verdict["class"], res.verdict["cause"]), ("partial", "requirement"))
                self.assertIn("report/2 A: done, but not met: D2 na", res.verdict["evidence"]["notes"])
                self.assertFalse(any("left out of the floor" in n for n in res.verdict["evidence"]["notes"]))

    def test_a_blank_reason_is_refused_by_the_forms(self):
        w = self.world()
        res = self.items(w, [{"id": "D1", "state": "met", "evidence": ["x.txt"]}, {"id": "D2", "state": "na", "evidence": ["  "]}])
        self.assertEqual((res.integrated, res.verdict["class"]), ({}, "insufficient"))

    def test_a_reasoned_na_does_not_excuse_an_unmet_item(self):
        w = self.world()
        res = self.items(w, [{"id": "D1", "state": "unmet", "evidence": ["x.txt"]},
                             {"id": "D2", "state": "na", "evidence": ["n/a here"]}])
        self.assertEqual(res.verdict["class"], "partial")
        self.assertIn("report/2 A: done, but not met: D1 unmet", res.verdict["evidence"]["notes"])

    def test_a_reasoned_na_beside_a_blocked_item_stays_blocked(self):
        w = self.world()
        res = self.items(w, [{"id": "D1", "state": "blocked", "evidence": ["x.txt"]},
                             {"id": "D2", "state": "na", "evidence": ["n/a here"]}])
        self.assertEqual(res.verdict["class"], "blocked")


class StateLinesTest(unittest.TestCase):
    def test_state_lines_land_in_state(self):
        lines = [json.dumps({"kind": "state", "labels": {"execution_health": "OK", "dc_incomplete": 0}}),
                 json.dumps({"kind": "guard", "result": {"verdict": "ALLOW"}}),
                 json.dumps({"kind": "state", "labels": {"execution_health": "UNRESOLVED_FAILURES", "ok": True,
                                                          "ratio": 0.5, "last": None}})]
        self.assertEqual(guard_summary(lines), {"allow": 1, "deny": 0, "errors": 0, "labels": [], "state": {
            "dc_incomplete": 0, "execution_health": "UNRESOLVED_FAILURES", "last": None, "ok": True, "ratio": 0.5}})

    def test_mixed_guard_and_state_lines_are_counted_apart(self):
        lines = [json.dumps({"decision": "allow"}), json.dumps({"kind": "state", "labels": {"a": "X"}}),
                 json.dumps({"kind": "guard", "result": {"verdict": "DENY", "rule": "A1"}}),
                 json.dumps({"kind": "state", "labels": {"b": "Y"}}), json.dumps({"kind": "guard_error"})]
        self.assertEqual(guard_summary(lines), {"allow": 1, "deny": 1, "errors": 1, "labels": ["A1"],
                                                "state": {"a": "X", "b": "Y"}})

    def test_no_state_line_no_state_field(self):
        self.assertNotIn("state", guard_summary([json.dumps({"decision": "allow"})]))

    def test_raw_text_is_not_kept(self):
        raw = ["https://example.com/secret", "rm -rf /tmp/x", "a" * 41, "two words", "/etc/passwd"]
        lines = [json.dumps({"kind": "state", "labels": {"good": "LABEL", **{f"k{i}": v for i, v in enumerate(raw)},
                                                          "nested": {"x": 1}, "list": [1], "bad key": "X"}}),
                 json.dumps({"kind": "state", "labels": "not a dict"}),
                 json.dumps({"kind": "state"})]
        got = guard_summary(lines)
        self.assertEqual((got["state"], got["errors"]), ({"good": "LABEL"}, 3))
        text = json.dumps(got)
        for r in raw + ["bad key", "not a dict"]:
            self.assertNotIn(r, text)


class StateInTheHubTest(test_rev15.GuardsInTheHubTest):
    """The rev 15 guard tests run again here (inherited); the state case is added."""

    def test_state_lands_in_the_turns_guards_and_diag(self):
        w, r = self.world([rlo_guard()], write=[{"kind": "guard", "result": {"verdict": "ALLOW"}},
                                                {"kind": "state", "labels": {"execution_health": "UNRESOLVED_FAILURES",
                                                                             "where": "/home/x/secret.txt"}},
                                                {"kind": "guard", "result": {"verdict": "DENY", "rule": "A1"}}])
        w.proposals.append(proposal("success", "wait", "ok"))
        w.hub.send(directive("CMD-A1", "A"))
        turn = w.hub.load_state()["turns"][-1]
        want = [{"guard": "rlo", "allow": 1, "deny": 1, "errors": 1, "labels": ["A1"],
                 "state": {"execution_health": "UNRESOLVED_FAILURES"}}]
        self.assertEqual((turn["guards"], turn["diag"]["guards"]), (want, want))
        self.assertNotIn("secret", json.dumps(w.hub.load_state()))


class PromptConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "G.md").write_text("hub guidance\n", encoding="utf-8")
        (self.tmp / "SG.md").write_text("session guidance\n", encoding="utf-8")

    def config(self, **hub):
        raw = {"schema": "ga-config/1", "hub": {"name": "hub", **hub}, "integration_branch": "i",
               "repos": {"r": {"path": "r"}}, "sessions": {"A": {"prefix": "A", "branch": "a", "repos": ["r"]}}}
        f = self.tmp / "ga.json"
        f.write_text(json.dumps(raw), encoding="utf-8")
        return f

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_missing_guidance_is_exit_2_naming_the_key(self):
        cases = [({"guidance": "G.md"}, ["A"], "$.hub.session_guidance"),
                 ({"session_guidance": "SG.md"}, ["--hub"], "$.hub.guidance"),
                 ({"guidance": "G.md", "session_guidance": "nope.md"}, ["A"], "$.hub.session_guidance"),
                 ({"guidance": "", "session_guidance": "SG.md"}, ["--hub"], "$.hub.guidance")]
        for hub, args, key in cases:
            with self.subTest(hub=hub, args=args):
                code, out, err = self.run_cli("--config", str(self.config(**hub)), "prompt", *args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(key, err)
                self.assertNotIn("Traceback", err)

    def test_with_guidance_it_prints(self):
        f = self.config(guidance="G.md", session_guidance="SG.md")
        code, out, _ = self.run_cli("--config", str(f), "prompt", "A")
        self.assertEqual(code, 0)
        self.assertIn("session guidance", out)
        code, out, _ = self.run_cli("--config", str(f), "prompt", "--hub")
        self.assertEqual(code, 0)
        self.assertIn("hub guidance", out)

    def test_unknown_session_is_exit_2(self):
        code, _, err = self.run_cli("--config", str(self.config(guidance="G.md", session_guidance="SG.md")), "prompt", "Z")
        self.assertEqual(code, 2)
        self.assertIn("no session 'Z'", err)

    def test_the_turn_prompt_says_na_needs_a_reason(self):
        from ga.prompts import turn_prompt
        cfg = gacfg.load(self.config(guidance="G.md", session_guidance="SG.md"))
        self.assertIn("`na` 는 `evidence` 에 해당 없는 까닭을 적는다", turn_prompt(cfg, "A", "x", directive("CMD-A1", "A")))

    def test_the_library_raises_a_form_error(self):
        cfg = gacfg.load(self.config())
        for fn in (lambda: worker_prompt(cfg, "A"), lambda: hub_prompt(cfg)):
            with self.assertRaises(FormError):
                fn()


if __name__ == "__main__":
    unittest.main()
