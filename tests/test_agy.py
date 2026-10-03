"""CMD-GA23 (BD-234/255): `ga gemini --host agy` — Antigravity CLI per model step, its weekly share per model family.

The fake agy (tests/fake_agy.py) speaks the ASSUMED shapes of ga/adapters/agy_cli.py (V/A/U as baseline gave them,
baseline#12 5971559909). D1: usage under the floor parks until agy's reset; a quota error parks with one probe; AI
credits are never accepted; a served-model mismatch fails; denied_actions are reported.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ga import gemini as G
from ga.adapters import agy_cli as A
from ga.adapters.gemini_cli import GeminiCLI

from test_gemini import needs_k12, plan

TESTS = Path(__file__).resolve().parent
FAKE = [sys.executable, str(TESTS / "fake_agy.py")]
PIN = "gemini-3.8-flash-high"
RESET = 1791647760.0           # 2026-10-11 00:56 KST, as the user's /usage showed (V)
START = RESET - 546960.0        # 6 d 7 h 56 min before it
USAGE = ("Gemini models: {pct}% remaining · resets 2026-10-11 00:56 KST (weekly)\n"
         "Claude and GPT models: 100% remaining · resets 2026-10-11 01:29 KST (weekly)\n")


class Clock:
    def __init__(self, t=START):
        self.t, self.sleeps = t, []

    def __call__(self):
        return self.t


class AgyBox:
    def __init__(self, test, script, pct=96, **agy):
        self.dir = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "script.json").write_text(json.dumps(script))
        self.usage(pct)
        p = mock.patch.dict(os.environ, {"FAKE_AGY_DIR": str(self.dir), "GA_GEMINI_TOOL_LOG": str(self.dir / "tools.log")})
        p.start()
        test.addCleanup(p.stop)
        raw = {"schema": G.CONFIG_SCHEMA, "host": "agy", "budget": {"rpm": 10},
               "agy": {"cli": FAKE, **agy}, "tools": {"add": {"python": "gemini_tools:add"}}}
        self.cfg = self.dir / "ga-gemini.json"
        self.cfg.write_text(json.dumps(raw))

    def usage(self, pct=None, text=None):
        (self.dir / "usage.txt").write_text(text if text is not None else USAGE.format(pct=pct))

    def calls(self, kind=None):
        f = self.dir / "calls.jsonl"
        rows = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
        return [r for r in rows if kind is None or r["kind"] == kind]

    def log(self):
        f = self.dir / ".ga-gemini" / "log.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


@needs_k12
class AgySupervisorTest(unittest.TestCase):
    def run_task(self, box, on_sleep=None, prompt="read issue 7"):
        clock, out = Clock(), io.StringIO()

        def sleep(d):
            clock.sleeps.append(d)
            clock.t += d
            if on_sleep:
                on_sleep(clock)
        sup = G.Supervisor(G.load_config(box.cfg), clock=clock, sleep=sleep, out=out)
        ok = sup.start(prompt)
        return sup, ok, clock, out.getvalue()

    def test_a_share_under_the_floor_parks_until_agys_reset(self):
        box = AgyBox(self, [{"plan": plan(say="done")}], pct=3)
        sup, ok, clock, out = self.run_task(box, on_sleep=lambda c: box.usage(96))  # the share is back at the reset
        self.assertTrue(ok, out)
        self.assertEqual(clock.sleeps, [RESET - START])
        self.assertIn("[ga gemini] agy quota (gemini, weekly): T1.m1 parked — 3% left (floor 5%); resets at", out)
        self.assertIn("in 6 d 7 h 56 min; one probe then", out)
        self.assertIn("agy share left 3%", out)
        self.assertEqual(len(box.calls("turn")), 1)  # no turn spent before the reset
        self.assertEqual(len(box.calls("usage")), 2)  # the reading that parked it, the one at the reset
        self.assertEqual([r["remaining_pct"] for r in box.log() if r["event"] == "usage"], [3.0, 96.0])

    def test_a_quota_error_parks_with_one_probe(self):
        box = AgyBox(self, [{"plan": plan(next_={"prompt": "on"})}, {"quota": True}, {"plan": plan()}])
        sup, ok, clock, out = self.run_task(box)
        self.assertTrue(ok, out)
        self.assertEqual(clock.sleeps, [RESET - START])
        self.assertEqual(len(box.calls("turn")), 3)
        # /usage is read before m1, after the quota stop and at the reset — not before every step (usage_every_s)
        self.assertEqual(len(box.calls("usage")), 3)
        self.assertEqual(out.count("[ga gemini] agy quota reset: one probe (T1.m2)"), 1)
        self.assertEqual([r["step"] for r in box.log() if r["event"] == "probe"], ["T1.m2"])
        turn = [r for r in box.log() if r["event"] == "turn" and not r["ok"]]
        self.assertEqual([(r["reason"], r["source"]) for r in turn], [("agy_quota", "quota reset")])

    def test_ai_credits_are_never_accepted(self):
        for kind in ("offer", "low"):
            with self.subTest(kind=kind):
                box = AgyBox(self, [{"plan": plan(next_={"prompt": "on"})}, {"credits": kind}, {"plan": plan()}])
                sup, ok, clock, out = self.run_task(box)
                self.assertTrue(ok, out)
                self.assertIn("ga never accepts them", out)
                self.assertEqual(clock.sleeps, [RESET - START])  # treated as the quota spent: wait for the reset
                calls = box.calls()
                self.assertFalse(any(c["credits_arg"] for c in calls))
                self.assertTrue(all(c["stdin_eof"] for c in calls))  # nothing can answer a credits prompt
                self.assertIn(("agy_credits", "quota reset"),
                              [(r["reason"], r.get("source")) for r in box.log() if r["event"] == "turn" and not r["ok"]])

    def test_the_served_model_is_the_pinned_one_or_the_turn_fails(self):
        cases = [({"plan": plan(), "model": "gemini-3.7-flash-high"}, "ModelMismatch", "served_model_mismatch"),
                 ({"plan": plan(), "model": None}, "ModelMismatch", "served_model_unknown"),
                 ({"plan": plan(), "model": {"display_name": "gemini-3.1-pro-high"}}, "ModelMismatch", "served_model_mismatch")]
        for entry, failed, reason in cases:
            with self.subTest(entry=entry.get("model")):
                box = AgyBox(self, [entry])
                sup, ok, clock, out = self.run_task(box)
                self.assertFalse(ok)
                self.assertEqual(sup.st["failed"], {"T1.m1": failed})
                self.assertIn(reason, [r.get("reason") for r in box.log()])
                self.assertEqual(len(box.calls("turn")), 1)  # not retried on another model
        box = AgyBox(self, [{"plan": plan(), "model": {"display_name": PIN}}])  # the object form (V: display_name)
        self.assertTrue(self.run_task(box)[1])

    def test_denied_actions_are_reported(self):
        box = AgyBox(self, [{"plan": plan(), "denied": [{"tool": "WebFetch", "url": "https://x.example/secret"}, {"x": 1}]}])
        sup, ok, clock, out = self.run_task(box)
        self.assertTrue(ok, out)
        self.assertIn("[ga gemini] agy refused 2 action(s) in T1.m1: WebFetch, action", out)
        d, = [r for r in box.log() if r["event"] == "denied"]
        self.assertEqual((d["count"], d["labels"]), (2, ["WebFetch", "action"]))
        self.assertNotIn("x.example", json.dumps(box.log()))

    def test_agy_error_fails_the_turn_once(self):
        box = AgyBox(self, [{"error": True}, {"plan": plan()}])
        sup, ok, clock, out = self.run_task(box)
        self.assertFalse(ok)
        self.assertEqual((sup.st["failed"], len(box.calls("turn")), clock.sleeps), ({"T1.m1": "GeminiError"}, 1, []))
        self.assertIn("agy_error", [r.get("reason") for r in box.log()])

    def test_every_prompt_is_self_contained_in_the_workspace(self):
        script = [{"plan": plan([{"id": "a", "tool": "add", "args": {"a": 1, "b": 2}}], {"prompt": "sum", "after": ["a"]})},
                  {"plan": plan(), "format": "json"}]
        box = AgyBox(self, script)
        sup, ok, clock, out = self.run_task(box)
        self.assertTrue(ok, out)
        t1, t2 = box.calls("turn")
        self.assertTrue(t2["has_task"] and t2["has_protocol"] and t2["has_results"])  # agy has no known --resume (U)
        self.assertEqual({(t["model"], t["format"], t["resume_arg"], t["cwd"]) for t in (t1, t2)},
                         {(PIN, "stream-json", False, str(box.dir.resolve()))})

    def test_an_unreadable_usage_text_does_not_block(self):
        box = AgyBox(self, [{"plan": plan()}])
        box.usage(text="something agy prints that ga cannot read\n")
        sup, ok, clock, out = self.run_task(box)
        self.assertTrue(ok, out)
        self.assertEqual([r["known"] for r in box.log() if r["event"] == "usage"], [False])

    def test_a_quota_stop_without_a_readable_reset_waits_the_fallback(self):
        box = AgyBox(self, [{"plan": plan(next_={"prompt": "on"})}, {"quota": True}, {"plan": plan()}],
                     reset_fallback_s=1800)
        box.usage(text="Gemini models: 0% remaining\n")  # no date: U
        calls = []

        def back(c):
            calls.append(c.t)
            box.usage(96)
        sup, ok, clock, out = self.run_task(box, on_sleep=back)
        self.assertTrue(ok, out)
        self.assertEqual(clock.sleeps[0], 1800)


class HostTest(unittest.TestCase):
    def test_the_default_host_is_gemini_cli(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        f = d / "g.json"
        f.write_text(json.dumps({"schema": G.CONFIG_SCHEMA}))
        cfg = G.load_config(f)
        self.assertEqual((cfg.host, cfg.active_model), ("gemini_cli", "gemini-3-flash-preview"))
        self.assertIsInstance(G.Supervisor(cfg).cli, GeminiCLI)
        cfg.host = "agy"
        sup = G.Supervisor(cfg)
        self.assertIsInstance(sup.cli, A.AgyCLI)
        self.assertEqual((sup.model, sup.cli.model), (PIN, PIN))

    @needs_k12
    def test_ga_gemini_host_agy_on_the_command_line(self):
        box = AgyBox(self, [{"plan": plan(say="hello from agy")}])
        raw = json.loads(box.cfg.read_text())
        raw["host"] = "gemini_cli"
        box.cfg.write_text(json.dumps(raw))
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = G.main(types.SimpleNamespace(gemini_config=str(box.cfg), resume=False, prompt=["hi"], host="agy"))
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(len(box.calls("turn")), 1)
        self.assertIn("hello from agy", out.getvalue())

    def test_config(self):
        ok = {"schema": G.CONFIG_SCHEMA, "host": "agy", "agy": {"model": PIN}}
        self.assertEqual(G.config_problems(ok), [])
        for bad in (dict(ok, host="vertex"), dict(ok, agy={"credits": True}), dict(ok, agy={"usage_floor_pct": 100}),
                    dict(ok, agy={"window": "daily"}), dict(ok, agy={"model": ""})):
            with self.subTest(bad=bad):
                self.assertTrue(G.config_problems(bad))


class ParseTest(unittest.TestCase):
    def test_usage(self):
        u = A.parse_usage(USAGE.format(pct=96))
        self.assertEqual(u["gemini"], {"remaining_pct": 96.0, "reset_at": RESET, "window": "weekly"})
        self.assertEqual(u["claude_gpt"]["reset_at"], RESET + 1980)
        u = A.parse_usage("Gemini: 12% used, resets 2026-10-11T00:56:00+09:00 (5-hour window)")
        self.assertEqual(u["gemini"], {"remaining_pct": 88.0, "reset_at": RESET, "window": "5-hour"})
        self.assertIsNone(A.parse_usage("Gemini models: 40% left, reset 2026-10-11 00:56 XYZ")["gemini"]["reset_at"])
        self.assertEqual(A.parse_usage("nothing here"), {})
        self.assertEqual((A.family_of(PIN), A.family_of("claude-sonnet-4-6"), A.family_of("gpt-oss-120b-medium")),
                         ("gemini", "claude_gpt", "claude_gpt"))

    def test_output(self):
        o = A.parse_output(json.dumps({"response": "x", "model": PIN, "denied_actions": [{"name": "Bash"}]}))
        self.assertEqual((o.text, o.served, o.denied), ("x", [PIN], ["Bash"]))
        o = A.parse_output("", "Your AI credits balance is too low to continue.")
        self.assertTrue(o.credits)
        o = A.parse_output("", "AGY_ERROR: agent failed")
        self.assertEqual((o.agy_error, o.quota), (True, False))
        o = A.parse_output("", "AGY_ERROR: You have reached the quota limit")
        self.assertTrue(o.quota)


if __name__ == "__main__":
    unittest.main()
