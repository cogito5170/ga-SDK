"""CMD-OPS2 D2: apply each mutation to a copy of the tree and run tests/test_ops2.py; every one must be killed.

    python tests/mutations_ops2.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
C = "ga/vm/core.py"
K = "ga/console/config.py"
M = [
 ("console binds 0.0.0.0", C, '--port {PORT} --no-open\\n"', '--port {PORT} --no-open --host 0.0.0.0\\n"'),
 ("token-web binds 0.0.0.0", K, '"--hostname", "127.0.0.1", "--port", "3000"', '"--hostname", "0.0.0.0", "--port", "3000"'),
 ("adopt skipped (old directives re-run)", C, "    for p in new:\n        box.mark_read(name, p)\n", "    for p in []:\n        box.mark_read(name, p)\n"),
 ("enable-bridge without --yes", C, "    if not yes:\n", "    if False:\n"),
 ("enable-bridge without the adopt marker", C, '    if not (home / ".ga" / ADOPTED).is_file():\n', '    if False:\n'),
 (".env written by the installer", C, '    """What the user must set up (never the installer): True = missing."""\n',
  '    """What the user must set up (never the installer): True = missing."""\n    (home / "token" / ".env").parent.mkdir(parents=True, exist_ok=True)\n    (home / "token" / ".env").write_text("X=1\\n")\n'),
 ("ask.json overwritten", C, "            if not ask.exists():", "            if True:"),
 ("sudo in an argv", C, 'r.run(["pg_isready"', 'r.run(["sudo", "pg_isready"'),
 ("disk guard skipped before npm ci", C, '            guard("before npm ci")\n', '            pass\n'),
 ("disk guard skipped after npm ci", C, '            guard("after npm ci")\n', '            pass\n'),
 (".env.example values printed", C, "            names.append(k.strip())", "            names.append(line)"),
 ("agy agents run without agy", C, "    if not agy:\n", "    if False:\n"),
 ("install enables the bridge", C, '            act(f"systemctl --user enable --now {CONSOLE_UNIT}")\n',
  '            act(f"systemctl --user enable --now {CONSOLE_UNIT}")\n            sc("enable", "--now", BRIDGE_UNIT)\n'),
 ("bridge unit without Restart=always", C, 'Restart=always\\nRestartSec=30', 'RestartSec=30'),
 ("console starts a disabled service", "ga/console/services.py", '        if spec.get("disabled"):\n', '        if False:\n'),
 ("url takes the oldest URL", C, "say(f\"console: {found[-1]}\")", "say(f\"console: {found[0]}\")"),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ops2"], cwd=tmp, capture_output=True, text=True, timeout=900)
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
