"""CMD-GA42 D2: apply each mutation to a copy of the tree and run tests/test_ga42*.py; every one must be killed, and
the unmutated copy must pass (the control).

    python tests/mutations_ga42.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [  # (name, file, [(old, new), ...])
 ("ACCEPT integrates despite a failed judge", "ga/hub.py",
  [('if decision == "ACCEPT" and not (j.cls == "success" and not j.needs):', 'if False:')]),
 ("integration push with --force", "ga/judge.py",
  [('git(repo, "push", "--quiet", remote,', 'git(repo, "push", "--force", "--quiet", remote,')]),
 ("baseline push with --force", "ga/hub.py",
  [('"push", "-q", self.conf.get("baseline_remote", "origin"), "HEAD"]',
    '"push", "-q", "--force", self.conf.get("baseline_remote", "origin"), "HEAD"]')]),
 ("approval accepted from model text", "ga/actions/registry.py",
  [('        out.append(propose({**p, "name": m.group(1)}, root, source=source, h=h, run_trial=run_trial))\n',
    '        out.append(propose({**p, "name": m.group(1)}, root, source=source, h=h, run_trial=run_trial))\n'
    '        if "APPROVE " + m.group(1) in text and out[-1]["check"]["ok"]:\n'
    '            approve(m.group(1), "model", h=h)\n')]),
 ("approval accepted without a person at a TTY", "ga/actions/cli.py",
  [("if not (hasattr(stdin, \"isatty\") and stdin.isatty()):", "if False:")]),
 ("shell metacharacters allowed", "ga/actions/check.py",
  [("meta = sorted({c for c in rest if c in META})", "meta = []")]),
 ("secret path allowed", "ga/actions/check.py",
  [("    return secret_path(rel) or any(p in SECRET_DIRS for p in parts)", "    return False")]),
 ("registry hash not checked", "ga/actions/registry.py",
  [('    if not isinstance(e, dict) or e.get("sha256") != digest(e):', '    if not isinstance(e, dict):'),
   ('if NAME.match(n) and isinstance(e, dict) and e.get("sha256") == digest(e)}', 'if NAME.match(n) and isinstance(e, dict)}')]),
 ("tick writes when nothing is new", "ga/hub.py",
  [("        if not msgs:\n            res.quiet = True\n",
    "        if not msgs:\n            self.save_state({**st, \"ticks\": st.get(\"ticks\", 0) + 1})\n            res.quiet = True\n")]),
 ("send-back cap ignored", "ga/hub.py",
  [('st["sendbacks"].get(did, 0) >= int(self.conf.get("sendback_cap", SENDBACK_CAP))', 'st["sendbacks"].get(did, 0) >= 10**9')]),
 ("policy auto-approves a prefix match", "ga/actions/registry.py",
  [("if argv[:len(pre)] != pre or any(a not in allowed for a in argv[len(pre):]):", "if argv[:len(pre)] != pre:")]),
 ("approved script hash not checked", "ga/actions/registry.py", [("    if changed:\n        raise ActionError", "    if False:\n        raise ActionError")]),
 ("revoke keeps the entry", "ga/actions/registry.py", [("    del reg[name]\n", "    pass\n")]),
]


def run(mutate):
    tmp = Path(tempfile.mkdtemp())
    try:
        for d in ("ga", "tests"):
            shutil.copytree(ROOT / d, tmp / d)
        shutil.copy(ROOT / "pyproject.toml", tmp / "pyproject.toml")
        mutate(tmp)
        r = subprocess.run([PY, "-m", "unittest", "tests.test_ga42", "tests.test_ga42_actions"], cwd=tmp,
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
