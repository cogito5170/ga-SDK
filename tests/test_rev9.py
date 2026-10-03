"""CMD-GA11: METHOD rev 9 (BD-146) and the GA10 findings.

- known skips (expected_skipped: exactly that many, with a reason) are not a floor; any other count is
- a packaged repository reproduced without a clean install floors at partial (not_install_checked)
- Bundle (b) imports the installed package from an empty directory: a sub-package the packaging leaves out fails
  (GA10 W1 was green on PYTHONPATH and missing from the installed copy)
- a relative ga_dir works (GA10 F4); the turn prompt carries the report/1 head to fill (GA10 F2)
- R4 non-fast-forward → rev+1 by the hub: tests/test_integration.py
"""
import os
import unittest
from pathlib import Path

from ga import config as gacfg
from ga.adapters.git import GitVcs, git
from ga.forms import FormError, parse_text
from ga.hub import Hub
from ga.prompts import turn_prompt

from world import TEST_OK, TEST_SKIP, World, directive, proposal, pyproject


def ok(ctx):
    return proposal("success", "wait", "판정")


class KnownSkipsTest(unittest.TestCase):
    def round_with_one_skip(self, known):
        w = World(judge_fn=ok)
        self.addCleanup(w.close)
        if known is not None:
            w.cfg.repos["alpha"].expected_skipped = known
        w.hub.send(directive("CMD-A1", "A"))
        sha = w.work("A", "alpha", {"tests/test_x.py": TEST_SKIP})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        return w.hub.tick()

    def test_exactly_the_known_skips_are_not_a_floor(self):
        res = self.round_with_one_skip({"count": 1, "why": "needs install metadata"})
        self.assertEqual(res.verdict["evidence"]["tests"]["alpha"]["skipped"], 1)
        self.assertEqual(res.verdict["class"], "success")
        self.assertTrue(any("known: needs install metadata" in n for n in res.verdict["evidence"]["notes"]))

    def test_other_counts_or_no_reason_are_a_floor(self):
        for known in (None, {"count": 2, "why": "x"}, {"count": 0, "why": "x"}, {"count": 1}, {"count": 1, "why": ""}):
            with self.subTest(known=known):
                res = self.round_with_one_skip(known)
                self.assertEqual((res.verdict["class"], res.verdict["cause"], res.verdict["subclass"]),
                                 ("partial", "measurement", "skipped"))

    def test_config_shape(self):
        raw = {"schema": "ga-config/1", "hub": {"name": "hub"}, "integration_branch": "i",
               "repos": {"r": {"path": "r"}}, "sessions": {"A": {"prefix": "A", "branch": "a", "repos": ["r"]}}}
        ok_ = dict(raw, repos={"r": {"path": "r", "expected_skipped": {"count": 2, "why": "metadata"}}})
        self.assertEqual(gacfg.from_dict(ok_).repos["r"].expected_skipped, {"count": 2, "why": "metadata"})
        self.assertEqual(gacfg.from_dict(raw).repos["r"].expected_skipped, {})
        for bad in (2, {"count": -1, "why": "x"}, {"count": True, "why": "x"}, {"count": "2"}, {"count": 1, "why": 3},
                    {"count": 1, "why": "x", "more": 1}):
            with self.subTest(bad=bad), self.assertRaises(FormError):
                gacfg.from_dict(dict(raw, repos={"r": {"path": "r", "expected_skipped": bad}}))


class InstallCheckTest(unittest.TestCase):
    def test_packaged_without_a_clean_install_is_partial(self):
        w = World(judge_fn=ok, packaged=True)  # path mode only
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        sha = w.work("A", "alpha", {"x.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        res = w.hub.tick()
        self.assertEqual(res.integrated, {"alpha": sha})
        self.assertEqual((res.verdict["class"], res.verdict["cause"], res.verdict["subclass"]),
                         ("partial", "measurement", "not_install_checked"))

    def test_unpackaged_path_only_is_not_floored(self):
        w = World(judge_fn=ok)  # no pyproject: nothing to install
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        sha = w.work("A", "alpha", {"x.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        self.assertEqual(w.hub.tick().verdict["class"], "success")

    def subpackage_round(self, listed):
        """alpha gets alphapkg/sub; the pyproject lists it or not. The tests import it from the checkout either way."""
        w = World(judge_fn=ok, remote=True, modes=("path", "install"))
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        text = pyproject("alphapkg")
        if listed:
            text = text.replace('packages = ["alphapkg"]', 'packages = ["alphapkg", "alphapkg.sub"]')
        sha = w.work("A", "alpha", {"alphapkg/sub/__init__.py": "W = 2\n", "pyproject.toml": text,
                                    "tests/test_sub.py": "import unittest\nimport alphapkg.sub\n\nclass T(unittest.TestCase):\n"
                                                         "    def test_w(self):\n        self.assertEqual(alphapkg.sub.W, 2)\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        return w.hub.tick()

    def test_a_subpackage_missing_from_the_install_fails(self):
        res = self.subpackage_round(listed=False)
        self.assertEqual(res.verdict["evidence"]["tests"]["alpha"]["failed"], 0)  # the tests alone stay green
        self.assertEqual((res.verdict["class"], res.verdict["cause"]), ("failure", "implementation"))
        self.assertTrue(any("missing_in_install" in n and "alphapkg.sub" in n for n in res.verdict["evidence"]["notes"]))

    def test_a_listed_subpackage_passes(self):
        res = self.subpackage_round(listed=True)
        self.assertEqual(res.verdict["class"], "success")
        self.assertFalse(any("missing_in_install" in n or "not_install_checked" in n for n in res.verdict["evidence"]["notes"]))


    def test_install_mode_drops_the_repository_pythonpath(self):
        w = World(judge_fn=ok, remote=True, modes=("path", "install"))
        self.addCleanup(w.close)
        out = w.tmp / "probe"
        out.mkdir()
        w.cfg.repos["alpha"].env = {"PYTHONPATH": "/nonexistent-shadow", "GA_OUT": str(out)}
        w.hub.send(directive("CMD-A1", "A"))
        probe = ("import os, pathlib, sys, unittest\n\nclass T(unittest.TestCase):\n    def test_env(self):\n"
                 "        mode = 'install' if '/install/' in sys.prefix else 'path'\n"
                 "        pathlib.Path(os.environ['GA_OUT'], mode).write_text(os.environ.get('PYTHONPATH', ''))\n")
        sha = w.work("A", "alpha", {"tests/test_env.py": probe})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        w.hub.tick()
        self.assertEqual((out / "path").read_text(), "/nonexistent-shadow")  # the path mode keeps the repository's own
        self.assertNotIn("/nonexistent-shadow", (out / "install").read_text())  # (b) never puts it back


class RelativeGaDirTest(unittest.TestCase):
    def test_relative_ga_dir_makes_the_session_clone(self):
        w = World()
        self.addCleanup(w.close)
        cwd = os.getcwd()
        os.chdir(w.tmp)
        self.addCleanup(os.chdir, cwd)
        vcs = GitVcs(w.cfg, ".rel")
        self.assertTrue(vcs.ga_dir.is_absolute())
        hub = Hub(w.cfg, ga_dir=Path(".rel"), channel=w.mail, vcs=vcs, judge=w.hub.judge, runner=w.runner, bundle=w.hub.bundle)
        self.assertTrue(hub.ga.is_absolute())
        post, _, _ = hub.send(directive("CMD-A1", "A"))
        self.assertIsNotNone(post)
        self.assertTrue((w.tmp / ".rel" / "worktrees" / "A" / "alpha" / ".git").is_dir())
        self.assertEqual(git(w.tmp / ".rel" / "worktrees" / "A" / "alpha", "branch", "--show-current"), "sess-a")


class ReportTemplateTest(unittest.TestCase):
    def test_turn_prompt_has_the_head_to_fill(self):
        w = World()
        self.addCleanup(w.close)
        d = directive("CMD-A1", "A", rev=3)
        p = turn_prompt(w.cfg, "A", "x", d)
        head, _ = parse_text(p[p.index("```ga"):])
        self.assertEqual(head["handled"], [{"id": "CMD-A1", "rev_seen": 3, "status": "done"}])
        self.assertEqual(head["commits"], [{"repo": "alpha", "branch": "sess-a", "sha": "<SHA>"}])
        self.assertIn("commits", p[: p.index("```ga")])
        # the hub hands it over on send
        w.hub.send(d)
        self.assertIn('"rev_seen": 3', w.paste("A"))


if __name__ == "__main__":
    unittest.main()
