"""CMD-GA48 D2: apply each mutation to a copy of the tree and run tests/test_ga48.py; every one must be killed.

    python tests/mutations_ga48.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
C = "ga/vm/core.py"
M = [
 ("pip on every run", C, "    if py.exists() and have == want and (not by_head or have_head == head):\n        return False\n", "    if False:\n        return False\n"),
 ("restart when nothing changed", C, "    if rc == 0 and sdk_changed:\n", "    if rc == 0:\n"),
 ("update a dirty checkout", C, "            plan_repo(r, repo)\n            if dry_run:", "            if dry_run:"),
 ("accept a non-ff history (reset --hard)", C, '                sync_repo(r, repo, "", branch, "update", lambda m: None)\n',
  '                r.run(["git", "-C", str(repo), "fetch", "origin", branch])\n                r.run(["git", "-C", str(repo), "reset", "--hard", f"origin/{branch}"])\n'),
 ("ack mailed twice", C, '    if last == ver:\n        return "same"\n', '    if False:\n        return "same"\n'),
 ("disabled unit restarted", C, "            if r.run([\"systemctl\", \"--user\", \"is-enabled\", unit], timeout=30)[0] == 0:  # never a disabled unit\n", "            if True:\n"),
 ("baseline change restarts", C, "    sdk_changed = before.get(\"ga-sdk\") != after.get(\"ga-sdk\")\n", "    sdk_changed = before != after\n"),
 ("disk guard after fetch", C, "    if not d[\"ok\"]:\n        say(f\"ga vm update: disk guard", "    if False:\n        say(f\"ga vm update: disk guard"),
 ("timer not enabled by --full", C, "    if full and not dry_run and sc(\"is-enabled\", UPDATE_TIMER)[0] != 0:", "    if False:"),
 ("mail failure marked noticed", C, "    except MailError as e:\n        return f\"failed: {str(e)[:120]}\"\n    _write(f,", "    except MailError as e:\n        _write(f, json.dumps({\"version\": ver}) + \"\\n\", lambda m: None)\n        return f\"failed: {str(e)[:120]}\"\n    _write(f,"),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga48"], cwd=tmp, capture_output=True, text=True, timeout=900)
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
