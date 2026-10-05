"""CMD-GA39 D2: apply each mutation to a copy of the tree and run tests/test_ga39.py; every one must be killed.

    python tests/mutations_ga39.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 ("generator emits a non-unique find", "ga/verify/mutate.py", "        if text.count(find) == 1:\n", "        if True:\n"),
 ("operators applied outside the diff", "ga/verify/mutate.py",
  "        if lineno not in changed:\n            continue\n        if src.b", "        if src.b"),
 ("--sha ignored (judges the report sha)", "ga/__main__.py", "        if args.sha:  # CMD-GA39", "        if False:  # CMD-GA39"),
 ("item lint misses TS without type-check", "ga/verify/items.py", "        if not (by_cmd or by_test):", "        if False:"),
]


def run(name, f=None, old=None, new=None):
    tmp = Path(tempfile.mkdtemp())
    for d in ("ga", "tests", ".git"):
        shutil.copytree(ROOT / d, tmp / d, symlinks=True)
    shutil.copy(ROOT / "pyproject.toml", tmp / "pyproject.toml")
    if f is not None:
        p = tmp / f
        s = p.read_text()
        assert s.count(old) == 1, (name, s.count(old))
        p.write_text(s.replace(old, new))
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga39"], cwd=tmp, capture_output=True, text=True, timeout=1800)
    shutil.rmtree(tmp, ignore_errors=True)
    return r.returncode, (r.stderr.strip().splitlines() or ["?"])[-1]


code, last = run("control")  # the unmutated copy must pass, or a kill means nothing
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
