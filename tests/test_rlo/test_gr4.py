"""CMD-GR4 (U1, worker side of rlo CMD-K11 in-turn ReAct): the substitutes map, the worker rule, doctor's checks.

The replays of W1's four cases against K11 rlo (S3) wait for K11 to land; these tests cover what exists now.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ga.rlo import remote

from .world import run_cli


class SubstitutesTest(unittest.TestCase):
    def test_the_map_serves_plumbing_only(self):
        self.assertEqual(remote.SUBSTITUTES, {"ReadNotifications": ["ga.mail.read", "mcp__github__issue_read"],
                                              "mcp__claude-code-remote__send_message": ["ga.mail.send"]})  # GR7 S1
        for t in ("WebFetch", "Agent", "mcp__claude-code-remote__create_session"):
            self.assertNotIn(t, remote.SUBSTITUTES)
        self.assertEqual(remote.substitute_problems(remote.model("W", substitutes=True)), [])

    def test_mutations_on_the_map_are_caught(self):
        good = remote.model("W", substitutes=True)
        cases = {
            "a substitute for WebFetch": {"WebFetch": ["mcp__github__issue_read"]},
            "a substitute outside the model": {"ReadNotifications": ["mcp__github__get_file_contents"]},
            "an ungranted external substitute": {"ReadNotifications": ["mcp__github__create_issue"]},
        }
        for what, subs in cases.items():
            with self.subTest(what=what):
                m = json.loads(json.dumps(good))
                if what == "an ungranted external substitute":
                    m["specs"].append(dict(m["specs"][0], name="mcp__github__create_issue", risk="external"))
                m["substitutes"] = subs
                self.assertTrue(remote.substitute_problems(m), what)

    def test_written_and_read_by_the_pinned_rlo(self):
        """rlo 0.6.0 (K11) splits 'substitutes' off before action-model/1; the model file judges the same with it."""
        from rlo.react import load_model

        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        remote.write(d, "W1")
        m = json.loads((d / "ops/rlo/model.json").read_text())
        self.assertEqual(m["substitutes"], remote.SUBSTITUTES)
        _, subs = load_model(str(d / "ops/rlo/model.json"))
        self.assertEqual(subs, remote.SUBSTITUTES)
        self.assertEqual(remote.problems(d), [])


class ReplayMutationTest(unittest.TestCase):
    """D2: a substitute for WebFetch, the rule removed, escalate ignored -- each fails doctor --profile remote."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.assertEqual(run_cli("init", "--profile", "remote", "--work-repo", str(self.repo), "--name", "W1")[0], 0)

    def failed(self):
        import sys

        rc, out, _ = run_cli("doctor", "--profile", "remote", "--work-repo", str(self.repo), "--venv", sys.prefix, "--json")
        return rc, {c["check"] for c in json.loads(out)["checks"] if not c["ok"]}

    def test_good(self):
        self.assertEqual(self.failed(), (0, set()))

    def test_w1_cases_are_replayed(self):
        names = {c.name: c.react for c in remote.cases(rlo="0.7.0")}
        self.assertEqual(names["W1 v1 model: ReadNotifications"]["kind"], "use_tool")
        self.assertEqual(names["Bash after 2h idle"]["kind"], "refresh_read")  # before K13
        self.assertIsNone({c.name: c.react for c in remote.cases(rlo="0.8.2")}["Bash after 2h idle"])  # K13: passes
        self.assertEqual(names["Bash beside a call with no result"]["kind"], "wait_previous")
        self.assertEqual(names["A7 send_message without its grant"]["kind"], "report")
        self.assertEqual(names["A1 WebFetch"]["kind"], "report")
        self.assertEqual(names["W1 v1 model: ReadNotifications, denied twice before"]["attempt"], 3)

    def test_a_substitute_for_webfetch(self):
        p = self.repo / "ops/rlo/model.json"
        m = json.loads(p.read_text())
        m["substitutes"]["WebFetch"] = ["mcp__github__issue_read"]
        p.write_text(json.dumps(m))
        rc, bad = self.failed()
        self.assertEqual(rc, 1)
        self.assertIn("remote.preset", bad)
        self.assertIn("remote.replay.A1 WebFetch", bad)  # rlo now offers use_tool, not report: the replay sees it

    def test_the_substitutes_map_dropped(self):
        p = self.repo / "ops/rlo/model.json"
        m = json.loads(p.read_text())
        m.pop("substitutes")
        p.write_text(json.dumps(m))
        _, bad = self.failed()
        self.assertIn("remote.replay.W1 v1 model: ReadNotifications", bad)  # report instead of use_tool

    def test_rule_removed_or_escalate_ignored(self):
        for cut in (remote.REACT_RULE, "When escalate is true, post the deny verbatim on your channel"):
            p = self.repo / "ops/rlo/PROMPT.md"
            good = p.read_text()
            p.write_text(good.replace(cut, ""))
            self.assertIn("remote.preset", self.failed()[1])
            p.write_text(good)


class WorkerRuleTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.assertEqual(run_cli("init", "--profile", "remote", "--work-repo", str(self.repo), "--name", "W1")[0], 0)

    def test_rule_in_guard_md_and_prompt(self):
        for f in ("GUARD.md", "PROMPT.md"):
            text = (self.repo / "ops/rlo" / f).read_text()
            self.assertIn(remote.REACT_RULE, text)
            self.assertIn("-- react:", text)
            self.assertIn("escalate is true", text)
            self.assertIn(remote.DENY_REPORT, text)  # a deny without a react line (older rlo) is still reported
        self.assertEqual(remote.problems(self.repo), [])

    def test_rule_removed_or_escalate_dropped_is_caught(self):
        for f in ("GUARD.md", "PROMPT.md"):
            for cut in (remote.REACT_RULE, "When escalate is true, post the deny verbatim on your channel and continue "
                                           "other work."):
                with self.subTest(f=f, cut=cut[:20]):
                    p = self.repo / "ops/rlo" / f
                    good = p.read_text()
                    p.write_text(good.replace(cut, ""))
                    self.assertTrue(any("ReAct rule" in x for x in remote.problems(self.repo)))
                    p.write_text(good)


if __name__ == "__main__":
    unittest.main()
