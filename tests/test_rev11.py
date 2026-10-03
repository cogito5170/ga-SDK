"""CMD-GA13: METHOD rev 11 (BD-148).

- a Judge's ask_user is a gate only when it names one of the seven §6 gates ("gate": 1..7); otherwise it is a note
  and the round's next is wait. A ground always stops — there is no switch that turns a gate off.
- an open outside verdict (review/1) followed by wait, with no directive taking it up, leaves a soft notice; the
  verdict is not changed.
"""
import json
import unittest

from ga.adapters.llm_judge import LLMJudge
from ga.gates import ask_basis, detect

from test_headless import Stub
from test_llm_judge import GOOD, ctx
from world import World, directive, proposal


def ask(gate=None, cls="partial", cause="requirement"):
    p = proposal(cls, "ask_user", "사람이 정할 일", cause=cause)
    if gate is not None:
        p["gate"] = gate
    return p


class AskBasisTest(unittest.TestCase):
    def test_basis_is_one_of_the_seven(self):
        for g, want in ((1, 1), (5, 5), (7, 7), (0, None), (8, None), ("5", None), (True, None), (None, None), (5.0, None)):
            with self.subTest(g=g):
                self.assertEqual(ask_basis({"gate": g}), want)
        self.assertEqual(ask_basis(None), None)

    def test_detect(self):
        w = World()
        self.addCleanup(w.close)
        self.assertEqual([g.number for g in detect(w.cfg, proposal={"next": "ask_user", "gate": 4, "reason": "x"})], [4])
        self.assertEqual(detect(w.cfg, proposal={"next": "ask_user", "reason": "x"}), [])
        self.assertEqual([g.number for g in detect(w.cfg, proposal={"action": "choose_design", "reason": "x"})], [5])


class AskUserInTheHubTest(unittest.TestCase):
    def round(self, prop):
        w = World(judge_fn=lambda ctx: prop)
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        w.paste("A")
        sha = w.work("A", "alpha", {"x.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        res = w.hub.tick()
        rec = json.loads((w.ga / "records" / "rounds" / f"round-{res.round:04d}.json").read_text(encoding="utf-8"))
        return w, res, rec

    def test_with_a_ground_it_stops(self):
        for g in (2, 5, 6):
            with self.subTest(g=g):
                w, res, rec = self.round(ask(g))
                self.assertEqual([q["gate"] for q in res.gates], [g])
                self.assertEqual(rec["next"], "ask_user")
                self.assertEqual(len(w.hub.load_state()["questions"]), 1)

    def test_without_a_ground_it_is_a_note(self):
        for g in (None, 0, 9, "5"):
            with self.subTest(g=g):
                w, res, rec = self.round(ask(g))
                self.assertEqual(res.gates, [])
                self.assertEqual(w.hub.load_state()["questions"], {})
                self.assertEqual(rec["next"], "wait")
                self.assertTrue(any("names no §6 gate" in n for n in rec["notices"]))
                self.assertEqual(res.verdict["next"]["choice"], "ask_user")  # the Judge's own proposal stays on record

    def test_r4_round_with_a_groundless_ask_user_asks_nothing(self):
        # GA10 round 2: W2 not a fast-forward; the Judge said blocked · ask_user without a ground
        w = World(judge_fn=lambda ctx: ask(None, "blocked", "dependency"), shared_alpha=True)
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        w.hub.send(directive("CMD-B1", "B"))
        w.paste("A"), w.paste("B")
        a = w.work("A", "alpha", {"one.txt": "a\n"})
        b = w.work("B", "alpha", {"B_OWNS.txt": "b\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", a)])
        w.report("B", [("CMD-B1", 1, "done")], [("alpha", b)])
        res = w.hub.tick()
        self.assertEqual((res.gates, res.sent), ([], ["CMD-B1"]))


class JudgeGateFieldTest(unittest.TestCase):
    def test_the_llm_judge_passes_a_valid_ground_only(self):
        for g, want in ((5, 5), (9, None), ("5", None), (None, None)):
            with self.subTest(g=g):
                stub = Stub([{"do": "reply", "text": json.dumps(dict(GOOD, next={"choice": "ask_user", "reason": "x"}, gate=g))}])
                self.addCleanup(stub.close)
                j = LLMJudge(stub.dir / "jhome", executable=stub.exe, timeout=20, extra_env={"GA_STUB_DIR": str(stub.dir)})
                out = j.propose(ctx())
                self.assertEqual(out.get("gate"), want)

    def test_the_prompt_lists_the_seven(self):
        from ga.adapters.llm_judge import SYSTEM
        self.assertIn('"gate": null', SYSTEM)
        for n in range(1, 8):
            self.assertIn(f"{n} ", SYSTEM)


class WaitAfterReviewTest(unittest.TestCase):
    def world(self, second):
        w = World(judge_fn=self._judge)
        self.addCleanup(w.close)
        self.plan = [proposal("success", "wait", "좋다"), second]
        w.hub.send(directive("CMD-A1", "A"))
        w.paste("A")
        sha = w.work("A", "alpha", {"x.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        w.hub.tick()
        w.hub.review("baseline", "alpha", sha, "partial", "깨끗한 설치 결함", cause="implementation")
        res = w.hub.tick()
        rec = json.loads((w.ga / "records" / "rounds" / f"round-{res.round:04d}.json").read_text(encoding="utf-8"))
        return w, res, rec

    def _judge(self, ctx):
        return self.plan.pop(0)

    def test_wait_after_an_open_review_is_noted_and_the_verdict_stays(self):
        w, res, rec = self.world(proposal("success", "wait", "더 할 일 없음"))
        self.assertEqual((res.verdict["class"], res.verdict["cause"]), ("partial", "implementation"))  # the review's floor, unchanged
        self.assertEqual(res.sent, [])
        self.assertTrue(any("is followed by wait and no directive takes it up" in n for n in rec["notices"]))
        self.assertEqual(res.gates, [])

    def test_refine_takes_it_up_and_no_notice(self):
        w, res, rec = self.world(proposal("success", "refine", "고친다"))
        self.assertEqual(res.sent, ["CMD-A1"])
        self.assertFalse(any("followed by wait" in n for n in rec["notices"]))

    def test_an_open_directive_to_that_session_takes_it_up(self):
        w, res, rec = None, None, None
        w = World(judge_fn=self._judge)
        self.addCleanup(w.close)
        self.plan = [proposal("success", "wait", "좋다"), proposal("success", "wait", "기다림")]
        w.hub.send(directive("CMD-A1", "A"))
        sha = w.work("A", "alpha", {"x.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        w.hub.tick()
        w.hub.send(directive("CMD-A2", "A"))  # already sent: the session has open work
        w.hub.review("baseline", "alpha", sha, "partial", "결함", cause="implementation")
        res = w.hub.tick()
        rec = json.loads((w.ga / "records" / "rounds" / f"round-{res.round:04d}.json").read_text(encoding="utf-8"))
        self.assertFalse(any("followed by wait" in n for n in rec["notices"]))


if __name__ == "__main__":
    unittest.main()
