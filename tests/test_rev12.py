"""CMD-GA14: METHOD rev 12 (BD-151) — a round whose Judge itself failed.

- one failure: the round is judged on the machine's class, no gate; the next tick calls the Judge once more on that
  round's evidence (within the budget)
- two rounds in a row: gate 6 (a person has to know)
- a success in between starts the count again
"""
import json
import tempfile
import unittest

from ga.adapters.llm_judge import LLMJudge

from test_headless import Stub
from test_llm_judge import ctx
from world import World, directive, proposal

FALLBACK = LLMJudge(tempfile.gettempdir())  # only its fallback() is used: the exact shape a failed call returns


def failed(why="exit 1", cost=0.01):
    def judge(c):
        return dict(FALLBACK.fallback(c, why), judge={"round": c.round, "error": why, "cost": cost, "seconds": 1.0, "valid": False})
    return judge


def ok(c):
    return dict(proposal("success", "wait", "됐다"), judge={"round": c.round, "error": "", "cost": 0.01, "seconds": 1.0, "valid": True})


class JudgeFailureTest(unittest.TestCase):
    def world(self, *plan, budget=None):
        self.plan, self.calls = list(plan), []

        def judge(c):
            self.calls.append(c.round)
            return self.plan.pop(0)(c)
        w = World(judge_fn=judge, budget=budget)
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        w.paste("A")
        return w

    def report(self, w, name="x.txt"):
        sha = w.work("A", "alpha", {name: "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        return sha

    def notices(self, w, n):
        return json.loads((w.ga / "records" / "rounds" / f"round-{n:04d}.json").read_text(encoding="utf-8"))["notices"]

    def test_one_failure_is_judged_by_the_machine_and_retried_next_tick(self):
        w = self.world(failed(), ok)
        sha = self.report(w)
        r1 = w.hub.tick()
        self.assertEqual(r1.integrated, {"alpha": sha})
        self.assertEqual(r1.gates, [])
        self.assertEqual((r1.verdict["class"], r1.verdict.get("cause")), ("insufficient", "measurement"))  # no floor: the machine cannot call it a success
        self.assertTrue(any("the Judge failed (exit 1)" in n for n in self.notices(w, 1)))
        st = w.hub.load_state()
        self.assertEqual((st["judge_failures"], st["judge_retry"]["round"]), (1, 1))
        r2 = w.hub.tick()  # nothing new: the retry alone makes a round
        self.assertFalse(r2.quiet)
        self.assertEqual(self.calls, [1, 2])
        self.assertEqual((r2.verdict["class"], r2.gates), ("success", []))
        self.assertTrue(any("retry of round 1" in n for n in self.notices(w, 2)))
        st = w.hub.load_state()
        self.assertEqual((st["judge_failures"], st["judge_retry"]), (0, None))
        self.assertTrue(w.hub.tick().quiet)

    def test_two_rounds_in_a_row_stop_at_gate_6(self):
        w = self.world(failed("exit 1"), failed("bad_json"))
        self.report(w)
        self.assertEqual(w.hub.tick().gates, [])
        r2 = w.hub.tick()
        self.assertEqual([q["gate"] for q in r2.gates], [6])
        self.assertIn("failed 2 rounds in a row", r2.gates[0]["about"])
        st = w.hub.load_state()
        self.assertEqual((st["judge_failures"], st["judge_retry"]), (2, None))
        self.assertTrue(w.hub.tick().quiet)  # stopped: no third call
        self.assertEqual(self.calls, [1, 2])

    def test_a_success_in_between_starts_the_count_again(self):
        w = self.world(failed(), ok, failed())
        self.report(w)
        w.hub.tick()   # fail 1
        w.hub.tick()   # retry succeeds
        self.report(w, "y.txt")
        r3 = w.hub.tick()  # a new round fails: one in a row again
        self.assertEqual(r3.gates, [])
        self.assertEqual(w.hub.load_state()["judge_failures"], 1)

    def test_new_work_during_a_retry_is_the_retry(self):
        w = self.world(failed(), ok)
        self.report(w)
        w.hub.tick()
        sha = self.report(w, "y.txt")
        r2 = w.hub.tick()  # a normal round with the new report: its Judge call is the second attempt
        self.assertEqual((r2.integrated, r2.verdict["class"]), ({"alpha": sha}, "success"))
        self.assertFalse(any("retry of round" in n for n in self.notices(w, 2)))
        self.assertEqual(w.hub.load_state()["judge_retry"], None)

    def test_the_retry_does_not_ask_the_failed_rounds_questions_again(self):
        w = self.world(failed(), ok)
        sha = w.work("A", "alpha", {"x.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)], needs=["new_repo"])
        r1 = w.hub.tick()
        self.assertEqual([q["gate"] for q in r1.gates], [7])  # asked once, in the round that failed
        r2 = w.hub.tick()
        self.assertEqual(r2.gates, [])
        self.assertEqual(sorted(w.hub.load_state()["questions"]), ["Q-1-1"])

    def test_the_retry_stays_within_the_judge_budget(self):
        w = self.world(failed(), ok, budget={"runs": 50, "judge_runs": 1})
        self.report(w)
        w.hub.tick()
        self.assertEqual(w.hub.load_state()["spent"]["judge_runs"], 1)
        r2 = w.hub.tick()
        self.assertEqual(self.calls, [1])  # not called again: the budget is spent
        self.assertEqual([q["gate"] for q in r2.gates], [6])
        self.assertIn("judge budget exhausted", r2.gates[0]["about"])


class FallbackMarkTest(unittest.TestCase):
    def test_a_real_failed_call_is_marked(self):
        stub = Stub([{"do": "exit", "code": 1}])
        self.addCleanup(stub.close)
        j = LLMJudge(stub.dir / "jhome", executable=stub.exe, timeout=20, extra_env={"GA_STUB_DIR": str(stub.dir)})
        out = j.propose(ctx("partial"))
        self.assertEqual(out["judge_failed"], "exit 1")
        self.assertEqual(out["verdict"]["class"], "partial")
        j2 = LLMJudge(stub.dir / "j2", max_runs=0)
        self.assertEqual(j2.propose(ctx())["judge_failed"], "budget: judge runs exhausted")


if __name__ == "__main__":
    unittest.main()
