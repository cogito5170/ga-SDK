"""CMD-GA41 D2: apply each mutation to a copy of the tree and run tests/test_ga41.py; every one must be killed, and the
unmutated copy must pass (the control).

    python tests/mutations_ga41.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [  # (name, file, [(old, new), ...])
 ("repair turn removed (supervise)", "ga/gemini.py", [("        if probs:  # GA41 S1", "        if False:  # GA41 S1")]),
 ("repair turn removed (act)", "ga/act/loop.py", [("if not err and _broken(answer):", "if False:")]),
 ("repair repeats the task (supervise)", "ga/gemini.py",
  [('prompt = repair.plan_prompt(probs, self.cfg.plan_schema, turn.text or "")',
    'prompt = prompt + "\\n" + repair.plan_prompt(probs, self.cfg.plan_schema, turn.text or "")')]),
 ("repair repeats the task (act)", "ga/act/loop.py",
  [("text = repair.prompt(labels, FORM, answer)", "text = cd.text + repair.prompt(labels, FORM, answer)")]),
 ("unlimited repairs", "ga/gemini.py",
  [("        if probs:  # GA41 S1", "        while probs:  # GA41 S1"),
   ('            if probs:\n                self.log("plan", step=sid, ok=False, problems=",".join(probs[:5])[:200], repaired=False)',
    '            if False:\n                self.log("plan", step=sid, ok=False, problems=",".join(probs[:5])[:200], repaired=False)')]),
 ("tool error still fails the task", "ga/gemini.py",
  [("                label = tool_error_label(e)\n", "                raise\n")]),
 ("transient retried more than once", "ga/backends/base.py",
  [("        sleep(float(backoff_s))\n    return call()\n",
    "        sleep(float(backoff_s))\n    try:\n        return call()\n    except Transient:\n        sleep(float(backoff_s))\n"
    "    return call()\n")]),
 ("transient never retried", "ga/backends/base.py", [("        sleep(float(backoff_s))\n    return call()\n", "        raise\n")]),
 ("transient reason hidden (failed as the type name)", "ga/gemini.py",
  [("                self._reasons[sid] = e.reason  # the step fails as transient:<code>", "                pass")]),
 ("exit-0 status ERROR treated as success", "ga/adapters/agy_cli.py",
  [('out.agy_error = "AGY_ERROR" in plain or out.status_error', 'out.agy_error = "AGY_ERROR" in plain'),
   ("            if o.status_error and code == 0:\n                raise", "            if False:\n                raise")]),
 ("--agent dropped", "ga/adapters/agy_cli.py", [('head = ["--agent", self.agent] if self.agent else []', "head = []")]),
 ("agy plugin install through a shell", "ga/backends/agy_agent.py",
  [("r = run(cmd, stdin=", 'r = run(" ".join(cmd), shell=True, stdin=')]),
 ("paths compared unresolved", "ga/hub.py",
  [('"protect": list(dict.fromkeys(real(p) for p in protect)), "writable": [real(p) for p in writable]',
    '"protect": [str(p) for p in protect], "writable": [str(p) for p in writable]'),
   ("        self.ga = real_path(ga_dir)", "        self.ga = Path(os.path.abspath(ga_dir))")]),
 ("tool error result carries the raw exception text", "ga/gemini.py",
  [("value = f\"{rec['tool']} failed: {label}\"\n", "value = f\"{rec['tool']} failed: {label}: {e}\"\n")]),
 ("tool error label not redacted", "ga/gemini.py", [("    return redact(text)[0][:200]", "    return text[:200]")]),
 ("quota treated as transient", "ga/adapters/agy_cli.py",
  [("        if o.quota:\n            raise AgyQuota(\"quota\")", "        if False:\n            raise AgyQuota(\"quota\")"),
   ("            c = transient_code(o.error_text", "            c = 500 if o.quota else transient_code(o.error_text")]),
]


def run(mutate):
    tmp = Path(tempfile.mkdtemp())
    try:
        for d in ("ga", "tests"):
            shutil.copytree(ROOT / d, tmp / d)
        shutil.copy(ROOT / "pyproject.toml", tmp / "pyproject.toml")  # tests.test_ga41.Version reads it
        mutate(tmp)
        r = subprocess.run([PY, "-m", "unittest", "tests.test_ga41"], cwd=tmp, capture_output=True, text=True)
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
