"""CMD-GA2: the local headless Runner, tested against a stub ``claude`` before any real run.

Paths: success · non-zero exit · timeout · broken JSON · is_error · session id resumed · cost recorded ·
a turn over budget stops before it runs and goes to gate 6 · clean environment · narrowed permissions ·
a full hub round with two sessions driven by the runner.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from ga.adapters.base import TurnRequest
from ga.adapters.git import git
from ga.adapters.headless import HeadlessRunner

from world import GIT_ENV, World, directive, proposal

HERE = Path(__file__).resolve().parent
GA_ROOT = str(HERE.parent)


class Stub:
    def __init__(self, plan):
        self.tmp = tempfile.TemporaryDirectory(prefix="ga-stub-")
        self.dir = Path(self.tmp.name)
        (self.dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        exe = self.dir / "claude"
        exe.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{HERE / 'stub_claude.py'}' \"$@\"\n", encoding="utf-8")
        exe.chmod(0o755)
        self.exe = str(exe)

    def runner(self, **kw):
        env = {"GA_STUB_DIR": str(self.dir), **GIT_ENV, "GIT_CONFIG_GLOBAL": os.environ.get("GIT_CONFIG_GLOBAL", "/dev/null")}
        kw.setdefault("timeout", 20)
        return HeadlessRunner(self.dir / "home", executable=self.exe, model="haiku", extra_env=env, **kw)

    def calls(self):
        p = self.dir / "calls.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def close(self):
        self.tmp.cleanup()


class RunnerPathsTest(unittest.TestCase):
    def run_one(self, plan, req_kw=None, **kw):
        stub = Stub(plan)
        self.addCleanup(stub.close)
        work = stub.dir / "work"
        work.mkdir()
        res = stub.runner(**kw).run_turn(TurnRequest("A", "지시 본문", work, **(req_kw or {})))
        return res, stub

    def test_success(self):
        res, stub = self.run_one([{"do": "ok", "cost": 0.0123}])
        self.assertEqual((res.ended, res.error, res.cost), (True, "", 0.0123))
        self.assertTrue(res.session_id)
        self.assertIsNotNone(res.seconds)
        call = stub.calls()[0]
        self.assertEqual(call["stdin_len"], len("지시 본문"))  # prompt via stdin, not argv
        self.assertNotIn("지시 본문", call["argv"])
        self.assertEqual(Path(call["cwd"]).resolve(), (stub.dir / "work").resolve())

    def test_nonzero_exit(self):
        res, _ = self.run_one([{"do": "exit", "code": 3}])
        self.assertEqual((res.ended, res.error, res.cost), (True, "exit 3", None))

    def test_timeout(self):
        res, _ = self.run_one([{"do": "sleep", "seconds": 30}], timeout=1)
        self.assertEqual((res.ended, res.error), (True, "timeout"))
        self.assertLess(res.seconds, 10)

    def test_broken_json(self):
        res, _ = self.run_one([{"do": "badjson"}])
        self.assertEqual(res.error, "bad_json")
        self.assertIsNone(res.session_id)

    def test_is_error(self):
        res, _ = self.run_one([{"do": "is_error"}])
        self.assertEqual(res.error, "is_error:error_max_turns")
        self.assertEqual(res.cost, 0.02)  # a failed turn still costs

    def test_missing_executable(self):
        stub = Stub([])
        self.addCleanup(stub.close)
        r = HeadlessRunner(stub.dir / "home", executable=str(stub.dir / "nope"))
        self.assertEqual(r.run_turn(TurnRequest("A", "x", stub.dir)).error, "not_started")

    def test_clean_env_and_temp_home(self):
        os.environ["CLAUDE_CODE_GA_TEST_MARKER"] = "1"
        os.environ["ANTHROPIC_API_KEY_GA_TEST"] = "x"
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_GA_TEST_MARKER")
        self.addCleanup(os.environ.pop, "ANTHROPIC_API_KEY_GA_TEST")
        _, stub = self.run_one([{"do": "ok"}])
        call = stub.calls()[0]
        self.assertFalse([k for k in call["env"] if k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE"])
        self.assertNotIn("ANTHROPIC_API_KEY_GA_TEST", call["env"])
        self.assertEqual(Path(call["HOME"]), stub.dir / "home" / "A")  # per session
        self.assertEqual(Path(call["CLAUDE_CONFIG_DIR"]), stub.dir / "home" / "A" / ".claude")
        self.assertNotEqual(call["HOME"], os.path.expanduser("~"))

    def test_narrowed_permissions_and_cap(self):
        _, stub = self.run_one([{"do": "ok"}], req_kw={"resume_id": "abc", "budget": {"max_budget_usd": 0.5}})
        argv = stub.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("Bash(git push origin:*)", argv[argv.index("--allowedTools") + 1])
        self.assertIn("Bash(git push --no-verify:*)", argv[argv.index("--disallowedTools") + 1])
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.5")
        self.assertEqual(argv[argv.index("--resume") + 1], "abc")
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")


def work(world, session, repo, directive_id, file, cost):
    return {"do": "work", "cost": cost, "repo": repo, "file": file, "text": f"{directive_id}\n", "ga_root": GA_ROOT,
            "mailbox": str(world.ga / "mailbox"),
            "report": {"session": session, "directive": directive_id, "branch": world.cfg.sessions[session].branch_for(repo)}}


class HubWithHeadlessTest(unittest.TestCase):
    def make(self, plan_fn, budget=None):
        stub = Stub([])
        self.addCleanup(stub.close)
        w = World(runner=stub.runner(), budget=budget)
        self.addCleanup(w.close)
        (stub.dir / "plan.json").write_text(json.dumps(plan_fn(w)), encoding="utf-8")
        return w, stub

    def test_round_two_sessions_resume_and_cost(self):
        w, stub = self.make(lambda w: [
            work(w, "A", "alpha", "CMD-A1", "a1.txt", 0.011),
            work(w, "B", "beta", "CMD-B1", "b1.txt", 0.022),
            work(w, "A", "alpha", "CMD-A2", "a2.txt", 0.033),
        ])
        w.hub.send(directive("CMD-A1", "A"))
        w.hub.send(directive("CMD-B1", "B"))
        w.proposals.append(proposal("success", "continue", "A 다음", directive_=directive("CMD-A2", "A")))
        r1 = w.hub.tick()
        self.assertEqual(sorted(r1.integrated), ["alpha", "beta"])
        self.assertEqual(r1.sent, ["CMD-A2"])
        r2 = w.hub.tick()
        self.assertEqual(list(r2.integrated), ["alpha"])
        calls = stub.calls()
        self.assertEqual(len(calls), 3)
        st = w.hub.load_state()
        turns = st["turns"]
        # A's second turn resumed A's first session id; B's turn did not
        self.assertNotIn("--resume", calls[0]["argv"])
        self.assertNotIn("--resume", calls[1]["argv"])
        self.assertEqual(calls[2]["argv"][calls[2]["argv"].index("--resume") + 1], turns[0]["session_id"])
        self.assertEqual(turns[2]["resumed"], turns[0]["session_id"])
        self.assertEqual([t["cost"] for t in turns], [0.011, 0.022, 0.033])
        self.assertAlmostEqual(st["spent"]["cost"], 0.066)
        self.assertEqual(st["spent"]["runs"], 3)
        self.assertNotIn("cost_unknown_runs", st["spent"])
        self.assertTrue(all(t["seconds"] is not None and t["error"] == "" for t in turns))
        self.assertTrue(all(Path(c["cwd"]).name in ("A", "B") for c in calls))  # the session's worktree directory

    def test_turn_over_run_budget_stops_before_running(self):
        w, stub = self.make(lambda w: [work(w, "A", "alpha", "CMD-A1", "a1.txt", 0.01)], budget={"runs": 1})
        w.hub.send(directive("CMD-A1", "A"))
        w.proposals.append(proposal("success", "continue", "다음", directive_=directive("CMD-A2", "A")))
        res = w.hub.tick()
        self.assertEqual([q["gate"] for q in res.gates], [6])
        self.assertEqual(res.sent, [])
        self.assertEqual(len(stub.calls()), 1)

    def test_turn_over_cost_budget_stops_before_running(self):
        w, stub = self.make(lambda w: [work(w, "A", "alpha", "CMD-A1", "a1.txt", 0.07)], budget={"runs": 10, "cost": 0.05})
        w.hub.send(directive("CMD-A1", "A"))
        w.proposals.append(proposal("success", "continue", "다음", directive_=directive("CMD-A2", "A")))
        res = w.hub.tick()
        self.assertEqual([q["gate"] for q in res.gates], [6])
        self.assertEqual(len(stub.calls()), 1)

    def test_no_verify_push_in_a_turn_is_refused_by_pre_receive(self):
        stub = Stub([])
        self.addCleanup(stub.close)
        w = World(remote=True, runner=stub.runner(), isolation="worktree")
        self.addCleanup(w.close)
        (stub.dir / "plan.json").write_text(json.dumps([
            {"do": "cmd", "argv": ["git", "-C", "beta", "push", "--no-verify", "origin", "HEAD:refs/heads/sess-a"]},
        ]), encoding="utf-8")
        w.hub.send(directive("CMD-B1", "B"))
        out = json.loads((stub.dir / "cmd-0.json").read_text(encoding="utf-8"))
        self.assertNotEqual(out["code"], 0)
        self.assertIn("ga R3: session B may push only to refs/heads/sess-b", out["stderr"])
        self.assertFalse(git(w.tmp / "remotes" / "beta.git", "rev-parse", "--verify", "--quiet", "refs/heads/sess-a", check=False))

    def test_failed_turn_is_recorded_and_directive_stays_open(self):
        w, stub = self.make(lambda w: [{"do": "exit", "code": 2}])
        post, findings, _ = w.hub.send(directive("CMD-A1", "A"))
        self.assertIsNotNone(post)
        self.assertIn("runner headless: exit 2", [p.message for p in findings])
        st = w.hub.load_state()
        self.assertEqual(st["turns"][0]["error"], "exit 2")
        self.assertEqual(st["directives"]["CMD-A1"]["status"], "open")
        self.assertEqual(st["spent"]["cost_unknown_runs"], 1)


if __name__ == "__main__":
    unittest.main()
