import subprocess, sys
from pathlib import Path
M = [
 ("resume leaks into fresh (runner)", "ga/adapters/headless.py", "if req.resume_id and not req.fresh:", "if req.resume_id:"),
 ("resume id kept in fresh (hub)", "ga/hub.py", "if result.session_id and runner is self.runner and not fresh:", "if result.session_id and runner is self.runner:"),
 ("pack over the cap", "ga/ctxpack.py", "while tokens(text) + reserve > cap:", "while False:"),
 ("directive head dropped", "ga/ctxpack.py", "if len(kept) == 1:", "if len(kept) == 0:"),
 ("drop not recorded", "ga/ctxpack.py", 'dropped.append({"part": name, "tokens": tokens(t)})', "pass"),
 ("cursor advanced before the post", "ga/hub.py", "        try:\n            p = self.channel.post(to, to, ans.report)", "        st.setdefault(\"ctx\", {}).setdefault(to, {})[\"cursor\"] = shown\n        try:\n            p = self.channel.post(to, to, ans.report)"),
 ("usage dropped", "ga/adapters/headless.py", "usage=usage_of(data),", "usage=None,"),
 ("usage dropped from hub turn record", "ga/hub.py", '"usage": result.usage, "model": result.model,', '"model": result.model,'),
 ("hook written to the person's ~/.claude", "ga/adapters/headless.py", 'path = home / "ga-settings.json"', 'path = Path(os.environ["HOME"]) / ".claude" / "settings.json"; path.parent.mkdir(parents=True, exist_ok=True)'),
 ("state file not read on the next turn", "ga/hub.py", 'state = sf.read_text(encoding="utf-8") if sf.exists() else None', "state = None"),
]
out = []
for name, f, a, b in M:
    p = Path(f); s = p.read_text()
    assert s.count(a) == 1, (name, a)
    p.write_text(s.replace(a, b))
    try:
        r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_ga29"], capture_output=True, text=True, timeout=600)
    finally:
        p.write_text(s)
    killed = r.returncode != 0
    out.append({"mutation": name, "file": f, "killed": killed, "tail": r.stderr.strip().splitlines()[-1]})
    print(("KILLED " if killed else "SURVIVED ") + name, flush=True)
Path("results/ga29").mkdir(parents=True, exist_ok=True)
import json; Path("results/ga29/mutations.json").write_text(json.dumps(out, indent=1) + "\n")
