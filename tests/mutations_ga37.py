"""CMD-GA37 D2 and D4 (rev 2): apply each mutation to a copy of the tree and run tests/test_ga37.py; every one must be killed.

    python tests/mutations_ga37.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 ("vague-word check removed", "ga/forms/task.py",
  "if not isinstance(text, str) or _MEASURE.search(text):", "if True:"),
 ("repair turns unbounded", "ga/intake/engine.py", "MAX_REPAIRS = 1\n", "MAX_REPAIRS = 5\n"),
 ("non-needs question passed to the human", "ga/forms/task.py",
  'Field("needs", one_of(*NEEDS))', 'Field("needs", is_str)'),
 ("prompt differing per backend", "ga/intake/engine.py",
  "whole = prompt.whole(text)", "whole = prompt.whole(text) + f\"\\n(backend: {self.backend})\""),
 ("summary over the cap", "ga/intake/summary.py",
  "while tokens(text) > cap and lines:", "while False and lines:"),
 # rev 2 (D4)
 ("state summary missing from the prompt", "ga/intake/engine.py",
  "summarize(root, state=state.lines() if state is not None else None)", "summarize(root, state=None)"),
 ("fragment turned into a human question", "ga/intake/engine.py",
  'spec["assumptions"].append(dict(resolution.assumption))',
  'spec["questions"].append({"text": resolution.assumption["text"], "needs": "irreversible"})'),
 ("answer routed to the planner", "ga/intake/route.py",
  "    if kind in WORK_KINDS:", '    if kind in WORK_KINDS + ("answer",):'),
]
ok = True
for name, f, old, new in M:
    tmp = Path(tempfile.mkdtemp())
    for d in ("ga", "tests"):
        shutil.copytree(ROOT / d, tmp / d)
    p = tmp / f
    s = p.read_text()
    assert s.count(old) == 1, (name, s.count(old))
    p.write_text(s.replace(old, new))
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga37"], cwd=tmp, capture_output=True, text=True)
    last = r.stderr.strip().splitlines()[-1]
    killed = r.returncode != 0
    ok &= killed
    print(("KILLED  " if killed else "SURVIVED") + f"  {name}: {last}")
    shutil.rmtree(tmp)
sys.exit(0 if ok else 1)
