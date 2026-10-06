"""CMD-GA50 (the hub's surviving mutations g1 g2 g4 g6, and more): apply each mutation to a copy of the tree and run tests/test_ga50.py; every one must be killed.

    python tests/mutations_ga50.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 ("g1 recent limit uncapped", "ga/console/server.py", "limit=min(int(lim), 5000) if lim.isdigit() else 200,", "limit=int(lim) if lim.isdigit() else 200,"),
 ("g2 ring unbounded", "ga/console/server.py", "deque(maxlen=5000)", "deque()"),
 ("g4 broken secret rule leaks", "ga/events.py", 'return "(withheld)"', "pass"),
 ("g6 failed NEED counted done", "ga/act/loop.py", ' or " failed: " in got.splitlines()[0]', ""),
 ("metadata not cleaned", "ga/events.py", '"metadata": _fit(_clean(metadata or {}))}', '"metadata": metadata or {}}'),
 ("no rotation", "ga/events.py", "                _rotate(f)\n", "                pass\n"),
 ("lint exit not an error", "ga/act/loop.py", "            if r.code is None or not red_ok:\n", "            if r.code is None:\n"),
 ("prompt written to LLM event", "ga/act/loop.py", "        sp.update(\"SELECTING_TOOL\", actions=kinds)", "        sp.update(\"SELECTING_TOOL\", actions=kinds, answer=answer)"),
 ("host keeps credentials", "ga/mailbox.py", "(?:[^@/]*@)?([^/:]+)", "([^/]+)"),
 ("no child parent env", "ga/events.py", "        e[ENV_PARENT] = p\n", "        pass\n"),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga50"], cwd=tmp, capture_output=True, text=True, timeout=900)
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
