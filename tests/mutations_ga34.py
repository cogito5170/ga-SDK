"""CMD-GA34 D2: apply each mutation to a copy of the tree and run tests/test_ga34.py; every one must be killed.

    python tests/mutations_ga34.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 ("dependency ignored", "ga/net/pool.py",
  'waiting = sorted(a for a, st in deps.items() if st != "done")', "waiting = []"),
 ("ownership overlap allowed", "ga/net/pool.py", "if not waiting and files:", "if False:"),
 ("out-of-ownership diff accepted", "ga/net/pool.py", "(files and not owned(f, files))", "False"),
 ("failing judge still integrated", "ga/net/pool.py", 'if j.cls != "success" or not j.ff:', "if False:"),
 ("integration by non-fast-forward", "ga/adapters/git.py",
  "if local_old is not None and not is_ancestor(rd, local_old, new):", "if False:"),
 ("worktree shared between two nodes", "ga/net/pool.py",
  'str((self.ga / "worktrees" / node).resolve())', 'str((self.ga / "worktrees" / "shared").resolve())'),
 ("bypassPermissions accepted", "ga/backends/builtin.py",
  'PERMISSION_MODES = ("dontAsk", "acceptEdits", "plan", "manual")',
  'PERMISSION_MODES = ("dontAsk", "acceptEdits", "plan", "manual", "bypassPermissions")'),
 ("tools on when the role did not ask", "ga/net/node.py", 'if self.n.get("tools") is not None:', "if True:"),
 ("JUnit failures counted as passes", "ga/judge.py",
  'if tc.find("failure") is not None or tc.find("error") is not None:', "if False:"),
 ("turn.progress carrying file contents", "ga/backends/builtin.py",
  '            if path:\n                row["path"] = path[:300]', '            row["input"] = inp'),
]
ok = True
for name, f, old, new in M:
    tmp = Path(tempfile.mkdtemp())
    for d in ("ga", "tests"):
        shutil.copytree(ROOT / d, tmp / d)
    p = tmp / f
    s = p.read_text()
    assert s.count(old) == 1, (name, s.count(old))
    p.write_text(s.replace(old, new))
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga34"], cwd=tmp, capture_output=True, text=True)
    last = r.stderr.strip().splitlines()[-1]
    killed = r.returncode != 0
    ok &= killed
    print(("KILLED  " if killed else "SURVIVED") + f"  {name}: {last}")
    shutil.rmtree(tmp)
sys.exit(0 if ok else 1)
