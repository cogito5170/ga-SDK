"""CMD-CON2 D2: apply each mutation to a copy of the tree and run tests/test_console_api.py and
test_console_services.py; every one must be killed.

    python tests/mutations_con2.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 ("env value leaked in logs", "ga/console/services.py", "        for v in s.masks:\n            text = text.replace(v, MASK)\n",
  ""),
 ("env value leaked in logs (the loaded note names the values)", "ga/console/services.py",
  'self._note(s, f"console: env file loaded ({len(extra)} names, values not shown)")',
  'self._note(s, f"console: env file loaded ({extra})")'),
 ("service started through a shell", "ga/console/services.py",
  'proc = subprocess.Popen(list(spec["argv"]), cwd=spec.get("cwd") or None, env=env, shell=False,',
  'proc = subprocess.Popen(" ".join(spec["argv"]), cwd=spec.get("cwd") or None, env=env, shell=True,'),
 ("token unchecked", "ga/ui/__init__.py",
  "return bool(got) and hmac.compare_digest(got.encode(), self.server.token.encode())", "return True"),
 ("Origin unchecked", "ga/ui/__init__.py", "if origin is not None and origin not in self.server.origins:", "if False:"),
 ("bound to 0.0.0.0", "ga/ui/__init__.py", 'BIND = "127.0.0.1"', 'BIND = "0.0.0.0"'),
 ("bound to 0.0.0.0 (the console's own bind)", "ga/console/server.py", "super().__init__((BIND, int(port)), ConsoleHandler)",
  'super().__init__(("0.0.0.0", int(port)), ConsoleHandler)'),
 ("mailbox messages marked read by the console", "ga/console/collectors.py",
  "                self._heads[path] = h\n",
  "                self._heads[path] = h\n                self.box.mark_read(recipient, path)\n"),
 ("a secret-looking line returned", "ga/console/collectors.py",
  "return WITHHELD if secrets_in(obj) else obj", "return obj"),
 ("a secret-looking log line returned", "ga/console/services.py", "        text = redact(text)\n", ""),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_console_api", "tests.test_console_services"], cwd=tmp,
                       capture_output=True, text=True, timeout=900)
    shutil.rmtree(tmp, ignore_errors=True)
    return r.returncode, (r.stderr.strip().splitlines() or ["?"])[-1]


code, last = run("control")  # the unmutated copy must pass, or a kill means nothing
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
