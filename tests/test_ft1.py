"""CMD-FT1 D1: the FINAL_TASK harness offline (0 model calls). A fake backend replays recorded usage JSON and scripted
replies; the tests cover the graders, the quota/metric arithmetic, the cap stop, the run order, bulk size and the three arms."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench" / "final_task"))

from ft import quota, world  # noqa: E402
from ft.arms import Run, execute, parse_reply  # noqa: E402
from ft.backend import FakeBackend  # noqa: E402
from ft.budget import Budget, CapReached  # noqa: E402
from ft.bulk import MIN_TOKENS, MAX_TOKENS, bulk_context  # noqa: E402
from ft.runner import HAIKU, SONNET, plan, run_matrix  # noqa: E402

# recorded from the real probe (claude -p, haiku, image smoke): the usage object as claude -p gives it
USAGE = {"input_tokens": 1097, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 123,
         "output_tokens_details": {"thinking_tokens": 116}}
SLUG = {"function": "def slugify(text):\n    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')",
        "test": "def test_slugify():\n    from textutil import slugify\n    assert slugify('Hello, World!') == 'hello-world'"}


class Sim:
    """A scripted model: answers like a well-behaved node/hub unless told to misbehave (tool / peer / guess)."""

    def __init__(self, mis=()):
        self.mis, self.n = set(mis), 0

    def __call__(self, model, system, prompt, image):
        self.n += 1
        return {"text": self.reply(system, prompt, image), "usage": dict(USAGE),
                "total_cost_usd": quota.quota_usd(model, USAGE)}

    def reply(self, system, prompt, image):
        task = prompt.split("## task\n")[-1] if "## task\n" in prompt else prompt
        if "You are the hub" in system:
            if "Judge request" not in prompt:
                w = "N2" if "img.code" in task else "N1"
                return json.dumps({"worker": w, "peer": "N2" if "hub_peer" in self.mis else (["N2", "N3"] if "svc.port" in task else None), "instruction": "do it"})
            return json.dumps({"answer": "WORKER"})
        if "## request from" in prompt:
            return json.dumps({"answer": "471926", "need": None} if image else {"answer": "UNKNOWN", "need": None})
        if "img.code" in task:
            if image:
                return json.dumps({"answer": "471926", "need": None})
            if "img.code = 471926" in prompt:
                return json.dumps({"answer": "471926", "need": None})
            return json.dumps({"answer": None, "need": {"kind": "peer", "to": "N2", "ref": "img.code"}})
        if "slugify" in task:
            return "```json\n" + json.dumps({"answer": SLUG, "need": None}) + "\n```"
        if "svc.port" in task:
            return json.dumps({"answer": "8081" if "CONTRADICTED" in prompt else "UNKNOWN", "need": None})
        if "svc.retry_limit" in task:
            if "tool" in self.mis:
                return json.dumps({"answer": None, "need": {"kind": "tool", "ref": "svc.retry_limit"}})
            return json.dumps({"answer": "5" if "svc.retry_limit = 5  [VALID" in prompt else "UNKNOWN", "need": None})
        if "billing.region_quota" in task:
            return json.dumps({"answer": "250000" if "guess" in self.mis else "UNKNOWN", "need": None})
        return json.dumps({"answer": "UNKNOWN", "need": None})


def do(arm, task, mis=(), model=HAIKU, mode="selective", max_calls=110):
    sim = Sim(mis)
    be = FakeBackend(sim)
    bd = Budget(None, max_calls=max_calls)
    bulk = bulk_context() if mode == "bulk" else None
    run = Run(world.make_tasks()[task], model, mode, be, bd, f"{arm}-{task}", bulk)
    return execute(run, arm), be, bd

# reference answer as a model would write it: the test calls slugify unqualified, relying on the fixture's imports
REF = {"function": SLUG["function"],
       "test": "def test_slugify():\n    assert slugify('Hello, World!') == 'hello-world'\n    assert slugify('  A  b__C ') == 'a-b-c'"}


class Graders(unittest.TestCase):
    def test_t2_reference_answer_passes_and_wrong_fails(self):
        """A void fixture (test file importing only the old names) must not recur: the reference answer is graded right."""
        self.assertTrue(world.grade_t2(REF, 0, 0)[0])
        self.assertFalse(world.grade_t2(dict(REF, function="def slugify(text):\n    return text.lower()"), 0, 0)[0])
        self.assertFalse(world.grade_t2(dict(REF, test="def test_slugify():\n    assert slugify('a b') == 'a_b'"), 0, 0)[0])
        self.assertIn("import *", (world.FIX / "t2" / "test_textutil.py").read_text())

    def test_t4_fails_if_a_tool_or_peer_message_is_used(self):
        g = world.grade_t4
        self.assertEqual(g("5", 0, 0), (True, None))
        self.assertFalse(g("5", 1, 0)[0])
        self.assertFalse(g("5", 0, 2)[0])
        self.assertFalse(g("6", 0, 0)[0])

    def test_t5_fails_on_any_guess(self):
        g = world.grade_t5
        self.assertEqual(g("UNKNOWN", 0, 0), (True, True))
        self.assertEqual(g(" unknown ", 0, 0), (True, True))
        for guess in ("250000", "0", "", None, "probably 100000"):
            self.assertEqual(g(guess, 0, 0), (False, False), guess)

    def test_t1_t3(self):
        self.assertTrue(world.grade_t1("471 926", 0, 0)[0])
        self.assertFalse(world.grade_t1("471925", 0, 0)[0])
        self.assertTrue(world.grade_t3("8081", 0, 0)[0])
        self.assertFalse(world.grade_t3("9090", 0, 0)[0])
        self.assertFalse(world.grade_t3("UNKNOWN", 0, 0)[0])
        self.assertFalse(world.grade_t3("8081 or 9090", 0, 0)[0])

    def test_t2_hidden_check(self):
        g = world.grade_t2
        self.assertTrue(g(SLUG, 0, 0)[0])
        bad = dict(SLUG, function="def slugify(text):\n    return text.lower()")
        self.assertFalse(g(bad, 0, 0)[0])
        self.assertFalse(g(dict(SLUG, test="def test_other():\n    assert True"), 0, 0)[0])   # no test of slugify
        self.assertFalse(g(dict(SLUG, test="def test_slugify():\n    assert False"), 0, 0)[0])  # its own test fails
        self.assertFalse(g("def slugify(t): pass", 0, 0)[0])
        self.assertFalse(g({"function": "def slugify(:", "test": "x"}, 0, 0)[0])

    def test_reply_parser(self):
        self.assertEqual(parse_reply('prose {"a": 1} more\n```json\n{"answer": "x", "need": null}\n```'), {"answer": "x", "need": None})
        self.assertIsNone(parse_reply("no json"))


class Arithmetic(unittest.TestCase):
    def test_quota_usd_prices_each_part(self):
        u = {"input_tokens": 1000, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 5000, "output_tokens": 300}
        self.assertAlmostEqual(quota.quota_usd(HAIKU, u), 1000e-6 + 200 * 1.25e-6 + 5000 * 0.1e-6 + 300 * 5e-6, 12)
        self.assertAlmostEqual(quota.quota_usd(SONNET, u), 2 * (1000e-6 + 200 * 1.25e-6 + 5000 * 0.1e-6) + 300 * 10e-6, 12)
        # cache_read is a tenth of input, never full input
        self.assertAlmostEqual(quota.quota_usd(HAIKU, {"cache_read_input_tokens": 1_000_000}), 0.1, 9)
        self.assertAlmostEqual(quota.quota_usd(HAIKU, {"input_tokens": 1_000_000}), 1.0, 9)
        self.assertAlmostEqual(quota.quota_usd(HAIKU, {"cache_creation_input_tokens": 1_000_000}), 1.25, 9)
        self.assertAlmostEqual(quota.quota_usd(SONNET, {"output_tokens": 1_000_000}), 10.0, 9)

    def test_metrics(self):
        a = {"usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 50}, "total_cost_usd": None}
        b = {"usage": {"input_tokens": 4000, "output_tokens": 10, "output_tokens_details": {"thinking_tokens": 4}},
             "total_cost_usd": None}
        m = quota.metrics(HAIKU, [a, b], correct=True, tool_calls=0, peer_messages=2,
                          ctx={"context_items_used": {}, "context_items_available": 0}, repeated=1, unknown_correct=None)
        self.assertEqual(m["input_tokens"], 5000)
        self.assertEqual(m["output_tokens"], 60)
        self.assertEqual(m["thinking_tokens"], 4)
        self.assertEqual(m["total_tokens"], 5060)
        self.assertEqual(m["max_call_input"], 4000)
        qa, qb = 100e-6 + 900 * 0.1e-6 + 50 * 5e-6, 4000e-6 + 10 * 5e-6
        self.assertAlmostEqual(m["quota_usd"], qa + qb, 12)
        self.assertAlmostEqual(m["max_call_share"], qb / (qa + qb), 9)
        self.assertAlmostEqual(m["quota_hte"], (qa + qb) / 1e-6, 4)
        self.assertAlmostEqual(m["quota_per_correct"], qa + qb, 12)
        m2 = quota.metrics(HAIKU, [a], correct=False, tool_calls=0, peer_messages=0,
                           ctx={"context_items_used": {}, "context_items_available": 0}, repeated=0, unknown_correct=None)
        self.assertIsNone(m2["quota_per_correct"])

    def test_cost_disagreement_flag(self):
        u = {"input_tokens": 1000, "output_tokens": 100}
        q = quota.quota_usd(HAIKU, u)
        ok = {"usage": u, "total_cost_usd": q * 1.04}
        bad = {"usage": u, "total_cost_usd": q * 1.20}
        m = quota.metrics(HAIKU, [ok, bad], correct=True, tool_calls=0, peer_messages=0,
                          ctx={"context_items_used": {}, "context_items_available": 0}, repeated=0, unknown_correct=None)
        self.assertEqual(m["cost_disagree_calls"], [1])


class Cap(unittest.TestCase):
    def test_call_cap_stops_the_matrix_with_partial_results(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bd = Budget(d / "l.jsonl", max_calls=10)
            out = run_matrix(FakeBackend(Sim()), bd, d / "runs.jsonl", items=plan()[10:], bulk=None, log=lambda *_: None)
            self.assertTrue(out["stopped"].startswith("cap"), out)
            self.assertLessEqual(bd.calls, 10)
            rows = [json.loads(x) for x in (d / "runs.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), out["started"])
            self.assertGreater(out["started"], 0)

    def test_usd_cap_stops(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bd = Budget(d / "l.jsonl", usd_cap=0.002)
            out = run_matrix(FakeBackend(Sim()), bd, d / "runs.jsonl", items=plan()[10:], bulk=None, log=lambda *_: None)
            self.assertTrue(out["stopped"].startswith("cap"))
            self.assertLessEqual(bd.usd, 0.002)

    def test_before_call_refuses_past_the_cap(self):
        bd = Budget(None, max_calls=1)
        bd.before_call()
        bd.record({"quota_usd": 0.0})
        with self.assertRaises(CapReached):
            bd.before_call()
        with self.assertRaises(CapReached):
            Budget(None, usd_cap=0.001).before_call(0.01)

    def test_ledger_persists_across_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "l.jsonl"
            a = Budget(p)
            a.record({"quota_usd": 1.5})
            a.record({"quota_usd": 2.0})
            b = Budget(p)
            self.assertEqual((b.calls, b.usd), (2, 3.5))
            self.assertFalse(Budget(p, max_calls=3).can_start(2, 0.0))
            self.assertFalse(Budget(p, usd_cap=4.0).can_start(1, 1.0))
            self.assertTrue(Budget(p).can_start(4, 30.0))
            self.assertEqual((Budget(None).max_calls, Budget(None).usd_cap), (110, 35.0))

    def test_resume_skips_done_runs(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            items = plan()[10:14]
            run_matrix(FakeBackend(Sim()), Budget(d / "l"), d / "r", items=items, log=lambda *_: None)
            be = FakeBackend(Sim())
            out = run_matrix(be, Budget(d / "l"), d / "r", items=items, log=lambda *_: None)
            self.assertEqual((out["started"], len(be.calls)), (0, 0))


class Order(unittest.TestCase):
    def test_matrix_and_order(self):
        p = plan()
        self.assertEqual(len(p), 100)
        self.assertEqual(len({(r["arm"], r["model"], r["mode"], r["task"], r["rep"]) for r in p}), 100)
        self.assertEqual(sum(r["mode"] == "bulk" for r in p), 10)
        self.assertTrue(all(r["arm"] == "A" and r["rep"] == 1 for r in p if r["mode"] == "bulk"))
        main = [r for r in p if r["mode"] == "selective"]
        self.assertEqual(len(main), 90)
        for arm in "ABC":
            for m in (HAIKU, SONNET):
                for t in ("T1", "T2", "T3", "T4", "T5"):
                    self.assertEqual(sorted(r["rep"] for r in main if (r["arm"], r["model"], r["task"]) == (arm, m, t)), [1, 2, 3])
        # the bulk control and its selective pair come first, each pair adjacent
        for i in range(0, 20, 2):
            a, b = p[i], p[i + 1]
            self.assertEqual((a["mode"], b["mode"]), ("bulk", "selective"))
            self.assertEqual((a["arm"], a["model"], a["task"]), (b["arm"], b["model"], b["task"]))
        self.assertEqual([r["model"] for r in p[:10]], [HAIKU] * 10)
        # then B and C rep 1 (both models of a task side by side) before any rep 2
        self.assertTrue(all(r["rep"] == 1 for r in p[20:40]) and {r["arm"] for r in p[20:40]} == {"B", "C"})
        self.assertEqual((p[20]["model"], p[21]["model"]), (HAIKU, SONNET))
        self.assertTrue(all(r["rep"] >= 2 for r in p[40:]))


class Bulk(unittest.TestCase):
    def test_bulk_is_whole_files_between_50k_and_150k(self):
        b = bulk_context()
        self.assertGreaterEqual(b["tokens"], MIN_TOKENS)
        self.assertLessEqual(b["tokens"], MAX_TOKENS)
        for f in b["files"][:5]:
            body = (ROOT / f).read_text(encoding="utf-8")
            self.assertIn(f"=== {f} ===\n{body}\n", b["text"])        # whole, not cut
        self.assertFalse(any(f.startswith("bench/") for f in b["files"]))   # the hidden check never goes in

    def test_bulk_never_silently_smaller(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.py").write_text("x = 1\n" * 100)
            with self.assertRaises(ValueError):
                bulk_context(Path(d))

    def test_bulk_prompt_reaches_every_call_and_selective_never_has_it(self):
        row, be, _ = do("A", "T4", mode="bulk")
        self.assertTrue(all("## project files (whole)" in c["prompt"] for c in be.calls))
        self.assertGreaterEqual(min(len(c["prompt"]) for c in be.calls) // 4, MIN_TOKENS)
        row, be, _ = do("A", "T4")
        self.assertFalse(any("project files" in c["prompt"] for c in be.calls))


class Arms(unittest.TestCase):
    def test_well_behaved_model_gets_every_task_right_in_every_arm(self):
        for arm in "ABC":
            for t in ("T1", "T2", "T3", "T4", "T5"):
                row, _, _ = do(arm, t)
                self.assertTrue(row["result_correct"], (arm, t, row["answer"], row["error"]))
                if t == "T4":
                    self.assertEqual((row["tool_calls"], row["peer_messages"]), (0, 0), arm)
                if t == "T5":
                    self.assertTrue(row["unknown_correct"])

    def test_call_counts_and_peer_messages(self):
        n = lambda arm, t, **k: do(arm, t, **k)[0]  # noqa: E731
        self.assertEqual([n("A", t)["llm_calls"] for t in ("T1", "T2", "T3", "T4", "T5")], [3, 3, 3, 3, 3])
        self.assertEqual([n("B", t)["llm_calls"] for t in ("T1", "T2", "T3", "T4", "T5")], [1, 1, 1, 1, 1])
        self.assertEqual(n("C", "T1")["llm_calls"], 2)               # N2's vision turn, then N1's own turn
        self.assertEqual(n("C", "T1")["peer_messages"], 2)
        self.assertEqual(n("C", "T3")["peer_messages"], 4)           # two consults (edge budget 2), a contradiction
        self.assertEqual(n("B", "T3")["peer_messages"], 0)           # the code hub reads exports, no node-to-node message
        self.assertEqual(n("C", "T5")["peer_messages"], 0)
        for arm in "ABC":
            self.assertLessEqual(max(n(arm, t)["llm_calls"] for t in ("T1", "T2", "T3", "T4", "T5")), {"A": 4, "B": 3, "C": 4}[arm])

    def test_t3_is_settled_from_evidence_by_state_rules(self):
        row, be, _ = do("C", "T3")
        self.assertIn("CONTRADICTED", be.calls[-1]["prompt"])
        self.assertIn("8081", be.calls[-1]["prompt"])
        self.assertIn("9090", be.calls[-1]["prompt"])

    def test_t4_misbehaviour_fails(self):
        for arm in "ABC":
            row, _, _ = do(arm, "T4", mis=("tool",))
            self.assertFalse(row["result_correct"], arm)
            self.assertGreaterEqual(row["tool_calls"], 1)
            self.assertGreaterEqual(row["repeated_information"], 1)    # re-asking for a VALID fact counts
        row, _, _ = do("A", "T4", mis=("hub_peer",))                   # an LLM hub that orders a pointless consult
        self.assertFalse(row["result_correct"])
        self.assertEqual(row["peer_messages"], 2)

    def test_t5_guess_fails(self):
        for arm in "ABC":
            row, _, _ = do(arm, "T5", mis=("guess",))
            self.assertFalse(row["result_correct"])
            self.assertFalse(row["unknown_correct"])

    def test_text_only_node_never_gets_the_image(self):
        for arm in "ABC":
            row, be, _ = do(arm, "T1")
            for c in be.calls:
                if c["image"]:
                    self.assertIn("node N2", c["system"])             # only the vision-capable node

    def test_selection_and_repetition(self):
        row, _, _ = do("B", "T4")
        self.assertEqual(row["context_items_used"]["keep"], 1)
        self.assertGreater(row["context_items_available"], 10)        # distractor facts and peer exports stay out
        a, _, _ = do("A", "T4")
        b, _, _ = do("B", "T4")
        self.assertGreater(a["repeated_information"], b["repeated_information"])   # the hub resends its first prompt

    def test_rows_hold_no_prompt_text(self):
        row, _, _ = do("A", "T2", mode="bulk")
        self.assertNotIn("prompt", json.dumps(row))
        self.assertNotIn("=== ga/", json.dumps(row))

    def test_failed_call_ends_the_run_and_still_counts(self):
        from ga.backends.base import BackendError

        def boom(*a):
            raise BackendError("exit_1")
        row, _, bd = do("B", "T4") if False else (None, None, None)
        be = FakeBackend(boom)
        bd = Budget(None)
        run = Run(world.make_tasks()["T4"], HAIKU, "selective", be, bd, "x")
        row = execute(run, "B")
        self.assertEqual(row["error"], "exit_1")
        self.assertFalse(row["result_correct"])
        self.assertEqual(bd.calls, 1)


if __name__ == "__main__":
    unittest.main()


class Summary(unittest.TestCase):
    def test_summary_renders_from_fake_runs(self):
        from ft import summary
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            run_matrix(FakeBackend(Sim()), Budget(d / "ledger.jsonl"), d / "runs.jsonl", items=plan(reps=1)[:60], log=lambda *_: None)
            md = summary.write(d, "test").read_text()
            for h in ("## 1.", "## 3. H1", "## 4. H2", "## 5. H3", "Haiku", "Sonnet", "(1 rep)"):
                self.assertIn(h, md)
            self.assertIn("PASS", md)
