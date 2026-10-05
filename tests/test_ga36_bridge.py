"""CMD-GA36 D2: ``ga bridge``. Part 1 is baseline's ops/agy_bridge/test_bridge.py ported as-is (only the imports
changed: ``ga.bridge`` and ``ga.bridge.tools``; PromptTest dropped with the deprecated prompt.py, BD-391). Part 2 adds
the GA36 rules: a 503 MODEL_CAPACITY_EXHAUSTED is retried once and then reported as capacity (not quota); the tools
come from ga; an end-to-end ``ga bridge --once`` through a real ``ga supervise`` with a fake agy. 0 model calls.
"""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from ga.bridge import tools as agy_tools  # noqa: E402
import ga.bridge as bridge  # noqa: E402
from ga.forms import hard, parse_text, validate  # noqa: E402
from ga.mailbox import Mailbox  # noqa: E402

DIRECTIVE = {"schema": "directive/2", "id": "CMD-AG1", "rev": 1, "to": "AGY", "after": [],
             "goal": "Summarize the project README.", "why": "bridge test",
             "scope": [{"id": "S1", "text": "read README.md"}],
             "done_when": [{"id": "D1", "text": "a three-line summary"}], "budget": {"claude_p_runs": 0}}


def git(*a, cwd=None):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)


def form(head):
    return "```ga\n" + json.dumps(head, separators=(",", ":")) + "\n```\n"


class World:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp())
        git("init", "-q", "--bare", str(self.tmp / "origin.git"))
        for who in ("hub", "mac"):
            git("clone", "-q", str(self.tmp / "origin.git"), str(self.tmp / who))
        self.work = self.tmp / "project"
        self.work.mkdir()
        (self.work / "README.md").write_text("hello\nworld\n")
        (self.work / "ga-supervise.json").write_text(json.dumps({"schema": "ga-supervise/1", "backend": "agv",
                                                                 "model": "gpt-oss-120b-medium", "tools": {}}))
        self.cfg = {**bridge.DEFAULTS, "mailbox_repo": str(self.tmp / "mac"), "workdir": str(self.work), "pull": False}
        self.hub = Mailbox(self.tmp / "hub", sleep=lambda s: None)
        self.mac = Mailbox(self.tmp / "mac", sleep=lambda s: None)


def fake_runner(out="done.\nTOOL_NEEDED: run_tests - run the repo's unit tests\n", code=0):
    calls = []

    def run(cfg, conf, task):
        calls.append({"conf": json.loads(conf.read_text()), "task": task})
        return {"code": code, "out": out, "events": [
            {"event": "turn", "model": "gpt-oss-120b-medium", "input_tokens": 11210, "tokens": 11331, "seconds": 8.2},
            {"event": "end", "status": "done" if code == 0 else "failed", "model_turns": 1, "tool_steps": 0}]}
    run.calls = calls
    return run


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.logs = []

    def pass_(self, runner):
        return bridge.one_pass(self.w.cfg, box=self.w.mac, runner=runner, log=self.logs.append)

    def replies(self):
        return [m for m in self.w.hub.unread("baseline")]

    def test_directive_runs_and_report_returns(self):
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        run = fake_runner()
        self.assertEqual(self.pass_(run), 1)
        self.assertIn("Summarize the project README.", run.calls[0]["task"])
        self.assertIn("D1: a three-line summary", run.calls[0]["task"])
        self.assertIn("read_file", run.calls[0]["conf"]["tools"])
        (msg,) = self.replies()
        head, _ = parse_text(msg.text)
        self.assertEqual(hard(validate(head)), [])
        self.assertEqual(head["handled"][0]["id"], "CMD-AG1")
        self.assertEqual(head["items"][0]["state"], "met")
        self.assertIn({"kind": "dependency", "what": "tool needed: run_tests - run the repo's unit tests"}, head["blockers"])
        self.assertEqual(next(r["value"] for r in head["results"] if r["name"] == "input_tokens"), 11210)
        self.assertEqual(self.pass_(fake_runner()), 0)  # read once, never run twice

    def test_each_directive_gets_its_own_state_dir(self):
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        run = fake_runner()
        self.pass_(run)
        self.assertEqual(run.calls[0]["conf"]["state_dir"], ".ga-supervise/CMD-AG1")

    def test_stuck_state_is_a_blocker(self):
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        self.pass_(fake_runner(out="[ga supervise] T1 is not finished — ga supervise --resume continues it first\n", code=1))
        head, _ = parse_text(self.replies()[0].text)
        self.assertIn("T1 unfinished", head["blockers"][0]["what"])

    def test_failed_run_is_unmet(self):
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        self.pass_(fake_runner(out="[ga supervise] agy refused 1 action(s) in T1.m1: command\n", code=1))
        head, _ = parse_text(self.replies()[0].text)
        self.assertEqual(head["items"][0]["state"], "unmet")
        self.assertEqual(head["blockers"][0]["kind"], "permission")

    def test_only_the_hub_may_direct(self):
        self.w.hub.send("AGY", form(DIRECTIVE), "stranger")
        run = fake_runner()
        self.pass_(run)
        self.assertEqual(run.calls, [])
        head, _ = parse_text(self.replies()[0].text)
        self.assertEqual(head["handled"][0]["status"], "declined")

    def test_secret_in_answer_is_withheld(self):
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        self.pass_(fake_runner(out="key " + "sk-" + "ant-api03-" + "A" * 40))
        (msg,) = self.replies()
        self.assertIn("withheld", msg.text)


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.d = Path(tempfile.mkdtemp())
        (self.d / "src").mkdir()
        (self.d / "src" / "a.py").write_text("x = 1\ny = 2\n")
        (self.d / ".env").write_text("SECRET=1\n")
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.old)

    def test_read_list_search(self):
        self.assertEqual(agy_tools.list_dir(), "src/")
        self.assertEqual(agy_tools.read_file("src/a.py", 2, 1), "2\ty = 2")
        self.assertEqual(agy_tools.search(r"y =", "."), "src/a.py:2: y = 2")

    def test_run_check_only_allowed_names(self):
        os.environ["AGY_BRIDGE_COMMANDS"] = json.dumps({"py": [sys.executable, "-c", "import os;print('ok', 'MY_API_KEY' in os.environ)"]})
        os.environ["MY_API_KEY"] = "fake"
        try:
            self.assertEqual(agy_tools.run_check("py").split(), ["exit", "0", "ok", "False"])
            with self.assertRaises(ValueError):
                agy_tools.run_check("rm")
        finally:
            del os.environ["AGY_BRIDGE_COMMANDS"], os.environ["MY_API_KEY"]

    def test_confined(self):
        for bad in ("../x", "/etc/passwd", ".env"):
            with self.assertRaises(ValueError):
                agy_tools.read_file(bad)


# ---- part 2: GA36 additions ---------------------------------------------------------------------------------------

def capacity_runner(results):
    """results: a list of 'capacity' | 'ok'; one per call."""
    calls = []

    def run(cfg, conf, task):
        kind = results[len(calls)]
        calls.append(kind)
        if kind == "capacity":
            return {"code": 1, "out": "[ga supervise] T1 failed: 0 model turn(s), 0 tool step(s), 1 failed — failed: "
                                      "T1.m1 (GeminiError)\n", "events": [
                {"event": "turn", "step": "T1.m1", "ok": False, "reason": "capacity", "model": "gpt-oss-120b-medium"},
                {"event": "end", "status": "failed", "model_turns": 0, "tool_steps": 0}]}
        return fake_runner(out="done.\n")(cfg, conf, task)
    run.calls = calls
    return run


class CapacityTest(BridgeTest):
    def pass_sleep(self, runner):
        self.slept = []
        return bridge.one_pass(self.w.cfg, box=self.w.mac, runner=runner, log=self.logs.append, sleep=self.slept.append)

    def test_capacity_is_retried_once_then_reported_as_capacity(self):
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        run = capacity_runner(["capacity", "capacity", "capacity"])
        self.pass_sleep(run)
        self.assertEqual(run.calls, ["capacity", "capacity"])  # one retry, not more
        self.assertEqual(self.slept, [30])                      # after a short backoff
        head, _ = parse_text(self.replies()[0].text)
        self.assertEqual(hard(validate(head)), [])
        self.assertEqual(head["items"][0]["state"], "unmet")
        b = head["blockers"][0]
        self.assertEqual(b["kind"], "dependency")
        self.assertTrue(b["what"].startswith("capacity:"), b)
        self.assertNotEqual(b["kind"], "budget")  # not a quota/budget blocker

    def test_capacity_then_success_is_met(self):
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        run = capacity_runner(["capacity", "ok"])
        self.pass_sleep(run)
        self.assertEqual(run.calls, ["capacity", "ok"])
        head, _ = parse_text(self.replies()[0].text)
        self.assertEqual(head["items"][0]["state"], "met")
        self.assertFalse(any(x["what"].startswith("capacity") for x in head.get("blockers", [])))

    def test_other_failures_are_not_retried(self):
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        run = fake_runner(out="[ga supervise] agy refused 1 action(s) in T1.m1: command\n", code=1)
        self.pass_sleep(run)
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(self.slept, [])

    def test_exit_3_or_the_capacity_text_is_capacity(self):
        self.assertTrue(bridge.is_capacity({"code": 3, "out": "", "events": []}))
        self.assertTrue(bridge.is_capacity({"code": 1, "out": "AGY_ERROR: 503 MODEL_CAPACITY_EXHAUSTED", "events": []}))
        self.assertFalse(bridge.is_capacity({"code": 1, "out": "AGY_ERROR: quota limit", "events": []}))

    def test_tools_come_from_ga(self):
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        run = fake_runner()
        self.pass_(run)
        tools = run.calls[0]["conf"]["tools"]
        self.assertEqual(sorted(tools), ["list_dir", "read_file", "run_check", "search"])
        self.assertTrue(all(t["python"].startswith("ga.bridge.tools:") for t in tools.values()))

    def test_a_lower_daily_cap_lowers_the_turn_cap(self):
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        run = fake_runner()
        bridge.one_pass(self.w.cfg, box=self.w.mac, runner=run, log=self.logs.append, max_steps=3)
        self.assertEqual(run.calls[0]["conf"]["max_model_steps"], 3)


class AgyAdapterCapacityTest(unittest.TestCase):
    def test_the_agy_adapter_names_capacity_not_quota(self):
        from ga.backends.builtin import AgvRunner
        from ga.adapters.gemini_cli import GeminiError, GeminiRateLimited
        d = Path(tempfile.mkdtemp())
        (d / "script.json").write_text(json.dumps([{"capacity": True}]))
        os.environ["FAKE_AGY_DIR"] = str(d)
        try:
            r = AgvRunner([sys.executable, str(TESTS / "fake_agy.py")], "gpt-oss-120b-medium")
            with self.assertRaises(GeminiError) as cm:
                r.run_turn("hello")
            self.assertEqual(cm.exception.reason, "transient:503")  # GA41 S3: the capacity class is transient
            self.assertNotIsInstance(cm.exception, GeminiRateLimited)
        finally:
            del os.environ["FAKE_AGY_DIR"]


class EndToEndTest(unittest.TestCase):
    """`ga bridge --once` with a real ga supervise process and a fake agy: directive in, report/2 out, 0 model calls."""

    def setUp(self):
        self.w = World()
        self.agy = Path(tempfile.mkdtemp())
        os.environ["FAKE_AGY_DIR"] = str(self.agy)
        os.environ["GA_ASK_HOME"] = str(self.w.tmp / "home")
        self.addCleanup(os.environ.pop, "FAKE_AGY_DIR", None)
        self.addCleanup(os.environ.pop, "GA_ASK_HOME", None)
        os.environ["GA_HOME"] = str(self.w.tmp / "ga-home")  # CMD-GA42: TOOL_NEEDED proposals land here, not in ~/.ga
        self.addCleanup(os.environ.pop, "GA_HOME", None)
        (self.w.work / "ga-supervise.json").write_text(json.dumps({
            "schema": "ga-supervise/1", "backend": "agv", "model": "gpt-oss-120b-medium",
            "options": {"cli": [sys.executable, str(TESTS / "fake_agy.py")]}, "tools": {},
            "transient_backoff_s": 0}))
        self.cfgfile = self.w.tmp / "agy-bridge.json"
        self.cfgfile.write_text(json.dumps({"mailbox_repo": str(self.w.tmp / "mac"), "workdir": str(self.w.work),
                                            "capacity_backoff_s": 0, "turn_timeout_s": 120}))

    def script(self, entries):
        (self.agy / "script.json").write_text(json.dumps(entries))

    def calls(self):
        f = self.agy / "calls.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def test_once_runs_the_directive_through_ga_supervise(self):
        self.script([{"plan": {"schema": "ga-plan/1", "steps": [{"id": "a", "tool": "read_file",
                                                                  "args": {"path": "README.md"}}],
                               "next": {"prompt": "summarize", "after": ["a"]}}},
                     {"plan": {"schema": "ga-plan/1", "steps": [], "next": None, "say": "hello world"}}])
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        self.assertEqual(bridge.main(["--config", str(self.cfgfile), "--once"]), 0)
        (msg,) = list(self.w.hub.unread("baseline"))
        head, body = parse_text(msg.text)
        self.assertEqual(hard(validate(head)), [])
        self.assertEqual(head["items"][0]["state"], "met")
        self.assertEqual(len([c for c in self.calls() if c["kind"] == "turn"]), 2)
        self.assertNotIn("--dangerously-skip-permissions", json.dumps(self.calls()))
        self.assertFalse((self.w.tmp / "home" / "bridge.pid").exists())  # removed when it ends

    def test_capacity_twice_through_the_real_loop(self):
        self.script([{"capacity": True}, {"capacity": True}])
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        bridge.main(["once", "--config", str(self.cfgfile)])
        head, _ = parse_text(list(self.w.hub.unread("baseline"))[0].text)
        # one retry (/usage probes are free): ga supervise retries the transient turn (GA41), the bridge not again
        self.assertEqual(len([c for c in self.calls() if c["kind"] == "turn"]), 2)
        self.assertTrue(head["blockers"][0]["what"].startswith("capacity:"))


if __name__ == "__main__":
    unittest.main()
