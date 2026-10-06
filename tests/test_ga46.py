"""CMD-GA46 D1: ga act wastes fewer turns: rejected turns count toward 'no progress', NEW may replace an owned file,
NEW content has no stray trailing blank lines. Scripted fake backends, 0 model runs."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ga.act import fmt as F  # noqa: E402
from ga.act.apply import apply  # noqa: E402
from tests.test_ga38 import FIX, run  # noqa: E402

PING_CFG = {"commands": {"test": ["{python}", "-c", "import sys; sys.exit(0 if open('probe/ping.txt').read()=='pong\\n' else 1)"]},
            "done_when": "test", "timeout_s": 60}
NEW_PONG = "NEW probe/ping.txt\n<<<<<<< CONTENT\npong\n\n>>>>>>> END\n"
BAD_EDIT = "EDIT probe/ping.txt\n<<<<<<< SEARCH\nnope\n=======\npong\n>>>>>>> REPLACE\n"


def ping_repo(case, start=None):
    d = Path(tempfile.mkdtemp(prefix="ga46-"))
    case.addCleanup(shutil.rmtree, d, True)
    (d / ".ga-act.json").write_text(json.dumps(PING_CFG))
    (d / "probe").mkdir()
    if start is not None:
        (d / "probe/ping.txt").write_text(start)
    return d


class Trailing(unittest.TestCase):
    def content(self, body):
        return F.parse(f"NEW a.txt\n<<<<<<< CONTENT\n{body}>>>>>>> END\n").actions[0].content

    def test_collapse(self):
        self.assertEqual(self.content("pong\n\n\n"), "pong\n")
        self.assertEqual(self.content("pong\n  \n"), "pong\n")
        self.assertEqual(self.content("a\n\nb\n\n"), "a\n\nb\n")  # inner blank lines stay
        self.assertEqual(self.content(""), "")
        self.assertEqual(self.content("\n\n"), "")

    def test_form_mentions_rewrite(self):
        self.assertIn("rewrite", F.SPEC)


class Replace(unittest.TestCase):
    def test_owned_existing_replaced(self):
        d = ping_repo(self, "old\n")
        out = apply(d, F.parse(NEW_PONG).actions, ["probe/ping.txt"])
        self.assertEqual((d / "probe/ping.txt").read_text(), "pong\n")
        self.assertEqual(out.replaced, ["probe/ping.txt"])
        self.assertIn("replaced", out.applied[0])
        self.assertEqual(out.rejected, [])

    def test_new_file_not_marked_replaced(self):
        d = ping_repo(self)
        out = apply(d, F.parse(NEW_PONG).actions, ["probe/*"])
        self.assertEqual(out.replaced, [])

    def test_outside_files_refused(self):
        d = ping_repo(self, "old\n")
        (d / "other.txt").write_text("keep\n")
        a = F.parse("NEW other.txt\n<<<<<<< CONTENT\nx\n>>>>>>> END\n").actions
        out = apply(d, a, ["probe/ping.txt"])
        self.assertEqual((d / "other.txt").read_text(), "keep\n")
        self.assertEqual(out.applied, [])
        self.assertIn("not in the item's files", out.rejected[0])

    def test_escape_refused(self):
        d = ping_repo(self)
        out = apply(d, F.parse("NEW ../x.txt\n<<<<<<< CONTENT\nx\n>>>>>>> END\n").actions, ["*"])
        self.assertEqual(out.applied, [])


class Loop(unittest.TestCase):
    def go(self, answers, start=None, **kw):
        d = ping_repo(self, start)
        return run(self, d, answers, files=["probe/ping.txt"], item={"goal": "write pong"}, **kw), d

    def test_first_answer_passes_in_one_turn(self):  # AGV0: extra blank line, then fine
        (res, fake, act, _), d = self.go([NEW_PONG])
        self.assertEqual((res.status, res.turns), ("done", 1))
        self.assertEqual((d / "probe/ping.txt").read_text(), "pong\n")

    def test_replay_stops_no_progress_after_three_turns(self):
        # AGV0: turn 1 writes (with the stray blank line, as before S3) but the set is unchanged, a RUN, then
        # rejected NEW/EDIT attempts: blocked 'no progress' on turn 3, not 10
        wrong = "NEW probe/ping.txt\n<<<<<<< CONTENT\nPONG\n>>>>>>> END\n"
        (res, fake, act, _), d = self.go([wrong, "RUN test\n"] + [BAD_EDIT] * 8, start=None)
        self.assertEqual(res.status, "blocked")
        self.assertTrue(res.reason.startswith("no progress"), res.reason)
        self.assertEqual((res.turns, len(fake.calls)), (3, 3))

    def test_rejected_turns_count_from_the_start(self):
        (res, fake, _, _), _ = self.go([BAD_EDIT] * 8, start="old\n")
        self.assertTrue(res.reason.startswith("no progress"), res.reason)
        self.assertEqual(res.turns, 2)

    def test_no_action_turns_count(self):
        (res, _, _, _), _ = self.go(["I think so\n"] * 8, start="old\n")
        self.assertTrue(res.reason.startswith("no progress"), res.reason)
        self.assertEqual(res.turns, 2)

    def test_replace_on_owned_file_ends_done(self):
        (res, _, _, _), d = self.go([BAD_EDIT, NEW_PONG], start="old\n")
        self.assertEqual((res.status, res.turns), ("done", 2))
        self.assertEqual(res.changed, ["probe/ping.txt"]) if hasattr(res, "changed") else None

    def test_replaced_in_row(self):
        (res, _, _, _), d = self.go([NEW_PONG], start="old\n")
        self.assertEqual(res.rows[0]["replaced"], ["probe/ping.txt"])

    def test_changed_failing_set_resets_the_stall(self):
        from tests.test_ga38 import py_repo
        d = py_repo(self)
        bad = ("EDIT calc.py\n<<<<<<< SEARCH\n    return a * b\n=======\n    return a - b\n>>>>>>> REPLACE\n")
        miss = "EDIT calc.py\n<<<<<<< SEARCH\nnope\n=======\nx\n>>>>>>> REPLACE\n"
        res, fake, _, _ = run(self, d, [miss, bad, miss, miss, miss, miss])
        self.assertTrue(res.reason.startswith("no progress"), res.reason)
        self.assertEqual(res.turns, 4)  # miss(1) bad(reset) miss(1) miss(2)


if __name__ == "__main__":
    unittest.main()
