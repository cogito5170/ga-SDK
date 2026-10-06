"""DEV-R0b: the router's ledger start can step back down when cheaper rungs keep winning, within the ladder's bounds.
Pure ledger rows, no model calls."""
import tempfile
import unittest
from pathlib import Path

from ga.act import route as RT

LADDER = ["gpt-oss-120b-medium", "gemini-3.6-flash-low", "gemini-3.1-pro-high", "claude-sonnet-5-5-high"]
LO, MID, PRO, HI = LADDER


def win(rung, b="b1", ok=True):
    return {"bucket": b, "success": ok, "rungs": [rung], "route": {"difficulty": 2}, "turns_last": 2}


def start(rows, **kw):
    r = RT.from_ledger(rows, "b1", LADDER, **kw)
    return r and r["start"]


class StepDown(unittest.TestCase):
    def test_recent_wins_on_a_cheaper_rung_start_there(self):
        rows = [win(HI), win(HI)] + [win(MID)] * 5  # it used to need the top; the last five won on MID
        self.assertEqual(start(rows), MID)

    def test_an_old_expensive_win_inside_the_window_still_holds_the_start(self):
        self.assertEqual(start([win(HI)] + [win(MID)] * 4), HI)  # window 5 covers it
        self.assertEqual(start([win(HI)] + [win(MID)] * 4, window=3), MID)  # window is a config value

    def test_a_recent_dearer_win_pulls_the_start_back_up(self):
        self.assertEqual(start([win(MID)] * 5 + [win(PRO)]), PRO)

    def test_never_below_the_lowest_rung(self):
        self.assertEqual(start([win(LO)] * 6), LO)
        self.assertEqual(LADDER.index(start([win(LO)] * 6)), 0)

    def test_never_above_the_highest_rung(self):
        self.assertEqual(start([win(HI)] * 6), HI)
        # a win on a model the ladder no longer lists is ignored, never a crash or an out-of-ladder start
        rows = [win("retired-model")] * 3 + [win(MID)] * 3
        self.assertEqual(start(rows), MID)
        self.assertIn(start(rows), LADDER)

    def test_too_few_wins_no_ledger_route(self):
        self.assertIsNone(start([win(MID)] * 2))
        self.assertIsNone(start([win(MID)] * 2 + [win(HI, ok=False)] * 5))
        self.assertEqual(start([win(MID)] * 2, minimum=2), MID)

    def test_other_buckets_do_not_vote(self):
        self.assertEqual(start([win(HI, b="b2")] * 5 + [win(MID)] * 3), MID)

    def test_bad_config_values_fall_back_to_the_defaults(self):
        self.assertEqual(RT._count(2, 5), 2)
        for bad in (0, -1, True, "3", None, 1.5):
            self.assertEqual(RT._count(bad, 5), 5)

    def test_default_window_is_documented(self):
        self.assertEqual((RT.LEDGER_MIN, RT.LEDGER_WINDOW), (3, 5))
        self.assertIn("route_window", RT.__doc__)


if __name__ == "__main__":
    unittest.main()
