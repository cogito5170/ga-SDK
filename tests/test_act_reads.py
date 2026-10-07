"""CMD-ACTR1 (baseline acceptance test, may not be edited): ga act lets a cheap model work inside large files.

Rules (policy spec_split.no_code_from_baseline: the worker designs the code; this docstring is the spec):
R1 kept reads. Every NEED result (file / symbol / grep) stays in the card on every later turn of the same item, not
   only the next one. A kept read is served again from the CURRENT files each turn (an edit shows up). Kept reads are
   dropped before the current turn's NEED results when the card is over its cap, oldest first; at most 8 are kept.
   The card never goes over its cap.
R2 outline. ``NEED file <path>`` with no ``lines a-b`` on a file longer than 150 lines answers with the file's outline:
   one line per top-level def/class (and per method of a top-level class) carrying its name and its line number, and
   a hint to ask ``NEED file <path> lines a-b``. A file with no def/class keeps today's answer (the first lines).
R3 trace. ``Result.to_dict()`` (act/1) carries ``trace``: one object per model turn, in order, with ``turn`` (int),
   ``card_tokens`` (int), ``actions`` (list of "<KIND> <arg>" strings, e.g. "EDIT calc.py",
   "NEED file calc.py lines 1-2", each at most 120 characters), ``applied`` (int), ``rejected`` (int) and ``dropped``
   (the card's dropped units, list).
Fake models on temporary repos: 0 network, 0 model calls.
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_ga38 import FIX, bodies, py_repo, run  # noqa: E402


def big(root, name="big.py", n=1200, marks=None):
    rows = [f"V{i} = {i}\n" for i in range(1, n + 1)]
    for ln, text in (marks or {}).items():
        rows[ln - 1] = text + "\n"
    (root / name).write_text("".join(rows))


class KeptReads(unittest.TestCase):
    def test_a_read_stays_on_later_turns(self):
        root = py_repo(self)
        big(root, marks={510: "MARK_510 = 1"})
        res, fake, act, _ = run(self, root, ["NEED file big.py lines 505-515\n", "NEED grep zzz_not_there\n",
                                             "RUN lint\n", "BLOCKED stop\n"], max_turns=4)
        cards = bodies(fake)
        self.assertEqual(len(cards), 4)
        for k in (1, 2, 3):
            self.assertIn("MARK_510 = 1", cards[k], f"card {k + 1} lost the read of turn 1")

    def test_a_kept_read_is_served_from_the_current_file(self):
        root = py_repo(self)
        big(root, marks={510: "MARK_510 = 1"})
        edit = "EDIT big.py\n<<<<<<< SEARCH\nMARK_510 = 1\n=======\nMARK_510 = 2\n>>>>>>> REPLACE\n"
        res, fake, act, _ = run(self, root, ["NEED file big.py lines 505-515\n", edit, "RUN lint\n", "BLOCKED stop\n"],
                                files=("calc.py", "big.py"), max_turns=4)
        cards = bodies(fake)
        self.assertIn("MARK_510 = 2", cards[2])
        self.assertNotIn("MARK_510 = 1", cards[2])

    def test_kept_reads_never_push_the_card_over_its_cap(self):
        root = py_repo(self)
        big(root, n=3000)
        asks = [f"NEED file big.py lines {a}-{a + 140}\n" for a in (1, 300, 600, 900, 1200, 1500, 1800, 2100, 2400)]
        res, fake, act, _ = run(self, root, asks + ["BLOCKED stop\n"], max_turns=10, cap=2500)
        self.assertTrue(all(c.tokens <= 2500 for c in act.cards), [c.tokens for c in act.cards])
        last = bodies(fake)[-1]
        self.assertIn("V2401 = 2401", last)  # the newest read survives; older ones are dropped first

    def test_at_most_eight_reads_are_kept(self):
        root = py_repo(self)
        big(root, n=1200)
        asks = [f"NEED file big.py lines {a}-{a + 1}\n" for a in range(100, 1100, 100)]  # 10 small reads
        res, fake, act, _ = run(self, root, asks + ["BLOCKED stop\n"], max_turns=11, cap=20000)
        last = bodies(fake)[-1]
        self.assertNotIn("V100 = 100", last)
        self.assertNotIn("V200 = 200", last)
        for a in range(300, 1100, 100):
            self.assertIn(f"V{a} = {a}", last)


class Outline(unittest.TestCase):
    def test_a_long_file_without_lines_answers_with_its_outline(self):
        root = py_repo(self)
        body = []
        for k in range(40):
            body.append(f"def fn_{k}():\n")
            body += ["    x = 0\n"] * 49
        body.append("class Box:\n    def open_lid(self):\n        return 1\n")
        (root / "big2.py").write_text("".join(body))
        res, fake, act, _ = run(self, root, ["NEED file big2.py\n", "BLOCKED stop\n"], max_turns=2)
        card = bodies(fake)[1]
        for k, ln in ((0, 1), (17, 851), (37, 1851), (39, 1951)):
            self.assertTrue(any(f"fn_{k}" in x and re.search(rf"(?<!\d){ln}(?!\d)", x) for x in card.splitlines()),
                            f"no outline line with fn_{k} and line {ln}")
        self.assertTrue(any("Box" in x and "2001" in x for x in card.splitlines()))
        self.assertTrue(any("open_lid" in x and "2002" in x for x in card.splitlines()))
        self.assertIn("lines a-b", card)
        self.assertNotIn("x = 0\n" * 20, card)  # not a dump of the first 150 lines

    def test_a_long_file_without_defs_keeps_the_first_lines(self):
        root = py_repo(self)
        big(root, n=400)
        res, fake, act, _ = run(self, root, ["NEED file big.py\n", "BLOCKED stop\n"], max_turns=2)
        self.assertIn("V1 = 1", bodies(fake)[1])


class Trace(unittest.TestCase):
    def test_act1_carries_one_trace_row_per_turn(self):
        root = py_repo(self)
        res, fake, act, _ = run(self, root, ["NEED file calc.py lines 1-2\n", FIX])
        self.assertEqual((res.status, res.turns), ("done", 2))
        tr = res.to_dict()["trace"]
        self.assertEqual([t["turn"] for t in tr], [1, 2])
        self.assertEqual(tr[0]["actions"], ["NEED file calc.py lines 1-2"])
        self.assertEqual(tr[1]["actions"], ["EDIT calc.py"])
        self.assertEqual((tr[1]["applied"], tr[1]["rejected"]), (1, 0))
        for t in tr:
            self.assertIsInstance(t["card_tokens"], int)
            self.assertIsInstance(t["dropped"], list)
            self.assertTrue(all(len(a) <= 120 for a in t["actions"]))

    def test_a_rejected_block_is_counted(self):
        root = py_repo(self)
        bad = "EDIT calc.py\n<<<<<<< SEARCH\n    return a / b\n=======\n    return 0\n>>>>>>> REPLACE\n"
        res, fake, act, _ = run(self, root, [bad, FIX])
        tr = res.to_dict()["trace"]
        self.assertEqual((tr[0]["applied"], tr[0]["rejected"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
