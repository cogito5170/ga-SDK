"""DEV-R0a: the model recorded for a task (report, ledger, events) is the model that answered the final turn."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ga import events as EV
from ga.act import loop as A
from ga.backends.base import BackendTurn
from ga.bridge import build_report, record_served
from tests.test_ga38 import CALC, FIX, Fake, py_repo
from tests.test_ga45 import CFG, DIRTY, sh


class Serving(Fake):
    """A scripted rung that says which model served each turn."""

    def __init__(self, answers, served):
        super().__init__(answers)
        self.served = served

    def run_turn(self, *a, **k):
        t = super().run_turn(*a, **k)
        return BackendTurn(t.answer, self.served if isinstance(self.served, list) else [self.served], t.usage, t.usage_format, None, 0.01, 1)


class Climb(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="r0a-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.dict(os.environ, {"GA_EVENTS": str(self.tmp / "ev.jsonl")})
        p.start()
        self.addCleanup(p.stop)

    def climb(self):
        root = py_repo(self, CFG)
        sh("git", "init", "-q", cwd=root)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@x", "add", ".", cwd=root)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "c1", cwd=root)
        state = self.tmp / "state"
        fakes = {"cheap": Serving([DIRTY], "model-A"), "mid": Serving([FIX], ["model-A", "model-B"])}  # the backend itself fell back A -> B
        res = A.run_item(root, {"id": "CMD-T1", "goal": "make the tests pass", "files": ["calc.py", "junk.py"]},
                         backend="fake", model="cheap", ladder="cheap,mid", make_runner=fakes.__getitem__,
                         state_dir=state, max_turns=1)
        self.assertEqual(res.status, "done", res.reason)
        return state

    def test_ledger_and_events_name_the_rung_that_answered(self):
        state = self.climb()
        rows = [json.loads(x) for f in (state / "ledger").glob("*.jsonl") for x in f.read_text().splitlines()]
        self.assertEqual([r["model"] for r in rows], ["model-A", "model-B"])
        tele = [json.loads(x)["data"]["model"] for x in (state / "telemetry.jsonl").read_text().splitlines()]
        self.assertEqual(tele, ["model-A", "model-B"])
        evs = [json.loads(x) for x in (self.tmp / "ev.jsonl").read_text().splitlines()]
        llm = [e["metadata"]["model"] for e in evs if e["type"] == "LLM" and e["status"] == "DONE"]
        self.assertEqual(llm[-1], "model-B")
        tasks = [e for e in evs if e["type"] == "TASK" and e["status"] == "DONE"]
        self.assertEqual(tasks[-1]["metadata"]["model"], "model-B")

    def test_report_and_served_record_name_the_last_turn(self):
        cfg = {"name": "w", "workdir": str(self.tmp), "max_answer_chars": 100, "served_file": str(self.tmp / "s.json")}
        run = {"code": 0, "out": "ok", "events": [{"event": "turn", "served": ["model-A"]},
                                                  {"event": "turn", "served": ["x", "model-B"]},
                                                  {"event": "end", "status": "done"}]}
        rep = build_report(cfg, {"id": "CMD-T1", "done_when": []}, run)
        self.assertIn('{"name":"model","value":"model-B"}', rep)
        self.assertEqual(record_served(cfg, run), "model-B")
        self.assertEqual(json.loads((self.tmp / "s.json").read_text())["model"], "model-B")


if __name__ == "__main__":
    unittest.main()
