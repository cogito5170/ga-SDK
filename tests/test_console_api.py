"""CMD-CON2 D1: GA API (`ga console`) — every endpoint's contract shape, work statuses, branches, decisions, tokens,
mail heads (nothing marked read, secrets withheld), SSE resume, ask/do confirmation, and GA36's security.
Offline: temp git repos, a temp baseline-shaped folder, a fake mailbox branch, fake service commands; 0 models."""
import contextlib
import http.client
import io
import json
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_world import ENV_VALUE, FAKE_SECRET, World  # noqa: E402

from ga.console import collectors as C  # noqa: E402
from ga.console import config as K  # noqa: E402
from ga.console import server as S  # noqa: E402
from ga.runlog import WITHHELD  # noqa: E402
from ga import ui as UI  # noqa: E402

STATE_KEYS = {"now", "bridge", "repos", "services", "counts"}
REPO_KEYS = {"name", "path", "branch", "head", "head_subject", "dirty", "integration_branch", "behind", "ahead"}
SVC_KEYS = {"name", "state", "port", "health", "started_at", "pid"}
WORK_KEYS = {"id", "title", "to", "kind", "status", "sent_at", "reported_at", "tokens", "bd", "summary_ko", "paths",
             "branch"}
BRANCH_KEYS = {"repo", "branch", "head", "subject", "at", "merged_into_integration", "ahead", "behind", "label_ko"}
TOKEN_ROW_KEYS = {"source", "id", "model", "turns", "input", "output", "cache_read", "total", "cost_usd", "at"}
MAIL_KEYS = {"path", "from", "to", "form", "schema", "valid", "at"}


class FakeEngine:
    """Stands in for GA36's ask Engine: records what ran; its prepare says what each would cost."""
    ran: list = []

    def __init__(self, out):
        self.out, self.log = out, None

    def prepare(self, name):
        cost = {"status": "free", "run": "model", "report": "mail"}.get(name, "free")
        return {"intent": name, "cost": cost, "lines": [f"would run {name}"], "refuse": None,
                **({"turns": 3, "estimate": 30000} if cost == "model" else {})}

    def prepare_model(self, q):
        return {"cost": "model", "prompt_tokens": 1234, "lines": ["agy 1 turn"], "refuse": None, "text": q}

    def new_log(self, kind):
        class L:
            id = "x"
            done_path = Path("/nonexistent/done")

            def close(self, *a):
                pass
        return L()

    def execute(self, name, confirmed=False):
        FakeEngine.ran.append((name, confirmed))
        self.out(f"ran {name}")
        return 0

    def run_model(self, p, confirmed=False):
        FakeEngine.ran.append(("model", confirmed))
        self.out("model ran")
        return 0


class Base(unittest.TestCase):
    services = None

    def setUp(self):
        self.w = World()
        self.addCleanup(shutil.rmtree, self.w.tmp, True)
        self.cfg = self.w.config(services=self.services)
        FakeEngine.ran = []
        self.do_runs = []
        self.srv = S.Console(self.cfg, engine_factory=lambda out: FakeEngine(out),
                             do_argv=lambda q: self.do_runs.append(q) or [sys.executable, "-c", "print('did it')"],
                             watch_every_s=0.05, sse_keepalive_s=0.1, health=lambda url: False)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.srv.close)
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
        data = json.dumps(body).encode() if body is not None else None
        h.update(extra or {})
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, dict(r.getheaders()), raw

    def get(self, path):
        st, _h, raw = self.req("GET", path)
        self.assertEqual(st, 200, raw[:300])
        return json.loads(raw)

    def post(self, path, body=None):
        st, _h, raw = self.req("POST", path, body or {})
        return st, json.loads(raw)

    def events(self, last_id=None, want=1, timeout=10.0):
        """Read the SSE stream until ``want`` events arrived (then close)."""
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=timeout)
        h = {"Host": f"127.0.0.1:{self.srv.port}"}
        if last_id is not None:
            h["Last-Event-ID"] = str(last_id)
        c.request("GET", f"/api/events?t={self.srv.token}", headers=h)
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        evs, cur, end = [], {}, time.time() + timeout
        while len(evs) < want and time.time() < end:
            line = r.fp.readline().decode().rstrip("\n")
            if not line:
                if cur:
                    cur["data"] = json.loads(cur["data"])
                    cur["id"] = int(cur["id"])
                    evs.append(cur)
                cur = {}
            elif not line.startswith(":"):
                k, v = line.split(":", 1)
                cur[k] = v.strip()
        c.close()
        return evs


class D1Shapes(Base):
    def test_state_shape(self):
        s = self.get("/api/state")
        self.assertEqual(set(s), STATE_KEYS)
        self.assertEqual(set(s["bridge"]), {"running", "last_seen", "last_message", "config_path"})
        self.assertEqual(set(s["counts"]), {"work_open", "mail_unread", "services_running"})
        self.assertEqual([set(r) for r in s["repos"]], [REPO_KEYS] * 3)
        self.assertEqual({x["name"] for x in s["services"]}, {"fake-api", "bridge"})
        for x in s["services"]:
            self.assertEqual(set(x), SVC_KEYS)
            self.assertEqual((x["state"], x["health"], x["pid"]), ("stopped", "unknown", None))
        code = s["repos"][1]
        self.assertEqual((code["branch"], code["integration_branch"], code["dirty"]), ("claude/open", "main", True))
        self.assertEqual((code["ahead"], code["behind"]), (2, 0))
        self.assertEqual(code["head_subject"], "more open work")
        self.assertEqual(len(code["head"]), 40)
        gone = s["repos"][2]
        self.assertEqual((gone["head"], gone["branch"], gone["dirty"]), (None, None, None))
        self.assertFalse(s["bridge"]["running"])
        self.assertTrue(s["bridge"]["config_path"].endswith("agy-bridge.json"))
        self.assertEqual(s["counts"]["work_open"], 3)  # A3 sent, A5 reported, ITEM-7 running
        self.assertEqual(s["counts"]["mail_unread"], 5)  # five to baseline, baseline never sent: since-last-send

    def test_work_shape_and_statuses(self):
        w = {x["id"]: x for x in self.get("/api/work")}
        for x in w.values():
            self.assertEqual(set(x), WORK_KEYS)
            self.assertEqual(set(x["paths"]), {"directive", "report", "item"})
            self.assertIn(x["status"], C.STATUSES)
        got = {i: x["status"] for i, x in w.items()}
        self.assertEqual(got, {"CMD-A1": "integrated", "CMD-A2": "sent_back", "CMD-A3": "sent", "CMD-A4": "draft",
                               "CMD-A5": "reported", "CMD-A6": "failed", "ITEM-7": "running"})
        a5 = w["CMD-A5"]
        self.assertEqual((a5["tokens"], a5["branch"], a5["kind"], a5["to"]), (550, "claude/a5", "directive", "AGY"))
        self.assertEqual(a5["sent_at"], "2026-10-01T00:00:04+00:00")
        self.assertEqual(a5["reported_at"], "2026-10-02T00:00:03+00:00")
        self.assertEqual(a5["paths"]["directive"], "directives/CMD-A5.md")
        self.assertTrue(a5["paths"]["report"].startswith("to/baseline/"))
        self.assertEqual(w["CMD-A1"]["bd"], ["BD-12", "BD-10"])
        self.assertEqual(w["CMD-A1"]["summary_ko"], "CMD-A1 통합 (integrated into main).")  # the BD row's first sentence
        self.assertEqual(w["CMD-A2"]["summary_ko"], "CMD-A2 sent back: the tests were missing.")
        self.assertEqual(w["CMD-A4"]["summary_ko"], "Only a draft.")  # no BD row: the directive goal
        self.assertEqual(w["CMD-A4"]["sent_at"], None)
        self.assertEqual(w["ITEM-7"]["kind"], "item")
        self.assertEqual(w["ITEM-7"]["paths"]["item"], "ops/agy_bridge/items/ITEM-7.json")

    def test_reported_then_integrated_by_a_new_decision_row(self):
        self.assertEqual({x["id"]: x["status"] for x in self.get("/api/work")}["CMD-A5"], "reported")
        p = self.w.base / "DECISION_LOG.md"
        text = p.read_text().replace("| BD-12 |", "| BD-13 | CMD-A5 merged into main. | BD-13 |\n| BD-12 |", 1)
        p.write_text(text)
        self.assertEqual({x["id"]: x["status"] for x in self.get("/api/work")}["CMD-A5"], "integrated")

    def test_running_while_the_bridge_names_it(self):
        st, b = self.post("/api/bridge/start")
        self.assertEqual(st, 200)
        for _ in range(200):
            if self.get("/api/state")["bridge"]["last_message"]:
                break
            time.sleep(0.02)
        s = self.get("/api/state")
        self.assertTrue(s["bridge"]["running"])
        self.assertEqual(s["bridge"]["last_message"], "bridge: waiting for CMD-A3")
        self.assertIsNotNone(s["bridge"]["last_seen"])
        self.assertEqual({x["id"]: x["status"] for x in self.get("/api/work")}["CMD-A3"], "running")
        self.assertEqual(self.post("/api/bridge/stop")[1]["state"], "stopped")
        self.assertFalse(self.get("/api/state")["bridge"]["running"])
        logs = self.get("/api/services/bridge/logs")
        self.assertIn("bridge: waiting for CMD-A3", [x["text"] for x in logs["lines"]])

    def test_branches_shape_and_merged(self):
        bs = self.get("/api/branches")
        for b in bs:
            self.assertEqual(set(b), BRANCH_KEYS)
        code = {b["branch"]: b for b in bs if b["repo"] == "code"}
        self.assertEqual(set(code), {"main", "claude/merged", "claude/open"})
        self.assertTrue(code["claude/merged"]["merged_into_integration"])
        self.assertEqual(code["claude/merged"]["label_ko"], "통합됨")
        self.assertFalse(code["claude/open"]["merged_into_integration"])
        self.assertEqual((code["claude/open"]["ahead"], code["claude/open"]["behind"]), (2, 0))
        self.assertEqual(code["main"]["label_ko"], "통합 브랜치")
        self.assertEqual(code["claude/open"]["subject"], "more open work")
        self.assertNotIn("ga-mailbox", {b["branch"] for b in bs})

    def test_decisions_search(self):
        all_ = self.get("/api/decisions")
        self.assertEqual([d["id"] for d in all_], ["BD-12", "BD-11", "BD-10", "BD-9"])
        self.assertEqual(set(all_[0]), {"id", "text", "n"})
        self.assertEqual(all_[0]["n"], 12)
        self.assertEqual([d["id"] for d in self.get("/api/decisions?q=CMD-A1")], ["BD-12", "BD-10"])
        self.assertEqual([d["id"] for d in self.get("/api/decisions?q=sent%20back")], ["BD-11"])
        self.assertEqual([d["id"] for d in self.get("/api/decisions?q=TOKEN%20path")], ["BD-9"])
        self.assertEqual(self.get("/api/decisions?q=nothing-like-this"), [])

    def test_tokens_shape(self):
        t = self.get("/api/tokens")
        self.assertEqual(set(t), {"today", "rows"})
        self.assertEqual(set(t["today"]), {"agy_turns", "input", "output", "total"})
        for r in t["rows"]:
            self.assertEqual(set(r), TOKEN_ROW_KEYS)
        by = {(r["source"], r["id"]): r for r in t["rows"]}
        act = by[("act", "ITEM-7")]
        self.assertEqual((act["turns"], act["input"], act["output"], act["cache_read"], act["total"]),
                         (2, 300, 30, 5, 335))
        self.assertEqual(by[("supervise", "CMD-A3")]["input"], 900)
        self.assertEqual(by[("bridge", "CMD-A5")]["total"], 550)
        self.assertEqual(by[("bridge", "CMD-A5")]["model"], "gpt-5.1")
        rw = by[("rw1", "CMD-A1")]
        self.assertEqual((rw["input"], rw["output"], rw["cache_read"], rw["cost_usd"], rw["model"]),
                         (12000, 3000, 50000, 0.42, "claude-opus"))

    def test_tokens_today(self):
        day = time.mktime((2026, 10, 5, 12, 0, 0, 0, 0, -1))
        t = C.tokens(self.cfg, self.srv.reader, clock=lambda: day)
        self.assertEqual(t["today"]["agy_turns"], 3)
        self.assertEqual((t["today"]["input"], t["today"]["output"], t["today"]["total"]), (300, 30, 335))

    def test_mail_heads_only_and_nothing_marked_read(self):
        m = self.get("/api/mail?limit=3")
        self.assertEqual(len(m), 3)
        for x in m:
            self.assertEqual(set(x), MAIL_KEYS)
        self.assertEqual(m[0]["form"], "leak")  # newest first
        self.assertEqual(len(self.get("/api/mail")), len(self.w.mail))
        for path in ("/api/state", "/api/work", "/api/tokens", "/api/mail"):
            self.get(path)
        box = self.srv.reader.box
        for name in ("baseline", "AGY", "GA"):
            self.assertFalse(box.cursor_file(name).exists(), name)
        self.assertEqual(len(list(box.unread("baseline"))), 5)  # still all unread for ga mail itself

    def test_index_and_static(self):
        st, h, body = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"GA Console", body)
        self.assertEqual(h["Content-Type"], "text/html; charset=utf-8")
        st, _h, _b = self.req("GET", "/index.html", token=False)
        self.assertEqual(st, 403)
        self.assertEqual(self.req("GET", "/../server.py", token=False)[0], 403)
        self.assertEqual(self.req("GET", "/%2e%2e/server.py")[0], 404)
        st, _h, _b = self.req("GET", "/api/services/nope/logs")
        self.assertEqual(st, 404)


class D1Secrets(Base):
    def test_a_secret_looking_string_is_withheld_everywhere(self):
        (self.w.base / "DECISION_LOG.md").write_text(
            (self.w.base / "DECISION_LOG.md").read_text() + f"| BD-3 | key {FAKE_SECRET} pasted | BD-3 |\n")
        d = (self.w.base / "directives" / "CMD-A4.md")
        d.write_text(d.read_text().replace("Only a draft.", f"Use {FAKE_SECRET} here."))
        blob = b""
        for path in ("/api/state", "/api/work", "/api/branches", "/api/tokens", "/api/decisions", "/api/mail",
                     "/api/decisions?q=BD-3"):
            st, _h, raw = self.req("GET", path)
            self.assertEqual(st, 200)
            blob += raw
        self.assertNotIn(FAKE_SECRET[:20].encode(), blob)
        self.assertIn(WITHHELD.encode(), blob)
        self.assertEqual(self.get("/api/decisions?q=BD-3")[0]["text"], WITHHELD)
        leak = [x for x in self.get("/api/mail") if x["form"] == "leak"][0]
        self.assertFalse(leak["valid"])

    def test_clean_walks_everything(self):
        self.assertEqual(C.clean({"a": [FAKE_SECRET, {"b": FAKE_SECRET}], "c": 1}),
                         {"a": [WITHHELD, {"b": WITHHELD}], "c": 1})


class D1Security(Base):
    def test_binds_127_0_0_1_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        self.assertIs(S.BIND, UI.BIND)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            S.main(["--no-open", "--host", "0.0.0.0"])

    def test_no_token_foreign_origin_or_host_is_403(self):
        for path in ("/", "/api/state", "/api/work", "/api/branches", "/api/tokens", "/api/decisions", "/api/mail",
                     "/api/events", "/api/services/fake-api/logs"):
            self.assertEqual(self.req("GET", path, token=False)[0], 403, path)
            self.assertEqual(self.req("GET", path + "?t=wrong", token=False)[0], 403, path)
            self.assertEqual(self.req("GET", path, origin="http://evil.example")[0], 403, path)
            self.assertEqual(self.req("GET", path, host="evil.example")[0], 403, path)
            self.assertEqual(self.req("GET", path, extra={"Sec-Fetch-Site": "cross-site"})[0], 403, path)
        self.assertEqual(self.req("GET", "/api/state", origin=f"http://127.0.0.1:{self.srv.port}")[0], 200)
        self.assertEqual(self.req("GET", "/api/state", host=f"localhost:{self.srv.port}")[0], 200)

    def test_actions_need_post_and_the_token_header(self):
        for path in ("/api/services/fake-api/start", "/api/bridge/start", "/api/ask"):
            self.assertEqual(self.req("POST", path, {}, token=False)[0], 403, path)
            self.assertEqual(self.req("POST", path, {}, header_token="wrong")[0], 403, path)
            self.assertEqual(self.req("POST", path, {}, origin="http://evil.example")[0], 403, path)
            self.assertNotEqual(self.req("GET", path)[0], 200, path)  # a GET never acts
            c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)  # the query token is not enough
            c.request("POST", f"{path}?t={self.srv.token}", body=b"{}", headers={"Host": f"127.0.0.1:{self.srv.port}"})
            self.assertEqual(c.getresponse().status, 403, path)
            c.close()
        self.assertEqual(self.get("/api/state")["counts"]["services_running"], 0)
        self.assertEqual(FakeEngine.ran, [])

    def test_csp_and_no_store_on_every_answer(self):
        for method, path in (("GET", "/"), ("GET", "/api/state"), ("GET", "/api/mail"), ("GET", "/api/nope"),
                             ("GET", "/api/state?t=wrong"), ("POST", "/api/ask")):
            st, h, body = self.req(method, path, {} if method == "POST" else None)
            self.assertEqual(h.get("Content-Security-Policy"), UI.CSP, path)
            self.assertEqual(h.get("Cache-Control"), "no-store", path)
            self.assertNotIn(b"http://", body.replace(f"http://127.0.0.1:{self.srv.port}".encode(), b""), path)
        self.assertIn("default-src 'none'", UI.CSP)
        self.assertNotIn("http", UI.CSP)

    def test_the_token_is_never_logged_or_returned(self):
        blob = b""
        for path in ("/api/state", "/api/work", "/api/branches", "/api/tokens", "/api/mail"):
            blob += self.req("GET", path)[2]
        self.assertNotIn(self.srv.token.encode(), blob)


class D1Events(Base):
    def test_sse_resumes_with_last_event_id(self):
        first = self.events(want=1)
        self.assertEqual(first[0]["event"], "state")
        self.assertEqual(set(first[0]["data"]), STATE_KEYS)
        a = self.srv.bus.publish("work", [{"id": "x"}])
        b = self.srv.bus.publish("mail", [{"path": "p"}])
        c = self.srv.bus.publish("log", {"text": "hello"})
        evs = self.events(last_id=a, want=2)
        self.assertEqual([(e["id"], e["event"]) for e in evs], [(b, "mail"), (c, "log")])
        evs = self.events(last_id=b, want=1)
        self.assertEqual([(e["id"], e["event"], e["data"]) for e in evs], [(c, "log", {"text": "hello"})])

    def test_a_reader_that_fell_out_of_the_ring_gets_state_first(self):
        self.srv.bus = S.Bus(size=3)
        for i in range(6):
            self.srv.bus.publish("log", {"i": i})
        evs = self.events(last_id=1, want=4)
        self.assertEqual(evs[0]["event"], "state")
        self.assertEqual([e["data"]["i"] for e in evs[1:]], [3, 4, 5])

    def test_changes_are_found_by_code(self):
        self.srv.watch_once()  # the baseline
        self.assertEqual(self.srv.watch_once(), [])  # nothing changed: nothing sent
        last = self.srv.bus.n
        (self.w.base / "directives" / "CMD-A9.md").write_text(
            "```ga\n" + json.dumps({"schema": "directive/2", "id": "CMD-A9", "to": "AGY", "goal": "New."}) + "\n```\n")
        sent = self.srv.watch_once()
        self.assertEqual(sent, ["work", "state"])
        evs = self.events(last_id=last, want=2)
        self.assertEqual([e["event"] for e in evs], ["work", "state"])
        self.assertIn("CMD-A9", [x["id"] for x in evs[0]["data"]])
        # the mailbox tip moved
        self.w.write_mail([("baseline", "20261003T000001.000001Z", "AGY", "CMD-A3",
                            self.w.report("CMD-A3", "done", 10, 1, 1))])
        self.assertEqual(self.srv.watch_once(), ["mail", "work", "state"])
        # a git head moved
        git_ = __import__("console_world").git
        git_(self.w.code, "commit", "-q", "-am", "dirty no more")
        self.assertEqual(self.srv.watch_once(), ["work", "state"])
        # a new act turn
        with (self.w.act / "2026-10-05.jsonl").open("a") as f:
            f.write(json.dumps({"item": "ITEM-7", "turn": 3, "model": "m1", "input": 7, "output": 1}) + "\n")
        sent = self.srv.watch_once()
        self.assertEqual(sent[0], "act_turn")
        last = self.srv.bus.n
        self.assertEqual([n for n, t, d in self.srv.bus.ring if t == "act_turn"][-1] <= last, True)
        turn = [d for n, t, d in self.srv.bus.ring if t == "act_turn"][-1]
        self.assertEqual((turn["item"], turn["turn"], turn["input"]), ("ITEM-7", 3, 7))

    def test_the_watcher_never_calls_a_model(self):
        import ga.ask.model as M
        calls = []
        orig = M.one_turn
        M.one_turn = lambda *a, **k: calls.append(a)
        self.addCleanup(setattr, M, "one_turn", orig)
        self.srv.start_watcher()
        (self.w.base / "DECISION_LOG.md").write_text("| BD-1 | x | BD-1 |\n")
        evs = self.events(want=3, timeout=5)
        self.assertGreaterEqual(len(evs), 2)
        self.assertEqual(calls, [])
        self.assertEqual(FakeEngine.ran, [])


class D1Ask(Base):
    def test_ask_shows_the_plan_and_cost_then_runs_only_on_confirm(self):
        st, p = self.post("/api/ask", {"q": "다음 지시 실행해", "mode": "ask"})
        self.assertEqual(st, 200)
        self.assertEqual(set(p), {"plan", "cost_estimate", "confirm_id"})
        self.assertEqual(p["cost_estimate"]["cost"], "model")
        self.assertEqual(p["cost_estimate"]["input_tokens"], 30000)
        self.assertEqual(FakeEngine.ran, [])  # nothing ran yet
        self.assertEqual(self.post("/api/ask/confirm", {"confirm_id": "nope"})[0], 404)
        st, r = self.post("/api/ask/confirm", {"confirm_id": p["confirm_id"]})
        self.assertEqual(st, 200)
        self.assertEqual(set(r), {"run_id"})
        for _ in range(200):
            if FakeEngine.ran:
                break
            time.sleep(0.02)
        self.assertEqual(FakeEngine.ran, [("run", True)])
        self.assertEqual(self.post("/api/ask/confirm", {"confirm_id": p["confirm_id"]})[0], 404)  # one use
        logs = [d for n, t, d in self.srv.bus.ring if t == "log" and d.get("run_id") == r["run_id"]]
        self.assertIn("ran run", [d.get("text") for d in logs])

    def test_free_text_goes_to_the_model_path_with_its_cost(self):
        st, p = self.post("/api/ask", {"q": "zzqx why is the sky", "mode": "ask"})
        self.assertEqual(st, 200)
        self.assertEqual(p["cost_estimate"], {"cost": "model", "input_tokens": 1234, "turns": 1})

    def test_do_runs_ga_do_as_a_child_after_confirm(self):
        st, p = self.post("/api/ask", {"q": "add a login test", "mode": "do"})
        self.assertEqual(st, 200)
        self.assertEqual(p["cost_estimate"]["cost"], "model")
        self.assertEqual(self.do_runs, [])
        st, r = self.post("/api/ask/confirm", {"confirm_id": p["confirm_id"]})
        self.assertEqual(st, 200)
        for _ in range(300):
            if any(t == "log" and d.get("end") for n, t, d in self.srv.bus.ring):
                break
            time.sleep(0.02)
        self.assertEqual(self.do_runs, ["add a login test"])
        texts = [d.get("text") for n, t, d in self.srv.bus.ring if t == "log" and d.get("run_id") == r["run_id"]]
        self.assertIn("did it", texts)

    def test_bad_requests(self):
        self.assertEqual(self.post("/api/ask", {"q": "", "mode": "ask"})[0], 400)
        self.assertEqual(self.post("/api/ask", {"q": "x", "mode": "rm"})[0], 400)

    def test_default_do_argv_has_no_shell(self):
        a = S.Asker(self.cfg, S.Bus(), engine_factory=lambda out: FakeEngine(out))
        argv = a.do_argv("x; rm -rf /")
        self.assertEqual(argv[-2:], ["--", "x; rm -rf /"])
        self.assertEqual(argv[1:4], ["-m", "ga", "do"])


class D2Version(unittest.TestCase):
    def test_version_0_10_0_and_the_static_page_is_packaged(self):
        import ga
        self.assertGreaterEqual(tuple(map(int, ga.__version__.split("."))), (0, 10, 0))  # GA43 moved it on to 0.11.0
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        self.assertIn(f'version = "{ga.__version__}"', text)
        self.assertIn('"console/static/*"', text)


class D1Config(unittest.TestCase):
    def test_init_writes_the_mac_layout(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        p = d / "c.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.main(["init", "--config", str(p)]), 0)
        raw = json.loads(p.read_text())
        self.assertEqual(raw["baseline"], "~/baseline")
        self.assertEqual(set(raw["services"]), {"token-api", "token-worker", "token-web"})
        api = raw["services"]["token-api"]
        self.assertEqual(api["argv"][1:], ["app.main:create_app", "--factory", "--port", "8000"])
        self.assertTrue(api["argv"][0].endswith("/bin/uvicorn"))
        self.assertEqual(api["argv"][0], "~/token/.venv/bin/uvicorn")  # README path B: the venv is at the checkout root
        self.assertEqual(raw["services"]["token-worker"]["argv"][0], "~/token/.venv/bin/python")
        self.assertEqual({r["integration_branch"] for r in raw["repos"]}, {"claude/gracious-meitner-vp49xe"})
        self.assertEqual((api["cwd"], api["env_file"]), ("~/token/backend", "~/token/.env"))
        self.assertEqual(api["health_url"], "http://127.0.0.1:8000/healthz")
        self.assertEqual(raw["services"]["token-worker"]["argv"][1:], ["-m", "app.worker"])
        self.assertEqual(raw["services"]["token-web"]["argv"], ["npm", "run", "dev"])
        self.assertEqual(raw["services"]["token-web"]["cwd"], "~/token/frontend")
        self.assertEqual(raw["bridge"]["argv"][0], sys.executable)  # the bridge imports the ga that ran init
        self.assertEqual(raw["bridge"]["argv"], [sys.executable, "~/baseline/ops/agy_bridge/bridge.py", "--config",
                                                 "~/agy-bridge.json"])
        self.assertIn("~/ga-sdk-check", [r["path"] for r in raw["repos"]])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(S.main(["init", "--config", str(p)]), 2)  # never overwritten without --force
        cfg = K.load(p)
        self.assertFalse(cfg["baseline"].startswith("~"))
        self.assertIn("bridge", cfg["services"])

    def test_a_shell_string_is_not_an_argv(self):
        for bad in ("npm run dev", [], ["ok", 3]):
            with self.assertRaises(K.ConfigError):
                K.check({"baseline": "/x", "services": {"web": {"argv": bad}}})
        with self.assertRaises(K.ConfigError):
            K.check({"baseline": "/x", "services": {"w": {"argv": ["x"], "health_url": "http://example.com/"}}})

    def test_the_cli_entry(self):
        import ga.__main__ as M
        self.assertEqual(M.OWN_PARSER["console"], "ga.console.server:main")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(M.main(["console", "--config", "/nonexistent/c.json", "--no-open"]), 2)


if __name__ == "__main__":
    unittest.main()
