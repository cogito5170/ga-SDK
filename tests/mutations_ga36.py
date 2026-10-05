"""CMD-GA36 D4 + D5: apply each mutation to a copy of the tree and run tests/test_ga36.py and test_ga36_bridge.py;
every one must be killed.

    python tests/mutations_ga36.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 # D4
 ("routing by a model", "ga/ask/intents.py", "    s = scores(text)\n",
  '    s = scores(text)\n    __import__("ga.ask.model", fromlist=["one_turn"]).one_turn(text)\n'),
 ("confirmation skipped", "ga/ask/__init__.py", 'return cost == "model" or (cost == "mail" and not yes)', "return False"),
 ("cap ignored", "ga/ask/store.py", "return max(self.cap - self.used(), 0)", "return 10 ** 6"),
 ("bound to 0.0.0.0", "ga/ui/__init__.py", 'BIND = "127.0.0.1"', 'BIND = "0.0.0.0"'),
 ("token not checked", "ga/ui/__init__.py",
  "return bool(got) and hmac.compare_digest(got.encode(), self.server.token.encode())", "return True"),
 ("Origin not checked", "ga/ui/__init__.py", "if origin is not None and origin not in self.server.origins:", "if False:"),
 ("secret shown", "ga/runlog.py", "return WITHHELD if secrets_in(text) else text", "return text"),
 ("waiting loop calling the model", "ga/runlog.py", "        sleep(poll_s)\n",
  '        sleep(poll_s)\n        __import__("ga.ask.model", fromlist=["one_turn"]).one_turn("status?")\n'),
 # D5
 ("solve: totals != turn sums", "ga/runlog.py", 'sum(t["input"] or 0 for t in self.turns)',
  'sum(t["input"] or 0 for t in self.turns[1:])'),
 ("solve: cap ignored", "ga/ask/solve.py", '"max_model_steps": int(cap)', '"max_model_steps": 20'),
 ("solve: non-table tool executed (the project's own tool table handed to the loop)", "ga/ask/solve.py",
  '"tools": dict(TOOLS),',
  '"tools": {**TOOLS, **json.loads(Path("ga-supervise.json").read_text()).get("tools", {})},'),
 ("solve: plan check lets a non-table tool through", "ga/gemini.py", 'if s.get("tool") not in tools:', "if False:"),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga36", "tests.test_ga36_bridge"], cwd=tmp,
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
