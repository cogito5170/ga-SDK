"""CMD-GA33 D2: apply each mutation to a copy of the tree and run tests/test_ga33.py; every one must be killed.

    python tests/mutations_ga33.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 ("live cap ignored", "ga/net/pool.py",
  'if len(reg["live"]) >= int(self.conf["max_live"]) or len(started)', "if len(started)"),
 ("retire overwrites role State", "ga/net/pool.py", "merge_state(rs, ns, node)\n            _atomic", "rs = ns\n            _atomic"),
 ("retired node's facts lost", "ga/net/pool.py", "            merge_state(rs, ns, node)\n", "            pass\n"),
 ("child work beyond max_depth accepted", "ga/net/pool.py", 'if depth >= int(self.conf["max_depth"]):', "if False:"),
 ("spawn with the runs budget exhausted", "ga/net/pool.py",
  'if reg["attempts"].get(item["id"], 0) >= self.limit(role):', "if False:"),
 ("one item started twice after a restart", "ga/net/pool.py",
  'claimed = {v["item"]["id"] for v in (reg or self.reg())["live"].values()}', "claimed = set()"),
 ("a node writing outside its dir", "ga/net/node.py", "if self.dir.resolve() not in p.parents:", "if False:"),
 ("pool peers ignored by pi", "ga/net/pool.py",
  '        names |= set((_read(Path(ga_dir) / "pool.json") or {}).get("live", {}))', "        pass"),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga33"], cwd=tmp, capture_output=True, text=True)
    last = r.stderr.strip().splitlines()[-1]
    killed = r.returncode != 0
    ok &= killed
    print(("KILLED  " if killed else "SURVIVED") + f"  {name}: {last}")
    shutil.rmtree(tmp)
sys.exit(0 if ok else 1)
