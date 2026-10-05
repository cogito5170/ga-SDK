"""CMD-GA38 D3 live smoke: ga act on claude_cli (bare, tools off) with claude-haiku-4-5 fixes a seeded bug in a
throwaway repo. Hard budget: at most 2 claude -p runs (max_turns 2 and a counter that refuses a third)."""
import json, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ga import backends
from ga.act import loop as A
from ga.backends.base import BackendError

tmp = Path(tempfile.mkdtemp(prefix="ga38-smoke-"))
app = tmp / "app"; app.mkdir()
(app / "stats.py").write_text(
    "def mean(xs):\n    \"\"\"The arithmetic mean of a non-empty list.\"\"\"\n    return sum(xs) / (len(xs) + 1)\n\n\n"
    "def spread(xs):\n    return max(xs) - min(xs)\n")
(app / "test_stats.py").write_text(
    "import unittest\nfrom stats import mean, spread\n\n\nclass T(unittest.TestCase):\n"
    "    def test_mean(self):\n        self.assertEqual(mean([2, 4, 6]), 4)\n\n"
    "    def test_spread(self):\n        self.assertEqual(spread([2, 4, 6]), 4)\n")
(app / ".ga-act.json").write_text(json.dumps({"commands": {"test": ["{python}", "-m", "unittest", "-v"]},
                                              "done_when": "test", "timeout_s": 60}))
subprocess.run(["git", "init", "-q"], cwd=app, check=True)

runs = [0]
real = backends.create("claude_cli", "claude-haiku-4-5", {}, {"cwd": str(app), "timeout_s": 300})
orig = real.run_turn
def run_turn(prompt, session_id=None, **kw):
    runs[0] += 1
    if runs[0] > 2:
        raise BackendError("smoke budget: 2 claude -p runs")
    print("RUN", runs[0], "bare", real.bare, "argv has --tools ''", real.argv("x", "y")[real.argv("x", "y").index("--tools") + 1] == "",
          file=sys.stderr)
    return orig(prompt, session_id, **kw)
real.run_turn = run_turn
item = {"id": "CMD-SM38", "goal": "Make the tests in test_stats.py pass by fixing stats.py.", "files": ["stats.py"],
        "done_when": "test"}
res = A.run_item(app, item, backend="claude_cli", model="claude-haiku-4-5", runner=real, state_dir=tmp / "state",
                 max_turns=2)
rows = [json.loads(x) for f in sorted((tmp / "state" / "ledger").glob("*.jsonl")) for x in f.read_text().splitlines()]
out = {"result": res.to_dict(), "claude_p_runs": runs[0],
       "per_turn": [{k: r.get(k) for k in ("turn", "model", "served", "input", "output", "cache_read", "cache_creation",
                                           "seconds", "card_tokens", "prefix_tokens", "applied", "rejected", "commands",
                                           "failing", "error")} for r in rows],
       "stats_py_after": (app / "stats.py").read_text()}
tot = sum((r.get("input") or 0) + (r.get("output") or 0) + (r.get("cache_read") or 0) + (r.get("cache_creation") or 0)
          for r in rows)
out["total_tokens"] = tot
out["rw1_average_item"] = {"cache_read": 3_826_000, "note": "RW1: 126,260,219 cache-read tokens over 33 items"}
print(json.dumps(out, indent=1))
