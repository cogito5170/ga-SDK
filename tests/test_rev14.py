"""CMD-GA16: METHOD rev 14 §4c 6 (BD-159) — the LLM Judge's calls need the same permission (measurement_calls).

- not permitted: the Judge is not called; the round is judged on the machine's class with the note "Judge 허락 없음";
  gate 6 asks once (not again while that question is open); no rev 12 retry, no failure count
- a Judge that calls no model needs nothing; a manual-Runner hub can permit the Judge alone (`ga permit --judge-only`)
"""
import contextlib
import io
import json
import unittest

from ga.__main__ import main
from ga.adapters.human import CallableJudge, FileJudge

from world import World, directive, proposal


class ModelJudge:
    """An LLM Judge stand-in: it calls a model (so it needs the permission) and counts its calls."""

    calls_model = True
    model = "haiku"

    def __init__(self, *plan):
        self.plan, self.calls = list(plan), 0

    def propose(self, ctx):
        self.calls += 1
        step = self.plan.pop(0) if self.plan else "ok"
        if step == "fail":
            return {"verdict": {"schema": "verdict/1", "class": ctx.machine_class or "insufficient", "cause": "measurement",
                                "evidence": ctx.evidence, "claims_vs_evidence": [], "next": {"choice": "ask_user", "reason": "x"}},
                    "summary": "실패", "directive": None, "judge_failed": "exit 1",
                    "judge": {"round": ctx.round, "error": "exit 1", "cost": 0.0, "seconds": 1.0, "valid": False}}
        return dict(proposal("success", "wait", "됐다"), judge={"round": ctx.round, "error": "", "cost": 0.01, "seconds": 1.0, "valid": True})


def world(judge):
    w = World()
    w.hub.judge = judge
    return w


def round_with_report(w, name="x.txt"):
    sha = w.work("A", "alpha", {name: "1\n"})
    w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
    return w.hub.tick()


class JudgePermissionTest(unittest.TestCase):
    def setUp(self):
        self.j = ModelJudge()
        self.w = world(self.j)
        self.addCleanup(self.w.close)
        self.w.hub.send(directive("CMD-A1", "A"))

    def test_no_permission_no_call(self):
        res = round_with_report(self.w)
        self.assertEqual(self.j.calls, 0)
        self.assertTrue(res.integrated)
        self.assertEqual((res.verdict["class"], res.verdict.get("cause")), ("insufficient", "measurement"))  # the machine's
        self.assertTrue(any(n.startswith("Judge 허락 없음") for n in res.verdict["evidence"]["notes"]))
        self.assertEqual([q["gate"] for q in res.gates], [6])
        self.assertEqual([o["label"] for o in res.gates[0]["options"]], ["허락한다", "멈춘다"])

    def test_measurement_calls_false_is_no_permission(self):
        d = self.w.hub.permit("headless", measurement_calls=False)
        self.w.cfg.runner["permission"] = d["id"]
        round_with_report(self.w)
        self.assertEqual(self.j.calls, 0)

    def test_a_hub_decision_does_not_permit(self):
        self.w.hub.records.put({"schema": "decision/1", "id": "BD-9", "date": "2026-10-03", "by": "hub", "supersedes": [],
                                "decision": "허브가 스스로", "basis": "허브", "scope": {"runner": "manual", "measurement_calls": True}})
        self.w.cfg.runner["permission"] = "BD-9"
        res = round_with_report(self.w)
        self.assertEqual(self.j.calls, 0)
        self.assertTrue(any("BD-9 is not a decision of the user" in n for n in res.verdict["evidence"]["notes"]))

    def test_the_real_llm_judge_is_held_back_too(self):
        from ga.adapters.llm_judge import LLMJudge
        from test_headless import Stub
        stub = Stub([{"do": "reply", "text": "{}"}])
        self.addCleanup(stub.close)
        self.w.hub.judge = LLMJudge(stub.dir / "jhome", executable=stub.exe, model="haiku", extra_env={"GA_STUB_DIR": str(stub.dir)})
        round_with_report(self.w)
        self.assertEqual(stub.calls(), [])  # the CLI was never started

    def test_permitted_calls(self):
        d = self.w.hub.permit("manual")  # the Judge alone (manual Runner hub)
        self.w.cfg.runner["permission"] = d["id"]
        res = round_with_report(self.w)
        self.assertEqual((self.j.calls, res.verdict["class"], res.gates), (1, "success", []))

    def test_a_judge_runs_budget_in_the_permission(self):
        d = self.w.hub.permit("manual", budget={"judge_runs": 1})
        self.w.cfg.runner["permission"] = d["id"]
        round_with_report(self.w)
        round_with_report(self.w, "y.txt")
        self.assertEqual(self.j.calls, 1)

    def test_asked_once_quiet_after_and_called_once_permitted(self):
        w, j = self.w, self.j
        r1 = round_with_report(w)
        self.assertEqual(len(r1.gates), 1)
        r2 = round_with_report(w, "y.txt")  # still not permitted: no second question while the first is open
        self.assertEqual((r2.gates, j.calls), ([], 0))
        self.assertTrue(w.hub.tick().quiet)
        qs = w.hub.load_state()["questions"]
        self.assertEqual(len(qs), 1)
        (qid, _), = qs.items()
        w.hub.answer(qid, "허락한다")
        self.assertTrue(w.hub.load_state()["questions"][qid]["processed"])  # no Judge round for the answer itself
        self.assertTrue(w.hub.tick().quiet)
        d = w.hub.permit("manual")
        w.cfg.runner["permission"] = d["id"]
        r3 = round_with_report(w, "z.txt")
        self.assertEqual((j.calls, r3.verdict["class"]), (1, "success"))

    def test_not_permitted_leaves_the_rev12_count_alone(self):
        w, j = self.w, self.j
        st = w.hub.load_state()
        round_with_report(w)
        st = w.hub.load_state()
        self.assertEqual((st.get("judge_failures", 0), st.get("judge_retry")), (0, None))
        # a failure while permitted, then the permission goes away: the retry is used up, the count stays
        j.plan = ["fail"]
        d = w.hub.permit("manual")
        w.cfg.runner["permission"] = d["id"]
        round_with_report(w, "y.txt")
        self.assertEqual((w.hub.load_state()["judge_failures"], j.calls), (1, 1))
        del w.cfg.runner["permission"]
        r = w.hub.tick()  # the retry round: not called (not permitted)
        st = w.hub.load_state()
        self.assertEqual((j.calls, st["judge_failures"], st["judge_retry"]), (1, 1, None))
        self.assertFalse(any(q["gate"] == 6 and "in a row" in q["doc"]["about"] for q in st["questions"].values()))
        self.assertTrue(w.hub.tick().quiet)


class NoModelJudgesTest(unittest.TestCase):
    def test_callable_and_file_judges_need_nothing(self):
        w = World()  # CallableJudge by default
        self.addCleanup(w.close)
        self.assertEqual(w.hub._judge_permission_gap(w.hub.load_state()), "")
        w.hub.judge = FileJudge(w.ga / "judge")
        self.assertEqual(w.hub._judge_permission_gap(w.hub.load_state()), "")
        w.hub.judge = CallableJudge(lambda c: proposal("success", "wait", "x"))
        w.hub.send(directive("CMD-A1", "A"))
        self.assertEqual(round_with_report(w).verdict["class"], "success")

    def test_a_judge_only_permission_does_not_open_a_model_runner(self):
        from test_rev13 import ModelRunner
        w = World()
        self.addCleanup(w.close)
        w.hub.runner = ModelRunner()
        d = w.hub.permit("manual")
        w.cfg.runner["permission"] = d["id"]
        post, _, gates = w.hub.send(directive("CMD-A1", "A"))
        self.assertIsNone(post)
        self.assertIn("permits the manual Runner, not agent_sdk", gates[0].reason)


class PermitCliTest(unittest.TestCase):
    def run_cli(self, w, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(w.tmp / "ga.json"), "--ga-dir", str(w.ga), "permit", *args])
        return code, out.getvalue().strip(), err.getvalue()

    def test_judge_only(self):
        w = World()
        self.addCleanup(w.close)
        code, bd, _ = self.run_cli(w, "--judge-only", "--note", "사용자가 Judge 만 허락한다고 답함")
        self.assertEqual(code, 0)
        d = json.loads((w.ga / "records" / "decisions" / f"BD-{int(bd[3:]):04d}.json").read_text(encoding="utf-8"))
        self.assertEqual((d["by"], d["scope"]), ("user", {"runner": "manual", "measurement_calls": True}))
        self.assertIn("사용자가 Judge 만 허락한다고 답함", d["basis"])
        self.assertEqual(self.run_cli(w)[0], 2)  # neither --runner nor --judge-only


if __name__ == "__main__":
    unittest.main()
