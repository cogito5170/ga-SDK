"""CMD-GA41: GA Engine's model loop with a small model (gpt-oss-120b-medium through agy).

One repair turn (S1), a failed tool step as a result (S2), transient server errors retried once (S3), agv under a
tool-less plugin agent (S4), real paths (S5), the thinking cap (S6). Fake backends and a fake agy only: 0 network,
0 model calls.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from ga import backends  # noqa: E402
from ga import gemini as G  # noqa: E402
from ga import repair as RP  # noqa: E402
from ga.backends import agy_agent  # noqa: E402
from ga.backends.base import (BackendError, BackendTurn, ConfigError, Transient, retry_transient,  # noqa: E402
                              transient_code)
from ga.backends.builtin import AGV, AgvRunner  # noqa: E402
from ga.paths import real, same  # noqa: E402

import ga41_tools  # noqa: E402
from test_gemini import FakeClock  # noqa: E402

TESTS = Path(__file__).parent
FAKE_AGY = [sys.executable, str(TESTS / "fake_agy.py")]
MODEL = "gpt-oss-120b-medium"
TASK = "TASK-MARKER-41: read notes.md and add two and three"
TOOLS = {"read_file": {"python": "ga41_tools:read_file", "about": "read a file"},
         "add": {"python": "ga41_tools:add", "about": "add two integers"}}
INTERNAL = "API error (attempt 3): INTERNAL (code 500)"


def plan(steps=(), nxt=None, say=None):
    p = {"schema": "ga-plan/1", "steps": list(steps), "next": nxt}
    if say is not None:
        p["say"] = say
    return p


def fenced(obj):
    return "```json\n" + json.dumps(obj) + "\n```\n"


DONE_PLAN = plan(say="five")
ADD_PLAN = plan([{"id": "a", "tool": "add", "args": {"a": 2, "b": 3}}], {"prompt": "report the sum", "after": ["a"]})


class TextCli:
    """An in-process runner answering scripted texts (an exception is raised); records prompt and system."""
    resumes, usage_format = False, "openai"

    def __init__(self, answers, bare=False):
        self.answers, self.bare, self.calls = list(answers), bare, []

    def run_turn(self, prompt, session=None, *, system=None, on_wait=None, wait_every_s=None):
        self.calls.append({"prompt": prompt, "system": system})
        a = self.answers[min(len(self.calls), len(self.answers)) - 1]
        if isinstance(a, BaseException):
            raise a
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                 "completion_tokens_details": {"reasoning_tokens": 15}}
        return BackendTurn(a, [MODEL], usage, "openai", None, 0.5, 1)


class Loop(unittest.TestCase):
    """ga supervise (ga-supervise/1) in-process on a temp dir, a fake clock."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ga41-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        ga41_tools.ROOT["dir"] = str(self.dir)
        (self.dir / "here.md").write_text("hello\n")

    def env(self, **kv):
        p = mock.patch.dict(os.environ, kv)
        p.start()
        self.addCleanup(p.stop)

    def supervise(self, cli=None, backend="agv", options=None, **extra):
        path = self.dir / "ga-supervise.json"
        path.write_text(json.dumps({"schema": G.SUPERVISE_SCHEMA, "backend": backend, "model": MODEL,
                                    "options": options or {"cli": FAKE_AGY}, "tools": TOOLS, **extra}))
        self.clock, self.out = FakeClock(), io.StringIO()
        sup = G.Supervisor(G.load_config(path), cli=cli, clock=self.clock, sleep=self.clock.sleep, out=self.out)
        ok = sup.start(TASK)
        return sup, ok

    def log(self, sup):
        return [json.loads(x) for x in sup.log_file.read_text().splitlines()]

    def state(self, sup):
        return json.loads(sup.state_file.read_text())

    # ---- fake agy (a real process, argv only)
    def agy(self, script):
        d = self.dir / "agy"
        d.mkdir(exist_ok=True)
        (d / "script.json").write_text(json.dumps(script))
        self.env(FAKE_AGY_DIR=str(d))
        return d

    def agy_calls(self, kind="turn"):
        f = self.dir / "agy" / "calls.jsonl"
        rows = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
        return [r for r in rows if r["kind"] == kind]


# ---- S1: one repair turn --------------------------------------------------------------------------------------------

class RepairSupervise(Loop):
    def test_not_json_gets_exactly_one_repair_turn_with_labels_and_previous_answer_only(self):
        bad = "Sure! I will read the file first and then add the numbers."
        cli = TextCli([bad, fenced(DONE_PLAN)])
        sup, ok = self.supervise(cli)
        self.assertTrue(ok, self.out.getvalue())
        self.assertEqual(len(cli.calls), 2)
        first, second = cli.calls[0]["prompt"], cli.calls[1]["prompt"]
        self.assertIn(TASK, first)
        self.assertIn("not_json", second)
        self.assertIn(bad, second)                       # its own previous answer
        self.assertIn('"schema": "ga-plan/1"', second)    # the form's short reminder
        self.assertNotIn(TASK, second)                    # never the task again
        self.assertNotIn("read a file", second)           # nor the tool table / protocol
        rows = self.log(sup)
        self.assertEqual([r["problems"] for r in rows if r["event"] == "repair"], [["not_json"]])
        self.assertEqual(rows[-1]["repairs"], 1)
        self.assertEqual([r.get("kind") for r in rows if r["event"] == "turn"], ["plan", "repair"])
        self.assertEqual(self.state(sup)["say"], "five")

    def test_a_non_string_say_is_repaired_by_its_label(self):
        bad = fenced({"schema": "ga-plan/1", "steps": [], "next": None, "say": {"text": "five"}})
        cli = TextCli([bad, fenced(DONE_PLAN)])
        sup, ok = self.supervise(cli)
        self.assertTrue(ok)
        self.assertEqual(len(cli.calls), 2)
        self.assertIn("- say: must be a string", cli.calls[1]["prompt"])
        self.assertIn('"say": {"text": "five"}', cli.calls[1]["prompt"])
        self.assertNotIn(TASK, cli.calls[1]["prompt"])

    def test_a_second_bad_answer_fails_the_step_no_third_turn(self):
        cli = TextCli(["not json at all", "still not json", fenced(DONE_PLAN)])
        sup, ok = self.supervise(cli)
        self.assertFalse(ok)
        self.assertEqual(len(cli.calls), 2)              # one repair, never more
        self.assertEqual(self.state(sup)["failed"], {"T1.m1": "PlanError"})
        self.assertEqual(self.log(sup)[-1]["repairs"], 1)

    def test_bare_host_repair_keeps_the_protocol_as_system_but_not_the_task(self):
        cli = TextCli(["{oops", fenced(DONE_PLAN)], bare=True)
        sup, ok = self.supervise(cli)
        self.assertTrue(ok)
        self.assertIn(TASK, cli.calls[0]["prompt"])
        self.assertNotIn(TASK, cli.calls[1]["prompt"])
        self.assertEqual(cli.calls[1]["system"], cli.calls[0]["system"])

    def test_previous_answer_is_capped(self):
        text = RP.prompt(["not_json: x"], "FORM", "y" * 10_000)
        self.assertLess(len(text), RP.PREVIOUS_CAP + 400)
        self.assertIn("(cut)", text)

    def test_every_model_turn_repairs_not_just_the_first(self):
        cli = TextCli([fenced(ADD_PLAN), "the sum is 5", fenced(DONE_PLAN)])
        sup, ok = self.supervise(cli)
        self.assertTrue(ok, self.out.getvalue())
        self.assertEqual(len(cli.calls), 3)
        self.assertIn("not_json", cli.calls[2]["prompt"])
        self.assertNotIn("Results", cli.calls[2]["prompt"])  # nor the tool results again


# ---- S2: a failed tool step is a result -----------------------------------------------------------------------------

class ToolErrors(Loop):
    def test_a_raising_tool_becomes_a_result_the_next_turn_sees_and_the_task_ends_done(self):
        p1 = plan([{"id": "r", "tool": "read_file", "args": {"path": "missing.md"}}], {"prompt": "go on", "after": ["r"]})
        cli = TextCli([fenced(p1), fenced(DONE_PLAN)])
        sup, ok = self.supervise(cli)
        self.assertTrue(ok, self.out.getvalue())
        st = self.state(sup)
        self.assertEqual((st["status"], st["failed"], st["tool_errors"]), ("done", {}, 1))
        self.assertIn("read_file failed: ValueError: no such file: missing.md", cli.calls[1]["prompt"])
        self.assertNotIn("Traceback", cli.calls[1]["prompt"])
        tool = [r for r in self.log(sup) if r["event"] == "tool"]
        self.assertEqual((tool[0]["ok"], tool[0]["error"]), (False, "ValueError: no such file: missing.md"))
        self.assertEqual(self.log(sup)[-1]["tool_errors"], 1)

    def test_a_tool_error_label_withholds_secret_shaped_text(self):
        label = G.tool_error_label(ValueError("bad token sk-ant-api03-" + "A" * 40 + "\n  at line 3"))
        self.assertNotIn("sk-ant-api03-AAAA", label)
        self.assertNotIn("line 3", label)

    def test_a_good_tool_still_runs(self):
        cli = TextCli([fenced(ADD_PLAN), fenced(DONE_PLAN)])
        sup, ok = self.supervise(cli)
        self.assertTrue(ok)
        self.assertIn("5", cli.calls[1]["prompt"])
        self.assertEqual(self.state(sup).get("tool_errors", 0), 0)


# ---- S3: transient server errors ------------------------------------------------------------------------------------

class TransientClass(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(transient_code(INTERNAL), 500)
        self.assertEqual(transient_code("AGY_ERROR: 503 MODEL_CAPACITY_EXHAUSTED"), 503)
        self.assertEqual(transient_code("", 529), 529)
        self.assertIsNone(transient_code("an internal note about the quota"))
        self.assertIsNone(transient_code("", 401))

    def test_retry_once_exactly(self):
        sleeps, n = [], []

        def boom():
            n.append(1)
            raise Transient(503)
        with self.assertRaises(Transient):
            retry_transient(boom, backoff_s=20, sleep=sleeps.append)
        self.assertEqual((len(n), sleeps), (2, [20]))
        n.clear()
        self.assertEqual(retry_transient(lambda: n.append(1) or "ok", sleep=sleeps.append), "ok")
        self.assertEqual(len(n), 1)


class TransientAgy(Loop):
    def runner(self):
        return AgvRunner(FAKE_AGY, MODEL)

    def test_exit_0_with_status_error_internal_is_transient_500_never_success(self):
        self.agy([{"status_error": INTERNAL}])
        with self.assertRaises(Transient) as cm:
            self.runner().run_turn("hello")
        self.assertEqual(cm.exception.reason, "transient:500")

    def test_exit_0_with_a_non_transient_status_error_is_an_error(self):
        self.agy([{"status_error": "agent failed: tool loop"}])
        with self.assertRaises(BackendError) as cm:
            self.runner().run_turn("hello")
        self.assertEqual(cm.exception.reason, "agy_status_error")
        self.assertNotIsInstance(cm.exception, Transient)

    def test_internal_500_retried_once_after_the_backoff_then_done(self):
        self.agy([{"status_error": INTERNAL}, {"plan": DONE_PLAN}])
        sup, ok = self.supervise()
        self.assertTrue(ok, self.out.getvalue())
        self.assertEqual(len(self.agy_calls()), 2)
        self.assertIn(20.0, self.clock.sleeps)  # the default backoff
        self.assertEqual([r["reason"] for r in self.log(sup) if r["event"] == "transient"], ["transient:500"])

    def test_internal_500_twice_fails_as_transient_500(self):
        self.agy([{"status_error": INTERNAL}, {"status_error": INTERNAL}, {"plan": DONE_PLAN}])
        sup, ok = self.supervise(transient_backoff_s=3)
        self.assertFalse(ok)
        self.assertEqual(len(self.agy_calls()), 2)  # retried exactly once
        self.assertEqual(self.state(sup)["failed"], {"T1.m1": "transient:500"})
        self.assertIn("transient:500", self.out.getvalue())
        self.assertEqual(self.clock.sleeps.count(3), 1)

    def test_503_capacity_exit_3_likewise(self):
        self.agy([{"capacity": True}, {"capacity": True}])
        sup, ok = self.supervise(transient_backoff_s=0)
        self.assertFalse(ok)
        self.assertEqual(len(self.agy_calls()), 2)
        self.assertEqual(self.state(sup)["failed"], {"T1.m1": "transient:503"})

    def test_503_capacity_with_exit_0_status_error(self):
        self.agy([{"capacity_exit0": True}, {"plan": DONE_PLAN}])
        sup, ok = self.supervise(transient_backoff_s=0)
        self.assertTrue(ok)
        self.assertEqual(len(self.agy_calls()), 2)

    def test_quota_still_parks_and_is_never_transient(self):
        self.agy([{"quota": True}, {"plan": DONE_PLAN}])
        sup, ok = self.supervise(transient_backoff_s=0)
        self.assertTrue(ok, self.out.getvalue())
        rows = self.log(sup)
        self.assertTrue(any(r["event"] == "park" for r in rows))
        self.assertFalse(any(r["event"] == "transient" for r in rows))
        self.assertIn("agy quota", self.out.getvalue())

    def test_the_config_checks_the_backoff(self):
        with self.assertRaises(Exception):
            self.supervise(transient_backoff_s=-1)


class TransientOtherBackends(unittest.TestCase):
    def test_http_500_and_503_are_transient_401_is_not(self):
        from ga.backends import http as H

        def transport(status):
            return lambda url, headers, body, t: (status, {}, b"{}")
        for status, transient in ((500, True), (503, True), (529, True), (401, False)):
            r = H.OpenAIRunner("m", "http://x", None, transport=transport(status))
            with self.assertRaises(BackendError) as cm:
                r.run_turn("p", system="s")
            self.assertEqual(isinstance(cm.exception, Transient), transient, status)

    def test_claude_cli_api_500_is_transient(self):
        from ga.backends.builtin import parse_claude
        out = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "api_error_status": 500, "result": "API Error: 500 Internal server error"})
        with self.assertRaises(Transient) as cm:
            parse_claude(out, 1, "claude-haiku-4-5")
        self.assertEqual(cm.exception.code, 500)

    def test_intake_turn_retries_a_transient_once(self):
        from ga.intake.engine import Intake
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        sleeps = []
        cli = TextCli([Transient(503), "{}"], bare=True)
        t, answer = Intake(cli, backend="fake", model=MODEL, ga_dir=d, sleep=sleeps.append).turn("t", 1, "intake", "x", [])
        self.assertEqual((t.error, answer, len(cli.calls), sleeps), (None, "{}", 2, [20.0]))
        self.assertEqual(t.thinking, 15)
        cli = TextCli([Transient(500), Transient(500), "{}"], bare=True)
        t, _ = Intake(cli, backend="fake", model=MODEL, ga_dir=d, sleep=sleeps.append).turn("t", 1, "intake", "x", [])
        self.assertEqual((t.error, len(cli.calls)), ("transient:500", 2))


# ---- ga act: repair, transient, tool errors -------------------------------------------------------------------------

CALC = "def add(a, b):\n    return a - b\n"
TEST_CALC = ("import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n"
             "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
FIX = "EDIT calc.py\n<<<<<<< SEARCH\n    return a - b\n=======\n    return a + b\n>>>>>>> REPLACE\n"
GOAL = "GOAL-MARKER-41 make the tests pass"


class Act(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ga41-act-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "calc.py").write_text(CALC)
        (self.root / "test_calc.py").write_text(TEST_CALC)
        (self.root / ".ga-act.json").write_text(json.dumps({"commands": {
            "test": ["{python}", "-m", "unittest"], "boom": ["/nonexistent/ga41-binary"]}, "done_when": "test"}))
        self.sleeps = []

    def run_act(self, answers, bare=True, **kw):
        from ga.act import loop as A
        cli = TextCli(answers, bare=bare)
        res = A.run_item(self.root, {"id": "CMD-T41", "goal": GOAL, "files": ["calc.py"]}, backend="fake",
                         model=MODEL, runner=cli, state_dir=self.root.parent / (self.root.name + "-state"),
                         sleep=self.sleeps.append, **kw)
        self.addCleanup(shutil.rmtree, self.root.parent / (self.root.name + "-state"), True)
        return res, cli

    def test_a_broken_action_block_gets_one_repair_turn_without_the_card(self):
        bad = "I think the bug is in add; let me fix it."
        res, cli = self.run_act([bad, FIX])
        self.assertEqual(res.status, "done", res.reason)
        self.assertEqual(len(cli.calls), 2)
        rep = cli.calls[1]
        self.assertIn("no action found", rep["prompt"])
        self.assertIn(bad, rep["prompt"])
        self.assertIn("EDIT <path>", rep["prompt"])     # the action form
        self.assertNotIn(GOAL, rep["prompt"] + (rep["system"] or ""))
        self.assertNotIn("def add", rep["prompt"])      # no card material
        self.assertEqual(res.rows[0]["repairs"], 1)

    def test_a_non_bare_runner_gets_the_repair_text_whole(self):
        res, cli = self.run_act(["nothing useful", FIX], bare=False)
        self.assertEqual(res.status, "done")
        self.assertNotIn(GOAL, cli.calls[1]["prompt"])
        self.assertIsNone(cli.calls[1]["system"])

    def test_a_second_bad_answer_is_not_repaired_again(self):
        res, cli = self.run_act(["prose", "more prose", FIX], max_turns=1)
        self.assertEqual(len(cli.calls), 2)
        self.assertEqual(res.status, "blocked")
        self.assertIn("turn cap", res.reason)

    def test_a_good_answer_has_no_repair(self):
        res, cli = self.run_act([FIX])
        self.assertEqual((res.status, len(cli.calls), res.rows[0]["repairs"]), ("done", 1, 0))

    def test_transient_retried_once_then_done(self):
        res, cli = self.run_act([Transient(503), FIX])
        self.assertEqual((res.status, len(cli.calls), self.sleeps), ("done", 2, [20.0]))

    def test_transient_twice_blocks_as_transient(self):
        res, cli = self.run_act([Transient(500), Transient(500), FIX], transient_backoff_s=1)
        self.assertEqual((res.status, res.reason, len(cli.calls), self.sleeps), ("blocked", "transient:500", 2, [1]))

    def test_a_command_that_cannot_start_is_a_note_not_a_crash(self):
        res, cli = self.run_act(["RUN boom\n", FIX])
        self.assertEqual(res.status, "done", res.reason)
        self.assertIn("boom", cli.calls[1]["prompt"])


# ---- S4: agv under a plugin agent -----------------------------------------------------------------------------------

class Agent(unittest.TestCase):
    def test_agent_option_puts_agent_before_p(self):
        r = backends.create("agv", MODEL, {"agent": "ga-plan"}, {})
        a = r.argv("PROMPT")
        self.assertEqual(a[:3], ["agy", "--agent", "ga-plan"])
        self.assertLess(a.index("--agent"), a.index("-p"))
        self.assertNotIn("--agent", backends.create("agv", MODEL, {}, {}).argv("PROMPT"))
        for bad in ("Bad Name", "../x", "", 3):
            with self.assertRaises(ConfigError):
                backends.create("agv", MODEL, {"agent": bad}, {})

    def test_agent_reaches_agy_through_the_loop(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "script.json").write_text(json.dumps([{"plan": DONE_PLAN}]))
        with mock.patch.dict(os.environ, {"FAKE_AGY_DIR": str(d)}):
            r = backends.create("agv", MODEL, {"cli": FAKE_AGY, "agent": "ga-plan"}, {})
            r.run_turn("hello")
        argv = json.loads((d / "calls.jsonl").read_text().splitlines()[0])["argv"]
        self.assertEqual(argv[:2], ["--agent", "ga-plan"])
        self.assertEqual(argv[2], "-p")

    def test_overhead_records_the_measured_numbers(self):
        self.assertEqual(AGV.overhead["tokens"], 9852)
        self.assertEqual(AGV.overhead["tokens_toolless_agent"], [2530, 2958])

    def test_install_writes_the_plugin_and_runs_agy_plugin_install_as_argv(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"FAKE_AGY_DIR": str(d)}):
            rc = agy_agent.main(["install", "--dir", str(d / "agents"), "--cli", json.dumps(FAKE_AGY)], out=out)
        self.assertEqual(rc, 0, out.getvalue())
        plug = d / "agents" / "ga-plan"
        manifest = json.loads((plug / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "ga-plan")
        md = (plug / "agents" / "ga-plan.md").read_text()
        self.assertIn("tools: []", md)
        self.assertIn("one JSON block", md)
        (row,) = [json.loads(x) for x in (d / "calls.jsonl").read_text().splitlines()]
        self.assertEqual((row["kind"], row["argv"]), ("plugin", ["plugin", "install", str(plug)]))

    def test_install_calls_run_with_a_list_and_no_shell(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        seen = []

        def run(cmd, **kw):
            seen.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.assertEqual(agy_agent.main(["install", "--dir", str(d), "--name", "plan-x"], run=run, out=io.StringIO()), 0)
        ((cmd, kw),) = seen
        self.assertEqual(cmd, ["agy", "plugin", "install", str(d / "plan-x")])
        self.assertNotIn("shell", kw)

    def test_print_only_writes_and_prints(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        out, seen = io.StringIO(), []
        rc = agy_agent.main(["install", "--dir", str(d), "--print"], run=lambda *a, **k: seen.append(a), out=out)
        self.assertEqual((rc, seen), (0, []))
        self.assertIn(f"agy plugin install {d / 'ga-plan'}", out.getvalue())
        self.assertTrue((d / "ga-plan" / "agents" / "ga-plan.md").exists())

    def test_ga_cli_routes_agy_agent(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        p = subprocess.run([sys.executable, "-m", "ga", "agy-agent", "install", "--dir", str(d), "--print"],
                           capture_output=True, text=True, cwd=str(TESTS.parent))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("plugin install", p.stdout)


# ---- S5: real paths -------------------------------------------------------------------------------------------------

class RealPaths(unittest.TestCase):
    def test_a_symlinked_temp_root_compares_equal(self):
        from world import World, directive
        base = Path(tempfile.mkdtemp(prefix="ga41-real-"))
        self.addCleanup(shutil.rmtree, base, True)
        (base / "private").mkdir()
        link = base / "var"
        link.symlink_to(base / "private")  # macOS: /var -> /private/var
        old = tempfile.tempdir
        tempfile.tempdir = str(link)
        try:
            w = World(remote=True)
        finally:
            tempfile.tempdir = old
        self.addCleanup(w.close)
        self.assertTrue(str(w.tmp).startswith(str(link)))       # the test's side is the link
        w.hub.send(directive("CMD-A1", "A"))
        paths = w.hub.sandbox_paths("A")
        ws = w.ga / "worktrees" / "A"
        self.assertNotEqual(str(ws), real(ws))                   # textually different ...
        self.assertTrue(same(ws, paths["writable"][0]))          # ... the same directory
        self.assertEqual(paths["writable"][0], real(ws))
        for p in paths["protect"] + paths["writable"]:
            self.assertEqual(p, real(p))                         # stored real
        for r in ("alpha", "beta"):
            self.assertIn(real(w.repos[r]), paths["protect"])
            self.assertIn(real(w.tmp / "remotes" / f"{r}.git"), paths["protect"])
        self.assertIn(real(w.tmp), paths["protect"])


# ---- S6: the thinking cap -------------------------------------------------------------------------------------------

class Thinking(unittest.TestCase):
    def test_options(self):
        self.assertEqual(backends.create("claude_cli", "claude-haiku-4-5", {"thinking": "off"}, {}).env[
            "MAX_THINKING_TOKENS"], "0")
        self.assertEqual(backends.create("claude_cli", "claude-haiku-4-5", {"thinking": "low"}, {}).env[
            "MAX_THINKING_TOKENS"], "1024")
        self.assertEqual(backends.create("claude_cli", "claude-haiku-4-5", {}, {}).env.get("MAX_THINKING_TOKENS"),
                         os.environ.get("MAX_THINKING_TOKENS"))  # default: the environment's, untouched
        self.assertEqual(backends.create("agv", MODEL, {"thinking": "low"}, {}).argv("P")[-2:], ["--effort", "low"])
        self.assertNotIn("--effort", backends.create("agv", MODEL, {}, {}).argv("P"))
        self.assertEqual(backends.create("anthropic_http", "claude-sonnet-5-5", {"thinking": "low"}, {}).effort, "low")
        self.assertEqual(backends.create("anthropic_http", "claude-sonnet-5-5", {"thinking": "low", "effort": "high"},
                                         {}).effort, "high")
        for b in ("claude_cli", "agv", "anthropic_http"):
            with self.assertRaises(ConfigError):
                backends.create(b, MODEL if b == "agv" else "claude-haiku-4-5", {"thinking": "max"}, {})

    def test_thinking_tokens_recorded(self):
        self.assertEqual(G.thinking_tokens({"completion_tokens_details": {"reasoning_tokens": 7}}), 7)
        self.assertEqual(G.thinking_tokens({"thoughts_token_count": 9}), 9)
        self.assertIsNone(G.thinking_tokens({"input_tokens": 1}))
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        ga41_tools.ROOT["dir"] = str(d)
        path = d / "ga-supervise.json"
        path.write_text(json.dumps({"schema": G.SUPERVISE_SCHEMA, "backend": "agv", "model": MODEL,
                                    "options": {"cli": FAKE_AGY}, "tools": TOOLS}))
        clock = FakeClock()
        sup = G.Supervisor(G.load_config(path), cli=TextCli([fenced(DONE_PLAN)]), clock=clock, sleep=clock.sleep,
                           out=io.StringIO())
        self.assertTrue(sup.start("x"))
        turn = [json.loads(x) for x in sup.log_file.read_text().splitlines() if '"turn"' in x][0]
        self.assertEqual(turn["thinking_tokens"], 15)


class Version(unittest.TestCase):
    def test_0_9_0(self):
        import ga
        self.assertEqual(ga.__version__, "0.9.0")
        self.assertIn('version = "0.9.0"', (TESTS.parent / "pyproject.toml").read_text())


if __name__ == "__main__":
    unittest.main()
