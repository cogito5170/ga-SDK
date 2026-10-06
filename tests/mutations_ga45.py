"""CMD-GA45 D2: apply each mutation to a copy of the tree and run tests/test_ga45.py; every one must be killed.

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
 ("hub unit without --config", C, 'tick --shadow --config {home}/.ga/hub.json "\n           f"--ga-dir {home}/.ga', 'tick --shadow'),
 ("hub.json overwritten", C, "            if not hub_json.exists():", "            if True:"),
 ("shadow row mailed twice", H, "            if not key or key in mailed:\n", "            if not key:\n"),
 ("shadow row mailed to 'baseline'", H, 'SHADOW_TO = "baseline-shadow"', 'SHADOW_TO = "baseline"'),
 ("shadow to own name allowed", H, "        if to == self.name:  # never", "        if False:  # never"),
 ("ladder escalates on done", L, '        if res.status == "done" or not res.reason.startswith(ESCALATE):', '        if not res.reason.startswith(ESCALATE) and res.status != "done":'),
 ("rung 2 starts from rung 1's dirty tree", L, "        if i:\n            tree.restore()\n", "        if i:\n            pass\n"),
 ("unknown model accepted", L, "    if bad:\n        raise", "    if False:\n        raise"),
 ("refusal escalates", L, 'ESCALATE = ("turn cap", "token cap", "no progress")', 'ESCALATE = ("turn cap", "token cap", "no progress", "model")'),
 ("missing config traceback", "ga/__main__.py", "    if not cf.is_file():", "    if False:"),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga45"], cwd=tmp, capture_output=True, text=True, timeout=900)
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
