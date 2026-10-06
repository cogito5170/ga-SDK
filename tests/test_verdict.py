"""DEV-R3-DET (R2): ``ga verdict --dry-run`` on fake repos. No network, no model, no push; decide() as a truth table."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ga import verdict as V  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CFG = {"setup": [], "repo": "x/y", "pythonpath": ["."], "test": ["{python}", "-m", "unittest", "discover", "-s", "tests"]}
BASE_MOD = "def check(x):\n    return x\n"
NEW_MOD = "def check(x):\n    if x < 0:\n        raise ValueError('neg')\n    return x\n"
OLD_TEST = "import unittest\nfrom mod import check\n\n\nclass T(unittest.TestCase):\n    def test_id(self):\n        self.assertEqual(check(1), 1)\n"
NEW_TEST = ("import unittest\nfrom mod import check\n\n\nclass N(unittest.TestCase):\n    def test_neg(self):\n"
            "        with self.assertRaises(ValueError):\n            check(-1)\n")


def sh(d: Path, *a: str) -> str:
    p = subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def make_repo(d: Path, *, mod: str = NEW_MOD, extra: dict[str, str] | None = None) -> tuple[str, str]:
    """base on main; branch feat adds the guard and its acceptance test. Returns (base sha, feat sha)."""
    sh(d, "init", "-q", "-b", "main")
    (d / "tests").mkdir()
    (d / "mod.py").write_text(BASE_MOD)
    (d / "tests/test_old.py").write_text(OLD_TEST)
    (d / ".ga-judge.json").write_text(json.dumps(CFG))
    sh(d, "add", "-A"); sh(d, "commit", "-qm", "base")
    base = sh(d, "rev-parse", "HEAD")
    sh(d, "checkout", "-qb", "feat")
    (d / "mod.py").write_text(mod)
    (d / "tests/test_new.py").write_text(NEW_TEST)
    for k, v in (extra or {}).items():
        (d / k).write_text(v)
    sh(d, "add", "-A"); sh(d, "commit", "-qm", "feat")
    return base, sh(d, "rev-parse", "HEAD")


def ok(n: str = "", d: str = "") -> V.Check:
    return True, d or n


class Decide(unittest.TestCase):
    def all_ok(self):
        return {n: (True, "ok") for n in V.ORDER}

    def test_accept(self):
        self.assertEqual(V.decide(self.all_ok())["decision"], "ACCEPT")

    def test_each_false_check_sends_back_with_its_name(self):
        for n in V.ORDER:
            c = self.all_ok()
            c[n] = (False, "bad")
            self.assertEqual(V.decide(c), {"decision": "SEND_BACK", "reason": f"{n}: bad"}, n)

    def test_first_failure_in_order_is_the_reason(self):
        c = self.all_ok()
        c["revert"], c["files"] = (False, "r"), (False, "f")
        self.assertEqual(V.decide(c)["reason"], "files: f")

    def test_unrun_check_is_shadow_and_false_beats_it(self):
        c = self.all_ok()
        c["mutations"] = (None, "none")
        self.assertEqual(V.decide(c), {"decision": "SHADOW", "reason": "mutations: none"})
        c["head"] = (False, "moved")
        self.assertEqual(V.decide(c)["decision"], "SEND_BACK")

    def test_nonexecutable_criterion_is_shadow_sorted(self):
        d = V.decide(self.all_ok(), ["b crit", "a crit"])
        self.assertEqual(d, {"decision": "SHADOW", "reason": "non-executable criterion: a crit; b crit"})

    def test_light_scope_ignores_suite_and_mutations_only(self):
        c = self.all_ok()
        c["suite"] = c["mutations"] = (None, "x")
        self.assertEqual(V.decide(c, scope="light")["decision"], "ACCEPT")
        c["suite"] = (False, "red")
        self.assertEqual(V.decide(c, scope="light")["decision"], "ACCEPT")
        c["revert"] = (None, "x")
        self.assertEqual(V.decide(c, scope="light")["decision"], "SHADOW")


class NoModel(unittest.TestCase):
    BANNED = ("ga.backends", "ga.adapters", "ga.llm", "ga.gemini", "ga.hub", "anthropic", "openai", "google")

    def graph(self) -> dict[str, set[str]]:
        seen: dict[str, set[str]] = {}
        todo = ["ga.verdict"]
        while todo:
            m = todo.pop()
            if m in seen:
                continue
            p = ROOT / (m.replace(".", "/") + ".py")
            if not p.is_file():
                p = ROOT / m.replace(".", "/") / "__init__.py"
            if not p.is_file():
                seen[m] = set()
                continue
            pkg = m if p.name == "__init__.py" else m.rpartition(".")[0]
            deps: set[str] = set()
            for node in ast.walk(ast.parse(p.read_text())):  # lazy (in-function) imports count
                if isinstance(node, ast.Import):
                    deps |= {a.name for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    base = pkg.split(".")[: len(pkg.split(".")) - (node.level - 1)] if node.level else []
                    mod = ".".join([*base, *([node.module] if node.module else [])])
                    deps.add(mod)
                    deps |= {f"{mod}.{a.name}" for a in node.names if (ROOT / f"{mod.replace('.', '/')}/{a.name}.py").is_file()}
            seen[m] = deps
            todo += sorted(d for d in deps if d.startswith("ga"))
        return seen

    def test_import_graph_reaches_no_backend_or_gateway(self):
        bad = {m: sorted(d for d in deps for b in self.BANNED if d == b or d.startswith(b + ".")) for m, deps in self.graph().items()}
        self.assertEqual({m: d for m, d in bad.items() if d}, {})

    def test_importing_the_module_loads_no_backend(self):
        code = "import sys, ga.verdict, ga.verify.mutate\nbad=[m for m in sys.modules if m.startswith(('ga.backends','ga.adapters','ga.llm','ga.gemini','ga.hub'))]\nprint(bad)\nsys.exit(1 if bad else 0)"
        p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class DryRun(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = Path(self._t.name)
        self.base, self.sha = make_repo(self.d)

    def tearDown(self):
        self._t.cleanup()

    def run_(self, **kw):
        a = dict(branch="feat", sha=self.sha, base="main", spec="tests/test_new.py", k=3)
        a.update(kw)
        return V.dry_run(self.d, **a)

    def test_accept_full(self):
        v = self.run_()
        self.assertEqual(v["decision"], "ACCEPT", v)
        self.assertTrue(all(c["ok"] for c in v["checks"].values()), v["checks"])
        self.assertTrue(v["mutations"] and all(m["killed"] for m in v["mutations"]))
        self.assertEqual(v["checks"]["ancestry"]["detail"], "fast-forward")

    def test_deterministic_twice_byte_equal(self):
        self.assertEqual(V.render(self.run_()), V.render(self.run_()))

    def test_head_moved_is_send_back(self):
        sh(self.d, "commit", "-q", "--allow-empty", "-m", "later")
        v = self.run_()
        self.assertEqual((v["decision"], v["reason"].split(":")[0]), ("SEND_BACK", "head"))

    def test_files_outside_allowed(self):
        v = self.run_(allowed=["mod.py", "tests/test_new.py"])
        self.assertEqual(v["decision"], "ACCEPT")
        sh(self.d, "checkout", "-q", "feat")
        (self.d / "other.txt").write_text("x")
        sh(self.d, "add", "-A"); sh(self.d, "commit", "-qm", "more")
        sha = sh(self.d, "rev-parse", "HEAD")
        v = self.run_(sha=sha, allowed=["mod.py", "tests/test_new.py"])
        self.assertEqual(v["decision"], "SEND_BACK")
        self.assertIn("other.txt", v["reason"])

    def test_spec_differs_from_authoritative_copy(self):
        sh(self.d, "checkout", "-q", "-b", "auth", self.base)
        (self.d / "tests/test_new.py").write_text(NEW_TEST + "\n# authoritative\n")
        sh(self.d, "add", "-A"); sh(self.d, "commit", "-qm", "auth")
        v = self.run_(spec_ref="auth")
        self.assertEqual((v["decision"], v["reason"].split(":")[0]), ("SEND_BACK", "spec"))

    def test_spec_green_on_base_is_send_back(self):
        v = self.run_(spec="tests/test_old.py")  # exists at the sha, passes on the base
        self.assertEqual(v["decision"], "SEND_BACK")
        self.assertTrue(v["reason"].startswith("red_base"), v["reason"])

    def test_conflicting_merge_is_send_back_clean_merge_is_not(self):
        sh(self.d, "checkout", "-q", "main")
        (self.d / "mod.py").write_text("def check(x):\n    return -x\n")
        sh(self.d, "commit", "-qam", "main moves, same lines")
        v = self.run_()
        self.assertEqual((v["decision"], v["reason"].split(":")[0]), ("SEND_BACK", "ancestry"))
        self.assertIn("mod.py", v["reason"])
        sh(self.d, "reset", "-q", "--hard", self.base)
        (self.d / "unrelated.txt").write_text("x")
        sh(self.d, "add", "-A"); sh(self.d, "commit", "-qm", "main moves elsewhere")
        v = self.run_()
        self.assertEqual(v["checks"]["ancestry"], {"ok": True, "detail": "clean merge"})

    def test_missing_spec_is_send_back(self):
        self.assertEqual(self.run_(spec="tests/nope.py")["decision"], "SEND_BACK")

    def test_surviving_mutation_is_send_back(self):
        # a change whose guard the acceptance test does not exercise: the mutation survives
        d2 = Path(tempfile.mkdtemp(prefix="v2-"))
        mod = NEW_MOD + "\n\ndef limit(x):\n    if x > 100:\n        raise ValueError('big')\n    return x\n"
        base, sha = make_repo(d2, mod=mod)
        v = V.dry_run(d2, branch="feat", sha=sha, base="main", spec="tests/test_new.py", k=50, seed=1)
        self.assertEqual(v["decision"], "SEND_BACK", v)
        self.assertTrue(v["reason"].startswith("mutations: survived"), v["reason"])

    def test_nonexecutable_criterion_is_shadow(self):
        v = self.run_(nonexec=["the UI feels right"])
        self.assertEqual((v["decision"], v["reason"]), ("SHADOW", "non-executable criterion: the UI feels right"))

    def test_dry_run_pushes_nothing_and_leaves_the_repo(self):
        before = sh(self.d, "for-each-ref")
        self.run_()
        self.assertEqual(sh(self.d, "for-each-ref"), before)
        self.assertEqual(sh(self.d, "status", "--porcelain"), "")


class Replay(unittest.TestCase):
    def test_replay_rows_counts_and_twice_identical(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "r"
            d.mkdir()
            base, sha = make_repo(d)
            rows = [{"id": "A", "rev": 1, "sha": sha[:9], "decision": "ACCEPT"},
                    {"id": "B", "rev": 1, "sha": sha, "decision": "SEND_BACK"},
                    {"id": "C", "rev": 1, "sha": "0" * 12, "decision": "ACCEPT"},
                    {"id": "D", "rev": 1, "decision": "SEND_BACK"},
                    {"id": "E", "rev": 1, "sha": base, "decision": "ACCEPT"},
                    {"id": "F", "rev": 1, "sha": sha, "decision": "INTEGRATED"}]
            f = Path(t) / "v.jsonl"
            f.write_text("".join(json.dumps(r) + "\n" for r in rows))
            a, ca = V.replay(f, d)
            b, cb = V.replay(f, d)
            self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
            self.assertEqual(ca, cb)
            by = {r["id"]: r for r in a}
            self.assertTrue(by["A"]["agree"])
            self.assertFalse(by["B"]["agree"])
            for k in "CDEF":
                self.assertIsNone(by[k]["dry_run"], k)
                self.assertTrue(by[k]["reason"].startswith("not_reproducible"), by[k])
            self.assertEqual(ca, {"agree": 1, "disagree": 1, "not_reproducible": 4, "flaky": 0})
            self.assertEqual(sorted(by["A"]), sorted(["id", "rev", "sha", "recorded", "dry_run", "agree", "reason", "flaky"]))

    def test_specs_file_supplies_unknown_test_path(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "r"
            d.mkdir()
            base, sha = make_repo(d)
            f = Path(t) / "v.jsonl"
            f.write_text(json.dumps({"id": "A", "rev": 1, "sha": sha, "decision": "ACCEPT"}) + "\n")
            s = Path(t) / "s.jsonl"
            s.write_text(json.dumps({"id": "A", "rev": 1, "spec": "tests/test_new.py", "base": base, "allowed": ["mod.py"]}) + "\n")
            rows, counts = V.replay(f, d, specs=s)
            self.assertEqual(rows[0]["dry_run"], "SEND_BACK(files: outside allowed: tests/test_new.py)")
            self.assertEqual(counts["disagree"], 1)


if __name__ == "__main__":
    unittest.main()
