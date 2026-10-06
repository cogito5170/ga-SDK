"""CMD-GA44 D2: apply each mutation to a copy of the tree and run tests/test_ga44.py; every one must be killed, and
the unmutated copy must pass (the control).

    python tests/mutations_ga42.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [  # (name, file, [(old, new), ...])
 ("--agent dropped from argv", "ga/ask/model.py",
  [('["agy", "--agent", s.get("ask_agent") or ASK_AGENT]', '["agy"]')]),
 ("setup-facts intent falls through to the model", "ga/ask/intents.py",
  [('Intent("setup", "free",', 'Intent("setup_off", "free",')]),
 ("warning threshold ignored", "ga/ask/model.py", [("    if inp > WARN_INPUT:", "    if False:")]),
 ("missing-agent check skipped", "ga/ask/__init__.py", [("if M.agent_installed(name) is False:", "if False:")]),
 ("IME composition check removed", "ga/console/static/app.js",
  [('!e.shiftKey && !e.isComposing && e.keyCode !== 229', '!e.shiftKey')]),
 ("confirm skipped for ga do", "ga/console/server.py",
  [('if mode == "ask" and "model" in job and not self.confirm_model_turns():', 'if not self.confirm_model_turns():')]),
 ("unknown-agent answer not recognised", "ga/ask/model.py", [('if p.returncode != 0 and UNKNOWN_AGENT.search', 'if False and UNKNOWN_AGENT.search')]),
]


def run(mutate):
    tmp = Path(tempfile.mkdtemp())
    try:
        for d in ("ga", "tests"):
            shutil.copytree(ROOT / d, tmp / d)
        shutil.copy(ROOT / "pyproject.toml", tmp / "pyproject.toml")
        mutate(tmp)
        r = subprocess.run([PY, "-m", "unittest", "tests.test_ga44", "tests.test_ga44_console"], cwd=tmp,
                           capture_output=True, text=True)
        return r.returncode, (r.stderr.strip().splitlines() or ["?"])[-1]
    finally:
        shutil.rmtree(tmp)


code, last = run(lambda tmp: None)
print(("control OK" if code == 0 else "CONTROL FAILED") + f"  {last}")
ok = code == 0
for name, f, reps in M:
    def mutate(tmp, f=f, reps=reps, name=name):
        p = tmp / f
        s = p.read_text()
        for old, new in reps:
            assert s.count(old) == 1, (name, old, s.count(old))
            s = s.replace(old, new)
        p.write_text(s)
    code, last = run(mutate)
    killed = code != 0
    ok &= killed
    print(("KILLED  " if killed else "SURVIVED") + f"  {name}: {last}")
sys.exit(0 if ok else 1)
