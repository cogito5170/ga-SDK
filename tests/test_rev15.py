"""CMD-GA17: METHOD rev 15 §4c 7 (BD-161) — the operator's PreToolUse guards (e.g. rlo.hooks enforce) in the worker
turn's own settings, after ga's guard; their verdicts as turn evidence; a missing guard program stops the send.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from ga import config as gacfg
from ga.__main__ import make_runner
from ga.adapters.agent_sdk import AgentSDKRunner, fake_sdk
from ga.adapters.base import TurnRequest, TurnResult
from ga.adapters.headless import HeadlessRunner
from ga.forms import FormError
from ga.hub import DOWNGRADE, guard_summary

from world import World, directive, proposal

RLO = "python3 -m rlo.hooks --model {home}/cc_tools_model.json --mode enforce --grant Bash --record {home}/rlo-{session}.jsonl"


def rlo_guard():
    return {"name": "rlo", "command": RLO, "record": "{home}/rlo-{session}.jsonl"}


class GuardedRunner:
    """A model-turn Runner stand-in with the headless layout (settings, guards); ``act`` plays the turn."""

    kind, model, sandbox = "headless", None, "auto"

    def __init__(self, home, guards, act):
        self.layout = HeadlessRunner(home, guards=guards, sandbox="off")
        self.guards, self.act, self.calls = self.layout.guards, act, []

    def fill(self, text, session):
        return self.layout.fill(text, session)

    def guard_records(self, session):
        return self.layout.guard_records(session)

    def run_turn(self, req):
        self.calls.append(req)
        self.act(req, self)
        return TurnResult(ended=True, session_id="s1", cost=0.01)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp)

    def test_the_guard_goes_after_gas_own_in_the_turns_settings(self):
        r = HeadlessRunner(self.tmp / "home", guards=[rlo_guard(), {"command": "/opt/g2", "matcher": "Bash"}])
        path = r.settings_file("A")
        self.assertEqual(path, self.tmp / "home" / "A" / "ga-settings.json")
        pre = json.loads(path.read_text())["hooks"]["PreToolUse"]
        self.assertIn("bash_guard.py", pre[0]["hooks"][0]["command"])  # ga first
        home = str(self.tmp / "home" / "A")
        self.assertEqual(pre[1], {"matcher": "*", "hooks": [{"type": "command", "command":
                         f"python3 -m rlo.hooks --model {home}/cc_tools_model.json --mode enforce --grant Bash --record {home}/rlo-A.jsonl"}]})
        self.assertEqual(pre[2]["matcher"], "Bash")
        argv = r.argv(TurnRequest("A", "p", self.tmp))
        self.assertEqual(argv[argv.index("--settings") + 1], str(path))

    def test_with_gas_guard_off_the_operators_guard_still_goes_in(self):
        r = HeadlessRunner(self.tmp / "home", guard=False, guards=[rlo_guard()])
        argv = r.argv(TurnRequest("A", "p", self.tmp))
        pre = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 1)
        self.assertIn("rlo.hooks", pre[0]["hooks"][0]["command"])
        self.assertNotIn("--settings", HeadlessRunner(self.tmp / "h2", guard=False).argv(TurnRequest("A", "p", self.tmp)))

    def test_the_persons_settings_are_not_touched(self):
        person = self.tmp / "person"
        (person / ".claude").mkdir(parents=True)
        mine = person / ".claude" / "settings.json"
        mine.write_text('{"hooks": {}}')
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(person)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old) if old else os.environ.pop("HOME"))
        r = HeadlessRunner(self.tmp / "home", guards=[rlo_guard()])
        r.settings_file("A")
        self.assertEqual(mine.read_text(), '{"hooks": {}}')
        self.assertEqual(sorted(p.name for p in (person / ".claude").iterdir()), ["settings.json"])
        self.assertEqual(r.env("A")["HOME"], str(self.tmp / "home" / "A"))  # the turn's HOME is its own

    def test_agent_sdk_hands_the_same_settings(self):
        rec = []
        r = AgentSDKRunner(self.tmp / "home", sdk=fake_sdk([{"session_id": "s", "total_cost_usd": 0.0, "is_error": False,
                                                            "subtype": "success", "num_turns": 1}], rec),
                           sandbox="off", guards=[rlo_guard()])
        r.run_turn(TurnRequest("A", "p", self.tmp))
        pre = json.loads(Path(rec[0]["options"].settings).read_text())["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 2)
        self.assertIn("rlo.hooks", pre[1]["hooks"][0]["command"])

    def test_config_to_settings_with_the_real_rlo_command_string(self):
        raw = {"schema": "ga-config/1", "hub": {"name": "hub"}, "integration_branch": "i",
               "repos": {"r": {"path": "r"}}, "sessions": {"A": {"prefix": "A", "branch": "a", "repos": ["r"]}},
               "runner": {"kind": "headless", "model": "haiku", "permission": "BD-1", "guards": [rlo_guard()]}}
        r = make_runner(gacfg.from_dict(raw), self.tmp)
        pre = json.loads(r.settings_file("A").read_text())["hooks"]["PreToolUse"]
        self.assertTrue(pre[1]["hooks"][0]["command"].startswith("python3 -m rlo.hooks --model "))
        self.assertTrue(pre[1]["hooks"][0]["command"].endswith("/A/rlo-A.jsonl"))
        for bad in ([{"matcher": "*"}], [{"command": ""}], [{"command": "x", "other": 1}], "x", [{"command": "x", "record": 3}]):
            with self.subTest(bad=bad), self.assertRaises(FormError):
                gacfg.from_dict(dict(raw, runner=dict(raw["runner"], guards=bad)))
        with self.assertRaises(FormError):  # only Runners that write a turn's settings
            gacfg.from_dict(dict(raw, runner={"kind": "manual", "guards": [rlo_guard()]}))


class GuardSummaryTest(unittest.TestCase):
    def test_both_record_shapes(self):
        lines = [json.dumps({"decision": "allow", "rule": ""}), json.dumps({"decision": "deny", "rule": "no_verify"}),
                 json.dumps({"kind": "guard", "tool_name": "Glob", "result": {"verdict": "DENY", "rule": "A1"}}),
                 json.dumps({"kind": "guard", "tool_name": "Read", "result": {"verdict": "ALLOW"}}),
                 json.dumps({"kind": "guard_error", "exception": "ValueError"}), "not json", "", json.dumps([1])]
        self.assertEqual(guard_summary(lines), {"allow": 2, "deny": 2, "errors": 3, "labels": ["A1", "no_verify"]})


class GuardsInTheHubTest(unittest.TestCase):
    def world(self, guards, write=None):
        w = World()
        self.addCleanup(w.close)

        def act(req, runner):
            sha = w.work(req.session, "alpha", {"x.txt": "1\n"})
            for name, path in runner.guard_records(req.session):
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    for line in write or []:
                        f.write(json.dumps(line) + "\n")
            w.report(req.session, [("CMD-A1", 1, "done")], [("alpha", sha)])
        r = GuardedRunner(w.tmp / "home", guards, act)
        w.permit_runner(r)
        return w, r

    def test_counts_and_labels_are_turn_evidence_and_denials_a_note(self):
        guard = rlo_guard()
        w, r = self.world([guard], write=[{"kind": "guard", "result": {"verdict": "ALLOW"}},
                                          {"kind": "guard", "result": {"verdict": "ALLOW"}},
                                          {"kind": "guard", "result": {"verdict": "DENY", "rule": "A1"}},
                                          {"kind": "guard_error", "exception": "KeyError"}])
        rec = r.layout.guard_records("A")[0][1]
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(json.dumps({"kind": "guard", "result": {"verdict": "DENY", "rule": "OLD"}}) + "\n")  # an earlier turn's
        w.proposals.append(proposal("success", "wait", "좋다"))
        post, _, gates = w.hub.send(directive("CMD-A1", "A"))  # the earlier line is not counted (offset)
        self.assertEqual(gates, [])
        turn = w.hub.load_state()["turns"][-1]
        want = [{"guard": "rlo", "allow": 2, "deny": 1, "errors": 1, "labels": ["A1"]}]
        self.assertEqual((turn["guards"], turn["diag"]["guards"]), (want, want))
        res = w.hub.tick()
        self.assertIn("guard rlo refused 1 tool call(s) in A's CMD-A1 rev 1 turn: A1", res.verdict["evidence"]["notes"])
        for raw in ("tool_name", "verdict", "KeyError", "OLD"):  # nothing beyond counts and labels
            self.assertNotIn(raw, json.dumps(turn))
        self.assertTrue(w.hub.load_state()["turns"][-1]["guards_noted"])

    def test_no_denial_no_note(self):
        w, r = self.world([rlo_guard()], write=[{"decision": "allow"}])
        w.proposals.append(proposal("success", "wait", "좋다"))
        w.hub.send(directive("CMD-A1", "A"))
        res = w.hub.tick()
        self.assertFalse(any(n.startswith("guard ") for n in res.verdict["evidence"]["notes"]))

    def test_a_missing_guard_program_stops_the_send(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp)
        not_exec = tmp / "guard.sh"
        not_exec.write_text("#!/bin/sh\n")
        os.chmod(not_exec, 0o644)
        for cmd, why in (("/nonexistent/ga-guard --x", "not found or not executable: /nonexistent/ga-guard"),
                         ("ga-no-such-guard-program --x", "not found on PATH: ga-no-such-guard-program"),
                         (f"{not_exec} --x", f"not found or not executable: {not_exec}"),
                         ("'unclosed", "cannot be read")):
            with self.subTest(cmd=cmd):
                w, r = self.world([{"command": cmd}])
                post, _, gates = w.hub.send(directive("CMD-A1", "A"))
                self.assertIsNone(post)
                self.assertEqual([g.number for g in gates], [6])
                self.assertIn(why, gates[0].reason)
                self.assertEqual(r.calls, [])
                self.assertEqual([p for p in w.mail.read("A") if p.author == "hub"], [])
                self.assertEqual(w.hub.load_state()["directives"]["CMD-A1"]["status"], "not_sent")
                q, = w.hub.load_state()["questions"].values()
                self.assertEqual([o["label"] for o in q["doc"]["options"]], ["가드를 고친다", DOWNGRADE, "멈춘다"])

    def test_a_present_program_runs_the_turn(self):
        w, r = self.world([{"command": "python3 -m rlo.hooks --mode enforce"}])  # python3 is there; rlo need not be
        post, _, gates = w.hub.send(directive("CMD-A1", "A"))
        self.assertIsNotNone(post)
        self.assertEqual((gates, len(r.calls)), ([], 1))


if __name__ == "__main__":
    unittest.main()
