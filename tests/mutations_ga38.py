"""CMD-GA38 D2: apply each mutation to a copy of the tree and run tests/test_ga38.py; every one must be killed.

    python tests/mutations_ga38.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [  # (name, file, [(old, new), ...])
 ("history appended to the card", "ga/act/loop.py",
  [("self.last = self._last_text(out, p, notes)", 'self.last = (self.last + "\\n" if self.last else "") + self._last_text(out, p, notes)')]),
 ("ownership check removed", "ga/act/apply.py", [("if not owned(a.arg, globs):", "if False:")]),
 ("SEARCH matched more than once", "ga/act/apply.py", [("if len(hits) != 1:", "if not hits:")]),
 ("SEARCH matched fuzzily (whitespace)", "ga/act/apply.py",
  [("    out, i = [], text.find(search)", "    search = search.strip()\n    out, i = [], text.find(search)")]),
 ("SEARCH matched fuzzily (case)", "ga/act/apply.py",
  [("    out, i = [], text.find(search)", "    text, search = text.lower(), search.lower()\n    out, i = [], text.find(search)")]),
 ("RUN executing an unlisted command", "ga/act/loop.py",
  [("if a.arg not in self.commands:", "if False:"),
   ("r = K.run(a.arg, self.commands[a.arg],", "r = K.run(a.arg, self.commands.get(a.arg, [a.arg]),")]),
 ("RUN through a shell", "ga/act/commands.py",
  [("p = subprocess.run(argv, cwd=", 'p = subprocess.run(" ".join(argv), cwd='), ("shell=False)", "shell=True)")]),
 ("DONE trusted without running done_when", "ga/act/loop.py",
  [('                if m.ok:\n                    status, reason = "done", "DONE accepted',
    '                if True:\n                    status, reason = "done", "DONE accepted')]),
 ("progress check removed", "ga/act/loop.py", [("if stall >= NO_PROGRESS_TURNS:", "if False:")]),
 ("card cap ignored", "ga/act/card.py", [("    kept = list(units)\n", "    cap = 10 ** 9\n    kept = list(units)\n")]),
 ("secret env passed to commands", "ga/act/commands.py", [("        env = clean_env()\n", "        env = dict(os.environ)\n")]),
 (".env / secret-file check removed", "ga/act/apply.py",
  [('return any(x in (".git", ".ga") or SECRET_FILE.match(x) for x', 'return any(x in (".git", ".ga") for x')]),
 ("card redaction removed", "ga/act/loop.py", [("                u.text, k = C.redact(u.text)", "                k = 0")]),
]
ok = True
for name, f, reps in M:
    tmp = Path(tempfile.mkdtemp())
    for d in ("ga", "tests"):
        shutil.copytree(ROOT / d, tmp / d)
    p = tmp / f
    s = p.read_text()
    for old, new in reps:
        assert s.count(old) == 1, (name, old, s.count(old))
        s = s.replace(old, new)
    p.write_text(s)
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga38"], cwd=tmp, capture_output=True, text=True)
    last = r.stderr.strip().splitlines()[-1]
    killed = r.returncode != 0
    ok &= killed
    print(("KILLED  " if killed else "SURVIVED") + f"  {name}: {last}")
    shutil.rmtree(tmp)
sys.exit(0 if ok else 1)
