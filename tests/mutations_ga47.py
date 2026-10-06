"""CMD-GA47 D2: apply each mutation to a copy of the tree and run tests/test_ga47.py; every one must be killed.

    python tests/mutations_ga47.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
L = "ga/act/loop.py"
R = "ga/act/route.py"
M = [
 ("file content in the triage card", R, '    text = card(str(raw.get("goal", "")), f, ladder)',
  '    text = card(str(raw.get("goal", "")) + " ".join((Path(root) / p).read_text(errors="ignore")[:200] for p, _ in f["files"] if (Path(root) / p).is_file()), f, ladder)'),
 ("climb unlimited", R, "    run = ladder[i:i + 1 + climb]", "    run = ladder[i:]"),
 ("ledger ignored", R, "    led = from_ledger(read(state), b, ladder)", "    led = None"),
 ("unknown start model accepted", R, "    if not isinstance(s, str) or s not in ladder:", "    if not isinstance(s, str):"),
 ("triage tokens not counted", L, 'total=int(res.tokens.get("total", 0)) + tri)', 'total=int(res.tokens.get("total", 0)))'),
 ("cheapest-rung cap not applied", R, '        caps[ladder[0]] = min(route["max_turns"], LOW_CAP)', '        caps[ladder[0]] = route["max_turns"]'),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga47"], cwd=tmp, capture_output=True, text=True, timeout=900)
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
