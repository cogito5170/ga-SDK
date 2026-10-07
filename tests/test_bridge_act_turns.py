"""CMD-ACTB1 (baseline acceptance test, may not be edited): the bridge lets each directive set its ga act turn cap and
mails the per-turn trace back, so baseline can see why a run failed without rebuilding it.

Rules (policy spec_split.no_code_from_baseline: the worker designs the code; this docstring is the spec):
B1 The ```ga-act block may carry ``max_turns``: an int from 1 to 30 (bool and str are not ints). Anything else makes
   ``item_spec`` raise ValueError. When given, ``act_argv`` passes it as ``--max-turns``.
B2 Without it, ``--max-turns`` is the bridge config's ``act.max_turns`` when set, else 20 (was 10).
B3 ``report`` adds a section ``## turns`` after ``## ga act result`` when the act/1 result carries ``trace``
   (CMD-ACTR1): one line per trace row,
       t<turn> card <card_tokens> applied <applied> rejected <rejected>: <actions joined by "; ">
   followed by " dropped <n>" when the row's ``dropped`` list is not empty. The section is cut to 4000 characters.
   Without ``trace`` there is no such section. The report head (report/2) is unchanged.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ga.bridge import act as ACT  # noqa: E402
from test_vm_bridge_act import DIRECTIVE, SPEC, form  # noqa: E402

CFG = {"name": "AGY", "act": {"repo": "/x", "backend": "agv", "model": "m", "options": {"agent": "ga-act"}}}


def argv_turns(cfg, spec):
    argv = ACT.act_argv(cfg, spec, Path("/wt"), Path("/tmp/x"), "m")
    return argv[argv.index("--max-turns") + 1]


class MaxTurns(unittest.TestCase):
    def test_the_block_may_set_max_turns(self):
        spec = ACT.item_spec(form(DIRECTIVE, {**SPEC, "max_turns": 25}))
        self.assertEqual(spec["max_turns"], 25)
        self.assertEqual(argv_turns(CFG, spec), "25")
        self.assertEqual(argv_turns(CFG, {**SPEC, "max_turns": 1}), "1")
        self.assertEqual(argv_turns(CFG, {**SPEC, "max_turns": 30}), "30")

    def test_bad_max_turns_is_refused(self):
        for bad in (0, 31, -1, "20", True, 2.5, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ACT.item_spec(form(DIRECTIVE, {**SPEC, "max_turns": bad}))

    def test_default_is_config_else_20(self):
        self.assertEqual(argv_turns(CFG, SPEC), "20")
        cfg = {**CFG, "act": {**CFG["act"], "max_turns": 12}}
        self.assertEqual(argv_turns(cfg, SPEC), "12")


class TurnsSection(unittest.TestCase):
    def body(self, trace=None):
        res = {"schema": "act/1", "id": "CMD-VA1", "status": "blocked", "reason": "turn cap (2)", "turns": 2,
               "failing": [], "tokens": {"input": 10, "total": 20}, "changed": []}
        if trace is not None:
            res["trace"] = trace
        return ACT.report(CFG, DIRECTIVE, {"code": 1, "out": "ga act output", "result": res}, "", "m")

    def test_trace_becomes_a_turns_section(self):
        tr = [{"turn": 1, "card_tokens": 2100, "actions": ["NEED file ga/hub.py lines 1700-1790", "NEED grep _SAME"],
               "applied": 0, "rejected": 0, "dropped": []},
              {"turn": 2, "card_tokens": 5400, "actions": ["EDIT ga/hub.py"], "applied": 0, "rejected": 1,
               "dropped": ["code:a", "code:b"]}]
        b = self.body(tr)
        self.assertIn("## turns", b)
        self.assertLess(b.index("## ga act result"), b.index("## turns"))
        self.assertIn("t1 card 2100 applied 0 rejected 0: NEED file ga/hub.py lines 1700-1790; NEED grep _SAME", b)
        self.assertIn("t2 card 5400 applied 0 rejected 1: EDIT ga/hub.py dropped 2", b)
        head = json.loads(b.split("```ga\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(head["schema"], "report/2")
        self.assertEqual(head, json.loads(self.body().split("```ga\n", 1)[1].split("\n```", 1)[0]))

    def test_no_trace_no_section(self):
        self.assertNotIn("## turns", self.body())

    def test_the_section_is_capped(self):
        tr = [{"turn": k, "card_tokens": 5000, "actions": ["NEED grep " + "y" * 100] * 6, "applied": 0,
               "rejected": 0, "dropped": []} for k in range(1, 40)]
        b = self.body(tr)
        sec = b.split("## turns", 1)[1]
        sec = sec.split("\n## ", 1)[0]
        self.assertLessEqual(len(sec), 4000 + 200)  # 4000 of lines plus the fence and headings


if __name__ == "__main__":
    unittest.main()
