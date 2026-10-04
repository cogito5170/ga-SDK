"""CMD-GA31 D2: apply each mutation to a copy of the tree and run tests/test_ga31.py; every one must be killed.

    python tests/mutations_ga31.py      (not part of the unittest discovery: it copies the tree once per mutation)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 ("peer message treated as a directive", "ga/net/node.py",
  "            head, item, probs = msg.parse(m.text)\n",
  "            head, item, probs = msg.parse(m.text)\n            if head and head.get('schema') == 'directive/2':\n                self.task = dict(head, uses=[]); self.required = []\n"),
 ("node writes hub state.json", "ga/net/node.py",
  "        self.dir = self.ga_dir / NODES / me\n", "        self.dir = self.ga_dir\n"),
 ("send below theta without policy or verify", "ga/net/node.py",
  '            self._out.setdefault("dropped", []).append({"to": to, "why": f"below theta ({p:.3f} < {self.netcfg.theta})"})\n            return\n',
  '            why = "pi"\n'),
 ("peer opinion writes State", "ga/net/state.py",
  '        if k == "opinion":\n',
  '        if k == "opinion":\n            return self.observe(item["ref"], item["value"], msg_id, f"session:{sender}", required)\n'),
 ("continuation after no progress", "ga/net/checkpoint.py",
  "        if self.keys and self.keys[-1] == key:\n", "        if False:\n"),
 ("router picks a model lacking a needed capability", "ga/net/router.py",
  'return (set(needs.get("capabilities", [])) <= set(e.get("capabilities", []))', "return (True"),
 ("served-model mismatch accepted", "ga/net/router.py",
  "        check_served(list(served or []), choice.model)\n", "        return None\n"),
 ("ga usage misses a context alarm (threshold)", "ga/net/usage.py",
  "            if c > ctx_max:\n", "            if c > ctx_max * 2:\n"),
 ("ga usage misses a context alarm (cache not counted)", "ga/net/usage.py",
  '    parts = [d.get(k) for k in ("reported_input_tokens", "reported_cache_read_input_tokens",\n                                "reported_cache_creation_input_tokens")]\n',
  '    parts = [d.get(k) for k in ("reported_input_tokens",)]\n'),
 ("send below theta (activation always on)", "ga/net/pi.py",
  "    if pi_ij >= cfg.theta:\n", "    if True:\n"),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga31"], cwd=tmp, capture_output=True, text=True)
    last = r.stderr.strip().splitlines()[-1]
    killed = r.returncode != 0
    ok &= killed
    print(("KILLED  " if killed else "SURVIVED") + f"  {name}: {last}")
    shutil.rmtree(tmp)
sys.exit(0 if ok else 1)
