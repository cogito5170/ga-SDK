"""CMD-GA32 D1: apply each mutation to a copy of the tree and run tests/test_ga32_net.py; every one must be killed.

    python tests/mutations_ga32.py      (needs ga-sdk[net]; not part of the unittest discovery)

The first four are 'fallback used when the extra is installed' for each seam (enabled, pi, l0 events, dc purpose);
the last two break the fallback itself, which the NET variants alone would not see.
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
M = [
 ("fallback used when the extra is installed (real.enabled)", "ga/net/real.py",
  '    return os.environ.get("GA_NET", "") != "fallback" and importable()\n', "    return False\n"),
 ("fallback used when the extra is installed (pi)", "ga/net/pi.py",
  "    if not real.enabled():\n        return None\n", "    return None\n"),
 ("fallback used when the extra is installed (l0 peer events)", "ga/l0.py",
  "if real.enabled() and schema is not None:", "if False:"),
 ("fallback used when the extra is installed (dc purpose)", "ga/net/dc.py",
  "    if real.enabled():\n        from dc.peer", "    if False:\n        from dc.peer"),
 ("fallback activation always on", "ga/net/pi.py",
  "    if pi_ij >= cfg.theta:\n        return True, \"pi\"\n    covers = covers or {}", "    if True:\n        return True, \"pi\"\n    covers = covers or {}"),
 ("fallback pi ignores relevance", "ga/net/pi.py",
  "    r = len(req & set(exported)) / max(1, len(req))\n", "    r = 1.0\n"),
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
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga32_net"], cwd=tmp, capture_output=True, text=True)
    last = r.stderr.strip().splitlines()[-1]
    killed = r.returncode != 0
    ok &= killed
    print(("KILLED  " if killed else "SURVIVED") + f"  {name}: {last}")
    shutil.rmtree(tmp)
sys.exit(0 if ok else 1)
