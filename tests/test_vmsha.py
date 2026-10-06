"""DEV-VMSHA: the VM reports the ga-sdk SHA it runs (same-version change, on request) and its R0 test result, by the
existing notify/1 mail; offline: local bare remotes, a fake Runner; no model anywhere on the path."""
import ast
import json
import unittest
from pathlib import Path
from unittest import mock

from ga import mailbox
from ga.forms import dump_text
from ga.vm import core
from tests.test_ga48 import R, Upd, sh


class Fake(R):
    """The suite output and the python version of the VM's venv."""

    def __init__(self, summary="1400 passed, 1 skipped in 20.00s", **kw):
        super().__init__(**kw)
        self.summary, self.pytests = summary, 0

    def run(self, argv, *, env=None, cwd=None, timeout=600):
        a = list(argv)
        if a[0].endswith("ga-venv/bin/python") and a[1:3] == ["-m", "pytest"]:
            self.pytests += 1
            return 0, f"....\n{self.summary}\n"
        if a[0].endswith("ga-venv/bin/python") and a[1:] == ["-V"]:
            return 0, "Python 3.13.1\n"
        return super().run(argv, env=env, cwd=cwd, timeout=timeout)


class Vm(Upd):
    def sent(self, only=None):
        """(id, kind, note, ref) of every mail to baseline-ops, oldest first."""
        box = self.root / "baseline.git"
        if "ga-mailbox" not in sh("git", "--git-dir", str(box), "branch", "--list", "ga-mailbox"):
            return []
        out = []
        for f in sorted(sh("git", "--git-dir", str(box), "ls-tree", "-r", "--name-only", "ga-mailbox").split()):
            if f.startswith(f"to/{core.NOTICE_TO}/"):
                h = json.loads(sh("git", "--git-dir", str(box), "show", f"ga-mailbox:{f}").split("```ga\n")[1].split("\n```")[0])
                out.append((h["id"], h["kind"], h.get("note", ""), h["ref"]))
        return [m for m in out if only is None or m[0].startswith(only)]

    def ask(self, fid):
        head = {"schema": "notify/1", "to": "vm", "kind": "question", "id": fid,
                "ref": "https://github.com/cogito5170/ga-sdk", "note": "sha?"}
        mailbox.Mailbox(self.home / "baseline").send("vm", dump_text(head), sender="baseline")

    def sdk_sha(self):
        return self.head("ga-sdk")


class SameVersionSha(Vm):
    def test_a_same_version_sha_change_is_reported_once_and_unchanged_not_at_all(self):
        self.up(Fake())
        acks = lambda: [m for m in self.sent() if m[1] == "ack"]  # noqa: E731
        self.assertEqual(len(acks()), 1)
        self.up(Fake())  # nothing new: no new mail
        self.assertEqual(len(acks()), 1)
        self.push("ga-sdk")
        rc, _ = self.up(Fake())  # the same version 1.0.0, a new SHA
        self.assertEqual(rc, 0)
        new = acks()
        self.assertEqual(len(new), 2)
        self.assertIn(self.sdk_sha(), new[-1][2])
        self.assertTrue(new[-1][3].endswith(self.sdk_sha()))
        self.up(Fake())
        self.assertEqual(len(acks()), 2)


class Ask(Vm):
    def test_baseline_ops_asks_for_the_sha_and_gets_one_answer(self):
        self.up(Fake())
        n = len([m for m in self.sent() if m[1] == "ack"])
        self.ask(core.SHA_ASK)
        self.up(Fake())
        answers = [m for m in self.sent(core.SHA_ASK)]
        self.assertEqual(len(answers), 1)
        self.assertIn(self.sdk_sha(), answers[0][2])
        self.assertEqual(len([m for m in self.sent() if m[1] == "ack"]), n + 1)
        self.up(Fake())  # read: not answered again
        self.assertEqual(len(self.sent(core.SHA_ASK)), 1)


class R0(Vm):
    def test_the_r0_result_goes_out_once_per_sha_with_counts_sha_and_env(self):
        r = Fake()
        self.up(r)
        rep = self.sent(core.R0_ASK)
        self.assertEqual(len(rep), 1)
        self.assertEqual(rep[0][1], "report")
        for want in (self.sdk_sha(), "1400 passed", "1 skipped", "Python 3.13.1"):
            self.assertIn(want, rep[0][2])
        self.up(r)
        self.assertEqual((r.pytests, len(self.sent(core.R0_ASK))), (1, 1))
        self.push("ga-sdk")
        self.up(r)
        self.assertEqual((r.pytests, len(self.sent(core.R0_ASK))), (2, 2))

    def test_a_failing_run_is_reported_with_its_counts(self):
        self.up(Fake("3 failed, 1397 passed in 9s"))
        self.assertIn("3 failed", self.sent(core.R0_ASK)[0][2])

    def test_baseline_ops_can_ask_for_a_fresh_run(self):
        r = Fake()
        self.up(r)
        self.ask(core.R0_ASK)
        self.up(r)
        self.assertEqual((r.pytests, len(self.sent(core.R0_ASK))), (2, 2))


class NoModel(Vm):
    def test_no_model_call_on_the_path(self):
        def boom(*a, **k):
            raise AssertionError("a model was called")
        with mock.patch("ga.backends.create", boom), mock.patch("ga.backends.get", boom):
            self.up(Fake())
            self.ask(core.SHA_ASK)
            self.push("ga-sdk")
            self.up(Fake())
        self.assertTrue(self.sent(core.SHA_ASK))

    def test_the_vm_module_imports_no_model_code(self):
        tree = ast.parse(Path(core.__file__).read_text(encoding="utf-8"))
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                names |= {("." * n.level) + (n.module or "") + "." + a.name for a in n.names}
        bad = [x for x in names if any(w in x for w in ("backends", "gemini", "agy", "hub", "bridge", "act", "claude"))]
        self.assertEqual([x for x in bad if "console" not in x], [])


if __name__ == "__main__":
    unittest.main()
