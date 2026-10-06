"""CMD-GA52 D1: apply each mutation to a copy of the tree and run tests/test_ga52_cloud.py; every one must be killed.

    python tests/mutations_ga52.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
CL = "ga/console/cloud.py"
SV = "ga/console/server.py"
JS = "ga/console/static/app.js"
M = [
 ("worktree read instead of ref", CL, "            text = git(repo, \"show\", f\"{sha}:{self.s['path']}\") if sha else None\n",
  "            text = None\n"),
 ("stale threshold ignored", CL, '"stale": age > STALE_S,', '"stale": False,'),
 ("whitelist removed", CL, "    return {k: out[k] for k in FIELDS}\n", "    return {**raw, **out}\n"),
 ("SSE sent without change", SV, '        if fp["cloud"] != old["cloud"]:', "        if True:"),
 ("text inserted as HTML", JS, 's.detail ? h("p", { class: "say" }, s.detail) : null,',
  's.detail ? h("p", { class: "say", prop: { innerHTML: s.detail } }) : null,'),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga52_cloud"], cwd=tmp, capture_output=True, text=True, timeout=900)
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
