"""CMD-GA30: ga judge on fake repos. No network, no model: a bare local origin, an in-tree PEP 517 backend, pip --no-index."""
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
from ga.forms import dump_wire, hard, parse_text, validate  # noqa: E402

BACKEND = '''import zipfile, base64, hashlib, os
def get_requires_for_build_wheel(config_settings=None): return []
def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    name = "fakepkg-%s-py3-none-any.whl" % os.environ.get("FAKE_V", "1.0")
    ver = open("VERSION").read().strip()
    name = "fakepkg-%s-py3-none-any.whl" % ver
    meta = "Metadata-Version: 2.1\\nName: fakepkg\\nVersion: %s\\n" % ver + "".join(
        "Requires-Dist: %s\\n" % l for l in open("REQUIRES").read().split())
    files = {"fakepkg/__init__.py": open("fakepkg/__init__.py").read(),
             "fakepkg-%s.dist-info/METADATA" % ver: meta,
             "fakepkg-%s.dist-info/WHEEL" % ver: "Wheel-Version: 1.0\\nGenerator: t\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"}
    rec = []
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as z:
        for p, c in files.items():
            z.writestr(p, c)
            h = base64.urlsafe_b64encode(hashlib.sha256(c.encode()).digest()).rstrip(b"=").decode()
            rec.append("%s,sha256=%s,%d" % (p, h, len(c)))
        z.writestr("fakepkg-%s.dist-info/RECORD" % ver, "\\n".join(rec) + "\\n")
    return name
'''
PYPROJECT = '[build-system]\nrequires = []\nbuild-backend = "tinybuild"\nbackend-path = ["."]\n'
INIT = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
TESTS = '''import unittest, socket
import fakepkg

class T(unittest.TestCase):
    def test_add(self):
        self.assertEqual(fakepkg.add(1, 2), 3)

    def test_sub(self):
        self.assertEqual(fakepkg.sub(3, 2), 1)

    @unittest.skip("s")
    def test_skip(self):
        pass

    def test_loopback_still_works(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        socket.create_connection(srv.getsockname(), timeout=1).close()
        srv.close()

    def test_network_is_blocked(self):
        with self.assertRaises(OSError):
            socket.create_connection(("192.0.2.1", 80), timeout=1)
'''
CONFIG = {"dist": "fakepkg", "repo": "o/fakepkg", "pip_args": ["--no-index"], "pythonpath": ["."],
          "test": ["{python}", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
          "test_named": ["{python}", "-m", "unittest", "{tests}"]}
MUT = [{"id": "add-plus", "file": "fakepkg/__init__.py", "find": "a + b", "replace": "a - b", "tests": ["tests.test_f.T.test_add"]},
       {"id": "sub-minus", "file": "fakepkg/__init__.py", "find": "a - b\n", "replace": "a + b\n", "tests": ["tests.test_f.T.test_sub"]}]


def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def report(sha, tests=None, **kw):
    h = {"schema": "report/2", "from": "W", "handled": [{"id": "CMD-X1", "rev_seen": 1, "status": "done"}],
         "commits": [{"repo": "o/fakepkg", "branch": "w", "sha": sha}], "items": [{"id": "D1", "state": "met"}]}
    if tests is not None:
        h["tests"] = tests
    h.update(kw)
    return dump_wire(h)


class World:
    def __init__(self, td: Path):
        self.td = td
        self.origin = td / "origin.git"
        self.repo = td / "repo"
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        sh("git", "clone", "-q", str(self.origin), str(self.repo))
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            sh("git", "-C", str(self.repo), "config", k, v)
        self.write("README", "x"); self.commit("init")  # one commit deeper than a --depth 2 clone reaches
        self.write("tinybuild.py", BACKEND); self.write("pyproject.toml", PYPROJECT); self.write("VERSION", "1.0")
        self.write("REQUIRES", ""); self.write("fakepkg/__init__.py", INIT); self.write("tests/test_f.py", TESTS)
        self.write("tests/__init__.py", ""); self.write(".ga-judge.json", json.dumps(CONFIG))
        self.base = self.commit("base")
        sh("git", "-C", str(self.repo), "push", "-q", "origin", "HEAD:main")
        sh("git", "-C", str(self.repo), "branch", "-M", "main")
        sh("git", "-C", str(self.repo), "branch", "--set-upstream-to=origin/main", "main")
        self.mut = td / "mut.json"
        self.mut.write_text(json.dumps(MUT))

    def write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg):
        sh("git", "-C", str(self.repo), "add", "-A")
        sh("git", "-C", str(self.repo), "commit", "-q", "-m", msg)
        return sh("git", "-C", str(self.repo), "rev-parse", "HEAD")

    def branch_commit(self, name, files, frm=None):
        """a commit on branch `name` pushed to origin, not on main; the repo returns to main"""
        sh("git", "-C", str(self.repo), "checkout", "-q", "-B", name, frm or self.base)
        for rel, text in files.items():
            self.write(rel, text)
        sha = self.commit(name)
        sh("git", "-C", str(self.repo), "push", "-q", "-f", "origin", f"{name}:{name}")
        sh("git", "-C", str(self.repo), "checkout", "-q", "main")
        return sha

    def judge(self, sha, tests=None, rep=None, mutations=True, seed=7, config=None, **kw):
        f = self.td / "report.md"
        f.write_text(rep if rep is not None else report(sha, tests, **kw))
        return J.judge(str(f), self.repo, "main", mutations=str(self.mut) if mutations else None, seed=seed, config=config)


class JudgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.w = World(Path(cls.tmp.name))
        cls.green = cls.w.branch_commit("green", {"VERSION": "1.1"})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_green_is_a_success_candidate_with_recorded_seed_and_blocked_network(self):
        j = self.w.judge(self.green, {"passed": 4, "failed": 0, "skipped": 1}, results=[{"name": "version:fakepkg", "value": "1.1"}])
        self.assertEqual((j.cls, j.needs), ("success", []), j.notes)
        self.assertEqual(j.tests["o/fakepkg"], {"passed": 4, "failed": 0, "skipped": 1})
        notes = " ".join(j.notes)
        self.assertIn("mutation seed 7", notes)
        self.assertIn("killed", notes)
        self.assertIn("pip check: ok", notes)
        self.assertIn("fakepkg==1.1", notes)
        self.assertIn("network blocked", notes)
        self.assertTrue(j.ff and j.clean)
        head = J.verdict_head(j)
        self.assertEqual(hard(validate(head)), [])
        self.assertEqual(hard(validate(parse_text(J.render(j))[0])), [])
        self.assertIn("seed 7", J.render(j))

    def test_mutation_choice_follows_the_seed(self):
        a = self.w.judge(self.green, seed=1).notes
        b = self.w.judge(self.green, seed=1).notes
        self.assertEqual([n for n in a if n.startswith("mutation")], [n for n in b if n.startswith("mutation")])

    def test_failing_test_is_failure(self):
        sha = self.w.branch_commit("red", {"fakepkg/__init__.py": INIT.replace("a - b", "a * b")})
        j = self.w.judge(sha)
        self.assertEqual((j.cls, j.cause), ("failure", "implementation"))
        self.assertEqual(j.tests["o/fakepkg"]["failed"], 1)

    def test_surviving_mutation_is_insufficient(self):
        tests = TESTS.replace("self.assertEqual(fakepkg.add(1, 2), 3)", "self.assertTrue(True)")
        sha = self.w.branch_commit("weak", {"tests/test_f.py": tests})
        self.w.mut.write_text(json.dumps(MUT[:1]))
        try:
            j = self.w.judge(sha)
        finally:
            self.w.mut.write_text(json.dumps(MUT))
        self.assertEqual((j.cls, j.cause), ("insufficient", "measurement"))
        self.assertTrue(any("survived" in n for n in j.needs), j.needs)
        self.assertTrue(any("SURVIVED" in n for n in j.notes))

    def test_pip_check_failure(self):
        sha = self.w.branch_commit("dep", {"REQUIRES": "absent-dep-xyz"})
        cfg = self.w.td / "nodeps.json"  # --no-deps: the install succeeds, the missing dependency shows in pip check
        cfg.write_text(json.dumps({**CONFIG, "pip_args": ["--no-index", "--no-deps"]}))
        j = self.w.judge(sha, config=str(cfg))
        self.assertEqual((j.cls, j.cause), ("failure", "dependency"), j.notes)
        self.assertTrue(any("pip check failed" in n for n in j.notes))

    def test_claim_mismatch_goes_to_judgement(self):
        j = self.w.judge(self.green, {"passed": 9, "failed": 0, "skipped": 1}, results=[{"name": "version:fakepkg", "value": "2.0"}])
        self.assertEqual(j.cls, "success")
        self.assertEqual(len([n for n in j.needs if n.startswith("claim mismatch")]), 2, j.needs)
        self.assertFalse(j.clean)

    def test_non_ff_head_lists_conflicts(self):
        self.w.write("fakepkg/__init__.py", INIT + "# main moved\n")
        main_sha = self.w.commit("main moves")
        sh("git", "-C", str(self.w.repo), "push", "-q", "origin", "main")
        try:
            sha = self.w.branch_commit("other", {"fakepkg/__init__.py": INIT + "# other\n"})
            j = self.w.judge(sha)
            self.assertFalse(j.ff)
            self.assertTrue(any(n.startswith("non-ff") and "fakepkg/__init__.py" in n for n in j.needs), j.needs)
        finally:
            sh("git", "-C", str(self.w.repo), "reset", "-q", "--hard", self.w.base)
            sh("git", "-C", str(self.w.repo), "push", "-q", "-f", "origin", "main")
            self.assertNotEqual(main_sha, self.w.base)

    def test_bad_head_is_insufficient_measurement(self):
        j = self.w.judge(self.green, rep="no head here\n")
        self.assertEqual((j.cls, j.cause), ("insufficient", "measurement"))

    def test_deviation_and_missing_mutations_need_judgement(self):
        j = self.w.judge(self.green, mutations=False, deviations=["used x"])
        self.assertTrue(any(n.startswith("deviation") for n in j.needs))
        self.assertTrue(any("no mutation run" in n for n in j.needs))

    def test_report_by_ref(self):
        rep = report(self.green)
        sha = self.w.branch_commit("rep", {"reports/R.md": rep, "VERSION": "1.1"}, frm=self.green)
        f = self.w.td / "unused"
        j = J.judge(f"o/fakepkg@{sha}:reports/R.md", self.w.repo, "main", mutations=str(self.w.mut), seed=3)
        self.assertEqual(j.sha, self.green)

    def test_shallow_source_repo(self):
        shallow = self.w.td / "shallow"
        sh("git", "clone", "-q", "--depth", "2", "--no-single-branch", f"file://{self.w.origin}", str(shallow))
        self.assertEqual(sh("git", "-C", str(shallow), "rev-parse", "--is-shallow-repository"), "true")
        f = self.w.td / "shallow-report.md"
        f.write_text(report(self.green))
        before = sh("git", "-C", str(shallow), "for-each-ref")
        j = J.judge(str(f), shallow, "main", mutations=str(self.w.mut), seed=2)
        self.assertEqual((j.cls, j.needs, j.sha), ("success", [], self.green), (j.notes, j.needs))
        self.assertEqual(sh("git", "-C", str(shallow), "for-each-ref"), before)  # the user's repo is untouched

    def test_apply_refuses_unless_clean_and_pushes_when_clean(self):
        dirty = self.w.judge(self.green, deviations=["d"])
        with self.assertRaises(J.JudgeError):
            J.apply(dirty, self.w.repo)
        self.assertNotEqual(sh("git", "-C", str(self.w.origin), "rev-parse", "main"), self.green)
        clean = self.w.judge(self.green, {"passed": 4, "failed": 0, "skipped": 1})
        self.assertTrue(clean.clean, (clean.cls, clean.needs))
        try:
            J.apply(clean, self.w.repo)
            self.assertEqual(sh("git", "-C", str(self.w.origin), "rev-parse", "main"), self.green)
        finally:
            sh("git", "-C", str(self.w.repo), "reset", "-q", "--hard", self.w.base)
            sh("git", "-C", str(self.w.repo), "push", "-q", "-f", "origin", "main")

    def test_cli_exit_codes_and_apply_refusal(self):
        f = self.w.td / "cli.md"
        f.write_text(report(self.green, deviations=["d"]))
        argv = ["judge", "--report", str(f), "--repo", str(self.w.repo), "--base", "main", "--mutations", str(self.w.mut), "--seed", "5"]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(argv), 1)
            self.assertEqual(main(argv + ["--apply"]), 3)
        self.assertIn("```ga", out.getvalue())
        self.assertIn("DECISION_LOG:", out.getvalue())
        self.assertIn("apply refused", err.getvalue())


class CountsTest(unittest.TestCase):
    def test_unittest_and_pytest(self):
        self.assertEqual(J.parse_counts("Ran 10 tests in 1s\n\nFAILED (failures=2, errors=1, skipped=3)"), {"passed": 4, "failed": 3, "skipped": 3})
        self.assertEqual(J.parse_counts("Ran 4 tests in 1s\n\nOK (skipped=1)"), {"passed": 3, "failed": 0, "skipped": 1})
        self.assertEqual(J.parse_counts("=== 3 passed, 1 failed, 2 skipped in 0.5s ==="), {"passed": 3, "failed": 1, "skipped": 2})
        self.assertIsNone(J.parse_counts("nothing"))


if __name__ == "__main__":
    unittest.main()
