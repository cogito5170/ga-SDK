"""CMD-GA24 (BD-254): R6 and ga mail catch the Google secrets this project holds — Gemini/Google API keys and Google
OAuth credentials — in every place that uses SECRET_PATTERNS. Fake values are assembled at runtime, so this repository
never holds one.
"""
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ga import mailbox as mb
from ga import rules
from ga.__main__ import main
from ga.forms import dump_text

from test_mailbox import Repo, report2
from test_rules_gates import cfg
from world import World, directive

ROOT = Path(__file__).resolve().parents[1]


def google_fakes():
    """name -> a fake value in the shape of each Google secret."""
    return {
        "api_key": "".join(["AI", "za", "Sy", "B" * 33]),
        "oauth_access": "".join(["ya", "29.", "a0A", "x" * 30]),
        "oauth_refresh": "".join(["1", "//", "0g", "Y" * 40]),
        "client_secret": "".join(["GOC", "SPX-", "z" * 28]),
        "json_field": "".join(['{"refresh', '_token": "', "abcd" * 5, '"}']),
    }


NAME_ONLY = dump_text(
    {"schema": "report/2", "from": "W1", "handled": [{"id": "CMD-W1", "rev_seen": 1, "status": "done"}],
     "items": [{"id": "D1", "state": "met", "evidence": ["GEMINI_API_KEY set in the environment"]}]},
    "## Result\nThe key comes from `GEMINI_API_KEY` (export GEMINI_API_KEY=... in the shell, never in a file).\n"
    "OAuth tokens live in ~/.gemini and antigravity-oauth-token; access_token and refresh_token are field names.\n")


class R6Test(unittest.TestCase):
    def test_each_google_pattern_is_hard_and_not_echoed(self):
        c = cfg()
        for name, s in google_fakes().items():
            with self.subTest(name=name):
                found = rules.r6_secrets(c, f"diff\n+{s}\n", "diff")
                self.assertEqual([(p.rule, p.strength) for p in found][:1], [("R6", "hard")])
                self.assertNotIn(s, " ".join(p.message for p in found))

    def test_no_false_positive(self):
        c = cfg()
        self.assertEqual(rules.r6_secrets(c, NAME_ONLY, "report"), [])
        # every tracked text file of this repository: docs, examples, fixtures, code (tests build fakes at runtime)
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        hits = []
        for f in files:
            try:
                text = (ROOT / f).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if rules.r6_secrets(c, text, f):
                hits.append(f)
        self.assertEqual(hits, [])

    def test_short_or_partial_shapes_pass(self):
        c = cfg()
        for s in ("AIza" + "B" * 20, "ya29." + "x" * 5, "1//" + "Y" * 10, "GOCSPX-" + "z" * 5,
                  '"access_token": "short"', "https://example.com//path"):
            with self.subTest(s=s):
                self.assertEqual(rules.r6_secrets(c, s, "x"), [])


class MailTest(unittest.TestCase):
    def test_ga_mail_send_refuses_each(self):
        r = Repo(self, "w1")
        box = r.box("w1")
        for name, s in google_fakes().items():
            with self.subTest(name=name), self.assertRaises(mb.MailError) as e:
                box.send("AMP", report2("W1", body=f"## Result\n{s}\n"))
            self.assertIn("looks like a secret", str(e.exception))
            self.assertNotIn(s, str(e.exception))
        self.assertEqual(r.files(), [])
        box.send("AMP", NAME_ONLY)  # naming the variable is fine
        self.assertEqual(len(r.files()), 1)

    def test_ga_mail_read_flags_each(self):
        r = Repo(self, "w1", "amp")
        with mock.patch.object(mb, "secrets_in", return_value=[]):  # a sender that skipped the check
            for i, s in enumerate(google_fakes().values(), 1):
                r.box("w1").send("AMP", report2("W1", f"CMD-W{i}", body=f"## Result\n{s}\n"))
        got = list(r.box("amp").unread("AMP"))
        self.assertEqual(len(got), 5)
        self.assertTrue(all("looks like a secret" in m.problems for m in got))


class CliTest(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_check_and_post(self):
        w = World()
        self.addCleanup(w.close)
        for name, s in google_fakes().items():
            with self.subTest(name=name):
                f = w.tmp / f"{name}.md"
                f.write_text(report2("A", body=f"## Evidence\n{s}\n"), encoding="utf-8")
                code, out, _ = self.run_cli("--config", str(w.tmp / "ga.json"), "check", str(f))
                self.assertEqual(code, 2)
                self.assertIn("R6", out)
                code, _, err = self.run_cli("--config", str(w.tmp / "ga.json"), "--ga-dir", str(w.ga), "post",
                                            "--channel", "A", "--from", "A", str(f))
                self.assertEqual(code, 2)
                self.assertIn("R6", err)


class HubTest(unittest.TestCase):
    def test_a_directive_carrying_one_is_not_sent(self):
        w = World()
        self.addCleanup(w.close)
        for i, (name, s) in enumerate(google_fakes().items(), 1):
            with self.subTest(name=name):
                post, findings, _ = w.hub.send(directive(f"CMD-A{i}", "A"), f"## Note\n{s}\n")
                self.assertIsNone(post)
                self.assertIn(("R6", "hard"), [(p.rule, p.strength) for p in findings])
        self.assertEqual([p for p in w.mail.read("A") if p.author == "hub"], [])

    def test_a_commit_carrying_one_is_not_integrated(self):
        from ga.adapters.git import git
        w = World(remote=True)
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        w.paste("A")
        before = git(w.tmp / "remotes" / "alpha.git", "rev-parse", "integ") if (w.tmp / "remotes").exists() else None
        sha = w.work("A", "alpha", {"cfg.json": '{"key": "' + google_fakes()["api_key"] + '"}\n'})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        res = w.hub.tick()
        self.assertEqual(res.integrated, {})
        self.assertIn("R6", [p.rule for p in res.findings])
        self.assertEqual(res.verdict["class"], "blocked")
        if before is not None:
            self.assertEqual(git(w.tmp / "remotes" / "alpha.git", "rev-parse", "integ"), before)


if __name__ == "__main__":
    unittest.main()
