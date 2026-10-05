"""CMD-OPS1 D2: apply each mutation to a copy of the tree and run tests/test_ops1.py; every one must be killed.

    python tests/mutations_ops1.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
C = "ga/vm/core.py"
M = [
 ("installer calls sudo", C, '    if not verdict["linger"]:\n', '    if not verdict["linger"]:\n        r.run(["sudo", "loginctl", "enable-linger", "x"])\n'),
 ("disk guard skipped in install", C, '    if not verdict["ready"]:\n        say(json.dumps', '    if False:\n        say(json.dumps'),
 ("disk-only guard always passes", "ga/vm/cli.py", "return 0 if d[\"ok\"] else 1", "return 0"),
 ("console started with a non-loopback host", C, '--port {PORT} --no-open\\n"', '--port {PORT} --no-open --host 0.0.0.0\\n"'),
 ("second install rewrites files", C, 'bool:\n    if path.is_file() and path.read_text(encoding="utf-8") == text:\n        return False\n    act(f"write {path}")\n    path.parent',
  'bool:\n    if False:\n        return False\n    act(f"write {path}")\n    path.parent'),
 ("pip cache left", C, '"install", "--no-cache-dir", "-e"', '"install", "-e"'),
 ("pip TMPDIR left behind", C, "        shutil.rmtree(tmpdir, ignore_errors=True)\n", "        pass\n"),
 ("dirty checkout not stopping", C, "    if rc != 0 or out.strip():\n", "    if rc != 0:\n"),
 ("dirty/diverged checkout reset", C, '"merge", "--ff-only", f"origin/{branch}"', '"reset", "--hard", f"origin/{branch}"'),
 ("a credential file read", C, "    r = runner or Runner()\n    problems: list[str] = []\n",
  "    r = runner or Runner()\n    problems: list[str] = []\n    try:\n        (home / \".git-credentials\").read_text()\n    except OSError:\n        pass\n"),
 ("hub enabled without --shadow", C, 'return rc == 0 and "--shadow" in out', "return True"),
]


def run(name, f=None, old=None, new=None):
    tmp = Path(tempfile.mkdtemp())
    for d in ("ga", "tests"):
        shutil.copytree(ROOT / d, tmp / d)
    shutil.copy(ROOT / "pyproject.toml", tmp / "pyproject.toml")
    if f is not None:
        p = tmp / f
        s = p.read_text()
        assert s.count(old) == 1, (name, s.count(old))
        p.write_text(s.replace(old, new))
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ops1"], cwd=tmp, capture_output=True, text=True, timeout=900)
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
