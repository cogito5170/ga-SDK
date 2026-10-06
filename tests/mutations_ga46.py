"""CMD-GA46 D2: apply each mutation to a copy of the tree and run tests/test_ga46.py; every one must be killed.

    python tests/mutations_ga45.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
C = "ga/vm/core.py"
H = "ga/hub.py"
L = "ga/act/loop.py"
M = [
 ("rejected turns not counted", L, 'elif row["applied"] or row.pop("idle"):', 'elif row["applied"]:'),
 ("NEW allowed outside the item's files", "ga/act/apply.py", "        if not owned(a.arg, globs):", "        if not owned(a.arg, globs) and a.kind != 'NEW':"),
 ("blank lines kept", "ga/act/fmt.py", "    while lines and not lines[-1].strip():", "    while False:"),
 ("stall threshold 10", L, "NO_PROGRESS_TURNS = 2", "NO_PROGRESS_TURNS = 10"),
 ("changed failing set does not reset", L, "                if now != prev:\n                    stall = 0\n                elif", "                if False:\n                    stall = 0\n                elif"),
 ("NEW refused on existing file", "ga/act/apply.py", "            if p.exists() and not p.is_file():", "            if p.exists():"),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga46"], cwd=tmp, capture_output=True, text=True, timeout=900)
    shutil.rmtree(tmp, ignore_errors=True)
    return r.returncode, (r.stderr.strip().splitlines() or ["?"])[-1]


code, last = run("control")
print(f"control (no mutation): {'green' if code == 0 else 'RED'} ({last})", flush=True)
if code != 0:
    sys.exit(2)
ok = True
for name, f, old, new in M:
    code, last = run(name, f, old, new)
    killed = code != 0
    ok &= killed
    print(f"{'killed' if killed else 'SURVIVED'}: {name} ({last})", flush=True)
print("all killed" if ok else "SOME SURVIVED")
sys.exit(0 if ok else 1)
