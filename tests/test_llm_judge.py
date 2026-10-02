"""CMD-GA3: the LLM Judge against a stub ``claude``: every path, then inside the hub (machine floor, gates)."""
import json
import os
import unittest

from ga.adapters.base import JudgeContext
from ga.adapters.llm_judge import LLMJudge

from test_headless import Stub
from world import World, directive

GOOD = {"class": "success", "subclass": None, "cause": None, "next": {"choice": "wait", "reason": "끝남"},
        "summary": "됐다", "claims_vs_evidence": [], "directive": None}


def ctx(machine=None):
    return JudgeContext(round=7, reports=[{"head": {"schema": "report/1", "from": "A", "handled": []}, "body": "## Result\n했다\n"}],
                        evidence={"heads": {"alpha": "a" * 40}, "tests": {}, "notes": []}, machine_class=machine,
                        findings=[], open_directives=[])


class JudgePathsTest(unittest.TestCase):
    def judge(self, plan, **kw):
        stub = Stub(plan)
        self.addCleanup(stub.close)
        kw.setdefault("timeout", 20)
        return LLMJudge(stub.dir / "jhome", executable=stub.exe, model="haiku", extra_env={"GA_STUB_DIR": str(stub.dir)}, **kw), stub

    def test_valid_reply(self):
        j, stub = self.judge([{"do": "reply", "text": json.dumps(GOOD), "cost": 0.004}], use_json_schema=True)
        out = j.propose(ctx())
        self.assertEqual(out["verdict"]["class"], "success")
        self.assertEqual(out["verdict"]["next"]["choice"], "wait")
        self.assertEqual((out["judge"]["valid"], out["judge"]["cost"]), (True, 0.004))
        call = stub.calls()[0]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--system-prompt", argv)
        self.assertIn("--json-schema", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertIn('"machine_class": null', call["stdin_head"] + "")  # context goes in on stdin
        self.assertFalse([k for k in call["env"] if k.startswith("CLAUDE_CODE_")])

    def test_fenced_reply_and_structured_output(self):
        j, _ = self.judge([{"do": "reply", "text": "판정:\n```json\n" + json.dumps(dict(GOOD, **{"class": "partial", "cause": "hub_directive"})) + "\n```"},
                           {"do": "reply", "text": "", "structured": dict(GOOD, **{"class": "failure", "cause": "implementation"})}])
        self.assertEqual(j.propose(ctx())["verdict"]["cause"], "hub_directive")
        self.assertEqual(j.propose(ctx())["verdict"]["class"], "failure")

    def test_bad_replies_fall_back_to_asking(self):
        plans = [
            ({"do": "reply", "text": "잘 모르겠다"}, "not JSON"),
            ({"do": "reply", "text": json.dumps(dict(GOOD, **{"class": "great"}))}, "verdict/1"),
            ({"do": "reply", "text": json.dumps(dict(GOOD, **{"class": "failure"}))}, "verdict/1"),  # no cause
            ({"do": "exit", "code": 1}, "exit 1"),
            ({"do": "is_error"}, "is_error"),
            ({"do": "badjson"}, "bad_json"),
        ]
        for step, why in plans:
            with self.subTest(why=why):
                j, _ = self.judge([step])
                out = j.propose(ctx(machine="blocked"))
                self.assertEqual(out["verdict"]["next"]["choice"], "ask_user")
                self.assertEqual(out["verdict"]["class"], "blocked")  # the machine's class, never better
                self.assertIn(why, out["verdict"]["next"]["reason"])
        j, _ = self.judge([{"do": "sleep", "seconds": 30}], timeout=1)
        self.assertIn("timeout", j.propose(ctx())["verdict"]["next"]["reason"])

    def test_directive_draft_checked(self):
        good = dict(GOOD, directive={"id": "CMD-A2", "rev": 1, "to": "A", "goal": "g", "why": "w", "scope": "s", "done_when": "d"},
                    next={"choice": "continue", "reason": "다음"})
        bad = dict(GOOD, directive={"id": "A2", "to": "A"})
        j, _ = self.judge([{"do": "reply", "text": json.dumps(good)}, {"do": "reply", "text": json.dumps(bad)}])
        self.assertEqual(j.propose(ctx())["directive"]["id"], "CMD-A2")
        out = j.propose(ctx())
        self.assertIsNone(out["directive"])
        self.assertIn("directive_dropped", out["judge"])

    def test_budget_exhausted_makes_no_call(self):
        j, stub = self.judge([{"do": "reply", "text": json.dumps(GOOD)}], max_runs=1)
        j.propose(ctx())
        out = j.propose(ctx())
        self.assertIn("budget", out["verdict"]["next"]["reason"])
        self.assertEqual(len(stub.calls()), 1)


class JudgeInHubTest(unittest.TestCase):
    def world(self, replies):
        stub = Stub(replies)
        self.addCleanup(stub.close)
        w = World()
        self.addCleanup(w.close)
        w.hub.judge = LLMJudge(stub.dir / "jhome", executable=stub.exe, model="haiku", extra_env={"GA_STUB_DIR": str(stub.dir)})
        w.hub.send(directive("CMD-A1", "A"))
        w.paste("A")
        return w, stub

    def test_machine_floor_wins_over_a_lenient_judge(self):
        w, _ = self.world([{"do": "reply", "text": json.dumps(GOOD), "cost": 0.003}])
        sha = w.work("A", "alpha", {"B_OWNS.txt": "x\n"})  # R2: not A's file
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        res = w.hub.tick()
        self.assertEqual(res.verdict["class"], "blocked")
        st = w.hub.load_state()
        self.assertEqual(st["judge_calls"][0]["cost"], 0.003)
        self.assertEqual(st["spent"]["judge_runs"], 1)

    def test_judge_draft_is_sent_and_gates_still_stop(self):
        draft = {"id": "CMD-A2", "rev": 1, "to": "A", "goal": "g", "why": "w", "scope": "s", "done_when": "d"}
        w, _ = self.world([
            {"do": "reply", "text": json.dumps(dict(GOOD, next={"choice": "continue", "reason": "다음"}, directive=draft))},
        ])
        sha = w.work("A", "alpha", {"a.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        self.assertEqual(w.hub.tick().sent, ["CMD-A2"])
        w2, _ = self.world([{"do": "reply", "text": json.dumps(dict(GOOD, next={"choice": "ask_user", "reason": "OQ 값"}))}])
        w2.report("A", [("CMD-A1", 1, "paused")], body="## Request\nOQ-19 값을 정해 달라\n")
        res = w2.hub.tick()
        self.assertEqual([q["gate"] for q in res.gates], [5])
        self.assertEqual(res.sent, [])


if __name__ == "__main__":
    unittest.main()
