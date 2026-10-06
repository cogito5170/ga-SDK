"""CMD-GA57 (ga ops tick): apply each mutation to a copy of the tree and run tests/test_ga57_ops.py; every one must be killed.

    python tests/mutations_ga57.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
C = "ga/ops/core.py"
M = [
 ("medium runs without alert", C, '{"low": "run", "medium": "run+alert"}', '{"low": "run", "medium": "run"}'),
 ("high risk runs", C, '.get(spec.get("risk"), "blocked")', '.get(spec.get("risk"), "run")'),
 ("blocked only on the third failure", C, 'if st["failures"][sig] >= 2:', 'if st["failures"][sig] >= 3:'),
 ("alert not once per kind per day", C, 'if st["alerts"].get(kind) == self.day():', 'if False:'),
 ("retry on the same rung", C, "j = LADDER.index(last) + 1 if last in LADDER else 0", "j = LADDER.index(last) if last in LADDER else 0"),
 ("verify ignores the window", C, 'if why is None and self.clock() < p["deadline"]:', 'if why is None:'),
 ("model asked although a rule matches", C, "            if rule is not None:\n", "            if False:\n"),
 ("card not capped", C, "card = body if len(raw) <= cap else", "card = body if True else"),
 ("model turns per tick unbounded", C, "if self.model_turns >= MODEL_TURNS_PER_TICK:", "if False:"),
 ("dearest working rung", C, "return best[0] if best else", "return best[-1] if best else"),
 ("survivor line off by one", C, 'count("\\n") + 1)', 'count("\\n"))'),
 ("first shadow row per mail", C, 'last[r["mail"]] = r', 'last.setdefault(r["mail"], r)'),
 ("ancestry inverted", C, "{0: True, 1: False}", "{0: False, 1: True}"),
 ("dry run writes state", C, "        if not dry_run:\n            self.save_state(st)", "        if True:\n            self.save_state(st)"),
 ("model ask with tokens is an anomaly", C, 'and (o.get("tokens_in") is None or o.get("error") is not None)', "and True"),
 ("blocked subject acted on again", C, ' or s in st["blocked"] or ', " or "),
 ("ledger row without tokens", C, '"input": u.get("input"), "output": u.get("output"),', '"input": None, "output": None,'),
 ("vm unit without ops tick", "ga/vm/core.py", "ExecStart={py} -m ga ops tick", "ExecStartX={py} -m ga ops tick"),
 ("no alert notify kind", "ga/forms/kinds.py", '"shadow", "alert")', '"shadow")'),
 ("send-back ask without the head", C, "(head {str(o.get('sha'))[:12]} does not descend", "(head does not descend"),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga57_ops"], cwd=tmp, capture_output=True, text=True, timeout=900)
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
