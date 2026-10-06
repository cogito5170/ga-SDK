"""VM-BRIDGE-MODEL-1: a directive/2 may name its model; ``ga bridge`` runs that one directive on it (the config file on
disk is not changed), checks it against ``agy models`` first and declines an unknown model before any model call.
0 model calls: ``agy models`` and the runs are fakes.
"""
import json, os, subprocess, sys, unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ga.bridge as bridge  # noqa: E402
from ga.bridge import act as ACT  # noqa: E402
from ga.bridge import models as MODELS  # noqa: E402
from ga.forms import hard, parse_text, validate  # noqa: E402
from test_vm_bridge_act import DIRECTIVE, SPEC, World, fake_act, form  # noqa: E402

AGY_MODELS = """gemini-3.8-flash-high     Gemini 3.8 Flash (High)
gemini-3.7-flash-low      Gemini 3.7 Flash (Low)
claude-sonnet-5-5-medium  Claude Sonnet 5.5 (Medium)
gpt-oss-120b-medium       GPT-OSS 120B (Medium)
"""


def fake_agy(text=AGY_MODELS, code=0):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=code, stdout=text, stderr="")
    run.calls = calls
    return run


def supervise_recorder():
    calls = []

    def run(cfg, conf, task):
        calls.append(json.loads(conf.read_text()))
        return {"code": 0, "out": "OK\n", "events": [
            {"event": "turn", "input_tokens": 1, "tokens": 2, "seconds": 1, "served": calls[-1]["model"]},
            {"event": "end", "status": "done", "model_turns": 1}]}
    run.calls = calls
    return run


class FormTest(unittest.TestCase):
    def test_model_is_an_optional_non_empty_string(self):
        self.assertEqual(hard(validate(DIRECTIVE)), [])
        self.assertEqual(hard(validate({**DIRECTIVE, "model": "gemini-3.7-flash-low"})), [])
        for bad in ("", "  ", 3, ["m"]):
            self.assertTrue(hard(validate({**DIRECTIVE, "model": bad})), bad)


class ModelTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.cfg["models_cache"] = str(self.w.tmp / "agy_models.json")
        self.logs = []
        os.environ["GA_HOME"] = str(self.w.tmp / "ga-home")
        self.addCleanup(os.environ.pop, "GA_HOME", None)
        self.agy = fake_agy()
        self.real = real = MODELS.available
        MODELS.available = lambda cfg, backend, **kw: real(cfg, backend, run=self.agy)
        self.addCleanup(setattr, MODELS, "available", real)

    def pass_(self, runner, act_run=None):
        handler = (lambda c, h, s, m: ACT.handle(c, h, s, m, runner=act_run)) if act_run else None
        return bridge.one_pass(self.w.cfg, box=self.w.vm, runner=runner, log=self.logs.append, act_handler=handler)

    def replies(self):
        return [parse_text(m.text)[0] for m in self.w.hub.unread("baseline")]

    def test_the_directive_model_runs_for_that_directive_only(self):
        on_disk = (self.w.work / "ga-supervise.json").read_text()
        self.w.hub.send("AGY", form({**DIRECTIVE, "model": "gemini-3.7-flash-low"}), "baseline")
        self.w.hub.send("AGY", form({**DIRECTIVE, "id": "CMD-VA2"}), "baseline")
        run = supervise_recorder()
        self.assertEqual(self.pass_(run), 2)
        self.assertEqual([c["model"] for c in run.calls], ["gemini-3.7-flash-low", "gpt-oss-120b-medium"])
        self.assertEqual((self.w.work / "ga-supervise.json").read_text(), on_disk)
        first, second = self.replies()
        self.assertEqual(next(r["value"] for r in first["results"] if r["name"] == "model"), "gemini-3.7-flash-low")
        self.assertEqual(second["items"][0]["state"], "met")

    def test_an_unknown_model_is_declined_before_any_model_call(self):
        self.w.hub.send("AGY", form({**DIRECTIVE, "model": "gemini-9-ultra"}), "baseline")
        run = supervise_recorder()
        self.pass_(run)
        self.assertEqual(run.calls, [])
        (head,) = self.replies()
        self.assertEqual(hard(validate(head)), [])
        self.assertEqual(head["handled"][0]["status"], "declined")
        self.assertIn("'gemini-9-ultra' not in agy models", head["handled"][0]["reason"])

    def test_code_work_runs_on_the_directive_model(self):
        self.w.hub.send("AGY", form({**DIRECTIVE, "model": "claude-sonnet-5-5-medium"}, SPEC), "baseline")
        run = fake_act()
        self.pass_(supervise_recorder(), act_run=run)
        self.assertEqual(run.calls[0]["model"], "claude-sonnet-5-5-medium")
        (head,) = self.replies()
        self.assertEqual(next(r["value"] for r in head["results"] if r["name"] == "model"), "claude-sonnet-5-5-medium")

    def test_agy_models_is_parsed_and_cached(self):
        self.assertEqual(MODELS.parse_agy_models(AGY_MODELS)[:2], ["gemini-3.8-flash-high", "gemini-3.7-flash-low"])
        agy = fake_agy()
        cfg = {"models_cache": str(self.w.tmp / "c.json"), "models_ttl_s": 3600}
        self.assertIn("gpt-oss-120b-medium", self.real(cfg, "agv", now=lambda: 1000.0, run=agy))
        self.real(cfg, "agv", now=lambda: 1010.0, run=agy)
        self.assertEqual(len(agy.calls), 1)  # cached
        self.real(cfg, "agv", now=lambda: 5000.0, run=agy)
        self.assertEqual(len(agy.calls), 2)  # expired
        self.assertEqual(agy.calls[0][-1], "models")

    def test_no_list_means_not_checked_and_declined(self):
        MODELS.available = self.real
        why = MODELS.check({"models_cache": str(self.w.tmp / "n.json")}, "gemini-3.7-flash-low", "agv",
                           run=fake_agy(text="", code=1))
        self.assertIn("not checked", why)
        self.assertIn("no model list", MODELS.check({}, "x-model", "anthropic_http"))
        self.assertIsNone(MODELS.check({"models": ["x-model"]}, "x-model", "anthropic_http"))

if __name__ == "__main__":
    unittest.main()
