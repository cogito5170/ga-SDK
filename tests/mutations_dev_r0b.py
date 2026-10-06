"""CMD-DEV-R0b (router step-down): apply each mutation to a copy of the tree and run tests/test_dev_r0b.py; every one must be killed.

    python tests/mutations_ga50.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 ('window ignored (only climbs)', 'ga/act/route.py', '    won = won[-max(window, minimum):]\n', '    pass\n'),
 ('window takes the oldest wins', 'ga/act/route.py', 'won[-max(window, minimum):]', 'won[:max(window, minimum)]'),
 ('start is the lowest rung', 'ga/act/route.py', 'start = max((r["rungs"][-1] for r in won), key=ladder.index)', 'start = min((r["rungs"][-1] for r in won), key=ladder.index)'),
 ('start capped below the top rung (upper bound)', 'ga/act/route.py', 'start = max((r["rungs"][-1] for r in won), key=ladder.index)', 'start = ladder[min(len(ladder) - 2, max(ladder.index(r["rungs"][-1]) for r in won))]'),
 ('start floored above the lowest rung (lower bound)', 'ga/act/route.py', 'start = max((r["rungs"][-1] for r in won), key=ladder.index)', 'start = ladder[max(1, max(ladder.index(r["rungs"][-1]) for r in won))]'),
 ('rung outside the ladder accepted', 'ga/act/route.py', 'and used[-1] in ladder:', 'and True:'),
 ('minimum ignored', 'ga/act/route.py', '    if len(won) < minimum:\n', '    if len(won) < 1:\n'),
 ('config value not validated', 'ga/act/route.py', 'return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else default', 'return v if isinstance(v, int) else default'),
]


def run(name, f=None, old=None, new=None):
    tmp = Path(tempfile.mkdtemp())
    for d in ("ga", "tests", "docs"):
        shutil.copytree(ROOT / d, tmp / d)
    shutil.copy(ROOT / "pyproject.toml", tmp / "pyproject.toml")
    if f is not None:
        p = tmp / f
        s = p.read_text()
        assert s.count(old) == 1, (name, s.count(old))
        p.write_text(s.replace(old, new))
    r = subprocess.run([PY, "-m", "unittest", "tests.test_dev_r0b"], cwd=tmp, capture_output=True, text=True, timeout=900)
    shutil.rmtree(tmp, ignore_errors=True)
    return r.returncode, (r.stderr.strip().splitlines() or ["?"])[-1]


code, last = run("control")
print(f"control (no mutation): {'green' if code == 0 else 'RED'} ({last})", flush=True)
if code != 0:
    sys.exit(2)
ok = True
for name, f, old, new in (M if len(sys.argv) < 2 else [m for m in M if sys.argv[1] in m[0]]):
    code, last = run(name, f, old, new)
    killed = code != 0
    ok &= killed
    print(f"{'killed' if killed else 'SURVIVED'}: {name} ({last})", flush=True)
print("all killed" if ok else "SOME SURVIVED")
sys.exit(0 if ok else 1)
