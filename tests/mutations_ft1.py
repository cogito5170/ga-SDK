"""CMD-FT1 D1: apply each mutation to a copy of the tree and run tests/test_ft1.py; every one must be killed.

    python tests/mutations_ft1.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 ("cache_read priced as full input", [("bench/final_task/ft/quota.py",
  'p["cache_read"] * 0.1 * p_in', 'p["cache_read"] * p_in')]),
 ("T5 guess graded correct", [("bench/final_task/ft/world.py",
  '    u = _s(a).upper() == "UNKNOWN"\n    return u, u', '    u = True\n    return u, u')]),
 ("cap not enforced", [("bench/final_task/ft/budget.py",
  "        return self.calls + run_max_calls <= self.max_calls and self.usd + run_est_usd <= self.usd_cap", "        return True"),
  ("bench/final_task/ft/budget.py", "        if self.calls + 1 > self.max_calls:\n            raise CapReached", "        if False:\n            raise CapReached"),
  ("bench/final_task/ft/budget.py", "        if self.usd + est_usd > self.usd_cap:\n            raise CapReached", "        if False:\n            raise CapReached")]),
 ("bulk context silently trimmed below 50k", [("bench/final_task/ft/bulk.py",
  "        if total >= target:", "        if total >= 30_000:"),
  ("bench/final_task/ft/bulk.py", "    if total < MIN_TOKENS:\n        raise", "    if False:\n        raise")]),
]
ok = True
for name, edits in M:
    tmp = Path(tempfile.mkdtemp())
    for d in ("ga", "tests", "bench", "pyproject.toml"):
        (shutil.copytree if (ROOT / d).is_dir() else shutil.copy)(ROOT / d, tmp / d)
    for f, old, new in edits:
        p = tmp / f
        s = p.read_text()
        assert s.count(old) == 1, (name, f, s.count(old))
        p.write_text(s.replace(old, new))
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ft1"], cwd=tmp, capture_output=True, text=True)
    last = r.stderr.strip().splitlines()[-1]
    killed = r.returncode != 0
    ok &= killed
    print(("KILLED  " if killed else "SURVIVED") + f"  {name}: {last}")
    shutil.rmtree(tmp)
sys.exit(0 if ok else 1)
