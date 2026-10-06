"""CMD-GA44 S4: GA Console 묻기 — Enter sends, Shift+Enter is a newline, no Enter on an IME composition; a model turn
runs at once unless `confirm_model_turns` is on; ga do always confirms; the daily cap still refuses. Fake engines only."""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "console_frontend"))
sys.path.insert(0, str(ROOT / "tests"))

from ga.console import judge as J  # noqa: E402
from ga.console import server as S  # noqa: E402
from test_console_frontend import Browser as _B  # noqa: E402

WHY = J.available()


class FakeEng:
    ran: list = []

    def __init__(self, out):
        self.out = out
        self.refuse = None

    def prepare_model(self, q):
        return {"lines": ["plan"], "prompt_tokens": 5, "refuse": FakeEng.refuse_text, "text": q}

    def run_model(self, p, confirmed=False):
        FakeEng.ran.append(("model", confirmed))
        self.out("answered")
        return 0

    def prepare(self, name):
        return {"lines": [], "cost": "free", "refuse": None}

    def execute(self, name, confirmed=False, yes=False):
        return 0

    class _Log:
        done_path = Path("/nonexistent")

        def close(self):
            pass

    def new_log(self, kind):
        return self._Log()

    log = None


FakeEng.refuse_text = None


def asker(tmp, **cfg):
    class Bus:
        def publish(self, *a, **k):
            pass
    cfg = {"ask_home": str(tmp), "baseline": str(tmp), **cfg}
    return S.Asker(cfg, Bus(), engine_factory=lambda out: FakeEng(out),
                   do_argv=lambda q: [sys.executable, "-c", "print(1)"])


class Api(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        FakeEng.ran, FakeEng.refuse_text = [], None
        self.a = asker(self.tmp)

    def wait_ran(self, n):
        for _ in range(200):
            if len(FakeEng.ran) >= n:
                break
            time.sleep(0.02)

    def test_default_model_turn_runs_at_once_without_confirm_round_trip(self):
        st, p = self.a.plan("zzqx blorp", "ask")
        self.assertEqual(st, 200)
        self.assertTrue(p["started"])
        self.assertIsNone(p["confirm_id"])
        self.assertTrue(p["run_id"])
        self.wait_ran(1)
        self.assertEqual(FakeEng.ran, [("model", True)])
        self.assertEqual(self.a.pending, {})

    def test_setting_true_keeps_the_confirm_step(self):
        self.a.set_confirm_model_turns(True)
        self.assertTrue(json.loads((self.tmp / "ask.json").read_text())["confirm_model_turns"])
        st, p = self.a.plan("zzqx blorp", "ask")
        self.assertTrue(p["confirm_id"])
        self.assertNotIn("started", p)
        self.assertEqual(FakeEng.ran, [])

    def test_ga_do_always_confirms(self):
        for on in (False, True):
            self.a.set_confirm_model_turns(on)
            st, p = self.a.plan("add a test", "do")
            self.assertTrue(p["confirm_id"], on)
            self.assertNotIn("started", p)

    def test_cap_still_refuses_without_running(self):
        FakeEng.refuse_text = "오늘 agy 턴 한도(10)를 다 썼습니다"
        st, p = self.a.plan("zzqx blorp", "ask")
        self.assertEqual(st, 409)
        self.assertEqual(FakeEng.ran, [])


@unittest.skipIf(WHY, f"no browser: {WHY}")
class Keys(unittest.TestCase):
    setUpClass = classmethod(_B.setUpClass.__func__)
    tearDownClass = classmethod(_B.tearDownClass.__func__)
    setUp = _B.setUp
    page = _B.page

    def ask_page(self):
        self.FakeEngine.ran = []
        pg = self.page("ask")
        pg.wait_for_selector("[data-key=confirm-model]")
        pg.wait_for_selector("html[data-live=open]", state="attached", timeout=20000)
        return pg

    def test_enter_sends_at_once_and_token_line_is_not_asked_first(self):
        pg = self.ask_page()
        self.assertFalse(pg.is_checked("[data-key=confirm-model]"))
        pg.fill("#q", "hello there")
        pg.press("#q", "Enter")
        pg.wait_for_selector("text=model answered: 42", timeout=10000)
        self.assertEqual(pg.locator(".cost button[data-key=confirm]").count(), 0)
        self.assertEqual(self.FakeEngine.ran, [("model", True)])

    def test_shift_enter_is_a_newline(self):
        pg = self.ask_page()
        pg.fill("#q", "a")
        pg.press("#q", "Shift+Enter")
        pg.keyboard.type("b")
        self.assertEqual(pg.input_value("#q"), "a\nb")
        self.assertEqual(self.FakeEngine.ran, [])

    def test_enter_during_ime_composition_does_not_send(self):
        pg = self.ask_page()
        pg.fill("#q", "한글")
        sent = []
        pg.on("request", lambda r: sent.append(r.url) if r.method == "POST" and "/api/ask" in r.url else None)
        pg.evaluate("""() => { const t = document.getElementById('q');
          t.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true, cancelable: true}));
          t.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 229, bubbles: true, cancelable: true})); }""")
        time.sleep(0.5)
        self.assertEqual(sent, [])  # the request itself, not the slower engine run (a sleep-then-ran check let the IME mutant live)
        self.assertEqual(self.FakeEngine.ran, [])

    def test_ticked_box_shows_the_confirm_card_for_a_model_turn(self):
        pg = self.ask_page()
        pg.check("[data-key=confirm-model]")
        pg.fill("#q", "hello there")
        pg.press("#q", "Enter")
        pg.wait_for_selector(".cost button[data-key=confirm]", timeout=10000)
        self.assertEqual(self.FakeEngine.ran, [])
        pg.uncheck("[data-key=confirm-model]")  # leave the shared world as found


if __name__ == "__main__":
    unittest.main()
