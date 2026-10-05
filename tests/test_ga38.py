"""CMD-GA38: ga act — action format, state card, named commands, loop policy, backends, pool executor: act.
Scripted fake models on temporary fixture repos (Python and a small TS one): 0 network, 0 model calls."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from ga import backends  # noqa: E402
from ga.act import card as C  # noqa: E402
from ga.act import commands as K  # noqa: E402
from ga.act import fmt as F  # noqa: E402
from ga.act import loop as A  # noqa: E402
from ga.act import retrieve as R  # noqa: E402
from ga.act.apply import apply  # noqa: E402
from ga.backends.base import BackendTurn  # noqa: E402
from ga.backends import http as H  # noqa: E402
from ga.ctxpack import tokens  # noqa: E402

CALC = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
TEST_CALC = ("import unittest\nfrom calc import add, mul\n\n\nclass T(unittest.TestCase):\n"
             "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
             "    def test_mul(self):\n        self.assertEqual(mul(2, 3), 6)\n")
LINT = "import sys\nfor i in range(12):\n    print('lint: calc.py:%d: line too long (%d > 79 characters)' % (i + 1, 80 + i))\nsys.exit(1)\n"
ACT_CFG = {"commands": {"test": ["{python}", "-m", "unittest", "-v"], "lint": ["{python}", "lint.py"],
                        "env": ["{python}", "-c", "import os; print(sorted(os.environ))"]},
           "done_when": "test", "timeout_s": 60}
FIX = "EDIT calc.py\n<<<<<<< SEARCH\n    return a - b\n=======\n    return a + b\n>>>>>>> REPLACE\n"


class Fake:
    """A scripted model: answers[i] (or a function of the card) for turn i; records what it was sent."""

    def __init__(self, answers, bare=True, usage=True):
        self.answers, self.bare, self.usage = list(answers), bare, usage
        self.calls = []

    def run_turn(self, prompt, session_id=None, *, system=None, on_wait=None, wait_every_s=None):
        self.calls.append({"prompt": prompt, "system": system})
        a = self.answers[min(len(self.calls), len(self.answers)) - 1]
        text = a(prompt, system) if callable(a) else a
        u = ({"input_tokens": tokens(prompt) + tokens(system or ""), "output_tokens": tokens(text),
              "cache_read_input_tokens": tokens(system or ""), "cache_creation_input_tokens": 0} if self.usage else None)
        return BackendTurn(text, ["fake-model"], u, "anthropic" if u else None, None, 0.01, 1)


def py_repo(case, cfg=ACT_CFG):
    d = Path(tempfile.mkdtemp(prefix="ga38-py-"))
    case.addCleanup(shutil.rmtree, d, True)
    (d / "calc.py").write_text(CALC)
    (d / "test_calc.py").write_text(TEST_CALC)
    (d / "lint.py").write_text(LINT)
    (d / ".ga-act.json").write_text(json.dumps(cfg))
    return d


def run(case, root, answers, files=("calc.py",), **kw):
    fake = kw.pop("fake", None) or Fake(answers)
    state = root.parent / (root.name + "-state")
    case.addCleanup(shutil.rmtree, state, True)
    item = {"id": "CMD-T1", "goal": "make the tests in test_calc.py pass", "files": list(files)}
    item.update(kw.pop("item", {}))
    ex = {}

    class Spy(A.Act):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            ex["act"] = self
    with mock.patch.object(A, "Act", Spy):
        res = A.run_item(root, item, backend="fake", model="fake-model", runner=fake, state_dir=state, **kw)
    return res, fake, ex["act"], state


def bodies(fake):
    return [c["prompt"] for c in fake.calls]


class Format(unittest.TestCase):
    def test_parse_every_action(self):
        p = F.parse("```\n" + FIX + "<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n"
                    "NEW pkg/new.py\n<<<<<<< CONTENT\nA = 1\n>>>>>>> END\nRUN test\nNEED symbol calc.add\n"
                    "NEED file calc.py lines 1-2\nNEED grep return\nchatter\nDONE\nBLOCKED needs a key\n```\n")
        kinds = [a.kind for a in p.actions]
        self.assertEqual(kinds, ["EDIT", "NEW", "RUN", "NEED", "NEED", "NEED", "DONE", "BLOCKED"])
        self.assertEqual(len(p.actions[0].blocks), 2)
        self.assertEqual(p.actions[0].blocks[0].search, "    return a - b\n")
        self.assertEqual(p.actions[1].content, "A = 1\n")
        self.assertEqual(p.actions[4].lines, (1, 2))
        self.assertEqual(p.actions[7].arg, "needs a key")
        self.assertEqual(p.noise, 1)

    def test_format_problems(self):
        p = F.parse("RUN python -c 'x'\nEDIT a.py\nNEW b.py\nhello\nNEED thing x\n")
        self.assertEqual(p.actions, [])
        self.assertEqual(len(p.problems), 4)

    def test_spec_is_about_300_tokens_and_has_no_command_names(self):
        self.assertTrue(200 <= tokens(F.SPEC) <= 400, tokens(F.SPEC))
        self.assertNotIn("lint", F.SPEC)


class Edits(unittest.TestCase):
    def test_exact_unique_search_applies(self):
        root = py_repo(self)
        out = apply(root, F.parse(FIX).actions, ["calc.py"])
        self.assertEqual(out.applied, ["EDIT calc.py block 1"])
        self.assertIn("return a + b", (root / "calc.py").read_text())

    def test_non_matching_and_ambiguous_and_whitespace_search_are_rejected(self):
        root = py_repo(self)
        for search, why in (("    return a -  b\n", "no exact match"), ("    return a - b \n", "no exact match"),
                            ("  RETURN A - B\n", "no exact match")):
            ans = f"EDIT calc.py\n<<<<<<< SEARCH\n{search}=======\n    return 0\n>>>>>>> REPLACE\n"
            out = apply(root, F.parse(ans).actions, ["calc.py"])
            self.assertEqual(out.applied, [], search)
            self.assertIn(why, out.rejected[0])
            self.assertEqual((root / "calc.py").read_text(), CALC)
        self.assertIn("    2|     return a - b", out.nearest[0])
        (root / "calc.py").write_text(CALC + "\n\ndef sub(a, b):\n    return a - b\n")
        out = apply(root, F.parse(FIX).actions, ["calc.py"])
        self.assertEqual(out.applied, [])
        self.assertIn("2 matches (make SEARCH unique)", out.rejected[0])

    def test_only_the_bad_block_is_rejected(self):
        root = py_repo(self)
        ans = FIX + "<<<<<<< SEARCH\nnot there\n=======\nx\n>>>>>>> REPLACE\n"
        out = apply(root, F.parse(ans).actions, ["calc.py"])
        self.assertEqual(len(out.applied), 1)
        self.assertEqual(len(out.rejected), 1)
        self.assertIn("return a + b", (root / "calc.py").read_text())

    def test_outside_files_and_outside_repo_are_rejected(self):
        root = py_repo(self)
        for path in ("test_calc.py", "../x.py", "/etc/passwd", ".git/config", ".ga/x"):
            ans = f"EDIT {path}\n<<<<<<< SEARCH\nimport unittest\n=======\nimport os\n>>>>>>> REPLACE\n" \
                  f"NEW {path}2\n<<<<<<< CONTENT\nx\n>>>>>>> END\n"
            out = apply(root, F.parse(ans).actions, ["calc.py"])
            self.assertEqual(out.applied, [], path)
            self.assertEqual(len(out.rejected), 2)
        self.assertEqual((root / "test_calc.py").read_text(), TEST_CALC)
        self.assertFalse((root / "test_calc.py2").exists())

    def test_new_only_for_missing_files(self):
        root = py_repo(self)
        out = apply(root, F.parse("NEW calc.py\n<<<<<<< CONTENT\nx\n>>>>>>> END\n"
                                  "NEW lib/x.py\n<<<<<<< CONTENT\nX = 1\n>>>>>>> END\n").actions, ["calc.py", "lib/**"])
        self.assertIn("exists", out.rejected[0])
        self.assertEqual((root / "lib" / "x.py").read_text(), "X = 1\n")


class Commands(unittest.TestCase):
    def test_config_is_checked(self):
        root = py_repo(self)
        for bad in ({"commands": {"Bad Name": ["x"]}}, {"commands": {"t": "make test"}}, {"commands": {"t": []}},
                    {"commands": {}, "done_when": "test"}, {"other": 1}):
            (root / ".ga-act.json").write_text(json.dumps(bad))
            with self.assertRaises(K.ActConfigError):
                K.load(root)

    def test_argv_runs_without_a_shell(self):
        root = py_repo(self)
        r = K.run("x", ["{python}", "-c", "import sys; print(sys.argv[1])", "$HOME;echo pwned"], root, 30)
        self.assertEqual(r.out.strip(), "$HOME;echo pwned")
        self.assertTrue(r.ok)

    def test_secret_named_env_is_stripped(self):
        root = py_repo(self)
        with mock.patch.dict(os.environ, {"GA38_API_KEY": "fake-not-a-key", "GA38_TOKEN": "fake", "GA38_PLAIN": "1"}):
            r = K.run("env", ACT_CFG["commands"]["env"], root, 30)
        self.assertIn("GA38_PLAIN", r.out)
        self.assertNotIn("GA38_API_KEY", r.out)
        self.assertNotIn("GA38_TOKEN", r.out)

    def test_timeout_and_missing_program(self):
        root = py_repo(self)
        self.assertIn("timeout", K.run("s", ["{python}", "-c", "import time; time.sleep(5)"], root, 0.5).note)
        self.assertIn("could not start", K.run("m", ["no-such-program-ga38"], root, 5).note)

    def test_summary_names_failing_tests_and_key_frames(self):
        root = py_repo(self)
        r = K.run("test", ACT_CFG["commands"]["test"], root, 60)
        s = K.summarize(r, root)
        self.assertIn("test_calc.T.test_add", s)
        self.assertIn("AssertionError", s)
        self.assertIn("test_calc.py", s)
        self.assertNotIn("test_mul", s)


class Retrieve(unittest.TestCase):
    def test_python_symbols(self):
        root = py_repo(self)
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("")
        (root / "pkg" / "mod.py").write_text("class Box:\n    def get(self):\n        return 1\n\n\ndef f():\n    pass\n")
        self.assertIn("1| def add", R.symbol(root, "calc.add"))
        self.assertIn("pkg/mod.py:2-3 Box.get", R.symbol(root, "pkg.mod.Box.get"))
        self.assertIn("Box.get", R.symbol(root, "Box.get"))
        self.assertIsNone(R.symbol(root, "nothing.here"))
        self.assertIn("calc.py:1-2 add", R.enclosing(root, "calc.py", 2))

    def test_ts_symbols_by_regex_index(self):
        root = ts_repo(self)
        s = R.symbol(root, "sum")
        self.assertTrue(s.startswith("src/sum.ts:1-3 sum"), s)
        m = R.symbol(root, "Calc.mul")
        self.assertIn("Calc.mul", m)
        self.assertIn("return a * b", m)
        self.assertIn("double", R.symbol(root, "src.sum.double"))

    def test_file_slice_and_grep_stay_inside(self):
        root = py_repo(self)
        self.assertIn("2|     return a - b", R.file_slice(root, "calc.py", (2, 2)))
        self.assertIsNone(R.file_slice(root, "../etc/passwd"))
        self.assertIn("calc.py:2:", R.grep(root, "return a - b"))


class Card(unittest.TestCase):
    def test_cap_is_enforced_by_dropping_then_cutting(self):
        pre = C.prefix("CMD-X1", "goal", ["true"], ["a.py"], ["a.py"], {"test": ["x"]})
        units = [C.Unit("failing", "f" * 4000, 1), C.Unit("code", "c" * 20000, 3), C.Unit("last", "l" * 3000, 2),
                 C.Unit("budget", "turn 1", 0)]
        cd = C.build(pre, units, 1500)
        self.assertLessEqual(cd.tokens, 1500)
        self.assertTrue(cd.dropped)
        self.assertIn("turn 1", cd.body)
        with self.assertRaises(C.CardError):
            C.build(pre, units, 50)


def ts_repo(case):
    d = Path(tempfile.mkdtemp(prefix="ga38-ts-"))
    case.addCleanup(shutil.rmtree, d, True)
    (d / "src").mkdir()
    (d / "test").mkdir()
    (d / "src" / "sum.ts").write_text(
        "export function sum(a: number, b: number): number {\n  return a - b;\n}\n\n"
        "export const double = (x: number): number => x * 2;\n\n"
        "export class Calc {\n  mul(a: number, b: number): number {\n    return a * b;\n  }\n}\n")
    (d / "test" / "sum.test.ts").write_text(
        "import { test } from 'node:test';\nimport assert from 'node:assert';\nimport { sum, Calc } from '../src/sum.ts';\n"
        "test('sum', () => { assert.strictEqual(sum(2, 3), 5); });\n"
        "test('mul', () => { assert.strictEqual(new Calc().mul(2, 3), 6); });\n")
    (d / "package.json").write_text('{"type": "module"}\n')
    (d / ".ga-act.json").write_text(json.dumps(
        {"commands": {"test": ["node", "--experimental-strip-types", "--no-warnings", "--test", "test/sum.test.ts"]},
         "done_when": "test"}))
    return d


class Loop(unittest.TestCase):
    def test_seeded_bug_fixed_through_need_edit_and_run_ends_done_when_green(self):
        root = py_repo(self)
        res, fake, act, state = run(self, root, ["NEED symbol calc.add\nRUN test\n", FIX + "RUN test\n"])
        self.assertEqual((res.status, res.turns), ("done", 2), res.reason)
        self.assertEqual(res.reason, "done_when passes")
        self.assertIn("return a + b", (root / "calc.py").read_text())
        c1, c2 = bodies(fake)
        self.assertIn("test_calc.T.test_add", c1)  # the failing set before turn 1, measured by code
        self.assertIn("calc.py:1-2 add", c2)      # NEED symbol served into the next card
        self.assertIn("$ test: exit 1", c2)       # RUN test's result
        self.assertEqual(res.changed, ["calc.py"])

    def test_done_only_when_done_when_passes(self):
        root = py_repo(self)
        res, fake, act, _ = run(self, root, ["DONE\n", "DONE\n", "DONE\n"], max_turns=3)
        self.assertEqual(res.status, "blocked")
        self.assertIn("turn cap", res.reason)
        self.assertIn("DONE not accepted", bodies(fake)[1])
        # after a fix the same DONE is accepted (the fix alone already turns it green)
        root2 = py_repo(self)
        res2, _, _, _ = run(self, root2, [FIX + "DONE\n"])
        self.assertEqual(res2.status, "done")

    def test_done_accepted_when_green_without_an_edit_this_turn(self):
        root = py_repo(self)
        (root / "calc.py").write_text(CALC.replace("a - b", "a + b"))
        (root / "test_calc.py").write_text(TEST_CALC.replace("6)", "7)"))
        res, _, _, _ = run(self, root, ["EDIT test_calc.py\n<<<<<<< SEARCH\n(2, 3), 7)\n=======\n(2, 3), 6)\n>>>>>>> REPLACE\n"],
                           files=["test_calc.py"])
        self.assertEqual(res.status, "done")

    def test_rejected_search_shows_nearest_lines_next_card(self):
        root = py_repo(self)
        bad = "EDIT calc.py\n<<<<<<< SEARCH\n    return a-b\n=======\n    return a + b\n>>>>>>> REPLACE\n"
        res, fake, _, _ = run(self, root, [bad, FIX])
        self.assertEqual(res.status, "done")
        c2 = bodies(fake)[1]
        self.assertIn("REJECTED EDIT calc.py block 1: no exact match", c2)
        self.assertIn("    2|     return a - b", c2)

    def test_edit_outside_files_and_unknown_run_are_rejected_in_the_loop(self):
        root = py_repo(self)
        calls = []
        real = K.run
        with mock.patch.object(K, "run", side_effect=lambda *a, **k: calls.append(a[0]) or real(*a, **k)):
            res, fake, _, _ = run(self, root, [
                "EDIT test_calc.py\n<<<<<<< SEARCH\n(2, 3), 5)\n=======\n(2, 3), -1)\n>>>>>>> REPLACE\nRUN rm\nRUN deploy\n",
                FIX])
        self.assertEqual(res.status, "done")
        self.assertEqual((root / "test_calc.py").read_text(), TEST_CALC)
        c2 = bodies(fake)[1]
        self.assertIn("REJECTED EDIT test_calc.py: not in the item's files", c2)
        self.assertIn("RUN rm: rejected, not a listed command name", c2)
        self.assertNotIn("rm", calls)
        self.assertNotIn("deploy", calls)

    def test_card_size_is_flat_over_8_turns_and_the_prefix_is_byte_identical(self):
        root = py_repo(self)
        res, fake, act, state = run(self, root, ["RUN lint\nNEED symbol calc.mul\n"], max_turns=8)
        self.assertEqual((res.status, res.turns), ("blocked", 8))
        sizes = [c.tokens for c in act.cards]
        self.assertEqual(len(sizes), 8)
        later = sizes[1:]  # turn 1 has no last-turn part yet
        lo, hi = min(later), max(later)
        self.assertLessEqual(hi, lo * 1.10, sizes)
        self.assertLessEqual(max(sizes), A.C.DEFAULT_CAP)
        systems = {c["system"] for c in fake.calls}
        self.assertEqual(len(systems), 1)  # the stable prefix: the same bytes every turn
        self.assertTrue(next(iter(systems)).startswith(F.SPEC))
        self.assertEqual(sum(1 for c in bodies(fake)[-1].splitlines() if c.startswith("$ lint")), 1)

    def test_not_bare_runner_gets_prefix_first_in_one_prompt(self):
        root = py_repo(self)
        fake = Fake(["RUN lint\n"], bare=False)
        res, _, act, _ = run(self, root, None, fake=fake, max_turns=3)
        self.assertTrue(all(c["system"] is None and c["prompt"].startswith(act.prefix) for c in fake.calls))
        self.assertEqual(len({c["prompt"][:len(act.prefix)] for c in fake.calls}), 1)

    def test_same_failing_set_after_two_edit_turns_stops_blocked(self):
        root = py_repo(self)
        n = [0]

        def churn(prompt, system):
            n[0] += 1
            return (f"EDIT calc.py\n<<<<<<< SEARCH\ndef mul(a, b):\n=======\n# touched {n[0]}\ndef mul(a, b):\n"
                    ">>>>>>> REPLACE\n")
        res, fake, _, _ = run(self, root, [churn], max_turns=10)
        self.assertEqual(res.status, "blocked")
        self.assertIn("no progress", res.reason)
        self.assertEqual(res.turns, 2)
        self.assertEqual(res.failing, ["test_calc.T.test_add"])

    def test_progress_resets_the_stall_count(self):
        root = py_repo(self)
        (root / "calc.py").write_text(CALC.replace("a * b", "a ** b"))  # two failing tests
        churn = "EDIT calc.py\n<<<<<<< SEARCH\ndef mul(a, b):\n=======\n# x\ndef mul(a, b):\n>>>>>>> REPLACE\n"
        mulfix = "EDIT calc.py\n<<<<<<< SEARCH\n    return a ** b\n=======\n    return a * b\n>>>>>>> REPLACE\n"
        res, _, _, _ = run(self, root, [churn, mulfix, FIX], max_turns=10)
        self.assertEqual((res.status, res.turns), ("done", 3), res.reason)

    def test_model_blocked_and_token_cap(self):
        root = py_repo(self)
        res, _, _, _ = run(self, root, ["BLOCKED needs a database\n"])
        self.assertEqual((res.status, res.reason), ("blocked", "model: needs a database"))
        res, _, _, _ = run(self, py_repo(self), ["RUN lint\n"], max_tokens=1500)
        self.assertEqual(res.status, "blocked")
        self.assertIn("token cap", res.reason)

    def test_big_need_respects_the_card_cap(self):
        root = py_repo(self)
        (root / "big.py").write_text("".join(f"X{i} = '{'y' * 60}'\n" for i in range(3000)))
        res, fake, act, _ = run(self, root, ["NEED file big.py\nNEED file big.py lines 1000-2400\nNEED grep y\n"],
                                max_turns=3, cap=2500)
        self.assertTrue(all(c.tokens <= 2500 for c in act.cards), [c.tokens for c in act.cards])
        self.assertTrue(act.cards[1].dropped or "(cut" in act.cards[1].body)

    def test_ledger_rows_equal_turns_and_l0(self):
        root = py_repo(self)
        res, fake, act, state = run(self, root, ["RUN lint\n", "NEED symbol calc.add\n", FIX])
        self.assertEqual((res.status, res.turns), ("done", 3))
        days = list((state / "ledger").glob("*.jsonl"))
        self.assertEqual(len(days), 1)
        rows = [json.loads(x) for x in days[0].read_text().splitlines()]
        self.assertEqual(len(rows), res.turns)
        self.assertEqual([r["turn"] for r in rows], [1, 2, 3])
        for r in rows:
            for k in ("backend", "model", "input", "output", "cache_read", "cache_creation", "seconds", "applied",
                      "rejected", "commands", "card_tokens"):
                self.assertIn(k, r)
            self.assertIsInstance(r["input"], int)
        self.assertEqual(rows[0]["commands"], ["lint"])
        self.assertEqual(rows[2]["applied"], 1)
        l0 = [json.loads(x) for x in (state / "telemetry.jsonl").read_text().splitlines()]
        self.assertEqual([e["type"] for e in l0], ["run.end"] * res.turns)
        self.assertEqual(l0[0]["data"]["reported_input_tokens"], rows[0]["input"])
        self.assertEqual(l0[0]["source"], "ga_act")
        self.assertEqual(res.tokens["total"], sum(r["input"] + r["output"] + r["cache_read"] + r["cache_creation"]
                                                  for r in rows))

    def test_unreported_usage_is_null_in_the_ledger_and_estimated(self):
        root = py_repo(self)
        res, _, _, state = run(self, root, [FIX], fake=Fake([FIX], usage=False))
        rows = [json.loads(x) for x in next((state / "ledger").glob("*.jsonl")).read_text().splitlines()]
        self.assertIsNone(rows[0]["input"])
        self.assertFalse(rows[0]["usage_reported"])
        self.assertGreater(res.tokens["estimated"], 0)

    def test_already_green_is_done_with_zero_turns(self):
        root = py_repo(self)
        (root / "calc.py").write_text(CALC.replace("a - b", "a + b"))
        res, fake, _, _ = run(self, root, ["DONE"])
        self.assertEqual((res.status, res.turns, fake.calls), ("done", 0, []))

    def test_backend_error_stops_blocked_with_a_ledger_row(self):
        from ga.backends.base import BackendError

        class Broken(Fake):
            def run_turn(self, *a, **k):
                raise BackendError("timeout")
        root = py_repo(self)
        res, _, _, state = run(self, root, None, fake=Broken([]))
        self.assertEqual((res.status, res.reason, res.turns), ("blocked", "backend:timeout", 1))
        self.assertEqual(len(next((state / "ledger").glob("*.jsonl")).read_text().splitlines()), 1)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_ts_fixture_seeded_bug_fixed(self):
        root = ts_repo(self)
        fix = "EDIT src/sum.ts\n<<<<<<< SEARCH\n  return a - b;\n=======\n  return a + b;\n>>>>>>> REPLACE\nRUN test\n"
        res, fake, _, _ = run(self, root, ["NEED symbol sum\nRUN test\n", fix], files=["src/**"])
        self.assertEqual((res.status, res.turns), ("done", 2), res.reason)
        self.assertIn("src/sum.ts:1-3 sum", bodies(fake)[1])


class Backends(unittest.TestCase):
    def test_all_five_builtins_make_a_runner_through_one_interface(self):
        for name, model, opts in (("claude_cli", "claude-haiku-4-5", {}), ("agv", "gemini-3.8-flash-high", {}),
                                  ("codex_cli", "gpt-5", {}), ("openai_http", "gpt-oss-120b", {"base_url": "http://127.0.0.1:9"}),
                                  ("anthropic_http", "claude-haiku-4-5", {"key_env": "GA38_FAKE_KEY_ENV"})):
            r = backends.create(name, model, opts, {"cwd": ".", "timeout_s": 5})
            self.assertTrue(hasattr(r, "run_turn"), name)

    def test_claude_cli_is_bare_with_tools_off(self):
        r = backends.create("claude_cli", "claude-haiku-4-5", {}, {"cwd": ".", "timeout_s": 5})
        self.assertTrue(r.bare)
        a = r.argv("card body", "stable prefix")
        self.assertEqual(a[a.index("--tools") + 1], "")
        self.assertEqual(a[a.index("--system-prompt") + 1], "stable prefix")
        self.assertNotIn("--dangerously-skip-permissions", a)
        self.assertNotIn("bypassPermissions", a)

    def _http(self, name, model, opts, answer_json, env=None):
        sent = []

        def transport(url, headers, body, timeout):
            sent.append((url, headers, json.loads(body)))
            return 200, {}, json.dumps(answer_json).encode()
        root = py_repo(self)
        with mock.patch.dict(os.environ, env or {}):
            r = backends.create(name, model, opts, {"cwd": str(root), "timeout_s": 5, "transport": transport})
            res, _, _, state = run(self, root, None, fake=r, max_turns=1)
        return sent, res, state

    def test_anthropic_http_marks_the_stable_prefix_for_caching(self):
        ans = {"model": "claude-haiku-4-5", "content": [{"type": "text", "text": FIX}],
               "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 900,
                         "cache_creation_input_tokens": 0}}
        sent, res, state = self._http("anthropic_http", "claude-haiku-4-5", {"key_env": "GA38_FAKE_KEY_ENV"}, ans,
                                      {"GA38_FAKE_KEY_ENV": "fake-not-a-key"})
        body = sent[0][2]
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertTrue(body["system"][0]["text"].startswith(F.SPEC))
        self.assertNotIn("tools", body)
        self.assertEqual(res.status, "done")
        self.assertEqual(res.tokens["cache_read"], 900)

    def test_openai_http_local_server_without_a_key(self):
        ans = {"model": "local-model", "choices": [{"message": {"content": FIX}}],
               "usage": {"prompt_tokens": 50, "completion_tokens": 9}}
        sent, res, _ = self._http("openai_http", "local-model", {"base_url": "http://127.0.0.1:8080/v1"}, ans)
        url, headers, body = sent[0]
        self.assertEqual(url, "http://127.0.0.1:8080/v1/chat/completions")
        self.assertNotIn("authorization", headers)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(res.status, "done")

    def test_plain_anthropic_http_unchanged_without_act(self):
        r = H.AnthropicRunner("claude-haiku-4-5", "https://x", None)
        _, _, body = r.request("p", "s")
        self.assertEqual(body["system"], "s")

    def test_agv_runs_as_is_with_its_overhead_recorded(self):
        self.assertFalse(backends.get("agv").overhead["bare"])
        root = py_repo(self)
        fake = Fake([FIX], bare=False)
        res, _, act, state = run(self, root, None, fake=fake)
        self.assertEqual(res.status, "done")
        self.assertTrue(fake.calls[0]["prompt"].startswith(F.SPEC))


class Cli(unittest.TestCase):
    def test_ga_act_cli_config_error_exit_2(self):
        from ga import __main__ as cli
        root = py_repo(self)
        item = root / "item.json"
        item.write_text(json.dumps({"id": "CMD-T1", "goal": "x"}))
        import io
        from contextlib import redirect_stderr
        e = io.StringIO()
        with redirect_stderr(e):
            code = cli.main(["act", "--item", str(item), "--repo", str(root), "--backend", "claude_cli",
                             "--model", "claude-haiku-4-5"])
        self.assertEqual(code, 2)
        self.assertIn("files", e.getvalue())

    def test_ga_act_cli_runs_an_item_with_a_plugin_backend(self):
        from ga import __main__ as cli
        root = py_repo(self)
        item = root.parent / (root.name + "-item.json")
        self.addCleanup(item.unlink)
        item.write_text(json.dumps({"id": "CMD-T1", "goal": "fix add", "files": ["calc.py"]}))
        fake = Fake([FIX])

        class Plugin:
            def create(self, model, options, ctx):
                return fake
        import io
        from contextlib import redirect_stdout
        o = io.StringIO()
        with mock.patch.object(backends, "get", lambda name: Plugin()), redirect_stdout(o):
            code = cli.main(["act", "--item", str(item), "--repo", str(root), "--backend", "x", "--model", "fake-model",
                             "--state", str(root.parent / (root.name + "-state"))])
        self.addCleanup(shutil.rmtree, root.parent / (root.name + "-state"), True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(o.getvalue())["status"], "done")


# ---------------------------------------------------------------------------------------------- pool executor: act
from test_ga33 import network  # noqa: E402
from test_ga34 import RepoCase, JUDGE_CFG, sh  # noqa: E402
from ga.net import pool as P  # noqa: E402


class PoolAct(RepoCase):
    def act_world(self, answers, **role):
        roles = {"writer": {"backends": ["fake_text"], "pack_max_tokens": 3000, "executor": "act", **role}}
        w = self.repo_world(roles=roles)
        (self.app / "calc.py").write_text(CALC)
        (self.app / "test_calc.py").write_text(TEST_CALC)
        (self.app / ".ga-act.json").write_text(json.dumps({"commands": {"test": ["{python}", "-m", "unittest", "-v"]},
                                                           "done_when": "test"}))
        sh(self.app, "add", "-A")
        sh(self.app, "commit", "-q", "-m", "seeded bug")
        fb = w.backends["fake_text"]
        fb.script = lambda prompt, system, model, options: answers[min(len(fb.calls), len(answers)) - 1]
        return w

    def test_config_problems(self):
        base = {"backends": ["fake_text"]}
        for role, pool_extra, word in (({**base, "executor": "loop"}, {"repo": {"path": "app"}}, "turn or act"),
                                       ({**base, "executor": "act", "tools": {"allow": ["Read"]}}, {"repo": {"path": "app"}}, "tools"),
                                       ({**base, "executor": "act"}, {}, "repo"),
                                       ({**base, "act": {"max_turns": 0}}, {}, "max_turns")):
            net = network(roles={"writer": role}, **pool_extra)
            msgs = [p.message for p in P.problems_of(net)]
            self.assertTrue(any(word in m for m in msgs), (word, msgs))
        self.assertEqual([p.message for p in P.problems_of(network(roles={"writer": {**base, "executor": "act"}},
                                                                    repo={"path": "app"}))], [])
        self.assertIn("done_when", " ".join(P.item_problems({"id": "CMD-A1", "role": "writer", "goal": "g",
                                                              "done_when": 3}, {"writer": {}})))

    def test_node_runs_ga_act_in_its_worktree_and_the_fix_is_integrated(self):
        self.act_world(["NEED symbol calc.add\n", FIX])
        old = self.head()
        self.add({"id": "CMD-A1", "role": "writer", "goal": "make test_calc pass", "files": ["calc.py"],
                  "done_when": "test"})
        self.run_until_idle()
        self.assertEqual(self.pool().status()["done"], ["CMD-A1"])
        new = self.head()
        self.assertNotEqual(new, old)
        self.assertIn("return a + b", (self.app / "calc.py").read_text())
        calls = self.w.backends["fake_text"].calls
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["system"].startswith(F.SPEC))
        self.assertIsNone(self.seen[0].get("tools"))
        self.assertIn("worktrees", self.seen[0]["cwd"])
        led = list((self.w.ga / "nodes" / "_retired" / "writer_1" / "act" / "ledger").glob("*.jsonl"))
        self.assertEqual(len(led[0].read_text().splitlines()), 2)
        self.assertEqual(sorted(sh(self.app, "diff", "--name-only", old, new).split()), ["calc.py"])

    def test_blocked_item_fails_and_is_not_integrated(self):
        self.act_world(["BLOCKED cannot\n"])
        old = self.head()
        self.add({"id": "CMD-A1", "role": "writer", "goal": "make test_calc pass", "files": ["calc.py"]})
        self.run_until_idle()
        self.assertEqual(self.pool().status()["failed"], ["CMD-A1"])
        self.assertEqual(self.head(), old)


if __name__ == "__main__":
    unittest.main()
