"""DEV-VMSHA: apply each mutation to a copy of the tree and run tests/test_vmsha.py; every one must be killed.

    python tests/mutations_vmsha.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
C = "ga/vm/core.py"
M = [
 ("notice keyed on version only", C, 'if st.get("version") == ver and (st.get("sha") or "") == sha:', 'if st.get("version") == ver:'),
 ("notice never remembers the sha", C, 'json.dumps(dict(st, version=ver, sha=sha))', 'json.dumps(dict(st, version=ver))'),
 ("notice names a short sha", C, 'f"VM runs ga {ver} at {sha or \'-\'}; heads "', 'f"VM runs ga {ver} at {sha[:12] or \'-\'}; heads "'),
 ("sha question ignored", C, 'if ask == SHA_ASK:', 'if ask == "never":'),
 ("answered question stays unread", C, "        box.mark_read(NOTICE_FROM, m.path)\n", "        pass\n"),
 ("r0 runs every cycle", C, 'if not force and st.get("r0_sha") == sha:', 'if False:'),
 ("r0 never remembers its sha", C, 'json.dumps(dict(_noticed(home), r0_sha=sha))', 'json.dumps(dict(_noticed(home)))'),
 ("r0 report without counts", C, 'f"R0 tests at {sha}: {res}; exit {rc}; env {env}"', 'f"R0 tests at {sha}; exit {rc}; env {env}"'),
 ("r0 ask ignored", C, 'elif ask == R0_ASK:', 'elif ask == "never":'),
 ("a model backend imported", C, "def _commit_url(", "from .. import backends  # noqa\n\n\ndef _commit_url("),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_vmsha"], cwd=tmp, capture_output=True, text=True, timeout=900)
    shutil.rmtree(tmp, ignore_errors=True)
    return r.returncode, (r.stderr.strip().splitlines() or ["?"])[-1]


code, last = run("control")
print(f"control (no mutation): {'green' if code == 0 else 'RED'} ({last})", flush=True)
if code != 0:
    sys.exit(2)
ok = True
for name, f, old, new in M:
    code, last = run(name, f, old, new)
    ok &= code != 0
    print(f"{'killed' if code != 0 else 'SURVIVED'}: {name} ({last})", flush=True)
print("all killed" if ok else "SOME SURVIVED")
sys.exit(0 if ok else 1)
