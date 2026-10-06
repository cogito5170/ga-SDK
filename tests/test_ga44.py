"""CMD-GA44: ga ask without agy's tool overhead. Fake agy on PATH only; 0 real model runs."""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ga.ask import Engine  # noqa: E402
from ga.ask import intents as I  # noqa: E402
from ga.backends import agy_agent  # noqa: E402


class World:
    def __init__(self, **cfg):
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.dir = self.tmp / "f"
        self.dir.mkdir()
        self.set(installed=["ga-ask"], models=["gpt-oss-120b-medium", "gemini-3-flash"], **cfg)
        shim = self.bin / "agy"
        shim.write_text(f"#!/bin/sh\nexec {sys.executable} {ROOT / 'tests' / 'fake_agy44.py'} \"$@\"\n")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.old = dict(os.environ)
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{os.environ['PATH']}"
        os.environ["FAKE44_DIR"] = str(self.dir)
        os.environ["GA_BRIDGE_CONFIG"] = str(self.tmp / "none.json")

    def set(self, **cfg):
        (self.dir / "cfg.json").write_text(json.dumps(cfg))

    def settings(self, **s):
        (self.home / "ask.json").write_text(json.dumps(s))

    def calls(self):
        f = self.dir / "calls.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def turns(self):
        return [c for c in self.calls() if "-p" in c]

    def engine(self):
        lines: list[str] = []
        e = Engine(self.home, out=lines.append)
        return e, lines

    def close(self):
        os.environ.clear()
        os.environ.update(self.old)
        shutil.rmtree(self.tmp, True)


class Base(unittest.TestCase):
    cfg: dict = {}

    def setUp(self):
        self.w = World(**self.cfg)
        self.addCleanup(self.w.close)

    def ask(self, q="why?"):
        e, lines = self.w.engine()
        code = e.run_model(e.prepare_model(q), confirmed=True)
        return e, lines, code


class D1Agent(Base):
    def test_default_argv_has_ga_ask_agent(self):
        self.ask()
        t = self.w.turns()[0]
        self.assertEqual(t[:2], ["--agent", "ga-ask"])

    def test_settings_agent_and_agy_cli_override(self):
        self.w.set(installed=["mine"])
        self.w.settings(ask_agent="mine")
        self.ask()
        self.assertEqual(self.w.turns()[0][:2], ["--agent", "mine"])
        self.w.settings(agy_cli=["agy", "--agent", "other"])
        self.ask()
        self.assertEqual(self.w.turns()[1][:2], ["--agent", "other"])

    def test_missing_agent_by_plugin_list_spends_nothing(self):
        self.w.set(installed=["minimal"])
        e, lines, code = self.ask()
        self.assertEqual(self.w.turns(), [])
        self.assertEqual(e.day.used(), 0)
        self.assertEqual(code, 2)
        self.assertTrue(any("ga agy-agent install --name ga-ask" in x and "설치" in x for x in lines))

    def test_unknown_agent_answer_becomes_hint_and_is_not_counted(self):
        self.w.set(installed=["ga-ask"], unknown_agent=True)
        e, lines, code = self.ask()
        self.assertEqual(len(self.w.turns()), 1)
        self.assertEqual(e.day.used(), 0)
        self.assertTrue(any("ga agy-agent install --name ga-ask" in x for x in lines))

    def test_ask_agent_files_are_toolless_free_text(self):
        f = agy_agent.files("ga-ask")
        a = f["agents/ga-ask.md"]
        self.assertIn("tools: []", a)
        self.assertNotIn("JSON", a)
        self.assertIn("JSON", agy_agent.files("ga-plan")["agents/ga-plan.md"])


class D1Facts(Base):
    def facts(self, q):
        self.assertEqual(I.route(q).intent.name, "setup", q)
        e, lines = self.w.engine()
        self.assertEqual(e.execute("setup"), 0)
        return "\n".join(lines), e

    def test_korean_and_english_code_answer(self):
        for q in ("사용 모델 종류는?", "which model do you use?"):
            text, e = self.facts(q)
            self.assertIn("gpt-oss-120b-medium", text)
            self.assertIn("gemini-3-flash", text)
            self.assertIn("ga-ask", text)
            self.assertEqual(e.day.used(), 0)
        self.assertEqual(self.w.turns(), [])

    def test_models_listing_failure_is_said(self):
        self.w.set(installed=["ga-ask"], models=None)
        text, _ = self.facts("what model do you use")
        self.assertIn("가져오지 못했습니다", text)


class D1Warning(Base):
    def test_big_input_warns_small_does_not(self):
        self.w.set(installed=["ga-ask"], input=10149)
        _, lines, _ = self.ask()
        warn = [x for x in lines if "입력이 큽니다(10,149 토큰)" in x]
        self.assertEqual(len(warn), 1)
        self.assertIn("도구 없는 에이전트가 아닌 것 같습니다", warn[0])
        self.assertIn("ga agy-agent install --name ga-ask", warn[0])
        self.w.set(installed=["ga-ask"], input=2600)
        _, lines, _ = self.ask()
        self.assertFalse(any("입력이 큽니다" in x for x in lines))


class D1Console(Base):
    def test_code_answer_has_no_confirm_and_model_turn_keeps_it(self):
        from ga.console import server as S
        srv = S.Asker.__new__(S.Asker)
        import threading
        srv.cfg, srv.lock, srv.pending, srv.clock = {}, threading.Lock(), {}, __import__("time").time
        srv.engine_factory = lambda out: Engine(self.w.home, out=out)
        st, body = srv.plan("사용 모델 종류는?", "ask")
        self.assertEqual(st, 200)
        self.assertTrue(body["code"])
        self.assertNotIn("confirm_id", body)
        self.assertEqual(body["cost_estimate"]["input_tokens"], 0)
        self.assertIn("gpt-oss-120b-medium", body["answer"])
        self.assertEqual(srv.pending, {})
        st, body = srv.plan("zzqx blorp wibble", "ask")
        self.assertIn("confirm_id", body)
        self.assertEqual(body["cost_estimate"]["cost"], "model")


class Version(unittest.TestCase):
    def test_version(self):
        import ga
        self.assertGreaterEqual(tuple(map(int, ga.__version__.split("."))), (0, 12, 0))
        self.assertIn(f'version = "{ga.__version__}"', (ROOT / "pyproject.toml").read_text())


if __name__ == "__main__":
    unittest.main()
