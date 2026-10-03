"""CMD-GA21 rev 2 (BD-221, BD-222): `ga gemini` — a fixed model, a closed step list, rlo's scheduler, wait-and-resume.

The fake Gemini CLI (tests/fake_gemini.py) speaks the 0.62.0 stream-json shapes baseline read from the source; quota,
mismatch, crash and max-turns turns replay recorded fixtures (tests/fixtures/gemini/*.jsonl).
D1 a per-minute quota error mid-plan -> one status block, the state file, tool steps not held, the model step resumed
with --resume, zero failed steps; a served-model mismatch is a failed turn. D2 kill while parked, then --resume.
D3 the fixture run. D5 the daily quota: reset time, requests left today, one probe at reset, no loop, maxAttempts 1
written. D6 (rev 3) the in-turn status line, the rpm from config, max_parallel. No test claims that maxAttempts 1 bounds
the preview model's retries: its policy allows 10 inside one turn (BD-232).
"""
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ga.adapters.gemini_cli import (SETTINGS_ENV, GeminiCLI, GeminiError, GeminiRateLimited, ModelMismatch,
                                    parse_stream, quota_of)
from ga.forms import FormError
from ga import gemini as G

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
FAKE = [sys.executable, str(TESTS / "fake_gemini.py")]
FIXTURES = TESTS / "fixtures" / "gemini"
MODEL = "gemini-3-flash-preview"
QUOTA_MIN_HINT = {"fixture": "quota_minute_hint", "exit": 173}   # "retry in 7s"
QUOTA_MIN = {"fixture": "quota_minute", "exit": 173}             # no hint: the Governor's window
QUOTA_DAY = {"fixture": "quota_day", "exit": 173}                # TerminalQuotaError

try:
    import rlo.scheduler  # noqa: F401  (K12)
    HAS_K12 = True
except ImportError:
    HAS_K12 = False
needs_k12 = unittest.skipUnless(HAS_K12, "needs rlo-sdk >= 0.7.0 (rlo.scheduler, CMD-K12) — installed with ga-sdk")


def plan(steps=(), next_=None, say=None):
    p = {"schema": G.PLAN_SCHEMA, "steps": list(steps), "next": next_}
    if say is not None:
        p["say"] = say
    return p


class Box:
    """A temp dir with a fake Gemini script, the fake MCP server and a ga-gemini.json."""

    def __init__(self, test, script, **cfg):
        self.dir = Path(tempfile.mkdtemp())
        test.addCleanup(__import__("shutil").rmtree, self.dir)
        (self.dir / "script.json").write_text(json.dumps(script))
        self.env = {"FAKE_GEMINI_DIR": str(self.dir), "FAKE_MCP_LOG": str(self.dir / "mcp.log"),
                    "GA_GEMINI_TOOL_LOG": str(self.dir / "tools.log")}
        for k, v in self.env.items():
            p = mock.patch.dict(os.environ, {k: v})
            p.start()
            test.addCleanup(p.stop)
        raw = {"schema": G.CONFIG_SCHEMA, "cli": FAKE, "budget": {"rpm": 10},
               "tools": {"echo": {"mcp": "fake", "tool": "echo", "about": "echo the arguments"},
                         "fail": {"mcp": "fake", "tool": "fail"},
                         "add": {"python": "gemini_tools:add", "about": "a + b"},
                         "big": {"python": "gemini_tools:big"}},
               "mcp_servers": {"fake": {"command": [sys.executable, str(TESTS / "fake_mcp.py")]}}, **cfg}
        self.cfg_path = self.dir / "ga-gemini.json"
        self.cfg_path.write_text(json.dumps(raw))

    def calls(self):
        f = self.dir / "calls.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def lines(self, name):
        f = self.dir / name
        return f.read_text().splitlines() if f.exists() else []

    def log(self):
        return [json.loads(x) for x in self.lines(".ga-gemini/log.jsonl")]

    def state(self):
        return json.loads((self.dir / ".ga-gemini" / "state.json").read_text())


class FakeClock:
    START = 1_800_032_400.0  # 2027-01-15 09:00 in America/Los_Angeles: the daily reset is 15 h away

    def __init__(self):
        self.t, self.sleeps = self.START, []

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.sleeps.append(d)
        self.t += d


# ---- S1: the headless adapter ---------------------------------------------------------------------------------------

class StreamTest(unittest.TestCase):
    def test_parse(self):
        s = parse_stream([json.dumps({"type": "init", "session_id": "s9", "model": MODEL}), "a notice",
                          json.dumps({"type": "message", "role": "assistant", "content": "ab", "delta": True}),
                          json.dumps({"type": "message", "role": "user", "content": "zz"}),
                          json.dumps({"type": "message", "role": "assistant", "content": "c", "delta": True}),
                          json.dumps({"type": "error", "severity": "warning", "message": "loop"}),
                          json.dumps({"type": "result", "status": "success", "stats": {
                              "input_tokens": 5, "output_tokens": 2, "total_tokens": 7, "models": {"other-model": {}}}})])
        # init.model is the asked model; only stats.models says who served
        self.assertEqual((s.session_id, s.asked, s.text, s.usage, s.served, s.status, s.warnings, s.has_result),
                         ("s9", MODEL, "abc", {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}, ["other-model"],
                          "success", 1, True))

    def test_quota_from_the_result_error_type(self):
        def q(name):
            return quota_of(parse_stream((FIXTURES / f"{name}.jsonl").read_text().splitlines()))
        self.assertEqual(q("quota_minute_hint"), ("minute", 7.0, "type"))
        self.assertEqual(q("quota_minute"), ("minute", None, "type"))
        self.assertEqual(q("quota_day"), ("day", None, "type"))
        # baseline's source-shaped messages: no "429" and no RESOURCE_EXHAUSTED in the text — the type decides
        self.assertEqual(q("quota_minute_source"), ("minute", 7.5, "type"))
        self.assertEqual(q("quota_day_source"), ("day", None, "type"))
        self.assertEqual(q("quota_text_only"), ("minute", 3.0, "text"))  # the second path: an unnamed class, a 429 text
        for none in ("mismatch", "crash", "api_500", "max_turns"):
            self.assertIsNone(q(none), none)
        s = parse_stream([json.dumps({"type": "result", "status": "error", "error": {
            "type": "RetryableQuotaError", "message": "Quota exceeded for requests per day. Please retry in 30s."}})])
        self.assertEqual(quota_of(s), ("day", None, "type"))  # a per-day quota in the message is the daily quota

    def test_init_model_is_never_counted_as_served(self):
        for name in ("quota_minute_source", "quota_day_source", "crash"):
            s = parse_stream((FIXTURES / f"{name}.jsonl").read_text().splitlines())
            self.assertEqual((s.asked, s.served), (MODEL, []), name)  # an error turn: served unknown


class AdapterTest(unittest.TestCase):
    def cli(self, script, model=MODEL, extra_env=None):
        self.box = Box(self, script)
        env = dict(os.environ, **extra_env) if extra_env else None
        return GeminiCLI(FAKE, model, cwd=str(self.box.dir), settings_dir=self.box.dir / "st", env=env)

    def test_headless_fixed_model_resume_and_no_tty(self):
        cli = self.cli([{"plan": plan(say="hi")}, {"plan": plan()}])
        t1 = cli.run_turn("first")
        t2 = cli.run_turn("second", t1.session_id)
        self.assertEqual((t1.session_id, t1.served, t1.usage["total_tokens"]), ("s-1", [MODEL], 1280))
        self.assertIn('"say": "hi"', t1.text)
        c1, c2 = self.box.calls()
        for c in (c1, c2):  # headless, fixed model, stream-json, no TTY, stdin at EOF
            self.assertEqual((c["has_p"], c["format"], c["model"], c["stdin_tty"], c["stdin_eof"]),
                             (True, "stream-json", MODEL, False, True))
        self.assertEqual((c1["resume"], c2["resume"]), (None, "s-1"))
        self.assertNotEqual(c1["pid"], c2["pid"])  # S4: one short-lived process per turn
        own = self.box.dir / "st" / "gemini-cli-settings.json"
        # written and handed over; it binds models outside the preview chain only (BD-232)
        self.assertEqual({(c["settings"], c["max_attempts"]) for c in (c1, c2)}, {(str(own), 1)})

    def test_private_settings_keep_the_system_settings_in_force(self):
        sysf = Path(tempfile.mkdtemp()) / "system.json"
        self.addCleanup(__import__("shutil").rmtree, sysf.parent)
        sysf.write_text(json.dumps({"general": {"maxAttempts": 10, "vimMode": True}, "ui": {"theme": "x"}}))
        cli = self.cli([{"plan": plan()}, {"plan": plan()}], extra_env={SETTINGS_ENV: str(sysf)})
        cli.run_turn("a")
        cli.run_turn("b", "s-1")  # the second turn still merges the system file, not ga's own
        got = json.loads((self.box.dir / "st" / "gemini-cli-settings.json").read_text())
        self.assertEqual(got, {"general": {"maxAttempts": 1, "vimMode": True}, "ui": {"theme": "x"}})
        self.assertEqual(json.loads(sysf.read_text())["general"]["maxAttempts"], 10)  # not touched
        sysf.write_text("{ not json")
        with self.assertRaises(GeminiError) as e:
            GeminiCLI(FAKE, MODEL, settings_dir=self.box.dir / "st2", env={SETTINGS_ENV: str(sysf)}).run_turn("x")
        self.assertEqual(e.exception.reason, "system_settings_unreadable")

    def test_served_model_mismatch_is_a_failed_turn(self):
        cli = self.cli([{"plan": plan(), "served": ["gemini-2.5-flash"]}, {"fixture": "mismatch"},
                        {"plan": plan(), "served": [MODEL, "gemini-2.5-flash"]}, {"plan": plan(), "served": []}])
        for want in ("served_model_mismatch", "served_model_mismatch", "served_model_mismatch", "served_model_unknown"):
            with self.assertRaises(ModelMismatch) as e:
                cli.run_turn("x")
            self.assertEqual(e.exception.reason, want)

    def test_quota_crash_and_max_turns(self):
        cli = self.cli([QUOTA_MIN_HINT, QUOTA_MIN, QUOTA_DAY, {"fixture": "crash", "exit": 1},
                        {"fixture": "max_turns", "exit": 0}, {"fixture": "quota_minute_hint", "exit": 0}])
        with self.assertRaises(GeminiRateLimited) as e:
            cli.run_turn("x")
        self.assertEqual((e.exception.kind, e.exception.hint_s, e.exception.status, e.exception.via),
                         ("minute", 7.0, 429, "type"))
        self.assertEqual(e.exception.body["error"]["details"][1]["retryDelay"], "7.000s")  # a hint, as a RetryInfo
        with self.assertRaises(GeminiRateLimited) as e:
            cli.run_turn("x")
        self.assertEqual((e.exception.kind, e.exception.hint_s, len(e.exception.body["error"]["details"])),
                         ("minute", None, 1))  # no wait known: the Governor's window
        with self.assertRaises(GeminiRateLimited) as e:
            cli.run_turn("x")
        self.assertEqual(e.exception.kind, "day")
        for want in ("no_result", "cli_error"):
            with self.assertRaises(GeminiError) as e:
                cli.run_turn("x")
            self.assertNotIsInstance(e.exception, GeminiRateLimited)
            self.assertEqual(e.exception.reason, want)
        with self.assertRaises(GeminiRateLimited):  # the exit code is never read: exit 0 with a quota result
            cli.run_turn("x")

    def test_env_drops_claude_code_variables(self):
        from ga.adapters.gemini_cli import clean_env
        self.assertEqual(clean_env({"CLAUDE_CODE_X": "1", "GEMINI_API_KEY": "k", "PATH": "/b"}),
                         {"GEMINI_API_KEY": "k", "PATH": "/b"})


# ---- the closed step list · config ----------------------------------------------------------------------------------

class PlanTest(unittest.TestCase):
    TOOLS = {"echo": {}, "add": {}}

    def test_good(self):
        p = plan([{"id": "a", "tool": "echo", "args": {"x": 1}}, {"id": "b", "tool": "add", "after": ["a"]}],
                 {"prompt": "go on", "after": ["b"]}, "ok")
        self.assertEqual(G.check_plan(p, self.TOOLS), [])
        self.assertEqual(G.extract_plan("x\n```json\n" + json.dumps(p) + "\n```\n"), p)
        self.assertEqual(G.extract_plan(json.dumps(p)), p)

    def test_closed(self):
        bad = [
            (plan([{"id": "a", "tool": "rm"}]), "steps[0].tool_not_in_table"),
            (plan([{"id": "a", "tool": "echo", "after": ["b"]}, {"id": "b", "tool": "echo"}]), "steps[0].after"),
            (plan([{"id": "a", "tool": "echo"}, {"id": "a", "tool": "echo"}]), "steps[1].id"),
            (plan([{"id": "a b", "tool": "echo"}]), "steps[0].id"),
            (plan([{"id": "a", "tool": "echo", "shell": "x"}]), "steps[0]"),
            (plan(next_=[{"prompt": "a"}, {"prompt": "b"}]), "next"),  # at most one next model step
            (plan(next_={"prompt": "a", "after": ["zz"]}), "next.after"),
            (plan(next_={"prompt": " "}), "next"),
            (dict(plan(), extra=1), "unknown_field"),
            (dict(plan(), schema="x"), "schema"),
            (plan([{"id": f"s{i}", "tool": "echo"} for i in range(G.MAX_PLAN_STEPS + 1)]), "steps"),
        ]
        for p, want in bad:
            with self.subTest(want=want):
                self.assertIn(want, G.check_plan(p, self.TOOLS))
        with self.assertRaises(G.PlanError):
            G.extract_plan("no json here")

    def test_config(self):
        ok = {"schema": G.CONFIG_SCHEMA, "tools": {"add": {"python": "m:f"}}}
        self.assertEqual(G.config_problems(ok), [])
        for bad in ({"schema": "x"}, dict(ok, model=""), dict(ok, budget={"rpm": 0}), dict(ok, budget={}),
                    dict(ok, tools={"gemini": {"python": "m:f"}}), dict(ok, tools={"t": {"mcp": "nope", "tool": "x"}}),
                    dict(ok, fallback_model="gemini-2.5-flash"), dict(ok, cli=[]), dict(ok, daily={"requests": 0}),
                    dict(ok, daily={"reset_tz": "Mars/Base"}), dict(ok, daily={"reset_at": "24:00"})):
            with self.subTest(bad=bad):
                self.assertTrue(G.config_problems(bad))


# ---- S2-S5: the supervisor ------------------------------------------------------------------------------------------

D1_SCRIPT = [
    {"plan": plan([{"id": "a", "tool": "echo", "args": {"q": "issues"}}, {"id": "b", "tool": "add", "args": {"a": 2, "b": 3}},
                   {"id": "c", "tool": "add", "args": {"a": 1, "b": 1}, "after": ["b"]},
                   {"id": "e", "tool": "echo", "args": {"q": "labels"}}],  # not needed by the next model step
                  {"prompt": "summarise a and c", "after": ["a", "c"]}, "looking")},
    QUOTA_MIN_HINT,
    {"plan": plan([{"id": "d", "tool": "echo", "args": {"q": "post"}}], None, "all done")},
]


class RunTask:
    def run_task(self, script, prompt="list the issues", **cfg):
        box = Box(self, script, **cfg)
        clock, out = FakeClock(), io.StringIO()
        snaps = []

        def sleep(d):  # what is on disk while the supervisor waits
            snaps.append((box.state(), out.getvalue()))
            clock.sleep(d)
        sup = G.Supervisor(G.load_config(box.cfg_path), clock=clock, sleep=sleep, out=out)
        ok = sup.start(prompt)
        return box, sup, ok, clock, out.getvalue(), snaps


@needs_k12
class SupervisorTest(RunTask, unittest.TestCase):
    def test_d1_429_mid_plan_waits_shows_saves_and_resumes(self):
        box, sup, ok, clock, out, snaps = self.run_task(D1_SCRIPT)
        self.assertTrue(ok, out)
        # one status block: ETA from retryDelay, the state, the next step
        self.assertEqual(out.count("[ga gemini] quota:"), 1)
        self.assertIn("[ga gemini] quota: T1.m2 parked — resumes in 7 s", out)
        self.assertIn("(hint)", out)
        self.assertIn("  now:  done 5 · running 0 · parked 1 · requests left today 18/20", out)  # m1, a, b, c, e
        self.assertIn("  next: T1.m2 (model)", out)
        self.assertIn("ga gemini --resume", out)
        # the state file at the park: parked m2 until park + 7 s, the tool steps done
        st, shown = snaps[0]
        self.assertEqual(sorted(st["done"]), ["T1.m1", "T1.r1.a", "T1.r1.b", "T1.r1.c", "T1.r1.e"])
        at, since, source = st["parked"]["T1.m2"]
        self.assertEqual((round(at - since, 3), source, st["status"]), (7.0, "hint", "running"))
        # no busy retry: three turns, one sleep of exactly the 7 s the error named
        self.assertEqual((len(box.calls()), clock.sleeps), (3, [7.0]))
        c = box.calls()
        self.assertEqual([x["resume"] for x in c], [None, "s-1", "s-1"])
        self.assertEqual({x["cwd"] for x in c}, {str(box.dir.resolve())})  # sessions live per project dir
        self.assertEqual({x["model"] for x in c}, {MODEL})
        self.assertTrue(c[2]["prompt_has_results"])
        # the tool steps ran once each, before the park, and were not blocked by it
        self.assertEqual(box.lines("mcp.log"), ["echo", "echo", "echo"])
        self.assertEqual(sorted(box.lines("tools.log")), ["add", "add"])
        log = box.log()
        park = next(r for r in log if r["event"] == "park")
        tools_before = [r for r in log if r["event"] == "tool" and r["at"] <= park["at"]]
        self.assertEqual(len(tools_before), 4)  # e too: a tool step is never held behind a parked model step
        self.assertEqual((park["resumes_in_s"], park["source"], park["left_today"]), (7.0, "hint", 18))
        self.assertEqual(next(r for r in log if r["event"] == "resume")["waited_s"], 7.0)
        self.assertTrue(all(r["served"] == [MODEL] for r in log if r["event"] == "turn" and r["ok"]))
        end = log[-1]
        self.assertEqual((end["status"], end["failed"], end["skipped"], end["model_turns"], end["tool_steps"]),
                         ("done", 0, 0, 2, 5))
        self.assertTrue(out.rstrip().endswith("all done"))
        self.assertEqual(box.state()["status"], "done")

    def test_a_restart_keeps_the_rest_of_the_saved_wait(self):
        box = Box(self, D1_SCRIPT)
        clock = FakeClock()

        def crash(d):
            raise KeyboardInterrupt  # the terminal is closed while parked
        sup = G.Supervisor(G.load_config(box.cfg_path), clock=clock, sleep=crash, out=io.StringIO())
        with self.assertRaises(KeyboardInterrupt):
            sup.start("t")
        clock.t += 3.0
        out = io.StringIO()
        sup2 = G.Supervisor(G.load_config(box.cfg_path), clock=clock, sleep=clock.sleep, out=out)
        self.assertTrue(sup2.resume(), out.getvalue())
        self.assertEqual(clock.sleeps, [4.0])  # 7 s from the server, 3 s already gone
        self.assertIn("resumes in 4 s", out.getvalue())
        self.assertIn("(hint)", out.getvalue())
        self.assertIn("resumed T1.m2 after 7 s", out.getvalue())
        self.assertEqual(len(box.calls()), 3)

    def test_the_log_holds_labels_and_numbers_only(self):
        box, *_ = self.run_task(D1_SCRIPT, prompt="SECRET-PROMPT-TEXT")
        text = "\n".join(box.lines(".ga-gemini/log.jsonl") + box.lines(".ga-gemini/ledger.jsonl"))
        for raw in ("SECRET-PROMPT-TEXT", "summarise", "issues", "all done", "looking"):
            self.assertNotIn(raw, text)

    def test_a_quota_error_without_a_hint_waits_the_window(self):
        script = [{"plan": plan(next_={"prompt": "next"})}, QUOTA_MIN, {"plan": plan()}]
        box, sup, ok, clock, out, snaps = self.run_task(script)
        self.assertTrue(ok, out)
        self.assertEqual(clock.sleeps, [60.0])
        self.assertIn("resumes in 60 s", out)
        self.assertIn("(window)", out)

    def test_window_park_without_a_429(self):
        script = [{"plan": plan([{"id": "a", "tool": "add", "args": {"a": 1, "b": 2}}], {"prompt": "next", "after": ["a"]})},
                  {"plan": plan()}]
        box, sup, ok, clock, out, snaps = self.run_task(script, budget={"rpm": 1})
        self.assertTrue(ok, out)
        self.assertIn("(window)", out)
        self.assertEqual(len(clock.sleeps), 1)
        self.assertAlmostEqual(clock.sleeps[0], 60.0, places=3)
        self.assertIn("resumes in 60 s", out)

    def test_served_model_mismatch_is_never_accepted(self):
        script = [{"plan": plan([{"id": "a", "tool": "add", "args": {"a": 1, "b": 2}}]), "served": ["gemini-2.5-flash"]}]
        box, sup, ok, clock, out, snaps = self.run_task(script)
        self.assertFalse(ok)
        st = box.state()
        self.assertEqual((st["status"], st["failed"], st["done"]), ("failed", {"T1.m1": "ModelMismatch"}, []))
        self.assertEqual([s["id"] for s in st["steps"]], ["T1.m1"])  # nothing of that answer was taken
        self.assertEqual(box.lines("tools.log"), [])
        self.assertIn("served_model_mismatch", [r.get("reason") for r in box.log()])
        self.assertEqual(len(box.calls()), 1)  # and not tried on another model

    def test_a_plan_outside_the_closed_table_fails_the_step(self):
        script = [{"plan": plan([{"id": "a", "tool": "shell", "args": {"cmd": "rm"}}])}]
        box, sup, ok, *_ = self.run_task(script)
        self.assertFalse(ok)
        self.assertEqual(box.state()["failed"], {"T1.m1": "PlanError"})

    def test_results_in_memory_are_capped_and_kept_on_disk(self):
        script = [{"plan": plan([{"id": "a", "tool": "big", "args": {"n": 50_000}}], {"prompt": "go", "after": ["a"]})},
                  {"plan": plan()}]
        box, sup, ok, *_ = self.run_task(script, result_cap=500)
        self.assertTrue(ok)
        self.assertEqual(len(sup.sched.results["T1.r1.a"]), 500)
        self.assertEqual(len(json.loads((box.dir / ".ga-gemini/results/T1.r1.a.json").read_text())), 50_000)
        self.assertLess(box.calls()[1]["prompt_len"], 2_000)

    def test_a_new_task_does_not_overwrite_an_unfinished_one(self):
        box = Box(self, D1_SCRIPT)
        st = {"schema": G.STATE_SCHEMA, "model": MODEL, "task": "T1", "status": "running", "steps": [], "done": [],
              "parked": {}, "tasks": 1}
        (box.dir / ".ga-gemini").mkdir()
        (box.dir / ".ga-gemini" / "state.json").write_text(json.dumps(st))
        out = io.StringIO()
        clock = FakeClock()
        sup = G.Supervisor(G.load_config(box.cfg_path), clock=clock, sleep=clock.sleep, out=out)
        self.assertFalse(sup.start("new"))
        self.assertIn("T1 is not finished", out.getvalue())
        self.assertEqual(box.calls(), [])

    def test_the_model_comes_from_the_config_only(self):
        box = Box(self, D1_SCRIPT, model="gemini-3-pro-preview")
        sup = G.Supervisor(G.load_config(box.cfg_path), clock=FakeClock(), sleep=lambda d: None, out=io.StringIO())
        self.assertEqual(sup.cli.model, "gemini-3-pro-preview")
        self.assertEqual(G.Supervisor(G.load_config(Box(self, []).cfg_path)).cli.model, MODEL)
        st = {"schema": G.STATE_SCHEMA, "model": "gemini-2.5-flash", "task": "T1", "status": "running", "steps": [],
              "done": [], "parked": {}}
        (box.dir / ".ga-gemini" / "state.json").write_text(json.dumps(st))
        with self.assertRaises(FormError):  # a saved task does not switch the model
            sup.resume()


@needs_k12
class FixtureRunTest(RunTask, unittest.TestCase):
    """D3: a `ga gemini` task over recorded fixtures in the 0.62 shapes: a per-minute quota error with a hint, then the
    daily TerminalQuotaError, then the one probe at the reset; and the turns that must fail."""

    def test_minute_then_day_then_probe(self):
        script = [{"plan": plan([{"id": "a", "tool": "add", "args": {"a": 1, "b": 2}}], {"prompt": "on", "after": ["a"]})},
                  QUOTA_MIN_HINT,
                  {"plan": plan([{"id": "b", "tool": "echo", "args": {"k": 2}}], {"prompt": "last", "after": ["b"]})},
                  QUOTA_DAY,
                  {"plan": plan(say="all done")}]
        box, sup, ok, clock, out, snaps = self.run_task(script)
        self.assertTrue(ok, out)
        self.assertEqual(clock.sleeps, [7.0, 54_000.0 - 7.0])  # the hint, then up to 00:00 America/Los_Angeles
        self.assertIn("quota: T1.m2 parked — resumes in 7 s", out)
        self.assertIn("[ga gemini] daily quota: T1.m3 parked — the quota resets at 00:00 America/Los_Angeles, in 15 h 0 min",
                      out)
        self.assertIn("one probe then", out)
        self.assertIn("requests left today 0/20", out)
        self.assertEqual(out.count("[ga gemini] daily quota reset: one probe (T1.m3)"), 1)
        log = box.log()
        self.assertEqual([r["source"] for r in log if r["event"] == "park"], ["hint", "daily reset"])
        self.assertEqual([r["step"] for r in log if r["event"] == "probe"], ["T1.m3"])
        calls = box.calls()
        self.assertEqual(len(calls), 5)
        self.assertEqual({c["max_attempts"] for c in calls}, {1})
        self.assertEqual(box.state()["failed"], {})
        self.assertEqual(json.loads((box.dir / ".ga-gemini" / "day.json").read_text())["used"], 1)  # a new day: the probe

    def test_source_shaped_quota_results_wait_and_resume(self):
        script = [{"plan": plan(next_={"prompt": "on"})}, {"fixture": "quota_minute_source", "exit": 173},
                  {"plan": plan(next_={"prompt": "last"})}, {"fixture": "quota_day_source", "exit": 173},
                  {"plan": plan()}]
        box, sup, ok, clock, out, snaps = self.run_task(script)
        self.assertTrue(ok, out)
        self.assertEqual(clock.sleeps, [7.5, 54_000.0 - 7.5])
        turns = [r for r in box.log() if r["event"] == "turn" and not r["ok"]]
        self.assertEqual([(r["reason"], r["source"], r["via"]) for r in turns],
                         [("rate_limited_minute", "hint", "type"), ("rate_limited_day", "daily reset", "type")])

    def test_a_quota_known_only_from_the_text(self):
        box, sup, ok, clock, out, snaps = self.run_task([{"plan": plan(next_={"prompt": "on"})},
                                                         {"fixture": "quota_text_only", "exit": 173}, {"plan": plan()}])
        self.assertTrue(ok, out)
        self.assertEqual(clock.sleeps, [3.0])
        self.assertIn("text", [r.get("via") for r in box.log()])

    def test_the_turns_that_fail(self):
        for fixture, reason in (("crash", "no_result"), ("max_turns", "cli_error"), ("mismatch", "served_model_mismatch"),
                                ("api_500", "result_ApiError")):
            with self.subTest(fixture=fixture):
                box, sup, ok, clock, out, snaps = self.run_task([{"fixture": fixture, "exit": 1}])
                self.assertFalse(ok)
                self.assertEqual(len(box.calls()), 1)  # failed, not retried
                self.assertEqual(clock.sleeps, [])
                self.assertIn(reason, [r.get("reason") for r in box.log()])
                self.assertEqual(box.lines("tools.log"), [])  # nothing of a failed turn ran


@needs_k12
class DailyQuotaTest(RunTask, unittest.TestCase):
    """D5 (S6): the daily quota is not a minute window."""

    def test_a_probe_that_hits_the_quota_again_waits_for_the_next_reset(self):
        script = [{"plan": plan(next_={"prompt": "on"})}, QUOTA_DAY, QUOTA_DAY, {"plan": plan()}]
        box, sup, ok, clock, out, snaps = self.run_task(script)
        self.assertTrue(ok, out)
        self.assertEqual(clock.sleeps, [54_000.0, 86_400.0])  # one probe per reset, nothing in between: no loop
        self.assertEqual(len(box.calls()), 4)
        self.assertEqual(out.count("[ga gemini] daily quota: T1.m2 parked"), 2)
        self.assertEqual(len([r for r in box.log() if r["event"] == "probe"]), 2)

    def test_requests_left_today_count_down_and_the_last_one_is_not_spent_on_a_known_429(self):
        script = [{"plan": plan(next_={"prompt": "2"})}, {"plan": plan(next_={"prompt": "3"})}, {"plan": plan()}]
        box, sup, ok, clock, out, snaps = self.run_task(script, daily={"requests": 2})
        self.assertTrue(ok, out)
        self.assertEqual(clock.sleeps, [54_000.0])
        st, shown = snaps[0]
        self.assertEqual(len(box.calls()), 3)
        self.assertIn("daily quota: T1.m3 parked", shown)
        self.assertIn("requests left today 0/2", shown)
        self.assertEqual(sorted(st["done"]), ["T1.m1", "T1.m2"])  # m3 waited for the reset without a call

    def test_next_reset(self):
        start = FakeClock.START
        self.assertEqual(G.next_reset(start, "America/Los_Angeles", "00:00"), start + 54_000)
        self.assertEqual(G.next_reset(start + 54_000, "America/Los_Angeles", "00:00"), start + 54_000 + 86_400)
        self.assertEqual(G.next_reset(start, "UTC", "00:00") % 86_400, 0)


@needs_k12
class InTurnStatusTest(RunTask, unittest.TestCase):
    """D6 (S7): a turn running longer than turn_status_s gets one line per interval; a short turn none."""

    def test_a_long_turn_gets_a_line_per_interval(self):
        box, sup, ok, clock, out, snaps = self.run_task([{"plan": plan(), "sleep": 1.6}], turn_status_s=0.5)
        self.assertTrue(ok, out)
        lines = [x for x in out.splitlines() if x.startswith("[ga gemini] turn T1.m1 running")]
        self.assertTrue(2 <= len(lines) <= 4, lines)  # at about 0.5, 1.0 and 1.5 s
        self.assertIn("the CLI may be retrying a quota error inside the turn · done 0 · running 1 · parked 0", lines[0])
        waits = [r for r in box.log() if r["event"] == "turn_wait"]
        self.assertEqual(len(waits), len(lines))
        self.assertTrue(all(0.4 <= b["seconds"] - a["seconds"] <= 0.9 for a, b in zip(waits, waits[1:])), waits)

    def test_a_short_turn_gets_none(self):
        box, sup, ok, clock, out, snaps = self.run_task([{"plan": plan()}], turn_status_s=0.5)
        self.assertTrue(ok, out)
        self.assertNotIn("running", out.replace("running 0", ""))
        self.assertFalse([r for r in box.log() if r["event"] == "turn_wait"])

    def test_the_default_interval_is_20_s(self):
        self.assertEqual(G.GeminiConfig(root=Path(".")).turn_status_s, 20.0)


@needs_k12
class ConfigToSchedulerTest(RunTask, unittest.TestCase):
    def test_the_rpm_comes_from_the_config(self):
        box, sup, ok, *_ = self.run_task([{"plan": plan()}], budget={"rpm": 7})
        self.assertEqual(sup.sched.gov.budgets[MODEL].rpm, 7)
        f = box.dir / "minimal.json"
        f.write_text(json.dumps({"schema": G.CONFIG_SCHEMA}))
        self.assertEqual(G.load_config(f).budget, G.DEFAULT_BUDGET)  # below the server limit, so 429s are rare
        self.assertEqual(G.DEFAULT_BUDGET, {"rpm": 5})

    def test_max_parallel_reaches_a_scheduler_that_takes_it(self):
        from rlo.scheduler import Scheduler
        import inspect
        box, sup, ok, *_ = self.run_task([{"plan": plan()}], max_parallel=3)
        self.assertTrue(ok)
        if "max_parallel" in inspect.signature(Scheduler).parameters:  # K12 rev 3
            self.assertEqual(sup.sched.max_parallel, 3)
        else:  # K12 rev 2 (the pin until rev 3 is integrated): accepted, pending
            self.assertEqual([r["max_parallel"] for r in box.log() if r["event"] == "max_parallel_pending"], [3])
        self.assertTrue(G.config_problems({"schema": G.CONFIG_SCHEMA, "max_parallel": 0}))


@needs_k12
class CrashResumeTest(unittest.TestCase):
    """D2: the real `ga gemini` process is killed while parked; `ga gemini --resume` completes the plan."""

    def test_kill_while_parked_then_resume(self):
        script = [{"plan": plan([{"id": "a", "tool": "add", "args": {"a": 2, "b": 2}},
                                 {"id": "b", "tool": "echo", "args": {"k": 1}}], {"prompt": "finish", "after": ["a", "b"]})},
                  {"fixture": "quota_minute_hint2", "exit": 173},
                  {"plan": plan([{"id": "c", "tool": "add", "args": {"a": 9, "b": 1}}], None, "finished")}]
        box = Box(self, script)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), str(TESTS)]), PYTHONDONTWRITEBYTECODE="1")
        cmd = [sys.executable, "-m", "ga", "gemini", "--config", str(box.cfg_path)]
        p = subprocess.Popen(cmd + ["do the thing"], cwd=box.dir, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        state = box.dir / ".ga-gemini" / "state.json"
        deadline = time.time() + 60
        while time.time() < deadline:
            if state.exists():
                try:
                    if json.loads(state.read_text()).get("parked"):
                        break
                except ValueError:
                    pass
            time.sleep(0.05)
        p.send_signal(signal.SIGKILL)
        out, _ = p.communicate(timeout=30)
        self.assertEqual(p.returncode, -signal.SIGKILL)
        self.assertIn("resumes in 2 s", out)
        st = json.loads(state.read_text())
        self.assertEqual((st["status"], sorted(st["parked"])), ("running", ["T1.m2"]))
        self.assertEqual(len(box.calls()), 2)
        r = subprocess.run(cmd + ["--resume"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("T1 done", r.stdout)
        self.assertIn("finished", r.stdout)
        st = json.loads(state.read_text())
        self.assertEqual((st["status"], st["failed"]), ("done", {}))
        self.assertEqual(sorted(st["done"]), ["T1.m1", "T1.m2", "T1.r1.a", "T1.r1.b", "T1.r2.c"])
        calls = box.calls()
        self.assertEqual((len(calls), calls[2]["resume"]), (3, "s-1"))
        # resumed from another directory, the CLI still runs where the task's session lives
        self.assertEqual({c["cwd"] for c in calls}, {str(box.dir.resolve())})
        self.assertEqual(box.lines("tools.log"), ["add", "add"])  # a before the kill, c after: none run twice
        self.assertEqual(box.lines("mcp.log"), ["echo"])
        resume = next(r for r in (json.loads(x) for x in box.lines(".ga-gemini/log.jsonl")) if r["event"] == "resume")
        self.assertGreaterEqual(resume["waited_s"], 2.0)  # the saved wait was kept across the kill
        again = subprocess.run(cmd + ["--resume"], cwd=box.dir, env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("nothing to resume: T1 is done", again.stdout)


class StatusBlockTest(unittest.TestCase):
    """The block is shown once per park, not on every sleep of the same park (a stub scheduler stays parked)."""

    def test_once_per_park(self):
        box = Box(self, [])
        clock, out = FakeClock(), io.StringIO()
        sup = G.Supervisor(G.load_config(box.cfg_path), clock=clock, sleep=clock.sleep, out=out)
        sup.st = {"schema": G.STATE_SCHEMA, "model": MODEL, "task": "T1", "parked": {}, "done": [], "steps": []}

        status = {"resumes_in_s": 30.0, "done": ["T1.m1"], "running": None, "parked": ["T1.m2"],
                  "next": [{"id": "T1.m2", "kind": "model"}]}
        sup.sched = mock.Mock(rows=[], status=lambda: dict(status))
        sup._sleep_hook(10.0)
        sup._sleep_hook(20.0)
        self.assertEqual(out.getvalue().count("[ga gemini] quota:"), 1)
        self.assertEqual(clock.sleeps, [10.0, 20.0])
        status.update(parked=["T1.m3"], done=["T1.m1", "T1.m2"], next=[{"id": "T1.m3", "kind": "model"}])
        sup._sleep_hook(5.0)
        self.assertEqual(out.getvalue().count("[ga gemini] quota:"), 2)  # a new park is shown

    def test_a_wait_that_never_opens(self):
        box = Box(self, [])
        clock, out = FakeClock(), io.StringIO()
        sup = G.Supervisor(G.load_config(box.cfg_path), clock=clock, sleep=clock.sleep, out=out)
        sup.st = {"schema": G.STATE_SCHEMA, "model": MODEL, "task": "T1", "parked": {}, "done": [], "steps": []}
        sup.sched = mock.Mock(rows=[], status=lambda: {"resumes_in_s": None, "done": [], "running": "T1.r1.a",
                                                        "parked": ["T1.m2"], "next": []})
        sup._sleep_hook(1.0)
        self.assertIn("T1.m2 parked — no window opens", out.getvalue())
        self.assertIn("running 1", out.getvalue())
        self.assertEqual(sup.st["parked"]["T1.m2"][0], None)


class CliTest(unittest.TestCase):
    def test_bad_config_is_exit_2(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d)
        (d / "g.json").write_text(json.dumps({"schema": G.CONFIG_SCHEMA, "fallback_model": "x"}))
        r = subprocess.run([sys.executable, "-m", "ga", "gemini", "--config", str(d / "g.json"), "hi"], cwd=ROOT,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("config: [hard] $.fallback_model: unknown field", r.stderr)


if __name__ == "__main__":
    unittest.main()
