"""CMD-GA36: ga ask, ga ui, ga ask --solve. Offline: 0 network beyond 127.0.0.1, agy is tests/fake_agy.py or an
in-process fake backend, the mailbox is temporary git repos. 0 model calls.

D1  routing by local rules (>=40 phrasings, 10 unrelated -> none with three suggestions); a model or mail action does
    not run without a confirmation; --yes does not skip a model action; the daily cap refuses the 11th agy turn;
    ga ask --model is one turn, tools off, prompt < 1500 tokens; waiting spends no model turn; doctor's five kinds
D3  ga ui: 127.0.0.1 only; no token, a foreign Origin or Host -> 403; actions need POST + token; CSP, nothing remote;
    the token in no log or file; SSE resumes; question -> confirmation -> fake run -> log line over plain HTTP
D5  solve: the turn cap; nothing without confirmation; per-turn lines from the supervise log; totals = sums, one
    ledger row; the overhead share from the backend's measured overhead; a non-table tool refused and listed; the ui
    Solve box drives the same path
"""
import contextlib
import http.client
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from ga import ask as A  # noqa: E402
from ga import bridge, runlog  # noqa: E402
from ga.ask import doctor, intents as I, solve as S  # noqa: E402
from ga.ask.store import AGV_OVERHEAD, DayTurns, Ledger  # noqa: E402
from ga.backends.base import BackendTurn  # noqa: E402
from ga.ctxpack import tokens  # noqa: E402
from ga.forms import parse_text  # noqa: E402
from test_ga36_bridge import DIRECTIVE, World, form  # noqa: E402
import ga.ui as UI  # noqa: E402

FAKE_AGY = [sys.executable, str(TESTS / "fake_agy.py")]
MODEL = "gpt-oss-120b-medium"

PHRASINGS = {
    "help": ["뭐 할 수 있어?", "도움말 보여줘", "사용법 알려줘", "어떻게 써?", "What can you do?", "help",
             "how do I use this?", "show me the commands"],
    "status": ["지금 상태 어때?", "새 메일 왔어?", "마지막 실행 어떻게 됐어?", "진행 상황 알려줘", "status please",
               "any new mail?", "how did the last run go?", "what's going on?"],
    "next": ["다음 할 일 뭐야?", "다음 지시 보여줘", "이제 뭐 해야 돼?", "what's next?", "show the next directive",
             "what should I do now?"],
    "run": ["다음 지시 실행해줘", "브리지 돌려줘", "이거 처리해", "run the next directive", "start the bridge",
            "go ahead and execute it"],
    "report": ["보고서 보내줘", "리포트 제출해줘", "보고 올려줘", "send the report", "submit my report"],
    "usage": ["오늘 얼마나 썼어?", "토큰 사용량 보여줘", "비용 얼마 들었어?", "how many tokens did I use today?", "usage",
              "how much did we spend?"],
    "doctor": ["왜 안 돼?", "진단해줘", "뭐가 문제야?", "에러 났어", "doctor", "why did it fail?", "it's not working",
               "check my setup"],
    "stop": ["멈춰", "브리지 중지해줘", "그만해", "stop the bridge", "halt", "cancel it"],
}
UNRELATED = ["오늘 날씨 어때?", "점심 뭐 먹을까", "고양이 사진 보여줘", "피자 주문해줘", "노래 틀어줘",
             "What is the capital of France?", "tell me a joke", "write a poem about autumn",
             "how tall is Mount Everest", "translate hello into Japanese"]


def plan(steps=(), nxt=None, say=None):
    p = {"schema": "ga-plan/1", "steps": list(steps), "next": nxt}
    if say is not None:
        p["say"] = say
    return p


class FakeCli:
    """An in-process backend runner: scripted plans, usage per turn, a call count. Never a model."""
    resumes, bare, usage_format = False, True, "anthropic"

    def __init__(self, answers, model=MODEL):
        self.answers, self.model, self.calls = list(answers), model, []

    def run_turn(self, prompt, session=None, *, system=None, on_wait=None, wait_every_s=None):
        i = len(self.calls)
        self.calls.append(prompt)
        a = self.answers[min(i, len(self.answers) - 1)]
        usage = {"input_tokens": 12000 + 111 * i, "output_tokens": 300 + 7 * i}
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        return BackendTurn("```json\n" + json.dumps(a) + "\n```", [self.model], usage, "anthropic", None, 1.5 + i, 1)


class Base(unittest.TestCase):
    """A temp home, a fake agy (FAKE_AGY_DIR), a World (hub + mac mailboxes, a project) and an agy-bridge.json."""

    def setUp(self):
        self.w = World()
        self.home = self.w.tmp / "home"
        self.home.mkdir()
        self.agy = self.w.tmp / "agy"
        self.agy.mkdir()
        self.env(GA_ASK_HOME=str(self.home), FAKE_AGY_DIR=str(self.agy), GA_BRIDGE_CONFIG="")
        self.bridge_cfg = self.w.tmp / "agy-bridge.json"
        self.bridge_cfg.write_text(json.dumps({"mailbox_repo": str(self.w.tmp / "mac"), "workdir": str(self.w.work),
                                               "capacity_backoff_s": 0, "turn_timeout_s": 120, "poll_s": 0.05}))
        (self.w.work / "ga-supervise.json").write_text(json.dumps({
            "schema": "ga-supervise/1", "backend": "agv", "model": MODEL, "options": {"cli": FAKE_AGY}, "tools": {}}))
        self.settings({})
        self.lines = []
        self.runner_calls = []

    def env(self, **kv):
        p = mock.patch.dict(os.environ, kv)
        p.start()
        self.addCleanup(p.stop)

    def settings(self, extra):
        (self.home / "ask.json").write_text(json.dumps({"bridge_config": str(self.bridge_cfg), "agy_cli": FAKE_AGY,
                                                        "ask_model": MODEL, **extra}))

    def script(self, entries):
        (self.agy / "script.json").write_text(json.dumps(entries))

    def agy_turns(self):
        f = self.agy / "calls.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines() if json.loads(x)["kind"] == "turn"] if f.exists() else []

    def fake_bridge_runner(self, cfg, conf, task, progress=None):
        self.runner_calls.append(task)
        if progress:
            progress("턴 1: 입력 11,210 · 출력 121 토큰 · 8.2초 · 도구 1단계")
        return {"code": 0, "out": "done.\n", "events": [
            {"event": "turn", "step": "T1.m1", "ok": True, "model": MODEL, "input_tokens": 11210, "output_tokens": 121,
             "tokens": 11331, "seconds": 8.2},
            {"event": "end", "status": "done", "model_turns": 1, "tool_steps": 1}]}

    def engine(self, **kw):
        kw.setdefault("bridge_runner", self.fake_bridge_runner)
        kw.setdefault("box", self.w.mac)
        return A.Engine(self.home, out=self.lines.append, sleep=lambda s: None, **kw)

    def ask(self, *argv, answer="n", engine=None):
        prompts = []

        def input_fn(p):
            prompts.append(p)
            return answer
        rc = A.main(list(argv), input_fn=input_fn, engine=engine or self.engine())
        return rc, prompts

    def directive(self):
        self.w.hub.send("AGY", form(DIRECTIVE), "baseline")

    def prepare_report(self, sender="AGY"):
        (self.w.work / "reports").mkdir(exist_ok=True)
        head = {"schema": "report/2", "from": sender, "handled": [{"id": "CMD-AG1", "rev_seen": 1, "status": "done"}],
                "items": [{"id": "D1", "state": "met", "evidence": ["tests"]}]}
        (self.w.work / "reports" / "CMD-AG1.md").write_text(form(head))


# ---- D1 ---------------------------------------------------------------------------------------------------------------

class D1Routing(unittest.TestCase):
    def test_at_least_40_phrasings_map_to_the_right_intent(self):
        n = 0
        for want, qs in PHRASINGS.items():
            for q in qs:
                m = I.route(q)
                self.assertIsNotNone(m.intent, q)
                self.assertEqual(m.intent.name, want, q)
                n += 1
        self.assertGreaterEqual(n, 40)
        self.assertTrue(any(re.search("[가-힣]", q) for qs in PHRASINGS.values() for q in qs))
        self.assertTrue(any(re.search("[a-z]", q) for qs in PHRASINGS.values() for q in qs))

    def test_ten_unrelated_map_to_none_with_three_suggestions(self):
        self.assertEqual(len(UNRELATED), 10)
        for q in UNRELATED:
            m = I.route(q)
            self.assertIsNone(m.intent, q)
            self.assertEqual(len(m.suggestions), 3, q)
            self.assertTrue(set(m.suggestions) <= set(I.BY_NAME), q)

    def test_routing_is_local_rules_only(self):
        # no backend, no subprocess, no network in routing: route() is pure; a patched subprocess would show a call
        with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            for qs in PHRASINGS.values():
                for q in qs:
                    I.route(q)
        run.assert_not_called()
        popen.assert_not_called()

    def test_every_intent_has_a_cost_class_and_a_fixed_command(self):
        self.assertEqual(sorted(I.BY_NAME), sorted(["help", "status", "next", "run", "report", "usage", "doctor", "stop"]))
        for it in I.TABLE:
            self.assertIn(it.cost, I.COSTS)
            self.assertTrue(it.argv and it.argv[0] == "ga")
        self.assertEqual(I.BY_NAME["run"].cost, "model")
        self.assertEqual(I.BY_NAME["report"].cost, "mail")


class D1Guard(Base):
    def test_unmatched_question_lists_three_and_runs_nothing(self):
        rc, prompts = self.ask("오늘 날씨 어때?")
        self.assertEqual(rc, 1)
        self.assertEqual(prompts, [])
        self.assertEqual(sum(1 for x in self.lines if x.startswith("  ")), 3)

    def test_a_model_action_without_confirmation_does_not_run(self):
        self.directive()
        rc, prompts = self.ask("다음 지시 실행해줘", answer="n")
        self.assertEqual(rc, 1)
        self.assertEqual(len(prompts), 1)                     # it asked y/N
        self.assertEqual(self.runner_calls, [])                # and nothing ran
        self.assertTrue(any("예상 입력 토큰" in x for x in self.lines))  # the cost was shown first
        self.assertEqual(list(self.w.hub.unread("baseline")), [])
        eng = self.engine()
        self.assertEqual(eng.execute("run"), 1)                # the engine's own guard, too
        self.assertEqual(self.runner_calls, [])

    def test_a_confirmed_model_action_runs_once(self):
        self.directive()
        rc, _ = self.ask("run the next directive", answer="y")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.runner_calls), 1)
        (msg,) = list(self.w.hub.unread("baseline"))
        self.assertEqual(parse_text(msg.text)[0]["items"][0]["state"], "met")
        self.assertEqual(DayTurns(self.home).used(), 1)
        self.assertEqual(Ledger(self.home).rows()[-1]["input"], 11210)

    def test_yes_does_not_skip_a_model_action(self):
        self.directive()
        rc, prompts = self.ask("--yes", "run the next directive", answer="n")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(self.runner_calls, [])
        self.assertEqual(rc, 1)
        self.script([{"text": "hi", "format": "json"}])
        rc, prompts = self.ask("--yes", "--model", "what is ga?", answer="n")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(self.agy_turns(), [])

    def test_a_mail_action_without_confirmation_does_not_send_and_yes_skips_only_that(self):
        self.prepare_report()
        rc, prompts = self.ask("보고서 보내줘", answer="n")
        self.assertEqual((rc, len(prompts)), (1, 1))
        self.assertEqual(list(self.w.hub.unread("baseline")), [])
        rc, prompts = self.ask("--yes", "send the report", answer="n")
        self.assertEqual((rc, prompts), (0, []))              # --yes skips y/N for mail
        (msg,) = list(self.w.hub.unread("baseline"))
        self.assertEqual(parse_text(msg.text)[0]["handled"][0]["id"], "CMD-AG1")

    def test_a_report_that_fails_ga_check_is_not_sent(self):
        (self.w.work / "reports").mkdir()
        (self.w.work / "reports" / "bad.md").write_text(form({"schema": "report/2", "from": "AGY"}))
        rc, _ = self.ask("--yes", "보고서 보내줘")
        self.assertEqual(rc, 2)
        self.assertEqual(list(self.w.hub.unread("baseline")), [])

    def test_the_daily_cap_refuses_the_11th_agy_turn(self):
        self.script([{"text": f"answer {i}", "format": "json", "usage": {"input_tokens": 9900, "output_tokens": 20}}
                     for i in range(12)])
        rcs = [self.ask("--model", f"question {i}", answer="y")[0] for i in range(11)]
        self.assertEqual(rcs[:10], [0] * 10)
        self.assertEqual(rcs[10], 3)
        self.assertEqual(len(self.agy_turns()), 10)
        self.assertTrue(any("한도" in x for x in self.lines[-3:]))
        self.assertEqual(DayTurns(self.home).used(), 10)
        self.directive()                                       # a bridge run is refused too, nothing runs
        rc, prompts = self.ask("run the next directive", answer="y")
        self.assertEqual((rc, prompts, self.runner_calls), (3, [], []))

    def test_model_is_one_turn_tools_off_prompt_under_1500_tokens(self):
        big = self.home / "runs"
        big.mkdir()
        (big / "20260101-000000-run-abc.log").write_text(("턴 1: 입력 10,000 · 긴 줄 " * 40 + "\n") * 200)
        self.script([{"text": "ga는 허브 루프입니다", "format": "json", "usage": {"input_tokens": 10100, "output_tokens": 9}}])
        rc, _ = self.ask("--model", "ga가 뭐야? " * 50, answer="y")
        self.assertEqual(rc, 0)
        (call,) = self.agy_turns()
        argv = call["argv"]
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.assertIn("--disable-slash-commands", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        text = argv[argv.index("-p") + 1]
        self.assertLess(tokens(text), 1500)
        self.assertIn("Tools are off", text)
        self.assertIn("ga는 허브 루프입니다", self.lines)
        self.assertEqual(Ledger(self.home).rows()[-1]["input"], 10100)

    def test_waiting_spends_no_model_turn(self):
        """A real ga supervise process with a fake agy whose turns take time: ga polls its log many times while it
        waits, and the fake agy sees exactly the loop's own turns."""
        self.script([{"plan": plan([{"id": "a", "tool": "list_dir", "args": {}}], {"prompt": "sum up", "after": ["a"]}),
                      "sleep": 0.5},
                     {"plan": plan(say="the project has a README"), "sleep": 0.5}])
        self.directive()
        polls = []

        def counting_sleep(s):
            polls.append(s)
            time.sleep(s)
        runner = lambda c, conf, task, progress: bridge.run_supervise(c, conf, task, progress, sleep=counting_sleep)  # noqa: E731
        rc, _ = self.ask("run the next directive", answer="y", engine=self.engine(bridge_runner=runner))
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(polls), 5)                       # it waited, polling the log
        self.assertEqual(len(self.agy_turns()), 2)                    # and only the loop's two turns reached agy
        self.assertEqual(DayTurns(self.home).used(), 2)
        self.assertTrue(any(x.startswith("턴 1:") for x in self.lines))
        self.assertTrue(any(x.startswith("턴 2:") for x in self.lines))

    def test_free_actions_run_at_once(self):
        for q in ("뭐 할 수 있어?", "오늘 얼마나 썼어?", "status"):
            rc, prompts = self.ask(q)
            self.assertEqual((rc, prompts), (0, []), q)

    def test_next_shows_the_directive_short_and_does_not_read_it_away(self):
        self.directive()
        rc, _ = self.ask("다음 할 일 뭐야?")
        self.assertEqual(rc, 0)
        self.assertTrue(any("CMD-AG1" in x for x in self.lines))
        self.assertEqual(len(list(self.w.mac.unread("AGY"))), 1)

    def test_stop_stops_a_running_bridge(self):
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(p.kill)
        (self.home / "bridge.pid").write_text(str(p.pid))
        rc, _ = self.ask("멈춰")
        self.assertEqual(rc, 0)
        self.assertIsNotNone(p.wait(10))
        self.assertFalse((self.home / "bridge.pid").exists())


class D1Doctor(Base):
    SAMPLES = {
        "sandbox": "zsh: /Users/me/ga-venv/bin/ga: operation not permitted\n",
        "capacity": "AGY_ERROR: 503 MODEL_CAPACITY_EXHAUSTED: no capacity for this model right now\n",
        "quota": "AGY_ERROR: You have reached the quota limit for Gemini models.\n",
        "refused": "[ga supervise] agy refused 1 action(s) in T1.m1: command\n",
        "missing": "done.\nTOOL_NEEDED: run_tests - run the repo's unit tests\n",
    }

    def test_doctor_classifies_the_five_failure_kinds(self):
        for kind, text in self.SAMPLES.items():
            self.assertEqual(doctor.classify(text)[0], kind, text)
        self.assertIsNone(doctor.classify("턴 1: 입력 10,503 · 출력 12 토큰 · 3초 · 도구 0단계\nall good\n"))

    def test_doctor_reads_the_newest_failing_run_log(self):
        runs = self.home / "runs"
        runs.mkdir()
        for i, (kind, text) in enumerate(self.SAMPLES.items()):
            p = runs / f"2026100{i}-run-{kind}.log"
            p.write_text(text)
            os.utime(p, (1_000_000 + i, 1_000_000 + i))
            lf = doctor.last_failure(runs)
            self.assertEqual(lf[:2], (p.stem, kind))
        rc, _ = self.ask("왜 안 돼?")
        self.assertIn("missing", "\n".join(self.lines))
        self.assertEqual(rc, 1)
        names = [n for n, _ok, _d in doctor.checks(A.Engine(self.home).settings, runs)]
        self.assertEqual(names, ["ga on PATH", "venv", "agy found", "bridge config", "supervise config", "last failure"])


# ---- D3: ga ui --------------------------------------------------------------------------------------------------------

class UiBase(Base):
    def setUp(self):
        super().setUp()
        self.fake = None
        self.srv = UI.Server(lambda out: A.Engine(
            self.home, out=out, bridge_runner=self.fake_bridge_runner, solve_cli=self.fake, box=self.w.mac,
            sleep=lambda s: None), sse_poll_s=0.02, sse_idle_s=0.2)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, method, path, body=None, token=True, header_token=None, host=None, origin=None, extra=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=20)
        h = {"Host": host or f"127.0.0.1:{self.srv.port}"}
        if origin:
            h["Origin"] = origin
        if method == "GET" and token:
            path += ("&" if "?" in path else "?") + "t=" + self.srv.token
        if method == "POST" and (header_token or token):
            h["X-GA-Token"] = header_token or self.srv.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(extra or {})
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, dict(r.getheaders()), raw

    def sse(self, run, last_id=None):
        extra = {"Last-Event-ID": str(last_id)} if last_id is not None else None
        st, _h, raw = self.req("GET", f"/api/log?run={run}", extra=extra)
        self.assertEqual(st, 200)
        events, cur = [], {}
        for line in raw.decode().splitlines():
            if not line:
                if cur:
                    events.append(cur)
                cur = {}
            elif ":" in line and not line.startswith(":"):
                k, v = line.split(":", 1)
                cur[k] = v.strip()
        return events

    def wait_done(self, run):
        p = self.home / "runs" / f"{run}.done"
        for _ in range(500):
            if p.exists():
                return
            time.sleep(0.02)
        self.fail("run did not end")


class D3Ui(UiBase):
    def test_binds_127_0_0_1_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        self.assertEqual(UI.BIND, "127.0.0.1")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            UI.main(["--no-open", "--host", "0.0.0.0"])  # no option to bind elsewhere

    def test_no_token_wrong_token_foreign_origin_or_host_is_403(self):
        self.assertEqual(self.req("GET", "/", token=False)[0], 403)
        self.assertEqual(self.req("GET", "/api/state", token=False)[0], 403)
        self.assertEqual(self.req("GET", "/?t=wrong-token", token=False)[0], 403)
        self.assertEqual(self.req("POST", "/api/route", {"q": "help"}, token=False)[0], 403)
        self.assertEqual(self.req("POST", "/api/route", {"q": "help"}, header_token="wrong")[0], 403)
        self.assertEqual(self.req("GET", "/api/state", origin="http://evil.example")[0], 403)
        self.assertEqual(self.req("POST", "/api/route", {"q": "help"}, origin="http://evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/api/state", host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/app.js", host="evil.example")[0], 403)
        self.assertEqual(self.req("POST", "/api/route", {"q": "help"}, extra={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.req("GET", "/", origin=f"http://127.0.0.1:{self.srv.port}")[0], 200)
        self.assertEqual(self.req("GET", "/api/state", host=f"localhost:{self.srv.port}")[0], 200)

    def test_actions_need_post_and_the_token_header(self):
        self.prepare_report()
        st, _h, _b = self.req("GET", "/api/run?intent=report&confirm=true")
        self.assertNotEqual(st, 200)
        # the token in the query is enough for GET reads, never for a POST action
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.request("POST", f"/api/run?t={self.srv.token}", body=json.dumps({"intent": "report", "confirm": True}),
                  headers={"Host": f"127.0.0.1:{self.srv.port}", "Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 403)
        c.close()
        self.assertEqual(list(self.w.hub.unread("baseline")), [])
        st, _h, b = self.req("POST", "/api/run", {"intent": "report"})          # no confirmation -> 409, nothing sent
        self.assertEqual(st, 409)
        self.assertTrue(json.loads(b)["needs_confirm"])
        self.assertEqual(list(self.w.hub.unread("baseline")), [])
        st, _h, b = self.req("POST", "/api/run", {"intent": "report", "confirm": True})
        self.assertEqual(st, 200)
        self.wait_done(json.loads(b)["run"])
        self.assertEqual(len(list(self.w.hub.unread("baseline"))), 1)

    def test_csp_present_and_nothing_remote(self):
        for path in ("/", "/app.js", "/app.css", "/api/state"):
            st, h, body = self.req("GET", path)
            self.assertEqual(st, 200, path)
            csp = h["Content-Security-Policy"]
            self.assertIn("default-src 'none'", csp)
            self.assertIn("script-src 'self'", csp)
            self.assertNotIn("unsafe-inline", csp)
            self.assertNotRegex(csp, r"https?:|\*")
            self.assertEqual(h["X-Content-Type-Options"], "nosniff")
            self.assertEqual(h["Referrer-Policy"], "no-referrer")
        st, _h, page = self.req("GET", "/")
        html = page.decode()
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=\"/app\.js\")")   # no inline script
        self.assertNotIn(" on", re.sub(r"<script[^>]*>", "", html).replace(" only", ""))  # no inline handlers
        for f in ("index.html", "app.js", "app.css"):
            text = (Path(UI.__file__).parent / f).read_text()
            self.assertNotRegex(text, r"(?i)https?://|//cdn|@import|url\(", f)
        self.assertIn('lang="ko"', html)

    def test_the_token_is_in_no_log_or_file(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.req("GET", "/")
            self.req("GET", "/nope")
            self.req("GET", "/api/state", token=False)
            st, _h, b = self.req("POST", "/api/run", {"intent": "help"})
            self.wait_done(json.loads(b)["run"])
        self.assertNotIn(self.srv.token, err.getvalue())
        for p in self.w.tmp.rglob("*"):
            if p.is_file() and ".git" not in p.parts:
                self.assertNotIn(self.srv.token, p.read_text(errors="replace"), p)

    def test_main_prints_the_url_once(self):
        out = io.StringIO()
        made = {}
        real = UI.Server

        class Once(real):
            def serve_forever(self, poll_interval=0.5):
                made["srv"] = self
        with mock.patch.object(UI, "Server", Once), contextlib.redirect_stdout(out):
            UI.main(["--no-open"], engine_factory=lambda o: A.Engine(self.home, out=o))
        made["srv"].server_close()
        self.assertEqual(out.getvalue().count(made["srv"].token), 1)

    def test_sse_resumes_from_last_event_id(self):
        log = runlog.RunLog(self.home / "runs", "20260101-000000-help-abc123")
        for line in ("one", "two", "three"):
            log.write(line)
        log.close()
        ev = self.sse(log.id)
        data = [json.loads(e["data"]) for e in ev if "id" in e]
        self.assertEqual(data, ["one", "two", "three"])
        self.assertEqual(ev[-1]["event"], "end")
        first = ev[0]["id"]
        again = [json.loads(e["data"]) for e in self.sse(log.id, last_id=first) if "id" in e]
        self.assertEqual(again, ["two", "three"])

    def test_sse_shows_secret_looking_text_as_withheld(self):
        log = runlog.RunLog(self.home / "runs", "20260101-000000-x-secret")
        with log.path.open("a") as f:  # written past RunLog's own redaction: the SSE redacts again
            f.write("token " + "sk-" + "ant-api03-" + "B" * 40 + "\n")
        log.close()
        body = json.dumps(self.sse(log.id))
        self.assertIn("withheld", body)
        self.assertNotIn("B" * 40, body)

    def test_question_confirmation_fake_run_log_line(self):
        self.directive()
        st, _h, b = self.req("POST", "/api/route", {"q": "다음 지시 실행해줘"})
        r = json.loads(b)
        self.assertEqual((st, r["intent"], r["cost"], r["needs_confirm"]), (200, "run", "model", True))
        self.assertTrue(any("예상 입력 토큰" in x for x in r["lines"]))
        st, _h, b = self.req("POST", "/api/run", {"intent": "run"})
        self.assertEqual(st, 409)
        self.assertEqual(self.runner_calls, [])
        st, _h, b = self.req("POST", "/api/run", {"intent": "run", "confirm": True})
        self.assertEqual(st, 200)
        run = json.loads(b)["run"]
        self.wait_done(run)
        data = [json.loads(e["data"]) for e in self.sse(run) if "data" in e and "id" in e]
        self.assertTrue(any(x.startswith("턴 1: 입력 11,210") for x in data), data)
        self.assertTrue(any("report sent to baseline" in x for x in data), data)
        self.assertEqual(len(self.runner_calls), 1)
        st, _h, b = self.req("GET", "/api/state")
        self.assertEqual(json.loads(b)["agy_turns"], 1)

    def test_unmatched_question_suggests_three(self):
        st, _h, b = self.req("POST", "/api/route", {"q": "tell me a joke"})
        r = json.loads(b)
        self.assertIsNone(r["intent"])
        self.assertEqual(len(r["suggestions"]), 3)


# ---- D5: solve ---------------------------------------------------------------------------------------------------------

class SolveBase(Base):
    def solve(self, cli, confirm=True, **kw):
        eng = self.engine(solve_cli=cli)
        self.seen = []
        return eng.solve(kw.pop("q", "README를 요약해 줘"), lambda lines: self.seen.extend(lines) or confirm, poll_s=0.01,
                         **kw)

    def log_rows(self, r):
        p = self.home / "solve" / r["run"] / "state" / "log.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()]


ALWAYS_MORE = plan([{"id": "a", "tool": "list_dir", "args": {}}], {"prompt": "keep going", "after": ["a"]})


class D5Solve(SolveBase):
    def test_stops_at_the_turn_cap(self):
        cli = FakeCli([ALWAYS_MORE])
        r = self.solve(cli, cap=3)
        self.assertEqual(len(cli.calls), 3)
        self.assertEqual(r["totals"]["turns"], 3)
        self.assertNotEqual(r["status"], "done")

    def test_the_daily_cap_lowers_the_turn_cap_on_agv(self):
        DayTurns(self.home).add(8)
        cli = FakeCli([ALWAYS_MORE])
        r = self.solve(cli, cap=8)
        self.assertEqual(len(cli.calls), 2)
        self.assertEqual(r["plan"]["cap"], 2)  # 8 used + 2 = 10: the cap is spent now
        with self.assertRaises(S.SolveRefused) as cm:
            self.solve(FakeCli([ALWAYS_MORE]))
        self.assertEqual(cm.exception.code, 3)

    def test_nothing_runs_without_confirmation(self):
        cli = FakeCli([plan(say="x")])
        with self.assertRaises(S.SolveRefused) as cm:
            self.solve(cli, confirm=False)
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(cli.calls, [])
        self.assertEqual(Ledger(self.home).rows(), [])
        text = "\n".join(self.seen)
        for word in ("백엔드: agv", f"모델: {MODEL}", "턴 한도: 8", "예상 입력 토큰", f"{8 * AGV_OVERHEAD:,}"):
            self.assertIn(word, text)
        rc, prompts = self.ask("--solve", "--yes", "README 요약", answer="n", engine=self.engine(solve_cli=cli))
        self.assertEqual((rc, len(prompts), cli.calls), (1, 1, []))   # --yes does not skip it either

    def test_every_turn_line_shows_tokens_and_seconds_from_the_supervise_log(self):
        cli = FakeCli([ALWAYS_MORE, ALWAYS_MORE, plan(say="the answer")])
        r = self.solve(cli, cap=5)
        rows = [x for x in self.log_rows(r) if x["event"] == "turn"]
        turn_lines = [x for x in self.lines if x.startswith("턴 ")]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(turn_lines), 3)
        for row, line in zip(rows, turn_lines):
            self.assertIn(f"입력 {row['input_tokens']:,}", line)
            self.assertIn(f"출력 {row['output_tokens']:,}", line)
            self.assertIn(f"{row['seconds']:g}초", line)
        self.assertIn("도구 1단계", turn_lines[0])
        self.assertIn("the answer", self.lines)

    def test_end_totals_equal_the_turn_sums_and_the_ledger_gets_one_row(self):
        cli = FakeCli([ALWAYS_MORE, plan(say="done")])
        r = self.solve(cli, cap=4)
        rows = [x for x in self.log_rows(r) if x["event"] == "turn"]
        t = r["totals"]
        self.assertEqual(t["turns"], len(rows))
        self.assertEqual(t["input"], sum(x["input_tokens"] for x in rows))
        self.assertEqual(t["output"], sum(x["output_tokens"] for x in rows))
        self.assertAlmostEqual(t["seconds"], sum(x["seconds"] for x in rows))
        self.assertEqual(t["tool_steps"], 1)
        (row,) = Ledger(self.home).rows()
        for k in ("turns", "input", "output", "seconds", "tool_steps"):
            self.assertEqual(row[k], t[k], k)
        summary = [x for x in self.lines if x.startswith("── 합계")][0]
        self.assertIn(f"입력 {t['input']:,}", summary)
        self.assertIn(f"출력 {t['output']:,}", summary)
        self.assertEqual(Ledger(self.home).day()["input"], t["input"])
        self.assertEqual(DayTurns(self.home).used(), 2)
        self.lines.clear()
        self.ask("오늘 얼마나 썼어?")
        self.assertTrue(any(f"입력 {t['input']:,}" in x for x in self.lines), self.lines)

    def test_overhead_share_uses_the_backends_measured_overhead(self):
        cli = FakeCli([ALWAYS_MORE, plan(say="done")])
        r = self.solve(cli, cap=4)
        t = r["totals"]
        self.assertEqual(r["overhead"], AGV_OVERHEAD)
        self.assertEqual(r["overhead_share"], round(2 * AGV_OVERHEAD / t["input"], 4))
        self.assertEqual(Ledger(self.home).rows()[0]["overhead_share"], r["overhead_share"])
        ov, src = S.overhead_of("claude_cli", "claude-haiku-4-5-20251001", Ledger(self.home))
        self.assertEqual(ov, 945)                     # the catalog's measured number, not a constant
        self.assertIn("measured", src)

    def test_a_non_table_tool_is_refused_and_listed(self):
        # the project declares a write tool in its own ga-supervise.json: solve still hands the loop ga's table only
        conf = json.loads((self.w.work / "ga-supervise.json").read_text())
        conf["tools"] = {"write_file": {"python": "ga36_tools:write_file", "about": "write a file"}}
        (self.w.work / "ga-supervise.json").write_text(json.dumps(conf))
        cwd = os.getcwd()
        os.chdir(self.w.work)
        self.addCleanup(os.chdir, cwd)
        cli = FakeCli([plan([{"id": "w", "tool": "write_file", "args": {"path": "x", "text": "y"}}], None),
                       plan(say="unused")])
        r = self.solve(cli, cap=3)
        self.assertEqual(r["refused"], ["write_file"])
        self.assertTrue(any(x.get("event") == "plan" and not x.get("ok") and "tool_not_in_table" in x.get("problems", "")
                            for x in self.log_rows(r)))  # refused by ga's own plan check, by label
        self.assertTrue(any("거부된 도구" in x and "write_file" in x for x in self.lines))
        self.assertFalse(any(x.get("event") == "tool" and x.get("tool") == "write_file" for x in self.log_rows(r)))
        self.assertFalse((self.w.work / "x").exists())
        self.assertEqual(Ledger(self.home).rows()[0]["refused_tools"], ["write_file"])

    def test_tool_needed_is_listed_never_installed(self):
        class Raw(FakeCli):  # the model writes a TOOL_NEEDED line beside its plan
            def run_turn(self, *a, **kw):
                t = super().run_turn(*a, **kw)
                return BackendTurn("TOOL_NEEDED: pip_install - install a package\n" + t.answer, t.served, t.usage,
                                   t.usage_format, None, t.seconds, 1)
        r = self.solve(Raw([plan(say="ok")]), cap=2)
        self.assertEqual(r["needed"], ["pip_install - install a package"])
        self.assertTrue(any("TOOL_NEEDED" in x for x in self.lines))

    def test_solve_through_the_real_agv_adapter_and_fake_agy(self):
        self.script([{"plan": plan([{"id": "a", "tool": "read_file", "args": {"path": "README.md"}}],
                                   {"prompt": "sum", "after": ["a"]}), "format": "json",
                      "usage": {"input_tokens": 10012, "output_tokens": 40, "total_tokens": 10052}},
                     {"plan": plan(say="hello world"), "format": "json",
                      "usage": {"input_tokens": 10500, "output_tokens": 15, "total_tokens": 10515}}])
        cwd = os.getcwd()
        os.chdir(self.w.work)
        self.addCleanup(os.chdir, cwd)
        r = self.solve(None, cap=4)
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["answer"], "hello world")
        self.assertEqual(r["totals"]["input"], 20512)
        self.assertEqual(r["totals"]["output"], 55)
        self.assertEqual(len(self.agy_turns()), 2)
        self.assertTrue(all("--dangerously-skip-permissions" not in c["argv"] for c in self.agy_turns()))

    def test_openai_http_needs_a_base_url(self):
        with self.assertRaises(S.SolveRefused):
            self.solve(FakeCli([plan(say="x")]), backend="openai_http", model="gpt-oss-120b")


class D5UiSolve(UiBase):
    def setUp(self):
        super().setUp()
        self.fake = FakeCli([ALWAYS_MORE, plan(say="ui answer")])

    def test_the_ui_solve_box_drives_the_same_path(self):
        st, _h, b = self.req("POST", "/api/solve", {"q": "README 요약", "cap": "4"})
        r = json.loads(b)
        self.assertEqual(st, 409)
        self.assertTrue(r["needs_confirm"])
        self.assertTrue(any("백엔드: agv" in x for x in r["lines"]))
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(Ledger(self.home).rows(), [])
        st, _h, b = self.req("POST", "/api/solve", {"q": "README 요약", "cap": "4", "confirm": True})
        self.assertEqual(st, 200)
        run = json.loads(b)["run"]
        self.wait_done(run)
        data = [json.loads(e["data"]) for e in self.sse(run) if "id" in e]
        self.assertTrue(any(x.startswith("턴 1: 입력 12,000") for x in data), data)
        self.assertTrue(any(x.startswith("── 합계") for x in data), data)
        self.assertIn("ui answer", data)
        self.assertEqual(len(self.fake.calls), 2)
        (row,) = Ledger(self.home).rows()
        self.assertEqual(row["kind"], "solve")
        self.assertEqual(row["input"], 12000 + 12111)


class Version(unittest.TestCase):
    def test_minor_bump(self):
        import ga
        self.assertEqual(ga.__version__, "0.8.0")
        text = (TESTS.parent / "pyproject.toml").read_text()
        self.assertIn('version = "0.8.0"', text)
        self.assertIn('"ui/*.html", "ui/*.js", "ui/*.css"', text)

    def test_cli_entries_are_listed(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            from ga.__main__ import main
            main(["--help"])
        for word in ("ask", "bridge", "ui", "GA Engine"):
            self.assertIn(word, out.getvalue())


if __name__ == "__main__":
    unittest.main()
