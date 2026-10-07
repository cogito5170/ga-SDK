"""CMD-GA42 rev 2 D1: accept-with-needs, a hand-written registry entry without network approval, shadow mode and
``ga hub shadow-compare``. Offline, fake judge / backend / mailbox from tests/test_ga42.py. 0 model runs.
VI-04b (baseline amendment): the shadow decision is ``ga verdict`` (0 model calls); the fake ``verdict_fn`` below is
ga.verdict.decide on given checks, so the shadow tests pin the hub's plumbing, not the checks."""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ga import actions
from ga.actions import registry as R
from ga.hub import MailHub, read_jsonl, shadow_compare
from tests.test_ga42 import DLOG, BASE, REPO, World, git, to
from tests.test_ga42_actions import Base


def hub(w, answers, cls="success", shadow=None, checks=None, **kw):
    """``checks``: the ga verdict checks the fake verdict_fn decides on (default: all pass)."""
    from tests.test_ga42 import Fake
    from ga import verdict as V
    w.runner, w.applied, w.verdicts = Fake(answers), [], []

    def apply(j, repo, remote):
        w.applied.append(j.sha)
        return "fake-pushed"

    def verdict_fn(repo, **k):
        w.verdicts.append(k)
        c = {n: (True, "ok") for n in V.ORDER}
        c.update(checks or {})
        return V.decide(c, k.get("nonexec"), "light")
    w.conf.setdefault("specs", {"CMD-T1": "tests/test_calc.py"})
    return MailHub(w.conf, ga_dir=w.tmp / ".ga", mailbox=w.mail, runner=w.runner, judge_fn=w.judgement(cls, **kw),
                   apply_fn=apply, today=lambda: "2026-10-05", shadow=shadow, verdict_fn=verdict_fn)


class AcceptWithNeeds(unittest.TestCase):
    def test_accept_on_success_with_needs_is_not_integrated_and_becomes_ask_human(self):
        w = World(self)
        w.report()
        res = hub(w, ["ACCEPT"], needs=["D1 claim not measured"]).tick()
        self.assertEqual(w.applied, [])  # the integration is never even tried
        self.assertEqual(res.sent, ["ASK_HUMAN CMD-T1"])
        self.assertEqual(res.integrated, {})
        (q, _), = to(w, "human")
        self.assertIn("judge class is success with 1 item(s) for judgement", q["next"]["reason"])
        self.assertEqual(to(w, "GA"), [])
        self.assertEqual((w.base / "DECISION_LOG.md").read_text(), DLOG)
        self.assertEqual((w.base / "BASELINE.md").read_text(), BASE)

    def test_accept_on_success_without_needs_is_integrated(self):  # the control of the test above
        w = World(self)
        w.report()
        res = hub(w, ["ACCEPT"]).tick()
        self.assertEqual(w.applied, [w.sha])
        self.assertEqual(res.sent, ["ACCEPT CMD-T1"])


class NetworkUnapproved(Base):
    def test_hand_written_entry_with_a_correct_sha256_but_network_false_is_refused_at_run(self):
        e = {"argv": ["git", "fetch", "origin"], "cwd": ".", "timeout_s": 30, "writes": [], "network": False,
             "about": "fetch", "files": {}, "approved_by": "nobody", "at": "2026-10-05"}
        e["sha256"] = R.digest(e)  # integrity holds: the hash is right, yet nobody approved network
        (self.home / "actions.json").write_text(json.dumps({"fetch-o": e}))
        self.assertIn("fetch-o", R.verified())
        with self.assertRaises(actions.ActionError) as c:
            actions.run("fetch-o", {}, self.root)
        self.assertIn("needs network", str(c.exception))

    def test_the_docstring_says_sha256_is_integrity_not_authenticity(self):
        self.assertIn("integrity, not authenticity", R.__doc__)


class Shadow(unittest.TestCase):
    def files(self, w):
        # CMD-GA45 S2: the shadow hub also mails its row to baseline-shadow and keeps the mailed path in state.json
        return {k: v for k, v in w.snapshot().items() if not k.endswith(("/hub/shadow.jsonl", "/hub/state.json"))}

    def check_shadow(self, answers, cls, decision, shadow=True, conf_shadow=False, checks=None, **kw):
        w = World(self)
        w.report()
        if conf_shadow:
            w.conf["shadow"] = True
        h = hub(w, answers, cls, shadow=shadow, checks=checks, **kw)
        before, tmp0 = self.files(w), set(Path(tempfile.gettempdir()).glob("ga-hub-shadow-*"))
        res = h.tick()
        self.assertEqual(self.files(w), before)  # no file outside shadow.jsonl changed
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("ga-hub-shadow-*")), tmp0)
        self.assertEqual([r for r, _h, _t in w.mail.sent], ["baseline-shadow"])  # only the shadow row (CMD-GA45)
        self.assertEqual(w.mail.read, set())  # not even a read mark: the real hub still sees it
        self.assertEqual(w.applied, [])  # nothing integrated
        self.assertEqual((w.remote_main(), w.remote_main("base.git")),
                         (w.main0, git(w.base, "rev-parse", "HEAD")))
        self.assertEqual(res.integrated, {})
        self.assertEqual(len(w.runner.calls), 0)  # VI-04b: no model turn in shadow
        self.assertEqual(len(w.verdicts), 1)  # ga verdict decided
        self.assertEqual(len(w.judged), 1)
        (row,) = read_jsonl(w.tmp / ".ga/hub/shadow.jsonl")
        for k in ("at", "id", "rev", "sha", "judge_class", "needs", "decision", "asks", "tokens"):
            self.assertIn(k, row)
        self.assertEqual((row["id"], row["rev"], row["sha"], row["judge_class"], row["decision"]),
                         ("CMD-T1", 1, w.sha, cls, decision))
        self.assertEqual(row["tokens"], {"input": None, "output": None})
        # the same mail again: quiet, no write, no model call
        before = w.snapshot()
        res = h.tick()
        self.assertTrue(res.quiet)
        self.assertEqual(w.snapshot(), before)
        self.assertEqual((len(w.runner.calls), len(w.verdicts)), (0, 1))
        return w, row

    def test_shadow_accept_changes_nothing_but_shadow_jsonl(self):
        self.check_shadow(["ACCEPT"], "success", "ACCEPT")

    def test_shadow_send_back_changes_nothing_but_shadow_jsonl(self):
        _, row = self.check_shadow(["SEND_BACK\n- fix add\n- add a test"], "partial", "SEND_BACK",
                                   checks={"files": (False, "outside allowed: other.py")})
        self.assertEqual(row["asks"], ["files: outside allowed: other.py"])

    def test_shadow_accept_with_needs_is_recorded_as_shadow(self):  # VI-04b: needs are non-executable criteria
        _, row = self.check_shadow(["ACCEPT"], "success", "SHADOW", needs=["D1 claim"])
        self.assertEqual(row["needs"], ["D1 claim"])
        self.assertEqual(row["asks"], ["non-executable criterion: D1 claim"])

    def test_shadow_from_the_config(self):
        self.check_shadow(["ACCEPT"], "success", "ACCEPT", shadow=None, conf_shadow=True)

    def test_without_shadow_the_same_tick_does_integrate(self):  # the control
        w = World(self)
        w.report()
        hub(w, ["ACCEPT"]).tick()
        self.assertEqual(w.applied, [w.sha])
        self.assertFalse((w.tmp / ".ga/hub/shadow.jsonl").exists())


class ShadowCompare(unittest.TestCase):
    SH = [{"id": "A", "rev": 1, "decision": "ACCEPT"}, {"id": "B", "rev": 2, "decision": "ACCEPT"},
          {"id": "C", "rev": 1, "decision": "SEND_BACK"}, {"id": "D", "rev": 1, "decision": "ASK_HUMAN"}]
    BL = [{"id": "A", "rev": 1, "decision": "ACCEPT"}, {"id": "B", "rev": 2, "decision": "SEND_BACK"},
          {"id": "C", "rev": 1, "decision": "ACCEPT"}, {"id": "D", "rev": 1, "decision": "ASK_HUMAN"},
          {"id": "E", "rev": 1, "decision": "ACCEPT"}]

    def test_counts_a_false_accept_and_an_extra_send_back(self):
        out = shadow_compare(self.BL, self.SH)
        self.assertEqual((out["compared"], out["agree"]), (4, 2))
        self.assertEqual(out["false_accepts"], ["B rev 2"])
        self.assertEqual(out["extra_send_backs"], ["C rev 1"])
        self.assertEqual(out["missing_in_shadow"], ["E rev 1"])
        self.assertFalse(out["gate_ok"])
        ok = shadow_compare([b for b in self.BL if b["id"] != "B"], self.SH)
        self.assertEqual(ok["false_accepts"], [])
        self.assertFalse(ok["gate_ok"])  # CMD-GA49 S2: 3 compared rows are not 10 agreements

    def test_cli_prints_and_exits_1_on_a_false_accept(self):
        from ga.__main__ import main
        d = Path(tempfile.mkdtemp(prefix="ga42-cmp-"))
        self.addCleanup(__import__("shutil").rmtree, d, True)
        (d / "hub").mkdir()
        (d / "hub" / "shadow.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.SH))
        (d / "bl.json").write_text(json.dumps(self.BL))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["hub", "shadow-compare", str(d / "bl.json"), "--ga-dir", str(d)])
        self.assertEqual(code, 1)
        self.assertIn("agreement 2/4", buf.getvalue())
        self.assertIn("false accepts 1", buf.getvalue())
        self.assertIn("extra send-backs 1", buf.getvalue())

    def test_cli_has_shadow(self):
        out = subprocess.run([sys.executable, "-m", "ga", "hub", "--help"], capture_output=True, text=True,
                             cwd=Path(__file__).parents[1]).stdout
        for c in ("--shadow", "shadow-compare"):
            self.assertIn(c, out)


class WrapperShell(Base):
    """rev 2 send-back: a shell reached through a wrapper later in argv is refused with the reason."""
    CASES = [(["env", "bash", "x.sh"], 1, "bash"), (["nice", "sh", "run.sh"], 1, "sh"),
             (["timeout", "5", "dash", "x"], 2, "dash"), (["xargs", "sh"], 1, "sh"),
             (["python3", "tool.py", "/bin/bash"], 2, "/bin/bash")]

    def test_a_shell_later_in_argv_is_refused_with_the_reason(self):
        from ga.actions import check as C
        for argv, i, sh in self.CASES:
            with self.subTest(argv=argv):
                reasons, _ = C.check({"name": "w", "argv": argv, "cwd": ".", "timeout_s": 5, "writes": []}, self.root,
                                     ["env", "nice", "timeout", "xargs", "python3"])
                self.assertIn(f"argv[{i}] {sh!r} evaluates code or starts a shell", reasons)
                rec = actions.propose(self.prop(argv, name="w"), self.root, source="test")
                self.assertFalse(rec["check"]["ok"])
                self.assertFalse(rec["trial"]["ran"])


class SymlinkedRoot(Base):
    """rev 2 send-back: the project root reached through a symlink is resolved before any containment test."""

    def setUp(self):
        super().setUp()
        self.outer = Path(tempfile.mkdtemp(prefix="ga42-link-"))
        self.addCleanup(__import__("shutil").rmtree, self.outer, True)
        (self.outer / "outside.py").write_text("print('outside')\n")
        self.real = self.outer / "real"
        __import__("shutil").copytree(self.root, self.real)
        self.link = self.outer / "link"
        self.link.symlink_to(self.real, target_is_directory=True)

    def test_a_path_inside_is_accepted_through_the_symlinked_root(self):
        from ga.actions import check as C
        self.assertIsNone(C.confined(self.link, "src/a.py"))
        reasons, _ = C.check({"name": "ok", "argv": ["python3", "tool.py", "src/a.py"], "cwd": "src", "timeout_s": 5,
                              "writes": ["src/*"]}, self.link, ["python3"])
        self.assertEqual(reasons, [])

    def test_an_escape_through_the_symlinked_root_is_refused(self):
        from ga.actions import check as C
        self.assertIn("escapes the project", C.confined(self.link, "../outside.py"))
        reasons, _ = C.check({"name": "esc", "argv": ["python3", "../outside.py"], "cwd": ".", "timeout_s": 5,
                              "writes": []}, self.link, ["python3"])
        self.assertTrue(any("escapes the project" in r for r in reasons), reasons)


if __name__ == "__main__":
    unittest.main()
