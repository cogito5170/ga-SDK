"""CMD-GA50 D1: ga.events/1 — the emitter, the emitters (ga act, bridge, mailbox, hub / shadow, vm update), the
console's event collector and /api/events/recent, and the 실시간 screen (golden + the app on a recorded stream) under
the judge at 375 and 1440. Offline: fake runners, fake commands, local bare remotes; 0 network, 0 models."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "console_frontend"))

from ga import events as EV  # noqa: E402
from ga.forms import dump_wire  # noqa: E402
from ga.mailbox import Message  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ga50" / "events.jsonl"
SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


class EvCase(unittest.TestCase):
    """Every test writes its events to its own file (GA_EVENTS), never to a person's ~/.ga."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ga50-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.evf = self.tmp / "events.jsonl"
        p = mock.patch.dict(os.environ, {"GA_EVENTS": str(self.evf)})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop(EV.ENV_PARENT, None)

    def evs(self):
        if not self.evf.exists():
            return []
        return [json.loads(x) for x in self.evf.read_text(encoding="utf-8").splitlines() if x.strip()]

    def assertValid(self, evs):
        for e in evs:
            self.assertEqual(EV.validate(e), [], e)


# ---- S1: the schema and the emitter --------------------------------------------------------------------------------

class Schema(EvCase):
    def test_emit_and_span_write_valid_linked_events(self):
        with EV.span("TASK", "CMD-X1", "item", goal="g", steps=["a", "b"]) as t:
            t.update("step", step="a")
            with EV.span("TOOL", "act", "RUN", input="RUN test") as tool:
                tool.retry(why="again")
            EV.emit("FILE", "act", "EDIT", "DONE", path="a.py")
        evs = self.evs()
        self.assertValid(evs)
        self.assertEqual([(e["type"], e["status"]) for e in evs],
                         [("TASK", "STARTED"), ("TASK", "RUNNING"), ("TOOL", "STARTED"), ("TOOL", "RETRY"),
                          ("TOOL", "DONE"), ("FILE", "DONE"), ("TASK", "DONE")])
        task, tool = evs[0], evs[2]
        self.assertIsNone(task["parent_id"])
        self.assertEqual(task["span_id"], task["event_id"])
        self.assertEqual(tool["parent_id"], task["event_id"])
        self.assertEqual(evs[5]["parent_id"], task["event_id"])  # the tool span had closed: back to the task
        self.assertEqual(evs[4]["span_id"], tool["event_id"])
        self.assertIsInstance(evs[4]["duration_ms"], int)
        self.assertIsInstance(evs[6]["duration_ms"], int)
        self.assertTrue(evs[0]["ts"].endswith("Z") and len(evs[0]["ts"]) == 24)
        self.assertEqual(len({e["event_id"] for e in evs}), len(evs))
        self.assertIsNone(EV.current())

    def test_validate_catches_bad_events(self):
        good = EV.make("LLM", "act", "REQUEST", "STARTED")
        self.assertEqual(EV.validate(good), [])
        for k, v in (("type", "MODEL"), ("status", "OK"), ("ts", "yesterday"), ("metadata", {"x": "y" * 3000})):
            self.assertTrue(EV.validate({**good, k: v}), k)
        self.assertTrue(EV.validate({k: v for k, v in good.items() if k != "parent_id"}))
        self.assertTrue(EV.validate({**good, "status": "DONE", "duration_ms": None}))

    def test_metadata_small_and_secrets_withheld(self):
        EV.emit("CODE", "act", "x", "DONE", out="y" * 5000, tail=["z" * 300] * 40, key=f"token {SECRET}")
        (e,) = self.evs()
        self.assertValid([e])
        self.assertLessEqual(len(json.dumps(e["metadata"], ensure_ascii=False).encode()), EV.META_BYTES)
        self.assertNotIn(SECRET, self.evf.read_text())
        self.assertIn("withheld", e["metadata"]["key"])
        self.assertEqual(EV.tail(f"a\nb {SECRET}\nc"), ["a", "(withheld: it looked like it held a secret)", "c"])
        self.assertLessEqual(len(EV.short("x" * 500)), EV.SUMMARY_CHARS)

    def test_a_broken_secret_rule_withholds_never_leaks(self):
        with mock.patch("ga.runlog.redact", side_effect=RuntimeError("rule broke")):
            self.assertEqual(EV.short(f"x {SECRET}"), "(withheld)")
            EV.emit("CODE", "act", "x", "DONE", out=f"key {SECRET}")
        self.assertNotIn(SECRET, self.evf.read_text())
        self.assertEqual(self.evs()[0]["metadata"]["out"], "(withheld)")

    def test_rotation_keeps_three(self):
        with mock.patch.object(EV, "ROTATE_BYTES", 2000):
            for i in range(60):
                EV.emit("SYSTEM", "t", f"n{i}", "DONE", pad="p" * 100)
        names = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(names, ["events.jsonl", "events.jsonl.1", "events.jsonl.2", "events.jsonl.3"])
        self.assertLessEqual(self.evf.stat().st_size, 2000)
        newest = EV.read(self.evf, limit=5)
        self.assertEqual(newest[0]["action"], "n59")  # newest first, across the rotated files

    def test_never_raises_and_off_writes_nothing(self):
        blocker = self.tmp / "file"
        blocker.write_text("x")
        with mock.patch.dict(os.environ, {"GA_EVENTS": str(blocker / "sub" / "events.jsonl")}):
            self.assertIsNone(EV.emit("SYSTEM", "t", "x", "DONE"))
            with EV.span("TASK", "t", "x") as sp:
                sp.update("y")
        with mock.patch.dict(os.environ, {"GA_EVENTS": "off"}):
            self.assertIsNone(EV.emit("SYSTEM", "t", "x", "DONE"))
        self.assertFalse(self.evf.exists())

    def test_an_exception_is_error_plus_failed_and_reraised(self):
        with self.assertRaises(ValueError):
            with EV.span("TOOL", "act", "RUN"):
                raise ValueError("boom")
        evs = self.evs()
        self.assertEqual([(e["type"], e["status"]) for e in evs], [("TOOL", "STARTED"), ("ERROR", "FAILED"), ("TOOL", "FAILED")])
        self.assertEqual(evs[1]["parent_id"], evs[0]["event_id"])

    def test_child_env_carries_the_span(self):
        with EV.span("TASK", "bridge", "x") as sp:
            env = EV.child_env({})
            self.assertEqual(env[EV.ENV_PARENT], sp.id)
            out = subprocess.run([sys.executable, "-c", "from ga import events as E; E.emit('AGENT','act','x','RUNNING')"],
                                 env={**os.environ, **env, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
        evs = self.evs()
        child = [e for e in evs if e["type"] == "AGENT"][0]
        self.assertEqual(child["parent_id"], evs[0]["event_id"])

    def test_select_task_tree_and_types(self):
        with EV.span("TASK", "CMD-A", "item"):
            with EV.span("TOOL", "act", "RUN"):
                EV.emit("CODE", "act", "test", "DONE")
        with EV.span("TASK", "CMD-B", "item"):
            EV.emit("CODE", "act", "other", "DONE")
        a = EV.read(self.evf, task="CMD-A", limit=100)
        self.assertEqual({e["action"] for e in a}, {"item", "RUN", "test"})
        self.assertEqual([e["action"] for e in EV.read(self.evf, types=["code"], limit=100)], ["other", "test"])


# ---- S2: ga act ----------------------------------------------------------------------------------------------------

class Act(EvCase):
    def run_act(self, answers, cfg=None, files=("calc.py",)):
        import test_ga38 as T
        root = T.py_repo(self, cfg or T.ACT_CFG)
        res, fake, act, _state = T.run(self, root, answers, files=files)
        return res, fake

    def test_fake_run_emits_ordered_linked_events_without_prompt_or_answer(self):
        import test_ga38 as T
        res, fake = self.run_act([T.FIX])
        self.assertEqual(res.status, "done")
        evs = self.evs()
        self.assertValid(evs)
        seq = [(e["type"], e["action"], e["status"]) for e in evs]
        want = [("TASK", "item", "STARTED"), ("AGENT", "agent", "STARTED"), ("TASK", "step", "RUNNING"),
                ("AGENT", "ANALYZING", "RUNNING"), ("CODE", "done_when", "STARTED"), ("CODE", "done_when", "FAILED"),
                ("TASK", "step", "RUNNING"), ("AGENT", "PLANNING", "RUNNING"),
                ("LLM", "REQUEST", "STARTED"), ("LLM", "PROCESSING", "RUNNING"), ("LLM", "RECEIVING_RESULT", "RUNNING"),
                ("LLM", "SELECTING_TOOL", "RUNNING"), ("LLM", "RESPONSE_READY", "DONE"),
                ("AGENT", "EXECUTING", "RUNNING"), ("TOOL", "EDIT", "STARTED"), ("FILE", "EDIT", "DONE"),
                ("TOOL", "EDIT", "DONE"), ("TASK", "step", "RUNNING"), ("AGENT", "ANALYZING", "RUNNING"),
                ("CODE", "done_when", "STARTED"), ("CODE", "done_when", "DONE"), ("AGENT", "COMPLETING", "RUNNING"),
                ("TASK", "step", "RUNNING"), ("AGENT", "COMPLETING", "DONE"), ("TASK", "item", "DONE")]
        self.assertEqual(seq, want)
        by = {e["event_id"]: e for e in evs}
        task, agent = evs[0], evs[1]
        self.assertEqual(task["component"], "CMD-T1")
        self.assertEqual(task["metadata"]["steps"], ["measure", "edit", "verify", "finish"])
        self.assertEqual(agent["parent_id"], task["event_id"])
        for e in evs[2:]:
            if e["status"] == "STARTED" and e["type"] in ("LLM", "TOOL", "CODE"):
                self.assertEqual(e["parent_id"], agent["event_id"], e)
        fil = [e for e in evs if e["type"] == "FILE"][0]
        self.assertEqual(by[fil["parent_id"]]["type"], "TOOL")
        self.assertEqual((fil["metadata"]["path"], fil["metadata"]["added"], fil["metadata"]["removed"]), ("calc.py", 1, 1))
        for e in evs:
            if e["status"] in ("DONE", "FAILED") and e["span_id"]:
                self.assertIsInstance(e["duration_ms"], int, e)
        llm = [e for e in evs if e["type"] == "LLM" and e["status"] == "DONE"][0]
        self.assertEqual(llm["metadata"]["model"], "fake-model")
        self.assertIsInstance(llm["metadata"]["input"], int)
        sel = [e for e in evs if e["action"] == "SELECTING_TOOL"][0]
        self.assertEqual(sel["metadata"]["actions"], {"EDIT": 1})
        # no prompt, no answer, no reasoning: nothing of what was sent or answered is in the file
        text = self.evf.read_text(encoding="utf-8")
        for needle in ("<<<<<<< SEARCH", "return a + b", "SEARCH", "turn 1 of", "tokens left", "def add"):
            self.assertNotIn(needle, text)
        prompt = fake.calls[0]["prompt"]
        for line in prompt.splitlines():
            if len(line) > 40:
                self.assertNotIn(line, text)
        self.assertEqual({e["metadata"].get("state") for e in evs if e["type"] == "LLM"}, {None})
        self.assertTrue({e["action"] for e in evs if e["type"] == "LLM"} <= set(EV.LLM_STATES))
        self.assertTrue({e["action"] for e in evs if e["type"] == "AGENT"} - {"agent"} <= set(EV.AGENT_STATES))

    def test_failing_command_is_error_and_failed(self):
        import test_ga38 as T
        res, _ = self.run_act(["RUN lint\n", T.FIX])
        evs = self.evs()
        self.assertValid(evs)
        lint = [e for e in evs if e["type"] == "CODE" and e["action"] == "lint"]
        self.assertEqual([e["status"] for e in lint], ["STARTED", "FAILED"])
        self.assertEqual(lint[1]["metadata"]["exit"], 1)
        self.assertEqual(len(lint[1]["metadata"]["tail"]), 12)
        self.assertIn("line too long", lint[1]["metadata"]["tail"][-1])
        err = [e for e in evs if e["type"] == "ERROR" and e["parent_id"] == lint[0]["event_id"]]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["status"], "FAILED")
        tool = [e for e in evs if e["type"] == "TOOL" and e["action"] == "RUN"]
        self.assertEqual([e["status"] for e in tool], ["STARTED", "FAILED"])
        self.assertEqual(lint[0]["parent_id"], tool[0]["event_id"])
        self.assertEqual(tool[0]["metadata"]["input"], "RUN lint")
        wt = [e for e in evs if e["type"] == "AGENT" and e["action"] == "WAITING_TOOL"]
        self.assertEqual(len(wt), 1)
        # the red done_when measure is FAILED but not an ERROR (red is expected before the fix)
        first = [e for e in evs if e["type"] == "CODE" and e["action"] == "done_when"][:2]
        self.assertEqual([e for e in evs if e["type"] == "ERROR" and e["parent_id"] == first[0]["event_id"]], [])

    def test_need_that_raises_is_a_failed_tool(self):
        import test_ga38 as T
        from ga.act import loop as A
        with mock.patch.object(A.Act, "serve_need", side_effect=RuntimeError("retriever broke")):
            self.run_act(["NEED symbol calc.add\n", T.FIX])
        need = [e for e in self.evs() if e["type"] == "TOOL" and e["action"] == "NEED"]
        self.assertEqual([e["status"] for e in need], ["STARTED", "FAILED"])
        self.assertIn("failed", need[1]["metadata"]["result"])

    def test_need_found_is_done_and_missing_is_failed(self):
        import test_ga38 as T
        self.run_act(["NEED symbol calc.add\nNEED symbol calc.nothing_here\n", T.FIX])
        need = [e for e in self.evs() if e["type"] == "TOOL" and e["action"] == "NEED" and e["status"] != "STARTED"]
        self.assertEqual([e["status"] for e in need], ["DONE", "FAILED"])

    def test_secrets_in_command_output_are_withheld(self):
        import test_ga38 as T
        cfg = json.loads(json.dumps(T.ACT_CFG))
        cfg["commands"]["leak"] = ["{python}", "-c", f"print('ok'); print('key {SECRET}'); raise SystemExit(3)"]
        self.run_act(["RUN leak\n", T.FIX], cfg=cfg)
        text = self.evf.read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)
        leak = [e for e in self.evs() if e["type"] == "CODE" and e["action"] == "leak" and e["status"] == "FAILED"][0]
        self.assertEqual(leak["metadata"]["tail"], ["ok", "(withheld: it looked like it held a secret)"])
        self.assertEqual(leak["metadata"]["exit"], 3)

    def test_backend_error_and_transient_retry(self):
        import test_ga38 as T
        from ga.backends.base import Transient

        class Flaky(T.Fake):
            def run_turn(self, prompt, session_id=None, **k):
                if not self.calls:
                    self.calls.append({"prompt": prompt})
                    raise Transient(503)
                return super().run_turn(prompt, session_id, **k)
        root = T.py_repo(self)
        res, _f, _a, _s = T.run(self, root, [T.FIX], fake=Flaky([T.FIX]), transient_backoff_s=0)
        evs = self.evs()
        self.assertEqual([e["status"] for e in evs if e["type"] == "LLM" and e["action"] == "REQUEST"], ["STARTED", "RETRY"])

        class Down(T.Fake):
            def run_turn(self, *a, **k):
                raise Transient(503)
        self.evf.unlink()
        root = T.py_repo(self)
        res, _f, _a, _s = T.run(self, root, [T.FIX], fake=Down([T.FIX]), transient_backoff_s=0)
        self.assertEqual(res.status, "blocked")
        evs = self.evs()
        self.assertValid(evs)
        llm_fail = [e for e in evs if e["type"] == "LLM" and e["status"] == "FAILED"]
        self.assertEqual(len(llm_fail), 1)
        err = [e for e in evs if e["type"] == "ERROR"]
        self.assertTrue(any("transient" in e["action"] for e in err), err)
        self.assertEqual(evs[-1]["type"], "TASK")
        self.assertEqual(evs[-1]["status"], "FAILED")


# ---- S2: bridge, mailbox (NET), hub / shadow, vm update ------------------------------------------------------------

class Box:
    def __init__(self, msgs):
        self.msgs, self.sent, self.read = msgs, [], []

    def unread(self, name):
        return iter(self.msgs)

    def send(self, to, text, sender=None):
        self.sent.append((to, text))
        return "to/x"

    def mark_read(self, name, path):
        self.read.append(path)


class Bridge(EvCase):
    def cfg(self):
        (self.tmp / "ga-supervise.json").write_text("{}")
        return {"name": "AGY", "hub": "baseline", "workdir": str(self.tmp), "supervise_config": "ga-supervise.json",
                "max_answer_chars": 4000, "capacity_backoff_s": 0}

    def msg(self, sender="baseline"):
        from test_ga36_bridge import DIRECTIVE
        return Message("to/AGY/x.md", "AGY", sender, "", "CMD-AG1", dump_wire(DIRECTIVE), "directive/2", [])

    def test_mail_in_run_report_ack(self):
        from ga import bridge as B
        box = Box([self.msg()])
        seen = []

        def runner(cfg, conf, task):
            seen.append(EV.current())
            return {"code": 0, "out": "done", "events": [{"event": "turn", "tokens": 5}, {"event": "end", "status": "done"}]}
        self.assertEqual(B.one_pass(self.cfg(), box=box, runner=runner, log=lambda s: None), 1)
        evs = self.evs()
        self.assertValid(evs)
        seq = [(e["type"], e["action"], e["status"]) for e in evs]
        self.assertEqual(seq[0], ("QUEUE", "mail received", "DONE"))
        self.assertEqual(seq[1], ("TASK", "directive", "STARTED"))
        self.assertIn(("AGENT", "ga supervise", "DONE"), seq)
        self.assertIn(("QUEUE", "report mailed", "DONE"), seq)
        self.assertEqual(seq[-2:], [("QUEUE", "mail acked", "DONE"), ("TASK", "directive", "DONE")])
        steps = [e["metadata"]["step"] for e in evs if e["action"] == "step"]
        self.assertEqual(steps, ["read", "run", "report", "ack"])
        task = evs[1]
        agent = [e for e in evs if e["type"] == "AGENT"][0]
        self.assertEqual(agent["parent_id"], task["event_id"])
        self.assertEqual(seen, [agent["event_id"]])  # ga supervise (a child process) gets the AGENT span as parent

    def test_failed_run_is_failed_task_with_error(self):
        from ga import bridge as B
        box = Box([self.msg()])
        B.one_pass(self.cfg(), box=box, runner=lambda c, f, t: {"code": 1, "out": "x", "events": []}, log=lambda s: None)
        evs = self.evs()
        self.assertEqual(evs[-1]["status"], "FAILED")
        self.assertTrue(any(e["type"] == "ERROR" for e in evs))


class MailNet(EvCase):
    def test_fetch_and_push_are_net_events_with_the_host_only(self):
        from ga.mailbox import Mailbox
        bare, work = self.tmp / "m.git", self.tmp / "w"
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x"}
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=env)
        subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, env=env, capture_output=True)
        from test_ga36_bridge import DIRECTIVE
        Mailbox(work).send("AGY", dump_wire(DIRECTIVE), "baseline")
        evs = self.evs()
        self.assertValid(evs)
        net = [(e["action"], e["status"]) for e in evs if e["type"] == "NET"]
        self.assertIn(("git fetch", "DONE"), net)
        self.assertIn(("git push", "DONE"), net)
        push = [e for e in evs if e["action"] == "git push" and e["status"] == "STARTED"][0]
        self.assertEqual(push["metadata"]["host"], "local")
        self.assertGreater(push["metadata"]["bytes"], 100)
        m = Mailbox(work)
        for url, host in (("https://x-access-token:abc@github.com/o/r.git", "github.com"),
                          ("git@github.com:o/r.git", "github.com"), ("/srv/m.git", "local")):
            m._host = None
            with mock.patch.object(m, "_git", return_value=url + "\n"):
                self.assertEqual(m.host(), host)


class HubEvents(EvCase):
    def test_shadow_decision_is_task_and_llm(self):
        from test_ga42 import World
        from test_ga42_shadow import hub
        w = World(self)
        w.report()
        hub(w, ["ACCEPT"], "success", shadow=True).tick()
        evs = self.evs()
        self.assertValid(evs)
        task = [e for e in evs if e["type"] == "TASK" and e["status"] == "STARTED"][0]
        self.assertEqual(task["component"], "hub-shadow")
        llm = [e for e in evs if e["type"] == "LLM"]
        self.assertEqual([e["action"] for e in llm], ["REQUEST", "PROCESSING", "RECEIVING_RESULT", "RESPONSE_READY"])
        self.assertEqual(llm[0]["parent_id"], task["event_id"])
        self.assertEqual(llm[-1]["metadata"]["decision"], "ACCEPT")
        self.assertEqual(evs[-1]["metadata"]["decision"], "shadow ACCEPT CMD-T1")
        self.assertNotIn("ACCEPT\n", self.evf.read_text())

    def test_backend_error_label(self):
        from test_ga42 import World
        from test_ga42_shadow import hub
        from ga.backends.base import BackendError
        w = World(self)
        w.report()
        h = hub(w, ["ACCEPT"], "success", shadow=True)

        class Broken:
            bare = True
            calls = []

            def run_turn(self, *a, **k):
                e = BackendError("x")
                e.reason = "http_500"
                raise e
        h._runner = lambda: Broken()
        h.tick()
        evs = self.evs()
        err = [e for e in evs if e["type"] == "ERROR"]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["metadata"]["label"], "backend:http_500")
        self.assertEqual([e["status"] for e in evs if e["type"] == "LLM"][-1], "FAILED")


import test_ga48 as T48  # noqa: E402


class VmUpdate(T48.Upd):
    def test_update_emits_system_heads_pip_restarts(self):
        evf = self.root / "events.jsonl"
        self.first()
        self.push("ga-sdk")
        with mock.patch.dict(os.environ, {"GA_EVENTS": str(evf)}):
            rc, _out = self.up(T48.R())
        self.assertEqual(rc, 0)
        evs = [json.loads(x) for x in evf.read_text().splitlines()]
        for e in evs:
            self.assertEqual(EV.validate(e), [], e)
        self.assertEqual((evs[0]["type"], evs[0]["component"], evs[0]["status"]), ("SYSTEM", "vm-update", "STARTED"))
        actions = [(e["type"], e["action"], e["status"]) for e in evs]
        self.assertIn(("SYSTEM", "heads", "RUNNING"), actions)
        self.assertIn(("SYSTEM", "pip install", "DONE"), actions)
        self.assertEqual([e["metadata"]["unit"] for e in evs if e["action"] == "restart"],
                         [__import__("ga.vm.core", fromlist=["x"]).CONSOLE_UNIT,
                          __import__("ga.vm.core", fromlist=["x"]).BRIDGE_UNIT])
        net = [e for e in evs if e["type"] == "NET" and e["status"] == "DONE"]
        self.assertEqual({e["metadata"]["changed"] for e in net if e["metadata"].get("changed") is not None}, {True, False})
        heads = [e for e in evs if e["action"] == "heads"][0]["metadata"]
        self.assertNotEqual(heads["before"]["ga-sdk"], heads["after"]["ga-sdk"])
        self.assertEqual(actions[-1], ("SYSTEM", "update", "DONE"))


# ---- S3: the console -----------------------------------------------------------------------------------------------

def _fixture_now(dst: Path, cut: int | None = None) -> list[dict]:
    """The recorded stream with its times moved so that its last line is now (open activities are alive)."""
    rows = [json.loads(x) for x in FIXTURE.read_text(encoding="utf-8").splitlines() if x.strip()]
    if cut:
        rows = rows[:cut]
    from datetime import datetime, timezone
    t = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()  # noqa: E731
    shift = time.time() - 2 - t(rows[-1]["ts"])
    for r in rows:
        r["ts"] = EV.now_iso(t(r["ts"]) + shift)
    dst.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return rows


class Console(EvCase):
    def serve(self, **cfg):
        from ga.console import server as S
        from console_world import World
        self.w = World()
        self.addCleanup(shutil.rmtree, self.w.tmp, True)
        conf = self.w.config(**cfg)
        conf["events"] = str(self.evf)
        srv = S.Console(conf, watch_every_s=0.2, sse_keepalive_s=0.3,
                        health=lambda u: False, events_every_s=0.05)
        srv.start_watcher()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.close)
        self.addCleanup(srv.shutdown)
        return srv

    def get(self, srv, path):
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", headers={"X-GA-Token": srv.token})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_recent_filters_and_sse_streams_new_lines(self):
        rows = _fixture_now(self.evf)
        srv = self.serve()
        allr = self.get(srv, "/api/events/recent?limit=5000")
        self.assertEqual(len(allr), len(rows))
        self.assertEqual(allr[0]["event_id"], rows[-1]["event_id"])  # newest first
        self.assertEqual(len(self.get(srv, "/api/events/recent?limit=3")), 3)
        llm = self.get(srv, "/api/events/recent?type=LLM&limit=1000")
        self.assertTrue(llm and all(e["type"] == "LLM" for e in llm))
        two = self.get(srv, "/api/events/recent?type=LLM,CODE&limit=1000")
        self.assertEqual({e["type"] for e in two}, {"LLM", "CODE"})
        tree = self.get(srv, "/api/events/recent?task=CMD-AG1&limit=5000")  # the directive and all under it
        self.assertEqual(len(tree), len(rows) - 3)  # not the bridge's first fetch nor the mail-received line
        self.assertNotIn("mail received", [e["action"] for e in tree])
        self.assertIn("CMD-T1", {e["component"] for e in tree})  # ga act's item hangs under the bridge's directive
        # the token is required
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/api/events/recent", timeout=5)
        # SSE: a line appended to the file arrives as an "ev" event
        url = f"http://127.0.0.1:{srv.port}/api/events?t={srv.token}&after={srv.bus.n}"
        got = []

        def reader():
            with urllib.request.urlopen(url, timeout=10) as r:
                ev = None
                for raw in r:
                    line = raw.decode().rstrip("\n")
                    if line.startswith("event: "):
                        ev = line[7:]
                    elif line.startswith("data: ") and ev == "ev":
                        got.append(json.loads(line[6:]))
                        return
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.4)
        EV.emit("TOOL", "act", "RUN", "STARTED", input=f"RUN x {SECRET}")
        t.join(8)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["type"], "TOOL")
        self.assertNotIn(SECRET, json.dumps(got[0]))
        self.assertEqual(self.get(srv, "/api/events/recent?limit=1")[0]["event_id"], got[0]["event_id"])

    def test_rotation_is_followed(self):
        srv = self.serve()
        with mock.patch.object(EV, "ROTATE_BYTES", 1500):
            for i in range(30):
                EV.emit("SYSTEM", "t", f"r{i}", "DONE", pad="p" * 80)
                srv.events_once()
        self.assertEqual(self.get(srv, "/api/events/recent?limit=1")[0]["action"], "r29")
        self.assertEqual(len(self.get(srv, "/api/events/recent?limit=100")), 30)

    def test_recent_is_capped_and_the_ring_is_bounded(self):
        from collections import deque
        srv = self.serve()
        self.assertEqual(srv.evring.maxlen, 5000)
        line = json.dumps(EV.make("SYSTEM", "t", "x", "DONE")) + "\n"
        with self.evf.open("a", encoding="utf-8") as f:
            f.write(line * 5100)
        srv.events_once()
        self.assertEqual(len(srv.evring), 5000)  # the ring keeps the newest 5000, not every line ever seen
        srv.evring = deque(srv.evring, maxlen=6000)
        srv.evring.extend([json.loads(line)] * 500)
        self.assertEqual(len(self.get(srv, "/api/events/recent?limit=99999")), 5000)  # the answer is capped
        self.assertEqual(len(self.get(srv, "/api/events/recent?limit=x")), 200)

    def test_state_has_version(self):
        import ga
        srv = self.serve()
        self.assertEqual(self.get(srv, "/api/state")["version"], ga.__version__)
        self.assertGreaterEqual(tuple(map(int, ga.__version__.split("."))), (0, 18, 0))  # GA51 moved it on to 0.18.1


# ---- S3: the screen under the judge --------------------------------------------------------------------------------

from ga.console import judge as J  # noqa: E402

WHY = J.available()


class GoldenLive(unittest.TestCase):
    @unittest.skipIf(WHY, f"no browser: {WHY}")
    def test_judge_passes_golden_live(self):
        v = J.measure(str(ROOT / "ga" / "console" / "golden" / "live.html"))
        self.assertTrue(v["pass"], (v["failed"], v["facts"]))

    def test_golden_live_shape(self):
        html = (ROOT / "ga" / "console" / "golden" / "live.html").read_text(encoding="utf-8")
        self.assertEqual(html.count("<h1"), 1)
        self.assertEqual(html.count('aria-current="page"'), 1)
        for s in ("TIME", "시각", "종류", "어디", "무엇", "상태", "걸린 시간", 'role="progressbar"', "class=\"steps\"",
                  "class=\"tree\"", "class=\"drawer\"", "class=\"chips\""):
            if s != "TIME":
                self.assertIn(s, html)
        for g in (ROOT / "ga" / "console" / "golden").glob("*.html"):
            self.assertIn('href="live.html"', g.read_text(encoding="utf-8"), g.name)
        idx = (ROOT / "ga" / "console" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<a data-line="functional" href="#/live" data-route="live">실시간', idx)

    def test_fixture_is_valid_and_has_no_model_text(self):
        rows = [json.loads(x) for x in FIXTURE.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertGreater(len(rows), 20)
        for r in rows:
            self.assertEqual(EV.validate(r), [], r)
        self.assertEqual({r["type"] for r in rows} >= {"TASK", "AGENT", "LLM", "TOOL", "CODE", "FILE", "NET", "QUEUE",
                                                        "ERROR"}, True)


@unittest.skipIf(WHY, f"no browser: {WHY}")
class LiveScreen(unittest.TestCase):
    """The app's 실시간 on a recorded stream (cut mid-run, so things are live), at 375 and 1440."""

    @classmethod
    def setUpClass(cls):
        from con3_world import serve, world
        from playwright.sync_api import sync_playwright
        cls.tmp = Path(tempfile.mkdtemp(prefix="ga50-live-"))
        cls.evf = cls.tmp / "events.jsonl"
        cls.rows = _fixture_now(cls.evf, cut=CUT)
        cls.w = world()
        from ga.console import server as S
        cfg = cls.w.config(services={"api": cls.w.svc("run")})
        cfg["events"] = str(cls.evf)
        cls.srv = S.Console(cfg, watch_every_s=0.2, sse_keepalive_s=0.5, health=lambda u: False, events_every_s=0.1)
        cls.srv.start_watcher()
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = J._launch(cls.pw)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()
        cls.srv.close()
        shutil.rmtree(cls.w.tmp, True)
        shutil.rmtree(cls.tmp, True)

    def page(self, width, hash_="#live"):
        ctx = self.browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce", bypass_csp=True,
                                       timezone_id="Asia/Seoul")
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(self.srv.url + hash_, wait_until="load")
        pg.wait_for_selector("#main section", timeout=10000)
        pg.errs = errs
        return pg

    def test_live_screen_shows_task_steps_activity_stream_and_drawer(self):
        for width in (375, 1440):
            pg = self.page(width)
            self.assertEqual(pg.evaluate("location.hash"), "#/live")
            h1 = pg.inner_text("h1")
            self.assertIn("CMD-AG1", h1)
            self.assertIn("도는 중", h1)
            cap = pg.inner_text(".mast .cap")
            self.assertIn("VM 도는 중", cap)
            self.assertIn(__import__("ga").__version__, cap)
            main = pg.inner_text("#main")
            self.assertIn("measure", main)
            self.assertGreater(pg.locator(".steps li.done").count(), 0)
            self.assertEqual(pg.locator(".steps li.now").count(), 1)
            self.assertEqual(pg.locator("[role=progressbar]").count(), 1)
            self.assertGreater(pg.locator(".tree .mark.live").count(), 1)  # nested live activity
            self.assertGreater(pg.locator(".tbl tr.fail").count(), 0)      # the failed lint is inverted
            self.assertEqual(pg.errs, [])
        # local time (KST): the newest row's time is the viewer's clock, not UTC
        from datetime import datetime, timedelta, timezone
        newest = datetime.now(timezone(timedelta(hours=9)))
        first = pg.locator(".tbl tbody tr").first.locator("td").first.inner_text()
        self.assertTrue(first.startswith(newest.strftime("%H:")) or first.startswith((newest - timedelta(minutes=1)).strftime("%H:")), first)
        # filter chips and the drawer
        pg.get_by_role("button", name="CODE", exact=True).click()
        types = pg.locator(".tbl tbody tr td:nth-child(2)").all_inner_texts()
        self.assertTrue(types and set(types) == {"CODE"}, types)
        pg.locator(".tbl tbody tr.fail button.row-open").first.click()
        d = pg.inner_text(".drawer")
        self.assertIn("부모 사슬", d)
        self.assertIn("line too long", d)  # CODE output's last lines
        self.assertIn("TASK", d)
        # a new event arrives over SSE without a reload
        pg.wait_for_selector("html[data-live=open]", state="attached", timeout=10000)
        pg.get_by_role("button", name="모두", exact=True).click()
        EV_line = EV.make("NET", "mailbox", "git push", "STARTED", parent_id=None, metadata={"host": "github.com"})
        with self.evf.open("a", encoding="utf-8") as f:
            f.write(json.dumps(EV_line) + "\n")
        pg.wait_for_function("document.querySelector('.tbl tbody tr td:nth-child(4)').innerText.includes('git push')",
                             timeout=10000)

    def test_judge_passes_on_the_live_screen(self):
        import threading as th
        out = {}
        t = th.Thread(target=lambda: out.update(J.measure(self.srv.url + "#/live", settle_ms=900)))
        t.start()
        t.join(240)
        self.assertTrue(out.get("pass"), (out.get("failed"), out.get("facts")))


CUT = 30  # the recorded stream is cut here: the bridge's directive, ga act's item and its lint run are open


if __name__ == "__main__":
    unittest.main()
