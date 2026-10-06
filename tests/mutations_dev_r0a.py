"""DEV-R0a: apply each mutation to a copy of the tree and run tests/test_dev_r0a.py; every one must be killed.

    python tests/mutations_dev_r0a.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 ("report takes the first turn", "ga/bridge/__init__.py", "for t in reversed(turns):", "for t in turns:"),
 ("answering takes the first name", "ga/l0.py", "return names[-1] if names else None", "return names[0] if names else None"),
 ("ledger row keeps the configured rung", "ga/act/loop.py", 'row["model"] = self.answered = l0.answering(served)', "self.answered = l0.answering(served)"),
 ("telemetry names the chain", "ga/act/loop.py", "model=l0.answering(served), seconds=float(secs)", "model=served, seconds=float(secs)"),
 ("LLM event keeps the configured rung", "ga/act/loop.py", "model=row[\"model\"])", "model=self.model)"),
 ("task event keeps the configured rung", "ga/act/loop.py", '"model": self.answered or self.model,', '"model": self.model,'),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_dev_r0a"], cwd=tmp, capture_output=True, text=True, timeout=900)
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
