"""VI-04b (baseline acceptance test, DEV-R3-DET): the shadow hub's decision is `ga verdict`, with 0 model calls.

Rules (ga/hub.py, MailHub; the non-shadow path is not changed):
  MailHub.__init__ takes a new keyword `verdict_fn` (default: ga.verdict.dry_run).
  In shadow mode, MailHub._one keeps the ga judge call (judge_fn) as it is, then decides with
  MailHub._verdict_decision(rp, rc, did, sha, owned, needs) -> (decision, [reason], err) instead of the card + model turn:
    sha    = j.sha or the report commit's sha;   rp = repo path;   rc = conf["repos"][repo];   owned = conf["owned"][did]
    spec   = conf["specs"][did] when given, else the first (sorted) file the diff rc["base"]...sha ADDS whose path is
             under tests/ (or contains /tests/) and whose name starts with test_;
             no spec -> ("SHADOW", ["no acceptance test for <did>"]) and verdict_fn is not called
    needs  = list(j.needs) (the judge's items left for judgement = criteria that cannot be executed)
    v      = verdict_fn(rp, branch=sha, sha=sha, base=rc["base"], spec=spec, allowed=owned or None,
                        nonexec=needs or None, scope=conf.get("verdict_scope", "full"), config=rc.get("config"))
             an exception -> ("SHADOW", ["ga verdict failed: <ExceptionName>"], "verdict:<ExceptionName>")
    result = (v["decision"], [v["reason"][:300]], "")      # ACCEPT | SEND_BACK | SHADOW, verbatim
  _verdict_decision returns (decision, lines, err). The shadow row (shadow.jsonl) carries that decision, asks == lines,
  error == err or null, served null, tokens {input: null, output: null}.
  shadow_compare: _SAME maps "SHADOW" to "ASK_HUMAN" (not integrated, a person looks), so a ga verdict failure row
  (decision SHADOW, error set) counts as a shadow error, not a disagreement.
  The model is never asked: no runner is created or called, MailHub._decide is not called, and the shadow tick
  imports no ga.llm / ga.backends / ga.gemini / ga.act module.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from ga import verdict as V
from ga.forms import dump_wire, hard, parse_text, validate
from ga.hub import MailHub, read_jsonl
from ga.judge import Judgement
from ga.mailbox import Message

ROOT = Path(__file__).resolve().parent.parent
REPO = "x/y"
DID = "CMD-T1"
BASE_MOD = "def check(x):\n    return x\n"
NEW_MOD = "def check(x):\n    if x < 0:\n        raise ValueError('neg')\n    return x\n"
OLD_TEST = "import unittest\nfrom mod import check\n\n\nclass T(unittest.TestCase):\n    def test_id(self):\n        self.assertEqual(check(1), 1)\n"
NEW_TEST = ("import unittest\nfrom mod import check\n\n\nclass N(unittest.TestCase):\n    def test_neg(self):\n"
            "        with self.assertRaises(ValueError):\n            check(-1)\n")
DIRECTIVE = {"schema": "directive/2", "id": DID, "rev": 1, "to": "GA", "goal": "guard negatives", "why": "test",
             "scope": [{"id": "S1", "text": "mod.py"}], "done_when": [{"id": "D1", "text": "tests green"}]}


def sh(d, *a):
    p = subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def make_repo(d, *, add_test=True):
    """main = base; branch feat changes mod.py (and adds tests/test_new.py). Returns the feat sha."""
    d.mkdir(parents=True)
    sh(d, "init", "-q", "-b", "main")
    (d / "tests").mkdir()
    (d / "mod.py").write_text(BASE_MOD)
    (d / "tests/test_old.py").write_text(OLD_TEST)
    sh(d, "add", "-A"); sh(d, "commit", "-qm", "base")
    sh(d, "checkout", "-qb", "feat")
    (d / "mod.py").write_text(NEW_MOD)
    if add_test:
        (d / "tests/test_new.py").write_text(NEW_TEST)
    sh(d, "add", "-A"); sh(d, "commit", "-qm", "feat")
    sha = sh(d, "rev-parse", "HEAD")
    sh(d, "checkout", "-q", "main")
    return sha


class Box:
    def __init__(self):
        self.box, self.read, self.sent, self.n = {}, set(), [], 0

    def put(self, to, text, sender):
        self.n += 1
        head, _ = parse_text(text)
        path = f"to/{to}/2026100{self.n:04d}-{sender}-{head.get('schema', '?').replace('/', '')}.md"
        probs = [str(p) for p in hard(validate(head))]
        self.box.setdefault(to, []).append(Message(path, to, sender, "", "", text, head.get("schema"), probs))
        return path

    def send(self, to, text, sender=None):
        self.sent.append((to, text))
        return self.put(to, text, sender)

    def unread(self, name):
        return [m for m in self.box.get(name, []) if m.path not in self.read]

    def mark_read(self, name, path):
        self.read.add(path)


class NoModel:
    bare = True

    def __init__(self):
        self.calls = 0

    def run_turn(self, *a, **k):
        self.calls += 1
        raise AssertionError("the shadow hub asked a model")


def world(tmp, *, add_test=True, owned=("mod.py", "tests/*"), specs=None, verdict_fn=None, needs=()):
    tmp = Path(tmp)
    proj = tmp / "proj"
    sha = make_repo(proj, add_test=add_test)
    (tmp / "directives").mkdir()
    (tmp / "directives" / f"{DID}.md").write_text(dump_wire(DIRECTIVE))
    conf = {"name": "baseline", "human": "human", "directives_dir": str(tmp / "directives"), "backend": "fake",
            "model": "fake-small", "repos": {REPO: {"path": str(proj), "base": "main"}},
            "owned": {DID: list(owned)}, "verdict_scope": "light", "shadow": True}
    if specs:
        conf["specs"] = specs
    box = Box()
    head = {"schema": "report/2", "from": "GA", "handled": [{"id": DID, "rev_seen": 1, "status": "done"}],
            "commits": [{"repo": REPO, "branch": "feat", "sha": sha}], "tests": {"passed": 1, "failed": 0, "skipped": 0},
            "items": [{"id": "D1", "state": "met"}]}
    box.put("baseline", dump_wire(head), "GA")

    def judge(report, repo, base, **k):
        return Judgement(cls="success", sha=sha, repo=REPO, ff=True, base=base, directive=DID,
                         tests={REPO: {"passed": 1, "failed": 0, "skipped": 0}}, needs=list(needs))
    runner = NoModel()
    kw = {"verdict_fn": verdict_fn} if verdict_fn else {}
    h = MailHub(conf, ga_dir=tmp / ".ga", mailbox=box, runner=runner, judge_fn=judge,
                apply_fn=lambda *a: (_ for _ in ()).throw(AssertionError("integrated in shadow")),
                today=lambda: "2026-10-07", shadow=True, **kw)
    h._decide = lambda *a, **k: (_ for _ in ()).throw(AssertionError("MailHub._decide called in shadow"))
    return h, runner, proj, sha


class ShadowIsVerdict(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def tick(self, **kw):
        h, runner, proj, sha = world(self.tmp, **kw)
        res = h.tick()
        rows = read_jsonl(self.tmp / ".ga/hub/shadow.jsonl")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(runner.calls, 0)
        return res, rows[0], proj, sha

    def expect(self, proj, sha, spec, owned):
        return V.dry_run(proj, branch=sha, sha=sha, base="main", spec=spec, allowed=list(owned), scope="light")

    def check_same(self, row, v):
        self.assertEqual(row["decision"], v["decision"])
        self.assertEqual(row["asks"], [v["reason"][:300]])
        self.assertIsNone(row["error"])
        self.assertEqual(row["tokens"], {"input": None, "output": None})

    def test_accept_equals_ga_verdict_with_the_added_test_as_spec(self):
        res, row, proj, sha = self.tick()
        v = self.expect(proj, sha, "tests/test_new.py", ("mod.py", "tests/*"))
        self.assertEqual(v["decision"], "ACCEPT")
        self.check_same(row, v)
        self.assertEqual(res.plan, [f"shadow ACCEPT {DID}"])
        self.assertEqual((row["id"], row["sha"], row["judge_class"]), (DID, sha, "success"))

    def test_file_outside_owned_is_send_back_as_ga_verdict_says(self):
        res, row, proj, sha = self.tick(owned=("tests/*",))
        v = self.expect(proj, sha, "tests/test_new.py", ("tests/*",))
        self.assertEqual((v["decision"], v["reason"].split(":")[0]), ("SEND_BACK", "files"))
        self.check_same(row, v)

    def test_configured_spec_wins_and_green_on_base_is_send_back(self):
        res, row, proj, sha = self.tick(specs={DID: "tests/test_old.py"})
        v = self.expect(proj, sha, "tests/test_old.py", ("mod.py", "tests/*"))
        self.assertEqual((v["decision"], v["reason"].split(":")[0]), ("SEND_BACK", "red_base"))
        self.check_same(row, v)

    def test_no_acceptance_test_is_shadow_without_calling_verdict(self):
        called = []
        res, row, proj, sha = self.tick(add_test=False, verdict_fn=lambda *a, **k: called.append(k))
        self.assertEqual(called, [])
        self.assertEqual((row["decision"], row["asks"]), ("SHADOW", [f"no acceptance test for {DID}"]))

    def test_verdict_fn_gets_the_inputs_and_its_answer_is_taken_verbatim(self):
        seen = []

        def fn(repo, **k):
            seen.append((str(repo), k))
            return {"decision": "SHADOW", "reason": "x" * 400}
        res, row, proj, sha = self.tick(verdict_fn=fn)
        (repo, k), = seen
        self.assertEqual(repo, str(proj))
        self.assertEqual(k, {"branch": sha, "sha": sha, "base": "main", "spec": "tests/test_new.py",
                             "allowed": ["mod.py", "tests/*"], "nonexec": None, "scope": "light", "config": None})
        self.assertEqual((row["decision"], row["asks"]), ("SHADOW", ["x" * 300]))

    def test_verdict_failure_is_shadow(self):
        def fn(repo, **k):
            raise RuntimeError("boom")
        res, row, proj, sha = self.tick(verdict_fn=fn)
        self.assertEqual((row["decision"], row["asks"], row["error"]),
                         ("SHADOW", ["ga verdict failed: RuntimeError"], "verdict:RuntimeError"))

    def test_judge_needs_are_non_executable_criteria_so_shadow(self):
        res, row, proj, sha = self.tick(needs=["D1 claim not measured"])
        v = V.dry_run(proj, branch=sha, sha=sha, base="main", spec="tests/test_new.py", allowed=["mod.py", "tests/*"],
                      nonexec=["D1 claim not measured"], scope="light")
        self.assertEqual(v["decision"], "SHADOW")
        self.check_same(row, v)

    def test_default_verdict_fn_is_ga_verdict_dry_run(self):
        h, *_ = world(self.tmp)
        self.assertIs(h.verdict_fn, V.dry_run)


class Compare(unittest.TestCase):
    def test_shadow_is_ask_human_and_a_verdict_error_is_a_shadow_error(self):
        from ga.hub import shadow_compare
        bl = [{"id": "A", "rev": 1, "decision": "ASK_HUMAN"}, {"id": "B", "rev": 1, "decision": "ACCEPT"}]
        sh = [{"id": "A", "rev": 1, "decision": "SHADOW"},
              {"id": "B", "rev": 1, "decision": "SHADOW", "error": "verdict:RuntimeError"}]
        out = shadow_compare(bl, sh)
        self.assertEqual((out["agree"], out["shadow_errors"], out["false_accepts"]), (1, ["B rev 1"], []))


class NoModelImport(unittest.TestCase):
    def test_a_shadow_tick_imports_no_model_code(self):
        code = textwrap.dedent("""
            import sys, tempfile
            sys.path.insert(0, %r)
            import tests.test_vi04b_shadow_verdict as T
            with tempfile.TemporaryDirectory() as d:
                h, runner, proj, sha = T.world(d)
                h.tick()
            bad = sorted(m for m in sys.modules if m.split('.')[:2] in (['ga', 'llm'], ['ga', 'backends'], ['ga', 'gemini'], ['ga', 'act']))
            print(bad)
            sys.exit(1 if bad or runner.calls else 0)
        """ % str(ROOT))
        env = {k: v for k, v in os.environ.items()}
        p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
