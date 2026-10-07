"""ISO-1 (baseline acceptance test): ga act runs a worktree's tests on the worktree's code only.

Found 10-07 15:3x: on the VM, ga-sdk is an editable install of the deployed tree (~/ga-sdk). Its meta-path finder
(module name starting with "__editable__") resolves any ga.* submodule missing from the worktree to the deployed tree,
so a test of a NEW module passed in a worktree that did not have it (bench CMD-BENCHB*: "done_when already passes").
Rules:
  - ga.act.commands.run sets GA_ACT_ISOLATE=<cwd> in the child environment (other env handling unchanged).
  - importing ga with GA_ACT_ISOLATE set removes every sys.meta_path finder and sys.path_hooks hook whose __module__
    starts with "__editable__", and clears sys.path_importer_cache; without the variable nothing is removed.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ga.act import commands as C

ROOT = Path(__file__).resolve().parents[1]
PROBE = (
    "import sys\n"
    "class F:\n"
    "    @classmethod\n"
    "    def find_spec(cls, *a, **k):\n"
    "        return None\n"
    "F.__module__ = '__editable___fake_finder'\n"
    "def hook(p):\n"
    "    raise ImportError\n"
    "hook.__module__ = '__editable___fake_finder'\n"
    "sys.meta_path.append(F)\n"
    "sys.path_hooks.append(hook)\n"
    "import ga\n"
    "print('finder', F in sys.meta_path, 'hook', hook in sys.path_hooks)\n"
)


def probe(isolate):
    env = {k: v for k, v in os.environ.items() if k != "GA_ACT_ISOLATE"}
    if isolate:
        env["GA_ACT_ISOLATE"] = str(ROOT)
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=str(ROOT), env=env, capture_output=True, text=True,
                         timeout=60)
    return out.stdout.strip() + out.stderr[-500:]


class Isolation(unittest.TestCase):
    def test_run_sets_the_isolation_variable_to_the_worktree(self):
        d = Path(tempfile.mkdtemp())
        r = C.run("t", ["{python}", "-c", "import os; print(os.environ.get('GA_ACT_ISOLATE', 'unset'))"], d, 60)
        self.assertEqual(r.code, 0, r.out)
        self.assertEqual(r.out.strip(), str(d))

    def test_ga_drops_editable_finders_when_isolated(self):
        self.assertEqual(probe(True), "finder False hook False")

    def test_ga_keeps_them_otherwise(self):
        self.assertEqual(probe(False), "finder True hook True")


if __name__ == "__main__":
    unittest.main()
