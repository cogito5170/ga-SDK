"""CMD-GA34 D3 smoke: one pool node, claude_cli on Haiku 4.5 with tools {Read, Edit, Write}, in a throwaway repo."""
import json, os, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, "/home/user/ga-sdk")
from ga import config as gacfg, backends
from ga.backends import builtin as B
from ga.backends.base import BackendError
from ga.net import pool as P

tmp = Path(tempfile.mkdtemp(prefix="ga34-smoke-"))
def git(cwd, *a): return subprocess.run(["git", *a], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()
os.environ.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
git(tmp, "init", "-q", "--bare", "mail.git"); git(tmp, "clone", "-q", "mail.git", "mail")
app = tmp / "app"; app.mkdir(); git(app, "init", "-q", "-b", "main")
(app / "calc.py").write_text("def sub(a, b):\n    return a - b\n")
(app / "test_sub.py").write_text("import unittest\nfrom calc import sub\n\nclass T(unittest.TestCase):\n    def test_sub(self):\n        self.assertEqual(sub(3, 1), 2)\n")
(app / ".ga-judge.json").write_text(json.dumps({"setup": [], "test": ["{python}", "-m", "unittest", "discover", "-v"]}))
git(app, "add", "-A"); git(app, "commit", "-q", "-m", "init")
base = git(app, "rev-parse", "main")
net = {"mode": "peer", "mailbox": "mail", "theta": 0.3, "nodes": {},
       "pool": {"max_live": 1, "max_queue": 5, "max_spawn_per_round": 1, "max_depth": 1, "idle_rounds": 2,
                "repo": {"path": "app", "branch": "main"},
                "roles": {"dev": {"backends": ["claude_cli"], "pack_max_tokens": 4000, "timeout_s": 300,
                                  "needs": {"capabilities": ["text"], "tier": "R0"}, "budget": {"runs": 1},
                                  "tools": {"allow": ["Read", "Edit", "Write"]}, "progress": True}}}}
raw = {"schema": "ga-config/1", "hub": {"name": "baseline"}, "integration_branch": "integration", "repos": {}, "sessions": {},
       "network": net}
(tmp / "ga.json").write_text(json.dumps(raw))
cfg = gacfg.load(tmp / "ga.json")
runs, raws = [0], []
orig_parse = B.parse_claude
def parse(stdout, code, model, seconds=0.0):
    try: raws.append(json.loads(stdout.strip().splitlines()[-1]))
    except Exception: raws.append({"unparsed": stdout[-300:]})
    return orig_parse(stdout, code, model, seconds)
B.parse_claude = parse
real = backends.get("claude_cli"); orig_create = real.create
def create(m, o, ctx):
    runs[0] += 1
    if runs[0] > 2: raise BackendError("smoke budget: 2 claude -p runs")
    print("RUN", runs[0], m, "cwd", ctx["cwd"], file=sys.stderr)
    return orig_create(m, o, ctx)
real.create = create
pl = P.Pool(cfg, tmp / ".ga")
ok, why = pl.add({"id": "CMD-SM1", "role": "dev", "files": ["calc.py", "test_calc.py"],
                  "goal": "In the repository in your current working directory: add a function add(a, b) that returns a + b to "
                          "calc.py (keep sub), and create test_calc.py with a unittest TestCase that checks add(2, 3) == 5. "
                          "Use your Edit/Write tools to change the files; do not touch any other file. Then answer."})
print("add", ok, why)
for r in range(5):
    out = pl.round()[-1]["pool"]
    print("round", r + 1, json.dumps({k: out[k] for k in ("started", "retired", "live")}))
    if not out["live"] and r > 0: break
head = git(app, "rev-parse", "main")
tel = [json.loads(x) for x in (tmp / ".ga/pool/telemetry.jsonl").read_text().splitlines()]
ntel = []
for f in (tmp / ".ga/nodes").rglob("telemetry.jsonl"): ntel += [json.loads(x) for x in f.read_text().splitlines()]
res = {"runs": runs[0], "base": base, "main": head, "moved": head != base,
       "ff": subprocess.run(["git", "merge-base", "--is-ancestor", base, head], cwd=app).returncode == 0,
       "files": git(app, "diff", "--name-only", base, head).split(), "status": pl.status(),
       "pool_l0": [(e["type"], {k: v for k, v in e["data"].items() if k in ("item", "sha", "reason", "tests", "outcome")}) for e in tel],
       "node_l0": [(e["type"], e["data"].get("tool"), e["data"].get("path"), e["data"].get("model")) for e in ntel if e["type"] != "run.end"],
       "claude": [{k: r.get(k) for k in ("total_cost_usd", "usage", "num_turns", "duration_ms", "subtype", "is_error", "modelUsage", "permission_denials")} for r in raws]}
print(json.dumps(res, indent=1)[:6000])
(Path(__file__).parent / "smoke_result.json").write_text(json.dumps(res, indent=1))
print("calc.py:\n" + (app / "calc.py").read_text()); print("test_calc.py:\n" + ((app / "test_calc.py").read_text() if (app / "test_calc.py").exists() else "-"))
