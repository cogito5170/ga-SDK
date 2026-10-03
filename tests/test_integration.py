"""G3: integration engine on local git repositories with a remote (fetch → new session commits → ff-only →
Bundle (a)(b) → push), plus --dry-run. Each situation gives its expected verdict.

Situations: ff · not ff · ownership violation · secret · pin conflict · skipped tests.
"""
import unittest

from ga.adapters.git import git
from ga.adapters.venv import file_url, parse_counts

from world import TEST_FAIL, TEST_SKIP, World, directive, proposal


def ok_judge(ctx):
    return proposal("success", "wait", "판정")


class IntegrationTest(unittest.TestCase):
    def world(self, **kw):
        w = World(judge_fn=ok_judge, remote=True, **kw)
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        return w

    def origin_integ(self, w, repo):
        return git(w.tmp / "remotes" / f"{repo}.git", "rev-parse", "integ")

    def test_ff_integrates_and_pushes(self):
        w = self.world()
        sha = w.work("A", "alpha", {"alphapkg/__init__.py": "V = 3\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        res = w.hub.tick()
        self.assertEqual(res.integrated, {"alpha": sha})
        self.assertEqual(self.origin_integ(w, "alpha"), sha)
        self.assertEqual(res.verdict["class"], "success")

    def test_not_ff_is_not_integrated_and_the_hub_sends_rev_plus_one(self):
        # METHOD rev 9 (BD-146, GA10 F3): a non-fast-forward is routine, not a gate: B's share is partial
        # (requirement) and the hub itself sends CMD-B1 rev 2 — merge the integration branch, report again
        w = World(judge_fn=ok_judge, remote=True, shared_alpha=True)
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        w.hub.send(directive("CMD-B1", "B"))
        w.paste("A"), w.paste("B")
        a = w.work("A", "alpha", {"one.txt": "a\n"})
        b = w.work("B", "alpha", {"B_OWNS.txt": "b\n"})  # both branched from the same integration head
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", a)])
        w.report("B", [("CMD-B1", 1, "done")], [("alpha", b)])
        res = w.hub.tick()
        self.assertEqual(res.integrated, {"alpha": a})
        self.assertEqual((res.verdict["class"], res.verdict["cause"]), ("partial", "requirement"))
        self.assertIn("R4", [p.rule for p in res.findings])
        self.assertEqual(res.gates, [])
        self.assertEqual(res.sent, ["CMD-B1"])
        self.assertTrue(any("rev+1" in n for n in res.verdict["evidence"]["notes"]))
        st = w.hub.load_state()
        self.assertEqual((st["directives"]["CMD-B1"]["rev"], st["directives"]["CMD-B1"]["status"]), (2, "open"))
        prompt = w.paste("B")
        self.assertIn("merge --no-edit", prompt)
        self.assertIn('"rev_seen": 2', prompt)  # the report template carries the new rev
        self.assertEqual(self.origin_integ(w, "alpha"), a)
        # B merges the integration branch first; then it fast-forwards
        wt = w.vcs.session_worktree("B", "alpha")
        git(wt, "fetch", "--quiet", "origin", "integ")
        git(wt, "merge", "--quiet", "--no-edit", "FETCH_HEAD")
        w.report("B", [("CMD-B1", 2, "done")], [("alpha", git(wt, "rev-parse", "HEAD"))])
        res2 = w.hub.tick()
        self.assertEqual(list(res2.integrated), ["alpha"])
        self.assertEqual(res2.verdict["class"], "success")
        self.assertEqual(res2.sent, [])

    def test_ownership_violation_is_blocked(self):
        w = self.world()
        sha = w.work("A", "alpha", {"B_OWNS.txt": "mine now\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        res = w.hub.tick()
        self.assertEqual(res.integrated, {})
        self.assertEqual((res.verdict["class"], res.verdict["cause"]), ("blocked", "requirement"))
        self.assertEqual([p.rule for p in res.findings if p.strength == "hard"], ["R2"])

    def test_secret_is_blocked_and_asks(self):
        w = self.world()
        sha = w.work("A", "alpha", {"cfg.txt": "key=" + "".join(["AK", "IA", "Z" * 16]) + "\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        res = w.hub.tick()
        self.assertEqual(res.integrated, {})
        self.assertEqual(res.verdict["class"], "blocked")
        self.assertEqual([q["gate"] for q in res.gates], [6])
        self.assertNotEqual(self.origin_integ(w, "alpha"), sha)

    def test_pin_conflict_fails_install_mode(self):
        holder = {}

        def beta_deps(w):
            alpha = w.tmp / "remotes" / "alpha.git"
            holder["pin"] = git(w.repos["alpha"], "rev-parse", "integ")
            return [f"alphapkg @ git+{file_url(alpha)}@{holder['pin']}"]

        w = World(judge_fn=ok_judge, remote=True, modes=("path", "install"), beta_deps=beta_deps)
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        sha = w.work("A", "alpha", {"alphapkg/__init__.py": "V = 9\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        res = w.hub.tick()
        self.assertEqual(res.integrated, {"alpha": sha})
        self.assertEqual((res.verdict["class"], res.verdict["cause"]), ("failure", "dependency"))
        self.assertIn("bundle (install): pin_conflict", res.verdict["evidence"]["notes"])
        # path mode alone stayed green: this is the F3 case the install mode exists for
        self.assertEqual(res.verdict["evidence"]["tests"]["alpha"]["failed"], 0)

    def test_matching_pins_install_cleanly(self):
        w = World(judge_fn=ok_judge, remote=True, modes=("install",))
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        sha = w.work("A", "alpha", {"alphapkg/__init__.py": "V = 5\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        res = w.hub.tick()
        self.assertEqual(res.verdict["class"], "success", res.verdict["evidence"])
        self.assertEqual(res.verdict["evidence"]["tests"]["alpha"], {"passed": 1, "failed": 0, "skipped": 0})

    def test_skipped_tests_surface_in_the_verdict(self):
        w = self.world()
        sha = w.work("A", "alpha", {"tests/test_x.py": TEST_SKIP})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)], tests={"passed": 2, "failed": 0, "skipped": 0})
        res = w.hub.tick()
        self.assertEqual((res.verdict["class"], res.verdict["subclass"]), ("partial", "skipped"))
        self.assertEqual(res.verdict["evidence"]["tests"]["alpha"], {"passed": 1, "failed": 0, "skipped": 1})
        # R11: the report claimed 2 passed / 0 skipped; the reproduced numbers are recorded
        self.assertTrue(res.verdict["claims_vs_evidence"])

    def test_failing_tests_fail(self):
        w = self.world()
        sha = w.work("A", "alpha", {"tests/test_x.py": TEST_FAIL})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        res = w.hub.tick()
        self.assertEqual((res.verdict["class"], res.verdict["cause"]), ("failure", "implementation"))

    def test_dry_run_changes_nothing(self):
        w = self.world()
        sha = w.work("A", "alpha", {"one.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        state_before = (w.ga / "state.json").read_bytes()
        before = self.origin_integ(w, "alpha")
        res = w.hub.tick(dry_run=True)
        self.assertTrue(any(p.startswith("ff alpha") for p in res.plan))
        self.assertEqual(self.origin_integ(w, "alpha"), before)
        self.assertEqual((w.ga / "state.json").read_bytes(), state_before)
        self.assertFalse((w.ga / "records" / "rounds").exists())
        self.assertEqual(w.contexts, [])  # no judgement in a dry run
        # the real tick afterwards still sees everything as new
        self.assertEqual(w.hub.tick().integrated, {"alpha": sha})

    def test_integration_moved_outside_hub_is_reported_once(self):
        w = self.world()
        sha = w.work("A", "alpha", {"one.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        w.hub.tick()
        # someone pushes the integration branch directly (no hook in the person's checkout)
        # (an admin moves it on the remote itself: an ordinary push is refused by the pre-receive hook)
        main = w.repos["alpha"]
        git(main, "fetch", "--quiet", "origin")
        git(main, "checkout", "--quiet", "-B", "tmp", "origin/integ")
        git(main, "commit", "--quiet", "--allow-empty", "-m", "direct")
        bare = w.tmp / "remotes" / "alpha.git"
        git(main, "push", "--quiet", "--no-verify", str(bare), "tmp:refs/heads/admin-tmp")
        git(bare, "update-ref", "refs/heads/integ", git(main, "rev-parse", "tmp"))
        res = w.hub.tick()
        self.assertIn("R3", [p.rule for p in res.findings if p.strength == "hard"])
        self.assertEqual(res.verdict["class"], "blocked")
        self.assertTrue(w.hub.tick().quiet)


class ParseCountsTest(unittest.TestCase):
    def test_unittest_and_pytest(self):
        self.assertEqual(parse_counts("Ran 5 tests in 0.1s\n\nOK (skipped=2)\n").as_dict(), {"passed": 3, "failed": 0, "skipped": 2})
        self.assertEqual(parse_counts("Ran 4 tests in 0.1s\n\nFAILED (failures=1, errors=1)\n").as_dict(),
                         {"passed": 2, "failed": 1, "skipped": 0, "errors": 1})
        self.assertEqual(parse_counts("==== 62 passed, 2 skipped in 3.10s ====\n").as_dict(), {"passed": 62, "failed": 0, "skipped": 2})
        self.assertEqual(parse_counts("1 failed, 3 passed, 1 warning in 0.5s\n").as_dict(), {"passed": 3, "failed": 1, "skipped": 0})
        self.assertIsNone(parse_counts("nothing here"))


if __name__ == "__main__":
    unittest.main()
