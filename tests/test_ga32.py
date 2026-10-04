"""CMD-GA32: ga judge names red tests and tells pre-existing failures from new ones (fake repos, no network),
the --template skeleton, and the built-in backend catalog (prices, effort knob, null price sorts last)."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_judge import TESTS, World, sh  # noqa: E402

from ga import judge as J  # noqa: E402
from ga.__main__ import main  # noqa: E402
from ga.backends import ConfigError  # noqa: E402
from ga.backends.catalog import CATALOG  # noqa: E402
from ga.backends.http import ANTHROPIC_HTTP, OPENAI_HTTP  # noqa: E402
from ga.forms import hard, parse_text, validate  # noqa: E402
from ga.net.router import Router  # noqa: E402

RED = '''import unittest

class R(unittest.TestCase):
    def test_old_red(self):
        self.fail("red on the base head")
'''
NEW_RED = RED + '''
    def test_new_red(self):
        self.fail("red only on the report")
'''
OLD = "tests.test_r.R.test_old_red"
NEW = "tests.test_r.R.test_new_red"


class ParseNames(unittest.TestCase):
    def test_unittest_names(self):
        out = ("FAIL: test_a (tests.test_r.R.test_a)\n----\nERROR: test_b (tests.test_r.R.test_b)\n"
               "ERROR: tests.test_x (unittest.loader._FailedTest.tests.test_x)\n"
               "FAIL: test_c (tests.test_r.R.test_c) (i=2)\n")
        self.assertEqual(J.failing_tests(out), ["tests.test_r.R.test_a", "tests.test_r.R.test_b", "tests.test_r.R.test_c",
                                                "unittest.loader._FailedTest.tests.test_x"])

    def test_pytest_names(self):
        out = "FAILED tests/test_a.py::T::test_x - assert 1 == 2\nERROR tests/test_b.py::test_y\n3 failed in 0.1s\n"
        self.assertEqual(J.failing_tests(out), ["tests/test_a.py::T::test_x", "tests/test_b.py::test_y"])

    def test_split(self):
        self.assertEqual(J.split_failures(["a", "b"], ["b"], 2), (["a"], ["b"], 0))
        self.assertEqual(J.split_failures(["a"], [], 3), (["a"], [], 2))  # unnamed failures are never excused


class PreExisting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.w = World(Path(cls.tmp.name))
        cls.w.write("tests/test_r.py", RED)
        cls.base = cls.w.commit("red base")  # main is red before the report
        sh("git", "-C", str(cls.w.repo), "push", "-q", "origin", "HEAD:main")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def judge(self, sha):
        return self.w.judge(sha, mutations=False)

    def test_base_red_test_is_pre_existing_and_does_not_decide_the_class(self):
        sha = self.w.branch_commit("noop", {"VERSION": "1.1"}, frm=self.base)
        j = self.judge(sha)
        self.assertEqual(j.preexisting, [OLD])
        self.assertEqual(j.failing, [])
        self.assertEqual(j.cls, "success", j.notes)
        self.assertTrue(any("pre-existing" in n and OLD in n for n in j.notes), j.notes)
        self.assertEqual(j.tests["o/fakepkg"]["failed"], 1)

    def test_new_red_test_is_named_and_decides_the_class(self):
        sha = self.w.branch_commit("newred", {"tests/test_r.py": NEW_RED}, frm=self.base)
        j = self.judge(sha)
        self.assertEqual(j.failing, [NEW])
        self.assertEqual(j.preexisting, [OLD])
        self.assertEqual((j.cls, j.cause), ("failure", "implementation"))
        self.assertTrue(any("(new)" in n and NEW in n for n in j.notes), j.notes)

    def test_green_base_means_every_failure_is_new(self):
        w = World(Path(self.tmp.name) / "g")
        sha = w.branch_commit("red", {"tests/test_r.py": RED})
        j = w.judge(sha, mutations=False)
        self.assertEqual((j.failing, j.preexisting, j.cls), ([OLD], [], "failure"))


class Template(unittest.TestCase):
    def test_template_is_a_valid_report2_head(self):
        text = J.template("CMD-GA32")
        head, _ = parse_text(text)
        self.assertEqual(hard(validate(head)), [])
        self.assertEqual((head["schema"], head["handled"][0]["id"]), ("report/2", "CMD-GA32"))

    def test_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(["judge", "--template", "CMD-X1"])
        self.assertEqual(rc, 0)
        self.assertIn('"id":"CMD-X1"', out.getvalue())
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(main(["judge", "--template", "bad id"]), 2)
            self.assertEqual(main(["judge"]), 2)


class Catalog(unittest.TestCase):
    def test_claude_prices_and_effort_knob(self):
        by = {e["model"]: e for e in CATALOG["anthropic_http"]}
        self.assertEqual(by["claude-sonnet-5-5"]["price"], {"in": 2.0, "out": 10.0})
        self.assertEqual(by["claude-opus-5-5"]["price"], {"in": 4.0, "out": 20.0})
        self.assertEqual(by["claude-opus-5-5"]["effort_option"], "effort")
        self.assertIsNone(by["claude-haiku-4-5-20251001"]["effort_option"])  # the API rejects effort on Haiku 4.5
        for e in CATALOG["claude_cli"]:
            self.assertIsNone(e["effort_option"])  # the CLI plugin has no effort option

    def test_every_declared_knob_is_an_option_the_plugin_accepts(self):
        for e in CATALOG["anthropic_http"]:
            for eff in (v for v in e["tiers"].values() if v):
                ANTHROPIC_HTTP.create(e["model"], {e["effort_option"]: eff}, {})
        with self.assertRaises(ConfigError):
            OPENAI_HTTP.create("gpt-5.1", {"effort": "high"}, {})
        with self.assertRaises(ConfigError):
            ANTHROPIC_HTTP.create("claude-opus-5-5", {"effort": "turbo"}, {})

    def test_effort_reaches_the_request(self):
        r = ANTHROPIC_HTTP.create("claude-opus-5-5", {"effort": "high"}, {})
        self.assertEqual(r.request("p", "s")[2]["output_config"], {"effort": "high"})
        self.assertNotIn("output_config", ANTHROPIC_HTTP.create("claude-opus-5-5", {}, {}).request("p", "s")[2])

    def test_null_price_is_the_most_expensive(self):
        def e(model, price):
            return {"model": model, "family": "f", "capabilities": ["text"], "context_window": 10, "effort_option": None,
                    "tiers": {"R0": None}, "price": price, "quota_family": "f", "backend": "b"}
        r = Router([e("unknown", None), e("dear", {"in": 100.0, "out": 500.0})])
        self.assertEqual(r.pick("c", {"capabilities": ["text"]}).model, "dear")
        for entries in CATALOG.values():  # null stays null where nothing documented is declared
            for x in entries:
                self.assertTrue(x["price"] is None or set(x["price"]) == {"in", "out"})


if __name__ == "__main__":
    unittest.main()
