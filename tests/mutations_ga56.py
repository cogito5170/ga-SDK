"""CMD-GA56 (the daily turn cap): apply each mutation to a copy of the tree and run tests/test_ga56_cap.py; every one must be killed.

    python tests/mutations_ga50.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 ("count every row", "ga/hub.py", 'r.get("at", "").startswith(self.today()) and any(pos((r.get("tokens") or {}).get(k)) for k in ("input", "output"))', 'r.get("at", "").startswith(self.today())'),
 ("count every ledger line", "ga/hub.py", '            n += any(pos(r.get(k)) for k in ("input", "output"))', '            n += 1'),
 ("alert every tick", "ga/hub.py", '        if st.get("cap_alerted") == day:\n            return\n', ''),
 ("no alert", "ga/hub.py", '                    self._cap_alert(res, limit, waiting)\n', '                    pass\n'),
 ("alert to own inbox", "ga/hub.py", '        if to == self.name:  # never into the hub\'s own inbox\n            return\n        st = self.load_state()', '        st = self.load_state()'),
 ("alert not retried (marked before mail)", "ga/hub.py", '        try:\n            self._mail(to, dump_wire(head))', '        st["cap_alerted"] = day\n        self.save_state(st)\n        try:\n            self._mail(to, dump_wire(head))'),
 ("console cap hidden", "ga/console/collectors.py", '"cap": hub_cap(cfg), ', ''),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga56_cap"], cwd=tmp, capture_output=True, text=True, timeout=900)
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
