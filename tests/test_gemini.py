"""CMD-GA21 (BD-221): `ga gemini` — a fixed model, a closed step list, rlo's scheduler, wait-and-resume.

D1 the fake Gemini CLI: a 429 with retryDelay mid-plan -> one status block (ETA, state, next), the state file, tool
steps not blocked, the model step resumed after the window with --resume, zero failed steps; a served-model mismatch
is a failed turn. D2 kill the supervisor while parked; `ga gemini --resume` completes from the state file.
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

from ga.adapters.gemini_cli import (GeminiCLI, GeminiError, GeminiRateLimited, ModelMismatch, parse_stream,
                                    rate_limit_body)
from ga.forms import FormError
from ga import gemini as G

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
FAKE = [sys.executable, str(TESTS / "fake_gemini.py")]
MODEL = "gemini-3-flash-preview"

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
    def __init__(self):
        self.t, self.sleeps = 1_800_000_000.0, []

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
                          json.dumps({"type": "result", "status": "success", "stats": {
                              "input_tokens": 5, "output_tokens": 2, "total_tokens": 7, "models": {"other-model": {}}}})])
        self.assertEqual((s.session_id, s.text, s.usage, s.served, s.status),
                         ("s9", "abc", {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}, [MODEL, "other-model"],
                          "success"))

    def test_rate_limit_body(self):
        api = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"}]}}
        ev = json.dumps({"type": "error", "message": "[API Error: " + json.dumps(api) + "]"})
        self.assertEqual(rate_limit_body([ev]), api)
        hint = rate_limit_body(["", "Error 429: quota exceeded. Please retry in 12.5s."])
        self.assertEqual(hint["error"]["details"][0]["retryDelay"], "12.5s")
        self.assertEqual(rate_limit_body(["429 RESOURCE_EXHAUSTED"])["error"]["details"], [])
        self.assertIsNone(rate_limit_body(["boom", json.dumps({"error": {"code": 500}})]))


class AdapterTest(unittest.TestCase):
    def cli(self, script, model=MODEL):
        self.box = Box(self, script)
        return GeminiCLI(FAKE, model, cwd=str(self.box.dir))

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

    def test_served_model_mismatch_is_a_failed_turn(self):
        cli = self.cli([{"plan": plan(), "served": "gemini-2.5-flash"}, {"plan": plan(), "no_model": True}])
        with self.assertRaises(ModelMismatch) as e:
            cli.run_turn("x")
        self.assertEqual(e.exception.reason, "served_model_mismatch")
        with self.assertRaises(ModelMismatch) as e:
            cli.run_turn("x")
        self.assertEqual(e.exception.reason, "served_model_unknown")

    def test_429_carries_the_retry_delay(self):
        cli = self.cli([{"rate_limit": "7s"}, {"rate_limit_text": "3"}, {"fail": True}])
        with self.assertRaises(GeminiRateLimited) as e:
            cli.run_turn("x")
        self.assertEqual((e.exception.status, e.exception.body["error"]["details"][1]["retryDelay"]), (429, "7s"))
        with self.assertRaises(GeminiRateLimited) as e:
            cli.run_turn("x")
        self.assertEqual(e.exception.body["error"]["details"][0]["retryDelay"], "3s")
        with self.assertRaises(GeminiError) as e:
            cli.run_turn("x")
        self.assertNotIsInstance(e.exception, GeminiRateLimited)

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
                    dict(ok, fallback_model="gemini-2.5-flash"), dict(ok, cli=[])):
            with self.subTest(bad=bad):
                self.assertTrue(G.config_problems(bad))


# ---- S2-S5: the supervisor ------------------------------------------------------------------------------------------

D1_SCRIPT = [
    {"plan": plan([{"id": "a", "tool": "echo", "args": {"q": "issues"}}, {"id": "b", "tool": "add", "args": {"a": 2, "b": 3}},
                   {"id": "c", "tool": "add", "args": {"a": 1, "b": 1}, "after": ["b"]},
                   {"id": "e", "tool": "echo", "args": {"q": "labels"}}],  # not needed by the next model step
                  {"prompt": "summarise a and c", "after": ["a", "c"]}, "looking")},
    {"rate_limit": "7s"},
    {"plan": plan([{"id": "d", "tool": "echo", "args": {"q": "post"}}], None, "all done")},
]


@needs_k12
class SupervisorTest(unittest.TestCase):
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

    def test_d1_429_mid_plan_waits_shows_saves_and_resumes(self):
        box, sup, ok, clock, out, snaps = self.run_task(D1_SCRIPT)
        self.assertTrue(ok, out)
        # one status block: ETA from retryDelay, the state, the next step
        self.assertEqual(out.count("[ga gemini] quota:"), 1)
        self.assertIn("[ga gemini] quota: T1.m2 parked — resumes in 7 s", out)
        self.assertIn("(retryDelay)", out)
        self.assertIn("  now:  done 5 · running 0 · parked 1", out)  # m1, a, b, c, e
        self.assertIn("  next: T1.m2 (model)", out)
        self.assertIn("ga gemini --resume", out)
        # the state file at the park: parked m2 until park + 7 s, the tool steps done
        st, shown = snaps[0]
        self.assertEqual(sorted(st["done"]), ["T1.m1", "T1.r1.a", "T1.r1.b", "T1.r1.c", "T1.r1.e"])
        at, since, source = st["parked"]["T1.m2"]
        self.assertEqual((round(at - since, 3), source, st["status"]), (7.0, "retryDelay", "running"))
        # no busy retry: three turns, one sleep of exactly the server's 7 s
        self.assertEqual((len(box.calls()), clock.sleeps), (3, [7.0]))
        c = box.calls()
        self.assertEqual([x["resume"] for x in c], [None, "s-1", "s-1"])
        self.assertEqual({x["model"] for x in c}, {MODEL})
        self.assertTrue(c[2]["prompt_has_results"])
        # the tool steps ran once each, before the park, and were not blocked by it
        self.assertEqual(box.lines("mcp.log"), ["echo", "echo", "echo"])
        self.assertEqual(sorted(box.lines("tools.log")), ["add", "add"])
        log = box.log()
        park = next(r for r in log if r["event"] == "park")
        tools_before = [r for r in log if r["event"] == "tool" and r["at"] <= park["at"]]
        self.assertEqual(len(tools_before), 4)  # e too: a tool step is never held behind a parked model step
        self.assertEqual((park["resumes_in_s"], park["source"]), (7.0, "retryDelay"))
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
        self.assertIn("(retryDelay)", out.getvalue())
        self.assertIn("resumed T1.m2 after 7 s", out.getvalue())
        self.assertEqual(len(box.calls()), 3)

    def test_the_log_holds_labels_and_numbers_only(self):
        box, *_ = self.run_task(D1_SCRIPT, prompt="SECRET-PROMPT-TEXT")
        text = "\n".join(box.lines(".ga-gemini/log.jsonl") + box.lines(".ga-gemini/ledger.jsonl"))
        for raw in ("SECRET-PROMPT-TEXT", "summarise", "issues", "all done", "looking"):
            self.assertNotIn(raw, text)

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
        script = [{"plan": plan([{"id": "a", "tool": "add", "args": {"a": 1, "b": 2}}]), "served": "gemini-2.5-flash"}]
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
class CrashResumeTest(unittest.TestCase):
    """D2: the real `ga gemini` process is killed while parked; `ga gemini --resume` completes the plan."""

    def test_kill_while_parked_then_resume(self):
        script = [{"plan": plan([{"id": "a", "tool": "add", "args": {"a": 2, "b": 2}},
                                 {"id": "b", "tool": "echo", "args": {"k": 1}}], {"prompt": "finish", "after": ["a", "b"]})},
                  {"rate_limit": "2s"},
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
        r = subprocess.run(cmd + ["--resume"], cwd=box.dir, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("T1 done", r.stdout)
        self.assertIn("finished", r.stdout)
        st = json.loads(state.read_text())
        self.assertEqual((st["status"], st["failed"]), ("done", {}))
        self.assertEqual(sorted(st["done"]), ["T1.m1", "T1.m2", "T1.r1.a", "T1.r1.b", "T1.r2.c"])
        calls = box.calls()
        self.assertEqual((len(calls), calls[2]["resume"]), (3, "s-1"))
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

    def test_a_daily_quota_says_not_today(self):
        box = Box(self, [])
        clock, out = FakeClock(), io.StringIO()
        sup = G.Supervisor(G.load_config(box.cfg_path), clock=clock, sleep=clock.sleep, out=out)
        sup.st = {"schema": G.STATE_SCHEMA, "model": MODEL, "task": "T1", "parked": {}, "done": [], "steps": []}
        sup.sched = mock.Mock(rows=[], status=lambda: {"resumes_in_s": None, "done": [], "running": "T1.r1.a",
                                                        "parked": ["T1.m2"], "next": []})
        sup._sleep_hook(1.0)
        self.assertIn("T1.m2 parked — does not resume today (daily quota)", out.getvalue())
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
