"""CMD-GA15: METHOD rev 13 §4c (BD-156) — permission before a Runner opens model turns; a refusal is "not sent".

- no permission · out of scope · permitted (and the manual Runner needs none)
- refused → recorded as not sent, gate 6, not retried, no half-written state
- the person's downgrade answer → the same directive through the manual Runner, recorded as such; the hub never
  downgrades by itself
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from ga import config as gacfg
from ga.__main__ import main, make_runner
from ga.adapters.base import TurnResult
from ga.adapters.headless import HeadlessRunner
from ga.adapters.runner import ManualRunner, RemoteSessionRunner
from ga.forms import FormError
from ga.hub import DOWNGRADE

from world import FakeHeadlessRunner, World, directive, proposal


class ModelRunner:
    """A model-turn Runner stand-in with the attributes a permission is about."""

    def __init__(self, kind="agent_sdk", model="haiku", sandbox="require", result=None, boom=None):
        self.kind, self.model, self.sandbox = kind, model, sandbox
        self.result = result or TurnResult(ended=True, session_id="s1", cost=0.01)
        self.boom = boom
        self.calls = []

    def run_turn(self, req):
        self.calls.append(req)
        if self.boom:
            raise self.boom
        return self.result


def hub_posts(w, session):
    return [p for p in w.mail.read(session) if p.author == "hub"]


class PermissionTest(unittest.TestCase):
    def world(self, runner, permit=True, **scope):
        w = World()
        self.addCleanup(w.close)
        w.hub.runner = w.runner = runner
        w.cfg.runner.pop("permission", None)
        if permit:
            d = w.hub.permit(scope.pop("kind", runner.kind), model=scope.pop("model", getattr(runner, "model", None)),
                             sandbox=scope.pop("sandbox", getattr(runner, "sandbox", None)), **scope)
            w.cfg.runner["permission"] = d["id"]
        return w

    def assert_not_sent(self, w, runner, post, gates, why):
        self.assertIsNone(post)
        self.assertEqual([g.number for g in gates], [6])
        self.assertIn(why, gates[0].reason)
        self.assertEqual(runner.calls, [])
        self.assertEqual(hub_posts(w, "A"), [])  # nothing was written to the channel
        st = w.hub.load_state()
        self.assertEqual(st["directives"]["CMD-A1"]["status"], "not_sent")
        self.assertEqual(st["spent"].get("runs", 0), 0)
        (qid, q), = st["questions"].items()
        self.assertEqual((q["gate"], q["directive"]), (6, "CMD-A1"))
        self.assertTrue((w.ga / "questions" / f"{qid}.md").exists())
        self.assertEqual(q["doc"]["recommendation"], "멈춘다")  # the closing choice; the hub never picks the downgrade

    def test_no_permission(self):
        r = ModelRunner()
        w = self.world(r, permit=False)
        post, _, gates = w.hub.send(directive("CMD-A1", "A"))
        self.assert_not_sent(w, r, post, gates, "no permission for the agent_sdk Runner")

    def test_out_of_scope(self):
        cases = [({"kind": "headless"}, "permits the headless Runner"), ({"model": "sonnet"}, "permits model 'sonnet'"),
                 ({"sandbox": "off"}, "permits sandbox 'off'"), ({"budget": {"runs": 0}}, "permits runs up to 0")]
        for scope, why in cases:
            with self.subTest(scope=scope):
                r = ModelRunner()
                w = self.world(r, **dict(scope))
                post, _, gates = w.hub.send(directive("CMD-A1", "A"))
                self.assert_not_sent(w, r, post, gates, why)

    def test_a_decision_that_is_not_the_users_does_not_permit(self):
        r = ModelRunner()
        w = self.world(r, permit=False)
        w.cfg.runner["permission"] = "BD-77"
        post, _, gates = w.hub.send(directive("CMD-A1", "A"))
        self.assert_not_sent(w, r, post, gates, "BD-77 is not a decision of the user")

    def test_a_hub_decision_does_not_permit_even_with_a_scope(self):
        r = ModelRunner()
        w = self.world(r, permit=False)
        w.hub.records.put({"schema": "decision/1", "id": "BD-5", "date": "2026-10-03", "by": "hub", "supersedes": [],
                           "decision": "허브가 스스로 허락함", "basis": "허브",
                           "scope": {"runner": "agent_sdk", "model": "haiku", "sandbox": "require"}})
        w.cfg.runner["permission"] = "BD-5"
        post, _, gates = w.hub.send(directive("CMD-A1", "A"))
        self.assert_not_sent(w, r, post, gates, "BD-5 is not a decision of the user")

    def test_permitted_and_within_the_budget(self):
        r = ModelRunner()
        w = self.world(r, budget={"runs": 2})
        post, _, gates = w.hub.send(directive("CMD-A1", "A"))
        self.assertIsNotNone(post)
        self.assertEqual((gates, len(r.calls)), ([], 1))
        self.assertEqual(w.hub.load_state()["directives"]["CMD-A1"]["status"], "open")
        w.hub.send(directive("CMD-A2", "A"))
        _, _, gates = w.hub.send(directive("CMD-A3", "A"))  # the permission's own budget: 2 runs
        self.assertIn("permits runs up to 2", gates[0].reason)
        self.assertEqual(len(r.calls), 2)

    def test_the_manual_runner_needs_none(self):
        w = World()
        self.addCleanup(w.close)
        self.assertNotIn("permission", w.cfg.runner)
        post, _, gates = w.hub.send(directive("CMD-A1", "A"))
        self.assertIsNotNone(post)
        self.assertEqual(gates, [])

    def test_a_tick_asks_with_the_same_choices(self):
        r = ModelRunner()
        w = self.world(r, permit=False)
        w.proposals.append(dict(proposal("partial", "refine", "다음", cause="requirement"), directive=directive("CMD-B1", "B")))
        w.mail.post("A", "A", "```ga\n{\"schema\": \"report/1\", \"from\": \"A\", \"handled\": []}\n```\n## Result\n없음\n")
        res = w.hub.tick()
        self.assertEqual(res.sent, [])
        self.assertEqual([q["gate"] for q in res.gates], [6])
        self.assertEqual([o["label"] for o in res.gates[0]["options"]], ["허락한다", DOWNGRADE, "멈춘다"])
        self.assertEqual(w.hub.load_state()["questions"][res.gates[0]["id"]]["directive"], "CMD-B1")


class RefusalTest(unittest.TestCase):
    def world(self, runner):
        w = World()
        self.addCleanup(w.close)
        w.permit_runner(runner)
        return w

    def test_refused_is_not_sent_not_counted_not_retried(self):
        r = ModelRunner(result=TurnResult(ended=True, error="refused:PermissionError"))
        w = self.world(r)
        post, _, gates = w.hub.send(directive("CMD-A1", "A"))
        self.assertIsNone(post)
        self.assertEqual([g.number for g in gates], [6])
        self.assertEqual(len(r.calls), 1)  # tried once, never another way
        st = w.hub.load_state()
        d = st["directives"]["CMD-A1"]
        self.assertEqual((d["status"], d["not_sent"]), ("not_sent", "refused:PermissionError"))
        self.assertEqual(st["spent"].get("runs", 0), 0)
        turn, = st["turns"]
        self.assertEqual((turn["sent"], turn["error"]), (False, "refused:PermissionError"))
        self.assertNotIn("diag", turn)
        # the directive post and the state agree: the post is recorded on the not-sent directive (no half-written state)
        self.assertEqual([p.id for p in hub_posts(w, "A")], [d["post"]])
        (qid, q), = st["questions"].items()
        self.assertEqual([o["label"] for o in q["doc"]["options"]], [DOWNGRADE, "멈춘다"])
        self.assertTrue(w.hub.tick().quiet)  # nothing new: the hub does not send it again
        self.assertEqual(len(r.calls), 1)
        self.assertEqual(list((w.ga / "outbox").glob("A/*.md")) if (w.ga / "outbox").exists() else [], [])  # no self-downgrade

    def test_an_error_after_posting_still_leaves_a_whole_state(self):
        r = ModelRunner(boom=FileNotFoundError("clone path"))
        w = self.world(r)
        with self.assertRaises(FileNotFoundError):
            w.hub.send(directive("CMD-A1", "A"))
        d = w.hub.load_state()["directives"]["CMD-A1"]  # GA10 intervention 1: the post had no state
        self.assertEqual((d["status"], d["not_sent"]), ("not_sent", "error:FileNotFoundError"))

    def test_real_refusals_are_marked(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "claude"
            exe.write_text("#!/bin/sh\necho {}\n")
            os.chmod(exe, 0o644)  # not executable: the OS refuses to start it
            res = HeadlessRunner(Path(d) / "home", executable=str(exe), guard=False, sandbox="off").run_turn(
                __import__("ga.adapters.base", fromlist=["TurnRequest"]).TurnRequest("A", "p", Path(d)))
            self.assertEqual(res.error, "refused:PermissionError")

        def deny(req):
            raise PermissionError("classifier")
        self.assertEqual(RemoteSessionRunner(deny).run_turn(_req()).error, "refused:PermissionError")
        self.assertEqual(RemoteSessionRunner(lambda req: {"refused": "classifier"}).run_turn(_req()).error, "refused:classifier")
        self.assertEqual(RemoteSessionRunner(lambda req: (_ for _ in ()).throw(OSError())).run_turn(_req()).error, "send_failed:OSError")

    def test_a_refused_directive_can_be_sent_again_after_the_fix(self):
        r = ModelRunner(result=TurnResult(ended=True, error="refused:PermissionError"))
        w = self.world(r)
        w.hub.send(directive("CMD-A1", "A"))
        r.result = TurnResult(ended=True, session_id="s1", cost=0.01)
        post, _, gates = w.hub.send(directive("CMD-A1", "A"))  # same id and rev: allowed because it was not sent
        self.assertIsNotNone(post)
        self.assertEqual(w.hub.load_state()["directives"]["CMD-A1"]["status"], "open")


def _req():
    from ga.adapters.base import TurnRequest
    return TurnRequest("A", "p", Path("."))


class DowngradeTest(unittest.TestCase):
    def refused(self):
        r = ModelRunner(result=TurnResult(ended=True, error="refused:PermissionError"))
        w = World()
        self.addCleanup(w.close)
        w.permit_runner(r)
        w.hub.send(directive("CMD-A1", "A"))
        (qid, _), = w.hub.load_state()["questions"].items()
        return w, r, qid

    def test_the_persons_downgrade_sends_it_manually(self):
        w, r, qid = self.refused()
        dec = w.hub.answer(qid, DOWNGRADE, note="권한이 없어서")
        st = w.hub.load_state()
        d = st["directives"]["CMD-A1"]
        self.assertEqual(d["status"], "open")
        self.assertEqual(d["downgraded"], {"from": "agent_sdk", "why": "refused:PermissionError", "decision": dec["id"]})
        turn = st["turns"][-1]
        self.assertEqual((turn["runner"], turn["downgraded_from"], turn["why"], turn["decision"], turn["sent"]),
                         ("manual", "agent_sdk", "refused:PermissionError", dec["id"], True))
        pending = ManualRunner(w.ga / "outbox").pending("A")  # the same directive, for the person to paste into the session
        self.assertEqual(len(pending), 1)
        self.assertIn("CMD-A1", pending[0].read_text(encoding="utf-8"))
        self.assertEqual(len(r.calls), 1)  # the refused Runner was not tried again
        self.assertTrue(st["questions"][qid]["processed"])
        self.assertTrue(w.hub.tick().quiet)  # no Judge round for it

    def test_stop_leaves_it_not_sent(self):
        w, r, qid = self.refused()
        w.hub.answer(qid, "멈춘다")
        self.assertEqual(w.hub.load_state()["directives"]["CMD-A1"]["status"], "not_sent")
        self.assertFalse((w.ga / "outbox").exists() and list((w.ga / "outbox").rglob("*.md")))


class PermitTest(unittest.TestCase):
    def test_permit_record_and_cli(self):
        w = World()
        self.addCleanup(w.close)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(w.tmp / "ga.json"), "--ga-dir", str(w.ga), "permit", "--runner", "agent_sdk",
                         "--model", "haiku", "--sandbox", "require", "--budget", "runs=8", "--note", "시험"])
        self.assertEqual(code, 0)
        bd = out.getvalue().strip()
        d = json.loads((w.ga / "records" / "decisions" / f"BD-{int(bd[3:]):04d}.json").read_text(encoding="utf-8"))
        self.assertEqual((d["by"], d["scope"]),
                         ("user", {"runner": "agent_sdk", "model": "haiku", "sandbox": "require", "budget": {"runs": 8}, "measurement_calls": False}))
        self.assertIn(f'"permission": "{bd}"', err.getvalue())

    def test_config_shape_and_runner_arguments(self):
        raw = {"schema": "ga-config/1", "hub": {"name": "hub"}, "integration_branch": "i",
               "repos": {"r": {"path": "r"}}, "sessions": {"A": {"prefix": "A", "branch": "a", "repos": ["r"]}}}
        self.assertEqual(gacfg.from_dict(dict(raw, runner={"kind": "headless", "permission": "BD-3"})).runner["permission"], "BD-3")
        for bad in ("3", "BD-x", 3):
            with self.subTest(bad=bad), self.assertRaises(FormError):
                gacfg.from_dict(dict(raw, runner={"kind": "headless", "permission": bad}))
        cfg = gacfg.from_dict(dict(raw, runner={"kind": "headless", "model": "haiku", "permission": "BD-3"}))
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(make_runner(cfg, Path(d)).model, "haiku")  # the permission is not a Runner argument


if __name__ == "__main__":
    unittest.main()
