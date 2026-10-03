"""CMD-GA6: the Agent SDK Runner against a fake SDK (the real package is optional), and turn diagnostic labels."""
import json
import os
import subprocess
import unittest
from pathlib import Path

from ga.adapters import agent_sdk, sandbox
from ga.adapters.agent_sdk import AgentSDKRunner, fake_sdk
from ga.adapters.base import TurnRequest
from ga.adapters.git import git
from ga.adapters.mailbox import FileMailbox
from ga.forms import dump_text

from test_headless import Stub
from world import GIT_ENV, World, directive, proposal

OK = {"session_id": "sid-1", "total_cost_usd": 0.021, "is_error": False, "subtype": "success", "num_turns": 3}


def runner(stub, sdk, **kw):
    kw.setdefault("timeout", 20)
    return AgentSDKRunner(stub.dir / "home", executable=stub.exe, model="haiku", sdk=sdk,
                          extra_env={"GA_STUB_DIR": str(stub.dir), **GIT_ENV, "GIT_CONFIG_GLOBAL": os.environ.get("GIT_CONFIG_GLOBAL", "/dev/null")}, **kw)


class AgentSDKPathsTest(unittest.TestCase):
    def setUp(self):
        self.stub = Stub([{"do": "ok"}])
        self.addCleanup(self.stub.close)
        self.work = self.stub.dir / "work"
        self.work.mkdir()

    def turn(self, plan, req_kw=None, **kw):
        rec = []
        r = runner(self.stub, fake_sdk(plan, rec), **kw)
        return r.run_turn(TurnRequest("A", "지시", self.work, **(req_kw or {}))), rec, r

    def test_success_and_options(self):
        res, rec, r = self.turn([OK], req_kw={"resume_id": "prev", "budget": {"max_budget_usd": 0.4}})
        self.assertEqual((res.ended, res.error, res.cost, res.session_id), (True, "", 0.021, "sid-1"))
        o = rec[0]["options"]
        self.assertEqual(rec[0]["prompt"], "지시")
        self.assertEqual((o.cwd, o.model, o.permission_mode, o.resume, o.max_budget_usd), (str(self.work), "haiku", "acceptEdits", "prev", 0.4))
        self.assertEqual(o.setting_sources, [])
        self.assertIn("Bash(git push --no-verify:*)", o.disallowed_tools)
        self.assertTrue(Path(o.settings).exists() and "bash_guard.py" in Path(o.settings).read_text())
        self.assertEqual(Path(o.cli_path), r.home / "_wrappers" / "A.sh")

    def test_error_paths(self):
        class ProcessError(Exception):
            pass
        cases = [
            ([ProcessError("x")], {}, "sdk_error:ProcessError"),
            ([("sleep", 5)], {"timeout": 0.3}, "timeout"),
            ([dict(OK, is_error=True, subtype="error_max_budget_usd")], {}, "is_error:error_max_budget_usd"),
            ([lambda o: None], {}, ""),
        ]
        for plan, kw, want in cases:
            with self.subTest(want=want):
                res, _, _ = self.turn(plan, **kw)
                self.assertEqual(res.error, want)
        # a fake whose query yields no ResultMessage at all
        sdk = fake_sdk([])
        async def empty(*, prompt, options):
            if False:
                yield None
        sdk.query = empty
        self.assertEqual(runner(self.stub, sdk).run_turn(TurnRequest("A", "x", self.work)).error, "no_result")

    def test_sdk_not_installed(self):
        real = agent_sdk.load_sdk
        agent_sdk.load_sdk = lambda: None
        try:
            r = AgentSDKRunner(self.stub.dir / "home")
            self.assertEqual(r.run_turn(TurnRequest("A", "x", self.work)).error, "sdk_not_installed")
        finally:
            agent_sdk.load_sdk = real

    def test_sandbox_require_without_sandbox(self):
        real = sandbox.available
        agent_sdk.sandbox.available = lambda: False
        try:
            res, rec, _ = self.turn([OK], req_kw={"permissions": {"protect": [str(self.work)], "writable": []}}, sandbox="require")
        finally:
            agent_sdk.sandbox.available = real
        self.assertEqual(res.error, "sandbox_unavailable")
        self.assertEqual(rec, [])

    def test_wrapper_runs_the_cli_with_a_clean_env(self):
        os.environ["CLAUDE_CODE_GA_TEST_MARKER"] = "1"
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_GA_TEST_MARKER")
        _, rec, r = self.turn([OK])
        cli = rec[0]["options"].cli_path
        text = Path(cli).read_text()
        self.assertIn("env -i", text)
        self.assertNotIn("CLAUDE_CODE_GA_TEST_MARKER", text)
        subprocess.run([cli, "-p"], input="x", capture_output=True, text=True, cwd=self.work)
        call = self.stub.calls()[-1]
        self.assertFalse([k for k in call["env"] if k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE"])
        self.assertEqual(Path(call["HOME"]), r.home / "A")

    @unittest.skipUnless(sandbox.available(), "unprivileged user namespaces are not available here")
    def test_sandboxed_wrapper_protects_its_own_directory(self):
        res, rec, r = self.turn([OK], req_kw={"permissions": {"protect": [str(self.work)], "writable": []}})
        self.assertTrue(res.sandboxed)
        text = Path(rec[0]["options"].cli_path).read_text()
        self.assertIn("unshare", text)
        import shlex
        q = shlex.quote(str(r.home.resolve()))
        self.assertIn(shlex.quote(f"mount -o remount,bind,ro {q}")[1:-1].replace("'\"'\"'", "'"), text.replace("'\"'\"'", "'"))  # the wrappers' directory is read-only to the turn
        self.assertIn("--sandboxed", Path(rec[0]["options"].settings).read_text())


def work_step(w, session, repo, did, report=True):
    def step(options):
        cwd = Path(options.cwd)
        (cwd / repo / f"{did}.txt").write_text("x\n", encoding="utf-8")
        git(cwd / repo, "add", "-A")
        git(cwd / repo, "commit", "--quiet", "-m", did)
        sha = git(cwd / repo, "rev-parse", "HEAD")
        if report:
            head = {"schema": "report/1", "from": session, "handled": [{"id": did, "rev_seen": 1, "status": "done"}],
                    "commits": [{"repo": repo, "branch": w.cfg.sessions[session].branch_for(repo), "sha": sha}]}
            FileMailbox(w.ga / "mailbox").post(session, session, dump_text(head, "## Result\n됐다\n"))
        return {"session_id": f"sid-{session}", "total_cost_usd": 0.013, "is_error": False, "subtype": "success", "num_turns": 2}
    return step


class AgentSDKInHubTest(unittest.TestCase):
    def world(self, plan_fn, budget=None):
        stub = Stub([])
        self.addCleanup(stub.close)
        rec = []
        w = World(budget=budget)
        self.addCleanup(w.close)
        w.hub.runner = w.runner = runner(stub, fake_sdk(plan_fn(w), rec), sandbox="off")
        return w, rec

    def test_round_resume_cost_and_labels(self):
        w, rec = self.world(lambda w: [work_step(w, "A", "alpha", "CMD-A1"), work_step(w, "B", "beta", "CMD-B1"),
                                       work_step(w, "A", "alpha", "CMD-A2")])
        w.hub.send(directive("CMD-A1", "A"))
        w.hub.send(directive("CMD-B1", "B"))
        w.proposals.append(proposal("success", "continue", "A 다음", directive_=directive("CMD-A2", "A")))
        r1 = w.hub.tick()
        self.assertEqual(sorted(r1.integrated), ["alpha", "beta"])
        st = w.hub.load_state()
        self.assertEqual(rec[2]["options"].resume, "sid-A")  # A's second turn continues A's session
        self.assertFalse(hasattr(rec[1]["options"], "resume"))
        self.assertAlmostEqual(st["spent"]["cost"], 0.039)
        for t in st["turns"]:
            self.assertEqual(t["runner"], "agent_sdk")
            self.assertEqual(t["diag"], {"committed": True, "posts": 1, "reports_ok": 1, "claims_known": True})

    def test_commit_without_report_is_labelled(self):
        """The GA5 rev 2 case: the turn committed but never reported. The label says so at once."""
        w, _ = self.world(lambda w: [work_step(w, "B", "beta", "CMD-B1", report=False)])
        w.hub.send(directive("CMD-B1", "B"))
        self.assertEqual(w.hub.load_state()["turns"][0]["diag"], {"committed": True, "posts": 0, "reports_ok": 0, "claims_known": None})
        self.assertTrue(w.hub.tick().quiet)  # nothing claimed: still not new for the hub (R1b), but the label is kept

    def test_budget_gate_before_running(self):
        w, rec = self.world(lambda w: [work_step(w, "A", "alpha", "CMD-A1")], budget={"runs": 1})
        w.hub.send(directive("CMD-A1", "A"))
        w.proposals.append(proposal("success", "continue", "다음", directive_=directive("CMD-A2", "A")))
        res = w.hub.tick()
        self.assertEqual([q["gate"] for q in res.gates], [6])
        self.assertEqual(len(rec), 1)


class ManualRunnerLabelsTest(unittest.TestCase):
    def test_labels_arrive_with_the_report(self):
        w = World()
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        self.assertNotIn("diag", w.hub.load_state()["turns"][0])  # the manual runner cannot see the end of a turn
        w.paste("A")
        sha = w.work("A", "alpha", {"m.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        w.hub.tick()
        self.assertEqual(w.hub.load_state()["turns"][0]["diag"], {"committed": True, "posts": 1, "reports_ok": 1, "claims_known": True})


if __name__ == "__main__":
    unittest.main()
