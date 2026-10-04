"""CMD-GA29: fresh turns with a bounded context pack (ctxpack/1), the state file, the cursor, usage + L0, the budget hook.

No network, no model: fake runners and the stub ``claude``."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ga import ctxpack, l0  # noqa: E402
from ga.adapters import budget_hook  # noqa: E402
from ga.adapters.base import TurnRequest, TurnResult  # noqa: E402
from ga.forms import dump_text  # noqa: E402
from test_headless import Stub  # noqa: E402
from world import World, directive  # noqa: E402

HEAD = {"schema": "directive/2", "id": "CMD-X1", "rev": 1, "to": "A", "goal": "g", "why": "w", "after": ["CMD-X0"],
        "refs": ["alpha/notes.md"], "scope": [{"id": "S1", "text": "s"}], "done_when": [{"id": "D1", "text": "d"}]}


def report_text(session="A", did="CMD-A1", rev=1, commits=()):
    head = {"schema": "report/2", "from": session, "handled": [{"id": did, "rev_seen": rev, "status": "done"}],
            "commits": [{"repo": r, "branch": "sess-a", "sha": sha} for r, sha in commits], "items": []}
    return dump_text(head, "## Result\ndone\n")


def answer(state="did step 1; next: step 2", **kw):
    return report_text(**kw) + "\n```state\n" + state + "\n```\n"


class PackTest(unittest.TestCase):
    def kw(self, **over):
        files = [("a.md", "CMD-X1 is here\nother line\n" + "x" * 400), ("b.md", "b" * 800)]
        kw = dict(cap=10_000, state="state line\n", inbox=["- 0001 hub {\"schema\":\"directive/2\"}"], cursor="0000",
                  files=files, ids=["CMD-X1", "CMD-X0"])
        kw.update(over)
        return kw

    def test_byte_identical_and_order(self):
        a, b = ctxpack.build(HEAD, **self.kw()), ctxpack.build(dict(HEAD), **self.kw())
        self.assertEqual(a.text.encode(), b.text.encode())
        self.assertEqual(a.parts, ["head", "state", "inbox", "ids", "ref:a.md", "ref:b.md"])
        self.assertEqual(a.dropped, [])
        self.assertIn("a.md:1: CMD-X1 is here", a.text)
        self.assertNotIn("other line\n## ", a.text.split("## 4a")[1].split("## 4b")[0])

    def test_cap_drops_lowest_first_recorded_in_order(self):
        full = ctxpack.build(HEAD, **self.kw())
        for cap in range(full.tokens - 1, 0, -7):
            try:
                p = ctxpack.build(HEAD, **self.kw(cap=cap))
            except ctxpack.CtxPackError:
                break
            self.assertLessEqual(ctxpack.tokens(p.text), cap)
            self.assertLessEqual(p.tokens, cap)
            self.assertEqual(p.parts[0], "head")
            order = ["ref:b.md", "ref:a.md", "ids", "inbox", "state"]
            self.assertEqual([d["part"] for d in p.dropped], order[:len(p.dropped)])
            self.assertEqual(p.parts + [d["part"] for d in reversed(p.dropped)], full.parts)
            meta = json.loads(p.text.split("\n")[1])
            self.assertEqual(meta["dropped"], p.dropped)  # the drop is recorded in the pack itself
            self.assertIn('"id":"CMD-X1"', p.text)  # the head is never dropped
        else:
            self.fail("never reached the head-only floor")

    def test_head_over_cap_is_an_error(self):
        with self.assertRaises(ctxpack.CtxPackError):
            ctxpack.build(HEAD, **self.kw(cap=20))
        with self.assertRaises(ctxpack.CtxPackError):  # the fixed instructions count against the cap too
            ctxpack.build(HEAD, **self.kw(cap=500, reserve=480))

    def test_ref_files_stay_inside_roots(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "w"
            (root / "alpha" / "ga" / "adapters").mkdir(parents=True)
            (root / "alpha" / "ga" / "adapters" / "headless.py").write_text("h")
            (root / "alpha" / "ga" / "adapters" / "agent_sdk.py").write_text("a")
            (Path(d) / "secret.txt").write_text("no")
            got = ctxpack.ref_files(["ga-sdk 438a34a ga/adapters/headless.py, agent_sdk.py", "../secret.txt /etc/passwd"],
                                    [root, root / "alpha"])
            self.assertEqual([n for n, _ in got], ["alpha/ga/adapters/headless.py", "alpha/ga/adapters/agent_sdk.py"])


class AnswerTest(unittest.TestCase):
    def test_ok(self):
        a, why = ctxpack.parse_answer("Some words first.\n" + answer(), "A")
        self.assertEqual(why, "")
        self.assertEqual(a.head["schema"], "report/2")
        self.assertNotIn("```state", a.report)
        self.assertEqual(a.state, "did step 1; next: step 2\n")

    def test_failures(self):
        cases = {"": "no answer", report_text(): "exactly one", answer() + "```state\nx\n```\n": "exactly one",
                 "```state\nx\n```\n": "no report/2", answer().replace('"from": "A"', '"from": "B"'): "not 'A'",
                 answer().replace("report/2", "report/9"): "report/2:", answer(state="y" * 9000): "state_max_tokens"}
        for text, want in cases.items():
            a, why = ctxpack.parse_answer(text, "A")
            self.assertIsNone(a, text[:40])
            self.assertIn(want, why)


class RunnerTest(unittest.TestCase):
    def test_fresh_never_resumes_and_usage_lands(self):
        usage = {"input_tokens": 12, "cache_read_input_tokens": 300, "cache_creation_input_tokens": 40, "output_tokens": 7}
        step = {"do": "reply", "text": "the answer", "usage": usage, "modelUsage": {"claude-haiku-4-5-20251001": {}}}
        stub = Stub([step, step])
        r = stub.runner(guard=False)
        with tempfile.TemporaryDirectory() as w:
            res = r.run_turn(TurnRequest("A", "p", Path(w), resume_id="old-sid", fresh=True))
            r.run_turn(TurnRequest("A", "p", Path(w), resume_id="old-sid"))
        calls = stub.calls()
        self.assertNotIn("--resume", calls[0]["argv"])
        self.assertIn("--resume", calls[1]["argv"])  # resume mode keeps today's behaviour
        self.assertEqual(res.usage, {"input": 12, "cache_read": 300, "cache_creation": 40, "output": 7})
        self.assertEqual(res.model, "claude-haiku-4-5-20251001")
        self.assertEqual(res.answer, "the answer")
        ev = l0.run_end("A:CMD-A1:1", res, decision_ref="CMD-A1")
        self.assertEqual(ev["data"]["reported_cache_read_input_tokens"], 300)
        self.assertEqual(ev["data"]["model"], "claude-haiku-4-5-20251001")
        try:
            from telemetry.event import check
        except ImportError:
            return
        self.assertEqual(check(ev), [])

    def test_budget_hook_only_in_own_dir(self):
        with tempfile.TemporaryDirectory() as person:
            old = os.environ.get("HOME")
            os.environ["HOME"] = person  # a person's home: nothing may land in it
            try:
                stub = Stub([{"do": "ok"}, {"do": "ok"}])
                r = stub.runner(guard=False, context_budget={"soft": 100, "hard": 200})
                with tempfile.TemporaryDirectory() as w:
                    r.run_turn(TurnRequest("A", "p", Path(w), fresh=True))
                    r.run_turn(TurnRequest("B", "p", Path(w)))  # a resume turn gets no budget hook
                calls = stub.calls()
                argv = calls[0]["argv"]
                settings = Path(argv[argv.index("--settings") + 1])
                self.assertEqual(settings.parent, r.session_home("A"))
                self.assertIn("budget_hook.py", settings.read_text())
                self.assertNotIn("--settings", calls[1]["argv"])
                self.assertEqual(calls[0]["HOME"], str(r.session_home("A")))
                self.assertTrue(calls[0]["CLAUDE_CONFIG_DIR"].startswith(str(r.session_home("A"))))
                self.assertEqual(list(Path(person).rglob("*")), [])
            finally:
                os.environ["HOME"] = old

    def test_budget_hook_script(self):
        with tempfile.TemporaryDirectory() as d:
            tr = Path(d) / "t.jsonl"
            u = {"input_tokens": 10, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 0, "output_tokens": 1}
            tr.write_text(json.dumps({"type": "assistant", "message": {"model": "m", "usage": u}}) + "\n")
            inp = json.dumps({"transcript_path": str(tr), "tool_name": "Read", "tool_input": {}})
            log = Path(d) / "log.jsonl"
            for mode, printed in (("shadow", False), ("enforce", True)):
                out = io.StringIO()
                budget_hook.main(["--soft", "100", "--hard", "200", "--mode", mode, "--log", str(log)], io.StringIO(inp), out)
                self.assertEqual(bool(out.getvalue()), printed)
            recs = [json.loads(x) for x in log.read_text().splitlines()]
            try:
                import rlo.ctxbudget  # noqa: F401
            except ImportError:
                self.assertEqual(recs[0]["stage"], "unknown")
                return
            self.assertEqual([r["stage"] for r in recs], ["checkpoint", "checkpoint"])
            self.assertEqual(recs[0]["ctx"], 510)
            self.assertIn("deny", out.getvalue())


class FreshRunner:
    """A headless runner whose answer is scripted; ``act`` may commit like a session would."""

    kind = "headless"

    def __init__(self, answers, act=None):
        self.answers, self.act, self.calls = list(answers), act, []

    def run_turn(self, req):
        self.calls.append(req)
        if self.act:
            self.act(req)
        a = self.answers.pop(0)
        return TurnResult(ended=True, session_id=f"sid-{len(self.calls)}", cost=0.01, answer=a, model="m",
                          usage={"input": 5, "cache_read": 100, "cache_creation": 20, "output": 3},
                          raw={"num_turns": 2, "subtype": "success", "is_error": False})


class HubFreshTest(unittest.TestCase):
    def world(self, answers, **kw):
        r = FreshRunner(answers, **kw)
        w = World(runner=r)
        w.cfg.sessions["A"].context = "fresh"
        w.cfg.sessions["A"].pack_max_tokens = 6000
        return w, r

    def test_two_turns_state_cursor_no_resume(self):
        w, r = self.world([answer(state="STATE-ONE"), answer(state="STATE-TWO", did="CMD-A2")])
        try:
            w.hub.send(directive("CMD-A1", "A"))
            st = w.hub.load_state()
            self.assertNotIn("A", st["resume"])
            self.assertTrue(r.calls[0].fresh)
            self.assertIsNone(r.calls[0].resume_id)
            self.assertIn("ctxpack/1", r.calls[0].prompt)
            self.assertEqual(w.hub.state_file("A").read_text(), "STATE-ONE\n")
            posts = w.mail.read("A")
            self.assertEqual([p.author for p in posts], ["hub", "A"])
            self.assertEqual(st["ctx"]["A"]["cursor"], posts[0].id)
            t = st["turns"][-1]
            self.assertEqual(t["usage"]["cache_read"], 100)
            self.assertEqual(t["report_post"], posts[1].id)
            self.assertEqual(t["error"], "")
            st["resume"]["A"] = "stale"  # even a stale id from an earlier resume run is never used
            w.hub.save_state(st)
            w.hub.send(directive("CMD-A2", "A"))
            st = w.hub.load_state()
            self.assertIsNone(r.calls[1].resume_id)
            self.assertNotIn("A", st["resume"])
            self.assertIn("STATE-ONE", r.calls[1].prompt)  # the next turn reads the state file
            self.assertIn(posts[1].id + " A ", r.calls[1].prompt)  # inbox header since the cursor
            self.assertNotIn(posts[0].id + " hub ", r.calls[1].prompt)  # not before it
            self.assertEqual(w.hub.state_file("A").read_text(), "STATE-TWO\n")
            ev = [json.loads(x) for x in (w.ga / "telemetry" / "A.jsonl").read_text().splitlines()]
            self.assertEqual(len(ev), 2)
            self.assertEqual(ev[1]["data"]["reported_cache_read_input_tokens"], 100)
            self.assertEqual(ev[1]["data"]["decision_ref"], "CMD-A2")
        finally:
            w.close()

    def test_bad_answer_is_a_failed_turn(self):
        w, r = self.world([report_text()])  # no state block
        try:
            _, findings, _ = w.hub.send(directive("CMD-A1", "A"))
            st = w.hub.load_state()
            self.assertTrue(st["turns"][-1]["error"].startswith("answer:"))
            self.assertTrue(any("answer:" in f.message for f in findings))
            self.assertEqual([p.author for p in w.mail.read("A")], ["hub"])  # nothing posted
            self.assertFalse(w.hub.state_file("A").exists())
            self.assertIsNone(st.get("ctx", {}).get("A", {}).get("cursor"))
            self.assertEqual(len(r.calls), 1)  # not retried
        finally:
            w.close()

    def test_cursor_moves_only_after_post(self):
        w, r = self.world([answer()])
        orig = w.mail.post

        def post(channel, author, text):
            if author == "A":
                raise OSError("down")
            return orig(channel, author, text)
        w.mail.post = post
        try:
            w.hub.send(directive("CMD-A1", "A"))
            st = w.hub.load_state()
            self.assertIn("post failed", st["turns"][-1]["error"])
            self.assertIsNone(st.get("ctx", {}).get("A", {}).get("cursor"))
            self.assertFalse(w.hub.state_file("A").exists())
        finally:
            w.close()

    def test_head_over_cap_runs_no_turn(self):
        w, r = self.world([answer()])
        w.cfg.sessions["A"].pack_max_tokens = 50
        try:
            _, findings, _ = w.hub.send(directive("CMD-A1", "A"))
            self.assertEqual(r.calls, [])
            self.assertTrue(w.hub.load_state()["turns"][-1]["error"].startswith("ctxpack:"))
        finally:
            w.close()

    def test_pack_respects_cap_in_hub(self):
        w, r = self.world([answer(), answer(did="CMD-A2")])
        try:
            w.hub.send(directive("CMD-A1", "A"))
            w.hub.state_file("A").write_text("S" * 30000)  # a state bigger than the cap: dropped, recorded
            w.hub.send(directive("CMD-A2", "A"))
            t = w.hub.load_state()["turns"][-1]
            self.assertLessEqual(ctxpack.tokens(r.calls[1].prompt), 6000 + 1)
            self.assertIn("state", [d["part"] for d in t["pack"]["dropped"]])
        finally:
            w.close()

    def test_config(self):
        from ga import config as gacfg
        from ga.forms import FormError
        w, _ = self.world([])
        try:
            raw = json.loads(json.dumps(w.raw))
            raw["sessions"]["A"]["context"] = "fresh"
            with self.assertRaises(FormError):
                gacfg.from_dict(raw, w.tmp)  # pack_max_tokens is required
            raw["sessions"]["A"]["pack_max_tokens"] = 8000
            self.assertEqual(gacfg.from_dict(raw, w.tmp).sessions["A"].context, "fresh")
            raw["sessions"]["A"]["context"] = "sometimes"
            with self.assertRaises(FormError):
                gacfg.from_dict(raw, w.tmp)
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
