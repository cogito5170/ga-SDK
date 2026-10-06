"""VM-BRIDGE-ACT-1: ``ga bridge`` code work. A directive/2 whose mail carries a ```ga-act block runs ``ga act`` in a
worktree, pushes agv/<id>-r<rev> and answers report/2; a directive without the block keeps the ga supervise path; a hub
directive that cannot be run is answered with an unmet report/2 instead of a log line only. 0 model calls: ``ga act``
is replaced by a runner that edits the worktree.
"""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ga.bridge as bridge  # noqa: E402
from ga.bridge import act as ACT  # noqa: E402
from ga.forms import hard, parse_text, validate  # noqa: E402
from ga.mailbox import Mailbox  # noqa: E402

DIRECTIVE = {"schema": "directive/2", "id": "CMD-VA1", "rev": 1, "to": "AGY", "after": [],
             "goal": "Make greet() say hello.", "why": "bridge act test",
             "scope": [{"id": "S1", "text": "only pkg/greet.py"}],
             "done_when": [{"id": "D1", "text": "ga act ends done: the test passes"}], "budget": {"claude_p_runs": 0}}
SPEC = {"item": {"id": "CMD-VA1", "goal": "greet() returns 'hello'", "files": ["pkg/greet.py"], "done_when": "test"},
        "tests": {"tests/test_greet.py": "from pkg.greet import greet\n\ndef test_greet():\n    assert greet() == 'hello'\n"},
        "commands": {"commands": {"test": ["{python}", "-m", "pytest", "-q", "tests/test_greet.py"]}, "timeout_s": 60},
        "base": "main"}


def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


def form(head, spec=None):
    text = "```ga\n" + json.dumps(head, separators=(",", ":")) + "\n```\n"
    if spec is not None:
        text += "\n```ga-act\n" + json.dumps(spec) + "\n```\n"
    return text


class World:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp())
        git("init", "-q", "--bare", str(self.tmp / "origin.git"))  # the mailbox remote
        for who in ("hub", "vm"):
            git("clone", "-q", str(self.tmp / "origin.git"), str(self.tmp / who))
        # the code repo the bridge works on (act.repo), with its own remote
        git("init", "-q", "--bare", "-b", "main", str(self.tmp / "code.git"))
        seed = self.tmp / "seed"
        git("clone", "-q", str(self.tmp / "code.git"), str(seed))
        (seed / "pkg").mkdir()
        (seed / "pkg" / "__init__.py").write_text("")
        (seed / "pkg" / "greet.py").write_text("def greet():\n    return 'bye'\n")
        git("-C", str(seed), "checkout", "-q", "-b", "main")
        git("-C", str(seed), "add", "-A")
        git("-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
        git("-C", str(seed), "push", "-q", "origin", "main")
        self.code = self.tmp / "token"
        git("clone", "-q", str(self.tmp / "code.git"), str(self.code))
        self.work = self.tmp / "work"
        self.work.mkdir()
        (self.work / "ga-supervise.json").write_text(json.dumps({"schema": "ga-supervise/1", "backend": "agv",
                                                                 "model": "gpt-oss-120b-medium", "tools": {}}))
        self.cfg = {**bridge.DEFAULTS, "mailbox_repo": str(self.tmp / "vm"), "workdir": str(self.work), "pull": False,
                    "act": {"repo": str(self.code), "repo_name": "cogito5170/Token", "backend": "agv",
                            "model": "gpt-oss-120b-medium", "options": {"agent": "ga-act"}, "max_turns": 10,
                            "timeout_s": 3600}}
        self.hub = Mailbox(self.tmp / "hub", sleep=lambda s: None)
        self.vm = Mailbox(self.tmp / "vm", sleep=lambda s: None)

    def remote_branches(self):
        return git("--git-dir", str(self.tmp / "code.git"), "branch", "--format=%(refname:short)").split()


def fake_act(code=0, status="done", text="def greet():\n    return 'hello'\n"):
    calls = []

    def run(cfg, spec, wt, checkout, model):
        calls.append({"wt": wt, "model": model, "test_there": (wt / "tests" / "test_greet.py").exists()})
        if text is not None:
            (wt / "pkg" / "greet.py").write_text(text)
        return {"code": code, "out": "ga act log\n", "result": {
            "status": status, "reason": "done_when passes" if status == "done" else "test fails", "turns": 2,
            "changed": ["pkg/greet.py"] if text is not None else [], "tokens": {"input": 3279, "total": 3448},
            "rungs": [{"model": model, "status": status}]}}
    run.calls = calls
    return run


def supervise_must_not_run(cfg, conf, task):
    raise AssertionError("ga supervise ran for a code-work directive")


class ActPathTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.logs = []
        os.environ["GA_HOME"] = str(self.w.tmp / "ga-home")
        self.addCleanup(os.environ.pop, "GA_HOME", None)

    def pass_(self, act_run=None, runner=supervise_must_not_run):
        handler = (lambda c, h, s: ACT.handle(c, h, s, runner=act_run)) if act_run else None
        return bridge.one_pass(self.w.cfg, box=self.w.vm, runner=runner, log=self.logs.append, act_handler=handler)

    def reply(self):
        (msg,) = self.w.hub.unread("baseline")
        head, body = parse_text(msg.text)
        self.assertEqual(hard(validate(head)), [])
        return head, body

    def test_code_work_runs_ga_act_pushes_and_reports(self):
        self.w.hub.send("AGY", form(DIRECTIVE, SPEC), "baseline")
        run = fake_act()
        self.assertEqual(self.pass_(run), 1)
        self.assertEqual(len(run.calls), 1)
        self.assertTrue(run.calls[0]["test_there"])  # baseline's test is in the worktree before ga act
        self.assertEqual(run.calls[0]["model"], "gpt-oss-120b-medium")
        self.assertFalse(run.calls[0]["wt"].exists())  # the worktree is removed afterwards
        head, body = self.reply()
        self.assertEqual(head["items"][0]["state"], "met")
        (commit,) = head["commits"]
        self.assertEqual(commit["branch"], "agv/CMD-VA1-r1")
        self.assertEqual(commit["repo"], "cogito5170/Token")
        self.assertIn("agv/CMD-VA1-r1", self.w.remote_branches())
        self.assertEqual(git("--git-dir", str(self.w.tmp / "code.git"), "rev-parse", "agv/CMD-VA1-r1").strip(), commit["sha"])
        self.assertIn("return 'hello'", body)  # the patch rides in the report
        self.assertEqual(next(r["value"] for r in head["results"] if r["name"] == "model"), "gpt-oss-120b-medium")
        self.assertEqual(git("-C", str(self.w.code), "status", "--porcelain"), "")  # the user's checkout is untouched

    def test_unmet_run_reports_unmet_with_the_reason(self):
        self.w.hub.send("AGY", form(DIRECTIVE, SPEC), "baseline")
        self.pass_(fake_act(code=1, status="blocked", text=None))
        head, _ = self.reply()
        self.assertEqual(head["items"][0]["state"], "unmet")
        self.assertNotIn("commits", head)
        self.assertTrue(any("ga act: test fails" in b["what"] for b in head["blockers"]))
        self.assertNotIn("agv/CMD-VA1-r1", self.w.remote_branches())

    def test_no_block_keeps_the_supervise_path(self):
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        calls = []

        def supervise(cfg, conf, task):
            calls.append(task)
            return {"code": 0, "out": "OK\n", "events": [{"event": "turn", "input_tokens": 1, "tokens": 2, "seconds": 1},
                                                         {"event": "end", "status": "done", "model_turns": 1}]}
        run = fake_act()
        self.pass_(run, runner=supervise)
        self.assertEqual(len(calls), 1)
        self.assertEqual(run.calls, [])
        self.assertEqual(self.reply()[0]["items"][0]["state"], "met")

    def test_a_bad_block_is_answered_not_silent(self):
        self.w.hub.send("AGY", form(DIRECTIVE, {**SPEC, "tests": {"../escape.py": "x"}}), "baseline")
        run = fake_act()
        self.pass_(run)
        self.assertEqual(run.calls, [])
        head, _ = self.reply()
        self.assertEqual(head["items"][0]["state"], "unmet")
        self.assertIn("bad test path", head["blockers"][0]["what"])

    def test_a_supervise_config_error_is_answered_not_silent(self):
        # 10-07 CMD-PING3: a missing ga-supervise.json left only a log line; now the hub gets an unmet report/2
        (self.w.work / "ga-supervise.json").unlink()
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")
        self.pass_(runner=lambda c, conf, t: {"code": 0, "out": "", "events": []})
        head, _ = self.reply()
        self.assertEqual(head["handled"][0]["id"], "CMD-VA1")
        self.assertEqual(head["items"][0]["state"], "unmet")
        self.assertIn("FileNotFoundError", head["blockers"][0]["what"])

    def test_a_secret_looking_patch_is_withheld_and_not_pushed(self):
        self.w.hub.send("AGY", form(DIRECTIVE, SPEC), "baseline")
        self.pass_(fake_act(text="KEY = '" + "sk-" + "ant-api03-" + "A" * 40 + "'\n"))
        (msg,) = self.w.hub.unread("baseline")
        self.assertNotIn("sk-ant-api03", msg.text)
        self.assertNotIn("agv/CMD-VA1-r1", self.w.remote_branches())

    def test_item_repo_must_be_configured(self):
        self.w.hub.send("AGY", form(DIRECTIVE, {**SPEC, "repo": "ga-sdk"}), "baseline")
        run = fake_act()
        self.pass_(run)
        self.assertEqual(run.calls, [])
        self.assertIn("unknown item repo", self.reply()[0]["blockers"][0]["what"])

    def test_the_block_is_strict(self):
        self.assertIsNone(ACT.item_spec("no block here"))
        with self.assertRaises(ValueError):
            ACT.item_spec("```ga-act\n{\"item\": {}, \"steps\": []}\n```")
        with self.assertRaises(ValueError):
            ACT.item_spec("```ga-act\nnot json\n```")

    def test_ga_act_argv(self):
        argv = ACT.act_argv(self.w.cfg, SPEC, Path("/wt"), Path("/tmp/x"), "gemini-3.7-flash-low")
        self.assertEqual(argv[1:4], ["-m", "ga", "act"])
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.7-flash-low")
        self.assertEqual(argv[argv.index("--backend") + 1], "agv")
        self.assertEqual(json.loads(argv[argv.index("--options") + 1]), {"agent": "ga-act"})


if __name__ == "__main__":
    unittest.main()
