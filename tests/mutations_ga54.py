"""CMD-GA54 D1: apply each mutation to a copy of the tree and run tests/test_ga54_project.py; every one must be killed.

    python tests/mutations_ga54.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 ("--yes check removed", "ga/project/cli.py", "        if not _tty(stdin):\n            print(\"ga project init --yes",
  "        if False:\n            print(\"ga project init --yes"),
 ("secret validation removed", "ga/project/schema.py", "        if secrets_in(s):\n", "        if False:\n"),
 ("ff-only replaced by reset", "ga/project/core.py", '"merge", "--ff-only", f"origin/{branch}"',
  '"reset", "--hard", f"origin/{branch}"'),
 ("raw shell routine allowed", "ga/project/schema.py", "    if SHELLISH.search(a) or \"/\" in a:\n        return",
  "    if False:\n        return"),
 ("file mode not 0600", "ga/project/schema.py", "    os.chmod(tmp, 0o600)\n", "    os.chmod(tmp, 0o644)\n"),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga54_project"], cwd=tmp, capture_output=True, text=True,
                       timeout=900)
    shutil.rmtree(tmp, ignore_errors=True)
    return r.returncode, (r.stderr.strip().splitlines() or ["?"])[-1]


code, last = run("control")
print(f"control (no mutation): {'green' if code == 0 else 'RED'} ({last})", flush=True)
if code != 0:
    sys.exit(1)
alive = 0
for name, f, old, new in M:
    code, last = run(name, f, old, new)
    alive += code == 0
    print(f"{name}: {'KILLED' if code else 'SURVIVED'} ({last})", flush=True)
print(f"{len(M) - alive}/{len(M)} killed")
sys.exit(1 if alive else 0)
