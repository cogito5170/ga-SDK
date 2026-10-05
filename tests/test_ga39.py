"""CMD-GA39: the GA Verifier. ga verify mutations (replayed on the real history), ga judge --sha / --mutations auto /
path-form test names, ga verify item. No network but the git fetch of this repo's own history; no model run."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ga import judge as J  # noqa: E402
from ga.__main__ import main  # noqa: E402
from ga.forms import parse_text  # noqa: E402
from ga.verify import items as I  # noqa: E402
from ga.verify import mutate as M  # noqa: E402
from test_judge import World, report, sh  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# (base, head) of the three directives whose survivors baseline found by hand
CON1 = ("718a2cd60a0699d264fbc300c5ed64a4c8a975d4", "0980f049e4cf3769c9bea58db12c0afeb89960c0")
GA42 = ("2fcc3838b39bf66a43e0c498ac639b469287ce5c", "7c4b3d850c368adb02f2d777affa80256ee8c334")
CON2 = ("718a2cd60a0699d264fbc300c5ed64a4c8a975d4", "0895ca507af64a98dcfc8e898f344022da712f3f")


def git(*a, cwd=ROOT):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


def have_history() -> str:
    """'' when the replay commits and their merge bases are here (fetched from origin if not), else why not"""
    shas = {s for pair in (CON1, GA42, CON2) for s in pair}
    missing = [s for s in shas if git("cat-file", "-e", f"{s}^{{commit}}").returncode]
    if missing:
        if git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
            git("fetch", "--quiet", "--unshallow", "origin")
        for s in missing:
            git("fetch", "--quiet", "origin", s)
    for base, head in (CON1, GA42, CON2):
        if git("merge-base", base, head).returncode:
            if git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
                git("fetch", "--quiet", "--unshallow", "origin")
            if git("merge-base", base, head).returncode:
                return f"history for {head[:7]} not reachable from origin"
    return ""


def hits(spec, file, find, replace=None):
    return [i for i, m in enumerate(spec) if m["file"].endswith(file) and find in m["find"]
            and (replace is None or replace in m["replace"])]


class ReplayTest(unittest.TestCase):
    """D1: the generator finds the mutations baseline wrote by hand."""

    @classmethod
    def setUpClass(cls):
        why = have_history()
        if why:
            raise unittest.SkipTest(why)
        cls.spec = {h: M.mutations(ROOT, b, h) for b, h in (CON1, GA42, CON2)}

    def test_con1_min_font_and_max_bytes_in_top_15(self):
        s = self.spec[CON1[1]][:15]
        self.assertTrue(hits(s, "console/judge.py", '["minfont"] >= 12', '["minfont"] >= 1,'), "min-font >= 12 widened")
        self.assertTrue(hits(s, "console/judge.py", "MAX_BYTES = 512 * 1024", "* 1000"), "MAX_BYTES widened")

    def test_ga42_accept_guard_and_runtime_network_check_in_top_20(self):
        s = self.spec[GA42[1]][:20]
        self.assertTrue(hits(s, "ga/hub.py", 'if decision == "ACCEPT" and not (j.cls == "success" and not j.needs):',
                             'if decision == "ACCEPT" and not (j.cls == "success"):'), "and not j.needs dropped")
        net = hits(s, "actions/registry.py", 'if net and not e.get("network"):')
        self.assertTrue([i for i in net if s[i]["op"] in ("guard", "raise") or "if net:" not in s[i]["replace"]],
                        "run-time network check dropped")

    def test_con2_at_least_five_of_baselines_six(self):
        s = self.spec[CON2[1]]
        six = {
            "post token check": hits(s, "console/server.py", "if not self._host_ok() or not self._token_ok(allow_query=False):",
                                     "if not self._host_ok():"),
            "query token for POST": hits(s, "console/server.py", "self._token_ok(allow_query=False)", "allow_query=True"),
            "GET token check": [i for i in hits(s, "console/server.py", "if not self._token_ok(allow_query=True):")
                                if s[i]["op"] == "guard"],
            "static path containment": hits(s, "console/server.py", "STATIC.resolve() not in f.parents"),
            "secrets_in withholding": hits(s, "console/collectors.py", "return WITHHELD if secrets_in(obj) else obj",
                                           "return obj"),
            "shell=False": hits(s, "console/server.py", "shell=False", "shell=True"),
        }
        found = [k for k, v in six.items() if v]
        self.assertGreaterEqual(len(found), 5, f"found only {found}")

    def test_every_find_is_unique_in_its_file_and_applies_in_the_diff(self):
        for (base, head) in (CON1, GA42, CON2):
            changed = M.changed_lines(ROOT, base, head)
            for mu in self.spec[head]:
                text = git("show", f"{head}:{mu['file']}").stdout
                self.assertEqual(text.count(mu["find"]), 1, mu["id"])
                self.assertIn(mu["line"], changed[mu["file"]], mu["id"])
                self.assertNotEqual(mu["find"], mu["replace"])

    def test_tests_are_the_modules_that_import_the_mutated_one(self):
        s = self.spec[CON2[1]]
        srv = next(m for m in s if m["file"] == "ga/console/server.py")
        self.assertIn("tests/test_console_api.py", srv["tests"])
        self.assertNotIn("tests/test_ga34.py", srv["tests"])


def mkrepo(td: Path, files: dict[str, str]) -> Path:
    r = td / "r"
    sh("git", "init", "-q", "-b", "main", str(r))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        sh("git", "-C", str(r), "config", k, v)
    for rel, text in files.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text)
    sh("git", "-C", str(r), "add", "-A")
    sh("git", "-C", str(r), "commit", "-q", "-m", "x")
    return r


GUARDS = '''def check_a(x):
    if not x:
        raise ValueError("refused")
    return x


def check_b(x):
    if not x:
        raise ValueError("refused")
    return x
'''


class GeneratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.r = mkrepo(Path(self.tmp.name), {"pkg/g.py": GUARDS.split("\n\n\n")[0] + "\n",
                                              "tests/test_g.py": "from pkg import g\n", "tests/test_other.py": "x = 1\n"})
        self.base = sh("git", "-C", str(self.r), "rev-parse", "HEAD")
        (self.r / "pkg/g.py").write_text(GUARDS + "\n\ndef limit(n):\n    return n <= 10 and n >= 2\n\n\n"
                                         "def run(a):\n    import subprocess\n    return subprocess.run(a, shell=False)\n")
        sh("git", "-C", str(self.r), "commit", "-qam", "y")

    def test_only_changed_lines_and_unique_finds(self):
        spec = M.mutations(self.r, self.base, "HEAD")
        text = (self.r / "pkg/g.py").read_text()
        self.assertTrue(spec)
        for mu in spec:
            self.assertEqual(text.count(mu["find"]), 1, mu)
            self.assertGreater(mu["line"], 5, f"{mu['id']} mutates a line the diff did not change")
        guard = [m for m in spec if m["op"] == "guard"]
        self.assertEqual(len(guard), 1)
        applied = text.replace(guard[0]["find"], guard[0]["replace"])
        self.assertIn("def check_a(x):\n    if not x:", applied)  # the unchanged twin is left alone
        self.assertIn("def check_b(x):\n    if False:", applied)
        ops = {m["op"] for m in spec}
        self.assertTrue({"guard", "raise", "limit", "flag"} <= ops, ops)
        self.assertEqual(spec[0]["tests"], ["tests/test_g.py"])
        self.assertTrue(any(m["replace"].strip().endswith("n >= 1") for m in spec))
        self.assertTrue(any("(10) * 1000" in m["replace"] for m in spec))

    def test_cli_max_and_out(self):
        out = Path(self.tmp.name) / "spec.json"
        with redirect_stdout(io.StringIO()):
            rc = main(["verify", "mutations", "--repo", str(self.r), "--base", self.base, "--head", "HEAD", "--max", "2",
                       "--out", str(out)])
        self.assertEqual(rc, 0)
        spec = json.loads(out.read_text())
        self.assertEqual(len(spec), 2)
        self.assertTrue(all({"id", "file", "find", "replace", "tests"} <= m.keys() for m in spec))
        self.assertEqual(J.load_mutations(str(out)), spec)  # the judge reads it as is


class TestNamesTest(unittest.TestCase):
    def test_path_and_module_forms(self):
        ut = ["{python}", "-m", "unittest", "{tests}"]
        self.assertEqual(J.test_names(["tests/test_x.py", "tests.test_y", "tests/sub/test_z.py::T::t"], ut, ROOT),
                         ["tests.test_x", "tests.test_y", "tests.sub.test_z.T.t"])
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "tests").mkdir()
            (Path(td) / "tests/test_y.py").write_text("")
            pt = ["{python}", "-m", "pytest", "{tests}"]
            self.assertEqual(J.test_names(["tests.test_y", "tests.test_y.T.t", "tests/test_q.py"], pt, Path(td)),
                             ["tests/test_y.py", "tests/test_y.py::T::t", "tests/test_q.py"])


GUARDED = '''def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def check_positive(a):
    if a < 0:
        raise ValueError("refused: negative")
    return a
'''
TEST_G = '''import unittest
import fakepkg

class G(unittest.TestCase):
    def test_refuses_negative(self):
        with self.assertRaises(ValueError):
            fakepkg.check_positive(-1)
        self.assertEqual(fakepkg.check_positive(2), 2)
'''


class JudgeShaTest(unittest.TestCase):
    """D1: ga judge --sha on a local merge commit, the same verdict head shape as --report; --mutations auto with
    path-form test names."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.w = World(Path(cls.tmp.name))
        cls.feat = cls.w.branch_commit("feat", {"VERSION": "1.1", "fakepkg/__init__.py": GUARDED, "tests/test_g.py": TEST_G})
        repo = str(cls.w.repo)
        sh("git", "-C", repo, "checkout", "-q", "-b", "hub", "main")
        sh("git", "-C", repo, "merge", "-q", "--no-ff", "-m", "hub-side merge", cls.feat)
        cls.merge = sh("git", "-C", repo, "rev-parse", "HEAD")
        sh("git", "-C", repo, "checkout", "-q", "main")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_main(self, *a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["judge", "--repo", str(self.w.repo), "--base", "main", "--seed", "3", *a])
        out = buf.getvalue()
        head, _ = parse_text(out[out.index("```ga"):].split("needs_judgement:")[0])
        return rc, head

    def test_sha_judges_the_merge_with_the_report_head_shape(self):
        rep = Path(self.tmp.name) / "r.md"
        rep.write_text(report(self.feat, {"passed": 5, "failed": 0, "skipped": 1}))
        rc1, by_report = self.run_main("--report", str(rep), "--mutations", str(self.w.mut))
        rc2, by_sha = self.run_main("--sha", self.merge, "--mutations", str(self.w.mut))
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(by_sha["schema"], "verdict/1")
        self.assertEqual(set(by_sha) - {"report_ref"}, set(by_report) - {"report_ref"})
        self.assertEqual(set(by_sha["evidence"]), set(by_report["evidence"]))
        self.assertEqual(list(by_sha["evidence"]["heads"].values()), [self.merge])  # the merge, not a report's sha
        self.assertEqual(list(by_report["evidence"]["heads"].values()), [self.feat])
        self.assertEqual(by_sha["class"], "success")

    def test_sha_and_report_together_or_neither_is_refused(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["judge", "--repo", str(self.w.repo), "--base", "main"]), 2)
            self.assertEqual(main(["judge", "--repo", str(self.w.repo), "--base", "main", "--sha", "x", "--report", "y"]), 2)

    def test_mutations_auto_generates_and_runs_path_form_tests(self):
        j = J.judge_commit(self.w.repo, self.merge, "main", mutations="auto", seed=1, k=3, remote="origin")
        notes = "\n".join(j.notes)
        self.assertRegex(notes, r"mutations auto: \d+ generated")
        self.assertIn("killed", notes)
        self.assertNotIn("SURVIVED", notes)
        self.assertEqual(j.cls, "success", notes)


AGA4_REV1 = {
    "item": {"id": "AGA4", "goal": "Render the action card with its status chip.", "files":
             ["src/components/ActionCard.tsx", "src/lib/status.ts"], "done_when": "test"},
    "tests": {"src/__tests__/card.test.tsx": "import { ActionCard } from '../components/ActionCard'\n"
                                             "import { chip } from '../lib/status'\n"
                                             "test('renders', () => { expect(chip('ok')).toBe('ok') })\n"},
    "commands": {"commands": {"test": ["npx", "vitest", "run"]}, "timeout_s": 600},
    "base": "main"}


def rev2() -> dict:
    d = json.loads(json.dumps(AGA4_REV1))
    d["tests"]["src/__tests__/types.test.ts"] = (
        "import { execFileSync } from 'node:child_process'\n"
        "test('type-checks', () => { execFileSync('npx', ['tsc', '--noEmit'], { stdio: 'inherit' }) })\n")
    return d


class ItemLintTest(unittest.TestCase):
    def lint_cli(self, doc, *extra):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "item.json"
            f.write_text(json.dumps(doc))
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["verify", "item", str(f), *extra])
        return rc, buf.getvalue()

    def test_aga4_rev1_flagged_rev2_passes(self):
        rc, out = self.lint_cli(AGA4_REV1)
        self.assertEqual(rc, 1, out)
        self.assertIn("error typecheck", out)
        rc, out = self.lint_cli(rev2())
        self.assertEqual(rc, 0, out)
        self.assertNotIn("error", out.replace("0 error(s)", ""))

    def test_typecheck_in_the_command_passes(self):
        d = json.loads(json.dumps(AGA4_REV1))
        d["commands"]["commands"]["check"] = ["sh", "-c", "x"]  # an extra command alone does not count
        d["commands"]["commands"]["test"] = ["npm", "run", "typecheck"]
        self.assertEqual([f for f in I.lint(d) if f[1] == "typecheck"], [])
        d["commands"]["commands"]["test"] = ["npx", "tsc", "--noEmit"]
        self.assertEqual([f for f in I.lint(d) if f[1] == "typecheck"], [])

    def test_done_when_not_a_command_and_unimported_file(self):
        d = rev2()
        d["item"]["done_when"] = "verify"
        d["item"]["files"].append("src/lib/extra.ts")
        codes = [(lv, c) for lv, c, _ in I.lint(d)]
        self.assertIn(("error", "done-when"), codes)
        self.assertIn(("error", "not-imported"), codes)
        self.assertEqual([m for lv, c, m in I.lint(d) if c == "not-imported"], ["no acceptance test imports src/lib/extra.ts"])

    def test_python_files_need_importing_tests(self):
        d = {"item": {"id": "P", "goal": "g", "files": ["ga/x/y.py", "ga/x/z.py"], "done_when": "t"},
             "tests": {"tests/test_y.py": "from ga.x import y\n"}, "commands": {"commands": {"t": ["python", "-m", "pytest"]}}}
        self.assertEqual([m for _, c, m in I.lint(d) if c == "not-imported"], ["no acceptance test imports ga/x/z.py"])
        d["tests"] = {}
        self.assertIn("no-tests", [c for _, c, _ in I.lint(d)])

    def test_goal_naming_a_schema_type_without_its_fields_warns(self):
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            (r / "src/types").mkdir(parents=True)
            (r / "src/types/index.ts").write_text("export interface ActionTurn {\n  name: string\n  exitCode: number\n}\n")
            (r / "src/lib").mkdir(parents=True)
            (r / "src/lib/status.ts").write_text("import { ActionTurn } from '../types'\nexport const chip = (t: ActionTurn) => t.name\n")
            d = rev2()
            d["item"]["goal"] = "Show each ActionTurn on the card."
            rc, out = self.lint_cli(d, "--repo", str(r))
            self.assertEqual(rc, 0, out)
            self.assertIn("warning shapes", out)
            d["item"]["goal"] = "Show each ActionTurn (name: string, exitCode: number) on the card."
            rc, out = self.lint_cli(d, "--repo", str(r))
            self.assertNotIn("warning shapes", out)


if __name__ == "__main__":
    unittest.main()
