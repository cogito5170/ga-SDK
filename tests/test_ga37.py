"""CMD-GA37: ga do / intake -> task/1, model-agnostic. Offline: every backend is a real ga runner on a fake host.

D1 same spec on all five backends · byte-identical prompt · one repair turn with only the problems, then blocked ·
   vague acceptance flagged · a non-needs question refused · repo summary under 2000 tokens on a large fixture ·
   ledger rows == turns
D2 the golden set (tests/fixtures/ga37/golden.json; mutations in tests/mutations_ga37.py)
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ga37_world import FIVE, MODELS, USAGE, Hosts, golden, make_repo  # noqa: E402

from ga import __main__ as ga_main  # noqa: E402
from ga.forms import hard, soft, validate  # noqa: E402
from ga.forms.task import NEEDS, REQUEST_CAP, is_command, names_path, vague_words  # noqa: E402
from ga.intake import CAP, INSTRUCTION, Intake, MAX_REPAIRS, parse, prompt, summarize, task_id  # noqa: E402
from ga.intake import cli as do_cli  # noqa: E402
from ga.intake.engine import overhead_of  # noqa: E402

os.environ.setdefault("GA37_FAKE_KEY", "fake-key-for-tests")  # the http runners read a key name; no real key exists


def good_answer(**over):
    a = {"kind": "change", "goal": "Add an annual toggle to the pricing page",
         "deliverables": [{"id": "V1", "what": "the toggle", "where": "src/pages/Pricing.tsx"}],
         "constraints": [], "acceptance": [{"id": "A1", "check": "npm test", "kind": "command"}],
         "non_goals": [], "assumptions": [{"text": "discount", "default": "20%"}], "questions": [], "risks": []}
    a.update(over)
    return a


def spec_of(**over):
    s = {"schema": "task/1", "id": "T-0123abcd", "request": "r", **good_answer(),
         "repo": {"languages": {"typescript": 3}, "test_cmds": ["npm test"], "build_cmds": []}}
    s.update(over)
    return s


def rules(problems):
    return [(p.rule, p.strength) for p in problems]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = make_repo(self.tmp / "repo")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_on(self, backend, answers, request="add an annual toggle", **kw):
        h = Hosts(answers, **kw.pop("host", {}))
        ga = self.tmp / f"ga-{backend}-{len(list(self.tmp.iterdir()))}"
        res = Intake(h.runner(backend, str(self.root)), backend=backend, model=MODELS[backend], ga_dir=ga,
                     **kw).run(request, self.root)
        return res, h, ga


# ---------------------------------------------------------------------------------------------------- S1: the form

class TaskForm(unittest.TestCase):
    def test_good_spec_has_no_problems(self):
        self.assertEqual(validate(spec_of()), [])

    def test_shape_rules(self):
        self.assertTrue(hard(validate(spec_of(deliverables=[]))))
        self.assertTrue(hard(validate(spec_of(acceptance=[]))))
        self.assertTrue(hard(validate(spec_of(id="task-1"))))
        self.assertTrue(hard(validate(spec_of(request="x" * (REQUEST_CAP + 1)))))
        self.assertTrue(hard(validate(spec_of(extra=1))))
        self.assertTrue(hard(validate(spec_of(assumptions=[{"text": "t"}]))))  # an assumption needs its default
        self.assertTrue(hard(validate(spec_of(repo={"languages": {}, "test_cmds": "npm test", "build_cmds": []}))))
        dup = [{"id": "A1", "check": "npm test", "kind": "command"}] * 2
        self.assertIn(("task:ids", "hard"), rules(validate(spec_of(acceptance=dup))))

    def test_question_needs_only_the_four(self):
        for n in NEEDS:
            self.assertEqual(validate(spec_of(questions=[{"text": "q", "needs": n}])), [], n)
        for n in ("preference", "clarification", "", "design"):
            ps = hard(validate(spec_of(questions=[{"text": "Which DB?", "needs": n}])))
            self.assertTrue(ps and ps[0].path == "$.questions", n)

    def test_acceptance_kinds(self):
        def probs(check, kind, **x):
            return rules(validate(spec_of(acceptance=[{"id": "A1", "check": check, "kind": kind, **x}])))
        for ok in ("npm test", "python -m pytest tests/test_login.py", "make test", "./run.sh --fast-path",
                   "npx vitest run tests/login.test.ts --repeat 50"):
            self.assertEqual(probs(ok, "command"), [], ok)
        for bad in ("Run the tests", "the tests pass", "테스트가 통과한다", "Login works fine now", ""):
            self.assertTrue(probs(bad, "command"), bad)
        self.assertEqual(probs("src/pages/Pricing.tsx", "file"), [])
        self.assertEqual(probs("docs/desktop-shell.md exists", "file"), [])
        self.assertIn(("task:check", "hard"), probs("a pricing file", "file"))
        self.assertEqual(probs("exits 0", "observable"), [])
        self.assertEqual(probs('the page shows "Saved"', "observable"), [])
        self.assertIn(("task:check", "hard"), probs("the page looks right", "observable"))
        self.assertIn(("task:review", "hard"), probs("the docs read well", "review"))  # no rubric
        self.assertEqual(probs("the docs read as a guide", "review", rubric=["links back to the index"]),
                         [("task:review", "soft")])
        self.assertIn(("task:review", "hard"), probs("npm test", "command", rubric=["x"]))

    def test_vague_words(self):
        for text in ("loads fast", "the code is clean", "works properly", "a nice page", "it is good",
                     "적절히 처리한다", "잘 동작한다", "깔끔하게 정리", "빠르게 로드된다"):
            self.assertTrue(vague_words(text), text)
        for text in ("loads in under 2 s", 'shows "fast mode"', "잘못된 입력을 거부한다", "goodbye page",
                     "2초 안에 빠르게 로드된다", "npm test"):
            self.assertEqual(vague_words(text), [], text)

    def test_vague_acceptance_is_hard_elsewhere_soft(self):
        ps = validate(spec_of(acceptance=[{"id": "A1", "check": "토글이 깔끔하게 동작한다", "kind": "observable"}]))
        self.assertIn(("task:vague", "hard"), rules(ps))
        ps = validate(spec_of(acceptance=[{"id": "A1", "check": "the search is fast", "kind": "review",
                                           "rubric": ["x"]}]))
        self.assertIn(("task:vague", "hard"), rules(ps))
        ps = validate(spec_of(goal="make the page fast", constraints=["keep it clean"]))
        self.assertEqual(hard(ps), [])
        self.assertEqual([p.path for p in soft(ps) if p.rule == "task:vague"], ["$.goal", "$.constraints[0]"])

    def test_helpers(self):
        self.assertTrue(is_command("go test ./..."))
        self.assertFalse(is_command("ensure it builds"))
        self.assertTrue(names_path("see web/app.vue"))
        self.assertFalse(names_path("no path here"))


# ---------------------------------------------------------------------------------------------- S2: summary, parse

class Summary(Base):
    def test_fixture_summary(self):
        s = summarize(self.root)
        self.assertEqual(s.repo(), {"languages": {"typescript": 5}, "test_cmds": ["npm test"],
                                    "build_cmds": ["npm run build"]})
        self.assertIn("npm run lint", s.text)
        self.assertIn("README.md", s.text)
        self.assertIn("# Pricing App", s.text)
        self.assertEqual(s.text, summarize(self.root).text)  # same tree, same bytes

    def test_large_fixture_stays_under_the_cap(self):
        big = self.tmp / "big"
        for i in range(120):
            d = big / f"package_with_a_rather_long_directory_name_{i:03d}"
            d.mkdir(parents=True)
            for j in range(25):
                (d / f"module_file_with_long_name_{j:03d}.py").write_text("x = 1\n")
        for i in range(400):
            (big / f"top_level_file_with_a_long_name_{i:04d}.ts").write_text("export {}\n")
        (big / "README.md").write_text("\n".join("lorem ipsum dolor sit amet " * 8 for _ in range(5000)))
        (big / "package.json").write_text(json.dumps({"scripts": {f"test:case{i}": "x" for i in range(400)}}))
        (big / "Makefile").write_text("".join(f"test{i}:\n\techo\n" for i in range(300)))
        (big / "ga-do.json").write_text(json.dumps({"backend": "claude_cli", "model": "m", "options": {"x": 1}}))
        s = summarize(big)
        self.assertLessEqual(s.tokens, CAP)
        self.assertTrue(s.dropped)
        self.assertEqual(s.languages, {"python": 3000, "typescript": 400})
        self.assertIn("[languages]", s.text)

    def test_config_values_other_than_backend_model_are_not_copied(self):
        (self.root / "ga-do.json").write_text(json.dumps({"backend": "claude_cli", "model": "claude-haiku-4-5",
                                                          "options": {"key_env": "SECRET_NAME"}}))
        s = summarize(self.root)
        self.assertIn("claude-haiku-4-5", s.text)
        self.assertNotIn("SECRET_NAME", s.text)

    def test_parse_fenced_bare_or_with_prose(self):
        o = {"goal": "g"}
        for text in (json.dumps(o), "```json\n" + json.dumps(o) + "\n```", "Sure:\n" + json.dumps(o) + "\nbye",
                     "```\n" + json.dumps(o) + "\n```", "x {not json} " + json.dumps(o)):
            self.assertEqual(parse(text), o, text)
        self.assertIsNone(parse("no json here"))


# ------------------------------------------------------------------------------------- S2/S3: the engine, D1 parity

class FiveBackends(Base):
    def test_same_request_same_answer_same_spec_on_all_five(self):
        ans = json.dumps(good_answer())
        specs = {}
        for b in FIVE:
            res, _, _ = self.run_on(b, [ans])
            self.assertEqual(res.status, "ready", (b, res.problems))
            specs[b] = res.spec
        self.assertEqual(len({json.dumps(s, sort_keys=True) for s in specs.values()}), 1)
        self.assertEqual(specs["agv"]["repo"]["test_cmds"], ["npm test"])
        self.assertEqual(specs["agv"]["id"], task_id("add an annual toggle"))

    def test_prompt_is_byte_identical_across_backends(self):
        """What reached each host, put back together as INSTRUCTION + SEP + message, is the same bytes."""
        texts = {}
        for b in FIVE:
            res, h, _ = self.run_on(b, [json.dumps(good_answer())])
            (call,) = h.calls()
            if call["system"] is not None:  # a system channel: the instruction there, the message as the prompt
                texts[b] = call["system"] + prompt.SEP + call["prompt"]
            else:
                texts[b] = call["prompt"]
            self.assertEqual(res.sent[0]["system"] is not None, b in ("claude_cli", "openai_http", "anthropic_http"))
        self.assertEqual(len(set(texts.values())), 1, {b: len(t) for b, t in texts.items()})
        self.assertTrue(next(iter(texts.values())).startswith(INSTRUCTION))
        self.assertIn("Request:\nadd an annual toggle", texts["agv"])

    def test_instruction_names_no_backend_or_model(self):
        for word in ("claude", "codex", "gpt", "gemini", "antigravity", "agy", "openai", "anthropic"):
            self.assertNotIn(word, INSTRUCTION.lower())

    def test_tools_off_where_the_backend_allows(self):
        _, h, _ = self.run_on("claude_cli", [json.dumps(good_answer())])
        argv = h.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("bypassPermissions", argv)
        for b in ("openai_http", "anthropic_http"):
            _, h, _ = self.run_on(b, [json.dumps(good_answer())])
            self.assertFalse(h.calls()[0]["tools"])
        runner = do_cli.make_runner("claude_cli", MODELS["claude_cli"], {"bare": False}, self.root, 5)
        self.assertTrue(runner.bare)  # intake forces bare even when the config says otherwise

    def test_agv_overhead_recorded(self):
        res, _, ga = self.run_on("agv", [json.dumps(good_answer())],
                                 overhead=overhead_of("agv", __import__("ga.backends", fromlist=["x"]).get("agv")))
        self.assertEqual(res.turns[0].overhead_tokens, 9852)  # GA41 S4: the plugin's measured default-agent figure
        self.assertFalse(res.turns[0].bare)
        row = json.loads(next((ga / "ledger").iterdir()).read_text().splitlines()[0])
        self.assertEqual((row["backend"], row["bare"], row["overhead_tokens"]), ("agv", False, 9852))
        res, _, _ = self.run_on("claude_cli", [json.dumps(good_answer())], overhead=945)
        self.assertIsNone(res.turns[0].overhead_tokens)  # bare: no host overhead beyond what usage reports

    def test_usage_tokens_read_for_every_backend(self):
        for b in FIVE:
            res, _, _ = self.run_on(b, [json.dumps(good_answer())])
            t = res.turns[0]
            self.assertEqual({"input": t.input, "output": t.output, "cache_read": t.cache_read}, USAGE, b)


class Repair(Base):
    def test_one_repair_with_only_the_problems_then_blocked(self):
        bad = json.dumps(good_answer(acceptance=[{"id": "A1", "check": "the toggle works nicely",
                                                  "kind": "observable"}]))
        for b in FIVE:
            res, h, _ = self.run_on(b, [bad, bad, bad, bad], request="연간 요금 토글을 추가해줘")
            self.assertEqual(res.status, "blocked", b)
            self.assertEqual(len(res.turns), 1 + MAX_REPAIRS, b)
            self.assertEqual(MAX_REPAIRS, 1)
            calls = h.calls()
            self.assertEqual(len(calls), 2, b)
            self.assertEqual([t.kind for t in res.turns], ["intake", "repair"])
            second = calls[1]["prompt"]
            if calls[1]["system"] is None:
                self.assertTrue(second.startswith(INSTRUCTION + prompt.SEP))
                second = second[len(INSTRUCTION + prompt.SEP):]
            first_problems = [p for p in res.problems]
            self.assertEqual(second, prompt.repair(first_problems, bad))
            self.assertNotIn("연간 요금", second)                # not the request
            self.assertNotIn("Material (gathered", second)     # not the material
            self.assertNotIn("[languages]", second)
            self.assertTrue(any("task:vague" in p for p in res.problems))

    def test_repair_that_fixes_is_ready(self):
        bad = json.dumps(good_answer(acceptance=[{"id": "A1", "check": "Run the tests", "kind": "command"}]))
        res, h, _ = self.run_on("codex_cli", [bad, json.dumps(good_answer())])
        self.assertEqual((res.status, len(res.turns)), ("ready", 2))

    def test_no_json_gets_the_repair_turn_too(self):
        res, _, _ = self.run_on("openai_http", ["I cannot do that.", json.dumps(good_answer())])
        self.assertEqual((res.status, len(res.turns)), ("ready", 2))

    def test_non_needs_question_never_reaches_a_person(self):
        q = [{"text": "Which colour do you like?", "needs": "preference"}]
        res, _, _ = self.run_on("anthropic_http", [json.dumps(good_answer(questions=q))] * 2)
        self.assertEqual(res.status, "blocked")
        self.assertEqual(res.human_questions(), [])
        self.assertTrue(any("$.questions" in p for p in res.problems))
        rep = do_cli.report(res)
        self.assertEqual((rep["status"], rep["questions"]), ("blocked", []))

    def test_needs_question_reaches_a_person_and_gaps_become_assumptions(self):
        q = [{"text": "Stripe test key?", "needs": "credential"}]
        res, _, _ = self.run_on("anthropic_http", [json.dumps(good_answer(questions=q))])
        self.assertEqual(res.status, "ready")
        self.assertEqual(res.human_questions(), q)
        self.assertEqual(do_cli.report(res)["assumptions"], [{"text": "discount", "default": "20%"}])

    def test_backend_failure_is_blocked_and_logged(self):
        h = Hosts([json.dumps(good_answer())])
        runner = h.runner("claude_cli", str(self.root))
        runner.command = [sys.executable, "-c", "import sys; sys.exit(1)"]
        ga = self.tmp / "ga-fail"
        res = Intake(runner, backend="claude_cli", model=MODELS["claude_cli"], ga_dir=ga).run("x", self.root)
        self.assertEqual(res.status, "blocked")
        self.assertEqual(len(res.turns), 1)
        self.assertTrue(res.problems[0].startswith("backend: "))
        self.assertEqual(len((ga / "telemetry.jsonl").read_text().splitlines()), 1)


class Ledger(Base):
    def test_rows_equal_turns(self):
        bad = json.dumps(good_answer(acceptance=[{"id": "A1", "check": "fast", "kind": "observable"}]))
        for answers, n in (([json.dumps(good_answer())], 1), ([bad, json.dumps(good_answer())], 2), ([bad], 2)):
            res, _, ga = self.run_on("claude_cli", answers, clock=lambda: 1_790_000_000.0)
            self.assertEqual(len(res.turns), n)
            l0 = [json.loads(x) for x in (ga / "telemetry.jsonl").read_text().splitlines()]
            self.assertEqual(len(l0), n)
            self.assertEqual({e["type"] for e in l0}, {"run.end"})
            self.assertEqual([e["run_id"] for e in l0], [f"{res.task_id}:{i}" for i in range(1, n + 1)])
            self.assertEqual(l0[0]["data"]["reported_input_tokens"], 100)
            self.assertEqual(l0[0]["data"]["model"], MODELS["claude_cli"])
            days = list((ga / "ledger").iterdir())
            self.assertEqual([d.name for d in days], ["2026-09-21.jsonl"])
            rows = [json.loads(x) for x in days[0].read_text().splitlines()]
            self.assertEqual(len(rows), n)
            labels = ["task:check@$.acceptance[0].check", "task:vague@$.acceptance[0].check"]
            if n == 2:
                self.assertEqual(rows[0]["problems"], labels)
            self.assertEqual(rows[-1]["problems"], labels if answers[-1] == bad else [])
            for r in rows:
                for k in ("backend", "model", "input", "output", "cache_read", "cache_creation", "seconds"):
                    self.assertIn(k, r)
                self.assertEqual((r["backend"], r["input"], r["output"], r["cache_read"]),
                                 ("claude_cli", 100, 50, 10))

    def test_progress_while_a_turn_runs(self):
        seen = []
        res, _, _ = self.run_on("claude_cli", [json.dumps(good_answer())], host={"sleep": 0.6},
                                on_progress=seen.append, wait_every_s=0.1)
        kinds = [e["event"] for e in seen]
        self.assertEqual(kinds[0], "started")
        self.assertEqual(kinds[-1], "done")
        self.assertGreaterEqual(kinds.count("waiting"), 2)


# --------------------------------------------------------------------------------------------- D2: the golden set

class Golden(Base):
    def test_golden_set_shape(self):
        cases = golden()
        self.assertGreaterEqual(len(cases), 8)
        ko = [c for c in cases if do_cli.is_korean(c["request"])]
        self.assertGreaterEqual(len(ko) * 2, len(cases))
        reqs = {c["request"] for c in cases}
        for r in ("Electron 데스크톱 셸을 설계하고 구현해줘", "가격 페이지에 연간 요금 토글을 추가해줘",
                  "fix the flaky login test"):
            self.assertIn(r, reqs)

    def test_golden_on_all_five_backends(self):
        for c in golden():
            for b in FIVE:
                res, _, _ = self.run_on(b, c["answers"], request=c["request"])
                msg = (c["name"], b, res.problems)
                self.assertEqual(res.status, c["expect_status"], msg)
                self.assertEqual(len(res.turns), c["expect_turns"], msg)
                if c["expect_status"] == "ready":
                    self.assertEqual(res.spec, c["expected"], msg)
                    self.assertEqual(hard(validate(res.spec)), [])
                else:
                    self.assertEqual(res.problems, c["expect_problems"], msg)


# ------------------------------------------------------------------------------------------------------ S5: ga do

class DoCli(Base):
    def ga(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ga_main.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def config(self, answers):
        h = Hosts(answers)
        cfg = {"schema": "ga-do/1", "backend": "claude_cli", "model": MODELS["claude_cli"],
               "options": {"cli": [sys.executable, str(Path(__file__).parent / "fake_hosts_ga37.py"), "claude",
                                   str(h.dir)]}}
        p = self.tmp / "ga-do.json"
        p.write_text(json.dumps(cfg))
        return p, h

    def test_dry_run_calls_no_model(self):
        p, h = self.config([json.dumps(good_answer())])
        code, out, _ = self.ga("do", "--dry-run", "--repo", str(self.root), "--do-config", str(p), "anything")
        self.assertEqual(code, 0)
        self.assertIn("[languages]", out)
        tail = json.loads(out.strip().splitlines()[-1])
        self.assertLessEqual(tail["summary_tokens"], 2000)
        self.assertEqual(h.calls(), [])

    def test_do_writes_the_spec_and_a_korean_summary(self):
        c = next(c for c in golden() if c["name"] == "pricing_toggle_ko")
        p, h = self.config(c["answers"])
        code, out, err = self.ga("do", "--repo", str(self.root), "--do-config", str(p), c["request"])
        self.assertEqual(code, 0, err)
        d = self.root / ".ga" / "tasks" / task_id(c["request"])
        self.assertEqual(json.loads((d / "task.json").read_text()), c["expected"])
        summary = (d / "summary.md").read_text()
        self.assertIn("수용 기준", summary)
        self.assertIn("토큰: 입력 200", summary)
        rep = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((rep["status"], rep["next"], rep["shape"], rep["turns"]), ("paused", "planner", "report/2", 2))
        self.assertEqual(rep["tokens"]["input"], 200)
        self.assertIn("tokens in 200 out 100", err)
        self.assertIn("turn 1 (intake) on claude_cli", err)

    def test_do_english_summary_and_blocked_exit(self):
        c = next(c for c in golden() if c["name"] == "search_speed_en")
        p, _ = self.config(c["answers"])
        code, out, _ = self.ga("do", "--repo", str(self.root), "--do-config", str(p), c["request"])
        self.assertEqual(code, 1)
        d = self.root / ".ga" / "tasks" / task_id(c["request"])
        self.assertIn("## Problems", (d / "summary.md").read_text())
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["status"], "blocked")

    def test_backend_and_model_choice(self):
        self.assertEqual(do_cli.resolve("codex_cli", "gpt-5.1-codex", {"backend": "agv", "options": {"cli": ["x"]}}),
                         ("codex_cli", "gpt-5.1-codex", {}, "flag"))
        self.assertEqual(do_cli.resolve(None, None, {"backend": "agv", "model": "gpt-5.1"})[:2], ("agv", "gpt-5.1"))
        b, m, _, how = do_cli.resolve(None, None, {})
        self.assertEqual((b, m, how), ("claude_cli", "claude-haiku-4-5-20251001", "router"))
        b, m, opts, how = do_cli.resolve(None, None, {"router": {"backends": ["anthropic_http"]}})
        self.assertEqual((b, m, how), ("anthropic_http", "claude-haiku-4-5-20251001", "router"))

    def test_bad_config_is_exit_2(self):
        p = self.tmp / "bad.json"
        p.write_text(json.dumps({"schema": "ga-do/1", "backend": "nope", "model": "m"}))
        code, _, err = self.ga("do", "--repo", str(self.root), "--do-config", str(p), "x")
        self.assertEqual(code, 2)
        self.assertIn("ga do:", err)


# ------------------------------------------------------------------------- rev 2 (S6-S8, D4): kinds, state, replay

import importlib  # noqa: E402
route_mod = importlib.import_module("ga.intake.route")  # the module, not the route() the package exports
from ga.intake.engine import handle  # noqa: E402
from ga.intake.fragment import WITHHELD, form_of, resolve, withhold  # noqa: E402
from ga.intake.state import State  # noqa: E402

KINDS_FIXTURE = Path(__file__).parent / "fixtures" / "ga37" / "kinds.json"


def kinds():
    return json.loads(KINDS_FIXTURE.read_text(encoding="utf-8"))


def prepared_state(ga_dir: Path, data: dict) -> State:
    st = State(ga_dir, clock=lambda: 1_790_000_000.0)
    st.set_options(data["options"]["task"], data["options"]["items"])
    for w in data["work"]:
        st.add_work(w["id"], w["title"], w["kind"])
    for d in data["decisions"]:
        st.add_decision(d, [], "T-0000aaaa")
    for row in data["ledger"]:
        (ga_dir / "ledger").mkdir(parents=True, exist_ok=True)
        with (ga_dir / "ledger" / "requests-2026-09-21.jsonl").open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return st


def fake_secret() -> str:
    return "sk-" + "ant-" + "A1b2C3d4" * 4  # built at run time: not a real key, but it has the shape of one


class Kinds(Base):
    def handle_on(self, backend, request, answers, state=None, planner=None):
        h = Hosts(answers)
        ga = self.tmp / f"k-{len(list(self.tmp.iterdir()))}"
        st = state if state is not None else prepared_state(ga, kinds()["state"])
        intake = Intake(h.runner(backend, str(self.root)), backend=backend, model=MODELS[backend],
                        ga_dir=st.ga_dir, clock=lambda: 1_790_000_000.0)
        return handle(intake, request, self.root, st, planner), h, st

    def test_fixture_shape(self):
        cases = kinds()["cases"]
        self.assertGreaterEqual(len(cases), 16)
        for k in ("answer", "investigate", "change", "decide"):
            mine = [c for c in cases if c["kind"] == k]
            self.assertGreaterEqual(len(mine), 4, k)
            self.assertTrue(any(do_cli.is_korean(c["request"]) for c in mine), k)
            self.assertTrue(any(not do_cli.is_korean(c["request"]) for c in mine), k)
        reqs = {c["request"] for c in cases}
        for frag in ("1번", "추천대로 해", "어디까지 되었어?"):
            self.assertIn(frag, reqs)

    def test_sixteen_requests_kind_outcome_and_resolution(self):
        for c in kinds()["cases"]:
            calls = []
            res, h, st = self.handle_on("anthropic_http", c["request"], [c["answer"]],
                                        planner=lambda spec: calls.append(spec["id"]) or {"status": "paused",
                                                                                        "next": "planner"})
            msg = (c["request"], res.problems)
            self.assertEqual(res.status, "ready", msg)
            self.assertEqual(res.spec["kind"], c["kind"], msg)
            self.assertEqual(res.outcome["outcome"], c["outcome"], msg)
            self.assertEqual((res.spec.get("resolution") or {}).get("how"), c["resolution"], msg)
            self.assertEqual(res.human_questions(), [], msg)
            self.assertEqual(calls, [res.task_id] if c["kind"] in ("investigate", "change") else [], msg)
            if c["kind"] == "decide":
                self.assertEqual(st.decisions()[-1]["text"], res.spec["decision"]["text"])
                self.assertEqual(st.decisions()[-1]["task"], res.task_id)
            if c["resolution"] == "state":  # the code's reading reached the model
                self.assertIn("Meaning (resolved by code)", h.calls()[0]["prompt"])

    def test_fragments_resolve_from_state(self):
        st = prepared_state(self.tmp / "s", kinds()["state"])
        self.assertIn("Electron", resolve("1번", st).to)
        self.assertIn("Tauri", resolve("2번", st).to)
        self.assertIn("Electron", resolve("추천대로 해", st).to)
        self.assertIn("T-1111bbbb", resolve("시작해", st).to)
        r = resolve("어디까지 되었어?", st)
        self.assertEqual((r.how, r.kind_hint), ("state", "answer"))
        for frag in ("1번", "option 2", "go with option 2", "pick 3", "추천대로 해", "시작해", "go ahead", "어디까지 됐어?", "status?"):
            self.assertIsNotNone(form_of(frag), frag)
        for not_frag in ("가격 페이지에 연간 요금 토글을 추가해줘", "fix the flaky login test", "add 1 test"):
            self.assertIsNone(form_of(not_frag), not_frag)

    def test_unresolved_fragment_is_an_assumption_never_a_question(self):
        empty = State(self.tmp / "empty")
        ans = json.dumps(good_answer())
        for frag in ("2번", "추천대로 해", "시작해"):
            res, _, _ = self.handle_on("claude_cli", frag, [ans], state=State(self.tmp / f"e-{frag}"))
            self.assertEqual(res.status, "ready", res.problems)
            self.assertEqual(res.spec["resolution"]["how"], "assumption")
            self.assertEqual(res.human_questions(), [])
            self.assertEqual(res.spec["questions"], [])
            self.assertTrue(any(frag in a["text"] for a in res.spec["assumptions"]), res.spec["assumptions"])
        st = prepared_state(self.tmp / "p", kinds()["state"])
        r = resolve("7번", st)  # an offer exists but has no option 7: the most recent offer's recommended option
        self.assertEqual(r.how, "assumption")
        self.assertIn("Electron", r.to)
        self.assertIsNone(resolve("1번", empty).kind_hint)

    def test_state_summary_is_in_the_prompt(self):
        c = next(c for c in kinds()["cases"] if c["request"] == "어디까지 되었어?")
        for b in FIVE:
            res, h, _ = self.handle_on(b, c["request"], [c["answer"]])
            p = h.calls()[0]["prompt"]
            for text in ("[state]", "Electron 데스크톱 셸", "T-1111bbbb", "테스트 러너는 vitest 를 쓴다"):
                self.assertIn(text, p, b)
            self.assertLessEqual(res.summary.tokens, CAP)

    def test_answer_never_reaches_the_planner(self):
        route_mod.PLANNER_CALLS.clear()
        for c in [c for c in kinds()["cases"] if c["kind"] == "answer"]:
            res, _, st = self.handle_on("codex_cli", c["request"], [c["answer"]])
            self.assertEqual(res.outcome["outcome"], "answered")
            self.assertIsNone(res.outcome["next"])
            self.assertNotIn(res.task_id, [w["id"] for w in st.work()])
        self.assertEqual(route_mod.PLANNER_CALLS, [])
        c = next(c for c in kinds()["cases"] if c["kind"] == "change")
        res, _, _ = self.handle_on("codex_cli", c["request"], [c["answer"]])
        self.assertEqual(route_mod.PLANNER_CALLS, [res.task_id])

    def test_answer_cites_must_be_found(self):
        bad = json.dumps(dict(json.loads(kinds()["cases"][2]["answer"]),
                              answer={"text": "x", "cites": ["src/does/not/exist.ts"]}))
        res, _, _ = self.handle_on("openai_http", "가격 페이지 파일은 어디 있어?", [bad, bad])
        self.assertEqual((res.status, len(res.turns)), ("blocked", 2))
        self.assertTrue(any("task:cite" in p for p in res.problems))

    def test_decide_writes_a_decision_row_and_checks_affects(self):
        c = next(c for c in kinds()["cases"] if c["request"].startswith("let's drop"))
        res, _, st = self.handle_on("agv", c["request"], [c["answer"]])
        d = st.decisions()[-1]
        self.assertEqual((d["id"], d["affects"], res.outcome["decision"]), ("D2", ["T-1111bbbb"], "D2"))
        self.assertEqual(json.loads((st.dir / "decisions.jsonl").read_text().splitlines()[-1])["text"], d["text"])
        bad = json.dumps(dict(json.loads(c["answer"]), decision={"text": "t", "affects": ["T-9999zzzz"]}))
        res, _, st = self.handle_on("agv", c["request"], [bad, bad])
        self.assertEqual(res.status, "blocked")
        self.assertTrue(any("task:affects" in p for p in res.problems))
        self.assertEqual(len(st.decisions()), 1)  # nothing written for a blocked decision

    def test_kind_rules(self):
        self.assertIn(("task:kind", "hard"), rules(validate(spec_of(kind="answer"))))
        self.assertIn(("task:kind", "hard"), rules(validate(spec_of(answer={"text": "t", "cites": ["x"]}))))
        self.assertIn(("task:kind", "hard"), rules(validate(spec_of(deliverables=[]))))
        self.assertEqual(validate(spec_of(kind="decide", deliverables=[], acceptance=[],
                                          decision={"text": "t", "affects": []})), [])
        self.assertTrue(hard(validate(spec_of(kind="plan"))))
        opts = [{"n": 2, "text": "a"}, {"n": 1, "text": "b"}]
        self.assertIn(("task:options", "hard"), rules(validate(spec_of(options=opts))))

    def test_secret_line_is_withheld_from_every_prompt_and_file(self):
        key = fake_secret()
        req = f"deploy the site\nuse token {key}\nthanks"
        clean, n = withhold(req)
        self.assertEqual(n, 1)
        self.assertNotIn(key, clean)
        self.assertIn(WITHHELD, clean)
        self.assertEqual(withhold("password = hunter2hunter2")[1], 1)
        self.assertEqual(withhold("add a password field to the form")[1], 0)
        (self.root / "README.md").write_text(f"# App\nkey: {key}\n")
        for b in FIVE:
            res, h, st = self.handle_on(b, req, [json.dumps(good_answer())])
            for call in h.calls():
                self.assertNotIn(key, json.dumps(call), b)
            self.assertNotIn(key, json.dumps(res.spec), b)
            row = json.loads(sorted((st.ga_dir / "ledger").glob("requests-*.jsonl"))[-1].read_text().splitlines()[-1])
            self.assertEqual(row["withheld"], 1)
            self.assertNotIn(key, json.dumps(row))


class Replay(DoCli):
    def test_six_requests_six_rows_state_kept(self):
        k = {c["request"]: c["answer"] for c in kinds()["cases"]}
        compare = json.loads(k["compare Electron and Tauri for the desktop shell"])
        status = json.dumps(dict(json.loads(k["어디까지 되었어?"]),
                                 answer={"text": "비교 작업 1건이 열려 있습니다.", "cites": ["open work:"]}))
        reqs = ["compare Electron and Tauri for the desktop shell", "1번", "어디까지 되었어?",
                "we'll use vitest, not jest", f"deploy the docs site\ntoken: {fake_secret()}",
                "is there a login test?"]
        answers = [json.dumps(compare), k["1번"], status, k["we'll use vitest, not jest"],
                   json.dumps(good_answer()), k["is there a login test?"]]
        p, h = self.config(answers)
        f = self.tmp / "requests.txt"
        f.write_text("\n\n".join(reqs) + "\n")
        code, out, err = self.ga("do", "--replay", str(f), "--repo", str(self.root), "--do-config", str(p))
        self.assertEqual(code, 0, err)
        rows = [json.loads(x) for x in next((self.root / ".ga" / "ledger").glob("requests-*.jsonl"))
                .read_text().splitlines()]
        self.assertEqual(len(rows), 6)
        self.assertEqual([r["kind"] for r in rows], ["investigate", "change", "answer", "decide", "change", "answer"])
        self.assertEqual([r["outcome"] for r in rows],
                         ["paused", "paused", "answered", "decided", "paused", "answered"])
        for r in rows:
            for key in ("kind", "tokens", "seconds", "asked_human", "outcome"):
                self.assertIn(key, r)
        self.assertEqual(rows[1]["resolution"], "state")
        calls = h.calls()
        self.assertEqual(len(calls), 6)
        self.assertIn("Electron desktop shell", calls[1]["prompt"])  # '1번' read the options request 1 offered
        self.assertIn("compare Electron and Tauri", calls[2]["prompt"])  # the ledger row of request 1
        self.assertNotIn(fake_secret(), json.dumps(calls))
        self.assertEqual(rows[4]["withheld"], 1)
        st = State(self.root / ".ga")
        self.assertEqual(st.decisions()[-1]["text"], "The test runner is vitest; jest is not used")
        self.assertEqual(len(out.strip().splitlines()), 6)


if __name__ == "__main__":
    unittest.main()
