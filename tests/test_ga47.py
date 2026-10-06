"""CMD-GA47: the model router for ga act — item route, outcome ledger, one triage turn, one rung up, the cheapest rung's
cap, the plan's route, `ga act routes` and triage-compare. Fake backends on temporary git repos: 0 model calls."""
import contextlib
import io
import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ga import __main__ as M  # noqa: E402
from ga.act import loop as A  # noqa: E402
from ga.act import commands as K  # noqa: E402
from ga.act import route as RT  # noqa: E402
from ga.plan import card as PC  # noqa: E402
from ga.plan import draft as PD  # noqa: E402
from tests.test_ga38 import FIX, Fake, py_repo  # noqa: E402
from tests.test_ga45 import sh  # noqa: E402

LOW, F1, PRO, SON = "gpt-oss-120b-medium", "gemini-3.6-flash-low", "gemini-3.1-pro-high", "claude-sonnet-5-5-high"
CFG = {"commands": {"test": ["{python}", "-m", "unittest", "-v"]}, "done_when": "test", "timeout_s": 60,
       "models": [SON, PRO, F1, LOW]}  # out of order on purpose: the ladder sorts them cheapest first
MARKER = "ZQX-UNIQUE-MARKER-7731"
STALL = "NEW junk.py\nprint('again')\n"           # applied every turn, the failing set never moves
NEED = "NEED grep add\n"                           # not an edit, not idle: only the turn cap stops it


def triage_answer(start, d=2, t=4):
    return "```json\n" + json.dumps({"difficulty": d, "start": start, "max_turns": t, "why": "small fix"}) + "\n```"


class Router(unittest.TestCase):
    def repo(self, extra=None):
        root = py_repo(self, CFG)
        (root / "calc.py").write_text((root / "calc.py").read_text() + f"# {MARKER}\n")
        for k, v in (extra or {}).items():
            (root / k).parent.mkdir(parents=True, exist_ok=True)
            (root / k).write_text(v)
        sh("git", "init", "-q", cwd=root)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@x", "add", ".", cwd=root)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "c1", cwd=root)
        return root

    def state(self):
        import tempfile
        s = Path(tempfile.mkdtemp(prefix="ga47-state-"))
        self.addCleanup(shutil.rmtree, s, True)
        return s

    def go(self, root, state, fakes, triage=None, item=None, **kw):
        made, tri = [], []

        def make(m):
            made.append(m)
            return fakes[m]

        def make_triage(m):
            tri.append(m)
            return triage[m] if isinstance(triage, dict) else triage
        it = item or {"id": "CMD-R1", "goal": "make the tests pass", "files": ["calc.py", "junk.py"]}
        res = A.run_item(root, it, backend="fake", model="", options={"route": True}, make_runner=make,
                         make_triage=make_triage, state_dir=state, **kw)
        return res, made, tri

    # ------------------------------------------------------------------ D1: card
    def test_card_small_and_never_holds_file_content(self):
        many = {f"pkg/mod_{i:03d}.py": f"x = '{MARKER}'\n" * 40 for i in range(120)}
        root, state = self.repo(many), self.state()
        tf = Fake([triage_answer(LOW)])
        item = {"id": "CMD-R1", "goal": "make the tests pass", "files": ["calc.py", "pkg/*.py"]}
        res, made, tri = self.go(root, state, {LOW: Fake([FIX])}, triage=tf, item=item)
        self.assertEqual(tri, [PRO])
        sent = tf.calls[0]["prompt"]
        self.assertLessEqual(len(sent.encode("utf-8")), RT.CARD_MAX)
        self.assertNotIn(MARKER, sent)
        self.assertIn("calc.py", sent)
        self.assertIn("test_calc.py", sent)       # test file names
        self.assertIn("done_when: test", sent)     # done_when command names
        self.assertEqual(res.status, "done")
        self.assertEqual(res.route["source"], "triage")

    def test_card_direct_has_sizes_and_no_content(self):
        root = self.repo()
        cfg = K.load(root)
        f = RT.features(root, {"files": ["calc.py"], "goal": "g"}, cfg)
        text = RT.card("g", f, RT.rungs(cfg["models"]))
        size = (root / "calc.py").stat().st_size
        self.assertIn(f"calc.py {size} py", text)
        self.assertNotIn(MARKER, text)
        self.assertNotIn("return a - b", text)

    # ------------------------------------------------------------------ D1: sources
    def test_valid_item_route_makes_no_triage_call(self):
        root, state = self.repo(), self.state()
        item = {"id": "CMD-R1", "goal": "g", "files": ["calc.py"], "route": {"difficulty": 2, "start": F1, "max_turns": 4}}
        res, made, tri = self.go(root, state, {F1: Fake([FIX])}, triage=Fake(["x"]), item=item)
        self.assertEqual(tri, [])
        self.assertEqual(made, [F1])
        self.assertEqual(res.route, {"difficulty": 2, "start": F1, "max_turns": 4, "source": "item"})

    def test_three_successes_route_the_fourth_from_the_ledger(self):
        state = self.state()
        starts = [LOW, F1, LOW]
        for i, s in enumerate(starts):
            item = {"id": f"CMD-R{i}", "goal": "g", "files": ["calc.py"],
                    "route": {"difficulty": 2, "start": s, "max_turns": 4}}
            res, _, _ = self.go(self.repo(), state, {s: Fake([FIX])}, triage=Fake(["x"]), item=item)
            self.assertEqual(res.status, "done")
        res, made, tri = self.go(self.repo(), state, {F1: Fake([FIX])}, triage=Fake(["x"]),
                                 item={"id": "CMD-R9", "goal": "g", "files": ["calc.py"]})
        self.assertEqual(tri, [])                       # 0 triage calls
        self.assertEqual(res.route["source"], "ledger")
        self.assertEqual(res.route["start"], F1)        # the cheapest rung that succeeded for all three
        self.assertEqual(made, [F1])
        rows = RT.read(state)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({r["bucket"] for r in rows}), 1)

    def test_two_successes_are_not_enough(self):
        state = self.state()
        for i in range(2):
            item = {"id": f"CMD-R{i}", "goal": "g", "files": ["calc.py"],
                    "route": {"difficulty": 2, "start": LOW, "max_turns": 4}}
            self.go(self.repo(), state, {LOW: Fake([FIX])}, triage=Fake(["x"]), item=item)
        res, made, tri = self.go(self.repo(), state, {LOW: Fake([FIX])}, triage=Fake([triage_answer(LOW)]),
                                 item={"id": "CMD-R9", "goal": "g", "files": ["calc.py"]})
        self.assertEqual(tri, [PRO])
        self.assertEqual(res.route["source"], "triage")

    def test_invalid_triage_falls_back_to_cheapest_with_three_turns(self):
        for bad in ("not json at all", triage_answer("gpt-9-ultra"), triage_answer(LOW, d=9),
                    triage_answer(LOW, t=0), "```json\n[1, 2]\n```"):
            with self.subTest(bad=bad[:30]):
                root, state = self.repo(), self.state()
                res, made, tri = self.go(root, state, {LOW: Fake([FIX])}, triage=Fake([bad]))
                self.assertEqual(res.route, {"difficulty": 1, "start": LOW, "max_turns": 3, "source": "fallback"})
                self.assertEqual(made, [LOW])

    def test_failed_triage_call_falls_back(self):
        class Boom:
            def run_turn(self, *a, **k):
                raise RuntimeError("down")
        root, state = self.repo(), self.state()
        res, made, _ = self.go(root, state, {LOW: Fake([FIX])}, triage=Boom())
        self.assertEqual(res.route["source"], "fallback")

    def test_unknown_item_route_model_not_accepted(self):
        self.assertIsNone(RT.valid({"difficulty": 2, "start": "evil; rm -rf", "max_turns": 3}, list(RT.LADDER)))
        self.assertIsNone(RT.valid({"difficulty": True, "start": LOW, "max_turns": 3}, list(RT.LADDER)))
        root, state = self.repo(), self.state()
        item = {"id": "CMD-R1", "goal": "g", "files": ["calc.py"],
                "route": {"difficulty": 2, "start": "gpt-9-ultra", "max_turns": 4}}
        res, made, tri = self.go(root, state, {F1: Fake([FIX])}, triage=Fake([triage_answer(F1)]), item=item)
        self.assertEqual(tri, [PRO])
        self.assertEqual(res.route["source"], "triage")

    # ------------------------------------------------------------------ D1: climb + cap
    def test_no_progress_climbs_exactly_one_rung_and_stops(self):
        root, state = self.repo(), self.state()
        fakes = {F1: Fake([STALL]), PRO: Fake([STALL]), SON: Fake([FIX])}
        item = {"id": "CMD-R1", "goal": "g", "files": ["calc.py", "junk.py"],
                "route": {"difficulty": 3, "start": F1, "max_turns": 5}}
        res, made, _ = self.go(root, state, fakes, item=item)
        self.assertEqual(made, [F1, PRO])
        self.assertEqual(res.status, "blocked")
        self.assertTrue(res.reason.startswith("no progress"))
        self.assertEqual([g["model"] for g in res.rungs], [F1, PRO])
        self.assertEqual(RT.read(state)[-1]["rungs"], [F1, PRO])

    def test_climb_configurable(self):
        root, state = self.repo(), self.state()
        fakes = {F1: Fake([STALL]), PRO: Fake([STALL]), SON: Fake([FIX])}
        item = {"id": "CMD-R1", "goal": "g", "files": ["calc.py", "junk.py"],
                "route": {"difficulty": 3, "start": F1, "max_turns": 5}}
        res, made, _ = self.go(root, state, fakes, item=item, climb=2)
        self.assertEqual(made, [F1, PRO, SON])
        self.assertEqual(res.status, "done")
        for bad in (3, -1, True):
            with self.assertRaises(ValueError):
                RT.climb_of(bad)

    def test_cheapest_rung_cap_is_three(self):
        root, state = self.repo(), self.state()
        item = {"id": "CMD-R1", "goal": "g", "files": ["calc.py"],
                "route": {"difficulty": 1, "start": LOW, "max_turns": 8}}
        res, made, _ = self.go(root, state, {LOW: Fake([NEED]), F1: Fake([NEED])}, item=item, climb=1)
        self.assertEqual(res.rungs[0]["model"], LOW)
        self.assertEqual(res.rungs[0]["turns"], 3)
        self.assertEqual(res.rungs[0]["reason"], "turn cap (3)")
        self.assertEqual(res.rungs[1]["turns"], 8)       # the rung above keeps max_turns
        self.assertEqual(RT.plan_rungs({"start": LOW, "max_turns": 2}, RT.rungs(CFG["models"]), 1)[1], {LOW: 2, F1: 2})

    # ------------------------------------------------------------------ D1: tokens + act/1
    def test_triage_tokens_in_totals_and_route_in_act1(self):
        root, state = self.repo(), self.state()
        res, _, _ = self.go(root, state, {LOW: Fake([FIX])}, triage=Fake([triage_answer(LOW)]))
        d = res.to_dict()
        self.assertEqual(d["schema"], "act/1")
        self.assertGreater(d["tokens"]["triage"], 0)
        work = sum(v for k, v in res.rungs[0]["tokens"].items() if k not in ("estimated", "total"))
        self.assertEqual(d["tokens"]["total"], work + d["tokens"]["triage"])
        self.assertEqual(d["route"]["source"], "triage")
        row = RT.read(state)[-1]
        self.assertEqual(row["triage_tokens"], d["tokens"]["triage"])
        self.assertEqual(row["tokens"], d["tokens"]["total"])

    # ------------------------------------------------------------------ D1: triage-compare
    def test_triage_compare_records_both_uses_first(self):
        root, state = self.repo(), self.state()
        tr = {PRO: Fake([triage_answer(F1, d=2)]), SON: Fake([triage_answer(LOW, d=1)])}
        res, made, tri = self.go(root, state, {F1: Fake([FIX])}, triage=tr, triage_compare=f"{PRO},{SON}")
        self.assertEqual(tri, [PRO, SON])
        self.assertEqual(res.route["start"], F1)
        p = RT.read(state)[-1]["predictions"]
        self.assertEqual(p[PRO]["route"]["start"], F1)
        self.assertEqual(p[SON]["route"]["start"], LOW)
        self.assertEqual(RT.read(state)[-1]["triage_tokens"], p[PRO]["tokens"] + p[SON]["tokens"])

    def test_unknown_triage_model_refused(self):
        root, state = self.repo(), self.state()
        with self.assertRaises(K.ActConfigError):
            self.go(root, state, {LOW: Fake([FIX])}, triage=Fake(["x"]), triage_model="evil-model")

    # ------------------------------------------------------------------ D1: ga plan
    def test_plan_item_carries_a_valid_route(self):
        self.assertIn('"route"', PC.TEMPLATE)
        self.assertIn(LOW, PC.TEMPLATE)
        for lvl in ("1 ", "2 ", "3 ", "4 ", "5 "):
            self.assertIn("\n" + lvl, PC.TEMPLATE)
        head = {"schema": "directive/2", "id": "CMD-GA99", "rev": 1, "to": "GA", "goal": "g", "why": "w",
                "refs": ["r"], "scope": [{"id": "S1", "text": "s"}], "done_when": [{"id": "D1", "text": "d"}],
                "budget": {"claude_p_runs": 0}}
        repo = self.repo()
        good = {"goal": "fix add", "files": ["calc.py"], "done_when": ["test"],
                "route": {"difficulty": 2, "start": F1, "max_turns": 4}}
        d = PD.plan("fix add", repo, Fake(["```json\n" + json.dumps({"head": head, "item": good}) + "\n```"]))
        self.assertEqual(d.item["route"], {"difficulty": 2, "start": F1, "max_turns": 4})
        self.assertEqual(RT.valid(d.item["route"], list(RT.LADDER)), d.item["route"])
        bad = dict(good, route={"difficulty": 2, "start": "nope", "max_turns": 4})
        d = PD.plan("fix add", repo, Fake(["```json\n" + json.dumps({"head": head, "item": bad}) + "\n```"]))
        self.assertNotIn("route", d.item)

    def test_ladder_order_explicit(self):
        self.assertEqual(RT.LADDER[0], LOW)
        self.assertEqual(RT.LADDER[-1], "claude-opus-5-5-high")
        self.assertEqual(set(RT.LADDER), set(K.MODELS))
        self.assertEqual(RT.rungs(CFG["models"]), [LOW, F1, PRO, SON])


FIXTURE = [
    {"id": "a", "bucket": "b1", "route": {"source": "item"}, "rungs": ["m1"], "success": True, "tokens": 100,
     "triage_tokens": 0},
    {"id": "b", "bucket": "b1", "route": {"source": "triage"}, "rungs": ["m1", "m2"], "success": True, "tokens": 300,
     "triage_tokens": 40},
    {"id": "c", "bucket": "b2", "route": {"source": "triage"}, "rungs": ["m1", "m2"], "success": False, "tokens": 500,
     "triage_tokens": 60},
    {"id": "d", "bucket": "b2", "route": {"source": "ledger"}, "rungs": ["m2"], "success": True, "tokens": 200,
     "triage_tokens": 0},
]


class Routes(unittest.TestCase):
    def test_routes_numbers_match_fixture(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "routes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in FIXTURE) + "not json\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = M.main(["act", "routes", "--state", str(d), "--json"])
        self.assertEqual(code, 0)
        s = json.loads(out.getvalue())
        self.assertEqual(s["all"], {"items": 4, "start_succeeded_pct": 50.0, "climbs": 2, "mean_tokens": 275.0,
                                    "triage_tokens": 100})
        self.assertEqual(s["source"]["triage"], {"items": 2, "start_succeeded_pct": 0.0, "climbs": 2,
                                                 "mean_tokens": 400.0, "triage_tokens": 100})
        self.assertEqual(s["source"]["item"]["start_succeeded_pct"], 100.0)
        self.assertEqual(s["bucket"]["b1"], {"items": 2, "start_succeeded_pct": 50.0, "climbs": 1,
                                             "mean_tokens": 200.0, "triage_tokens": 40})
        self.assertEqual(s["bucket"]["b2"]["climbs"], 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            M.main(["act", "routes", "--state", str(d)])
        self.assertIn("by source", out.getvalue())
        self.assertIn("triage", out.getvalue())


class Version(unittest.TestCase):
    def test_version_0_16_0(self):
        import ga
        self.assertEqual(ga.__version__, "0.17.0")
        self.assertIn('version = "0.17.0"', (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())


if __name__ == "__main__":
    unittest.main()
