"""CMD-GA29 S5: the same small task, end to end through the hub loop, in resume mode and in fresh mode (real claude -p).

    python examples/ga29_measure.py <variant> <out dir>     variant: resume | fresh | fresh-small

A scratch world (tests/world.py: a git repository ``alpha``, session A, a file mailbox) gets three directives in a row;
after each one the hub's turn runs (HeadlessRunner, model haiku) and ``tick`` integrates the reported commit and runs
the tests. The three steps build on each other (mean -> median -> describe), so a resumed session carries its earlier
turns and a fresh one carries only its pack and state file.

Written to <out dir>: turns.json (per turn: the claude -p result JSON without the answer text, usage, served model,
pack meta, tick outcome), l0_turns.jsonl (ga's run.end record per turn), l0_calls.jsonl (Telemetry from_cc_jsonl of the
runner's own transcripts: one llm.response per model call), checks.json and summary.json. No prompt text is written.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "tests"))

from ga.adapters.headless import DEFAULT_ALLOWED, HeadlessRunner  # noqa: E402
from world import GIT_ENV, World  # noqa: E402

SMALL_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
# the child's HOME is the runner's own directory (no git identity there) and the task runs its tests: same for every variant
TEST_ALLOWED = ("Bash(python -m unittest:*)", "Bash(python3 -m unittest:*)")


def directives() -> list[dict]:
    base = {"schema": "directive/2", "rev": 1, "to": "A", "why": "S5 measurement task", "refs": ["alphapkg/__init__.py"]}
    return [
        dict(base, id="CMD-A1", goal="Add mean(xs) to the alpha repository, with a unit test.",
             scope=[{"id": "S1", "text": "Create alphapkg/stats.py with mean(xs): the arithmetic mean of a non-empty list of numbers; "
                                         "raise ValueError on an empty list."},
                    {"id": "S2", "text": "Create tests/test_stats.py (unittest) covering mean, including the empty case. Commit both."}],
             done_when=[{"id": "D1", "text": "alphapkg/stats.py and tests/test_stats.py are committed on your branch and "
                                             "python -m unittest discover -s tests passes."}]),
        dict(base, id="CMD-A2", goal="Add median(xs) next to mean in alphapkg/stats.py, with tests.", after=["CMD-A1"],
             refs=["alphapkg/stats.py", "tests/test_stats.py"],
             scope=[{"id": "S1", "text": "Add median(xs) to alphapkg/stats.py (even length: mean of the two middle values; "
                                         "empty: ValueError). Keep mean as it is."},
                    {"id": "S2", "text": "Add median tests to tests/test_stats.py. Commit."}],
             done_when=[{"id": "D1", "text": "median is committed with tests and the test suite passes."}]),
        dict(base, id="CMD-A3", goal="Add describe(xs) using mean and median, with tests, and list the functions in README.md.",
             after=["CMD-A2"], refs=["alphapkg/stats.py", "tests/test_stats.py", "README.md"],
             scope=[{"id": "S1", "text": "Add describe(xs) to alphapkg/stats.py returning {\"n\": len(xs), \"mean\": mean(xs), "
                                         "\"median\": median(xs)}."},
                    {"id": "S2", "text": "Add describe tests; add a line per function (mean, median, describe) to README.md. Commit."}],
             done_when=[{"id": "D1", "text": "describe is committed with tests, README.md lists the three functions, the suite passes."}]),
    ]


CHECK = """
import sys; sys.path.insert(0, sys.argv[1])
from alphapkg.stats import mean, median, describe
assert mean([1, 2, 3]) == 2 and median([3, 1, 2]) == 2 and median([1, 2, 3, 4]) == 2.5
assert describe([1, 2, 3]) == {"n": 3, "mean": 2, "median": 2}
for f in (mean, median):
    try:
        f([])
    except ValueError:
        pass
    else:
        raise AssertionError("no ValueError on empty")
print("ok")
"""


class Recording(HeadlessRunner):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.results = []

    def run_turn(self, req):
        r = super().run_turn(req)
        self.results.append(r)
        return r


def calls_l0(home: Path) -> list[dict]:
    from telemetry.collect import from_cc_jsonl

    out, seen = [], set()
    for i, p in enumerate(sorted(home.rglob("projects/**/*.jsonl"))):
        for e in from_cc_jsonl(str(p), f"t{i}"):
            if e["type"] == "llm.response":
                rid = e["data"].get("response_id")
                if rid in seen:  # a resumed transcript may repeat earlier responses
                    continue
                seen.add(rid)
            out.append(e)
    return out


def main(variant: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    w = World(modes=("path",), packaged=False)
    try:
        runner = Recording(w.ga / "headless" / "home", executable=os.environ.get("GA29_CLAUDE", "claude"), model="haiku", timeout=900, max_budget_usd=1.0,
                           tools=SMALL_TOOLS if variant == "fresh-small" else None,
                           allowed_tools=DEFAULT_ALLOWED + TEST_ALLOWED, extra_env=dict(GIT_ENV),
                           context_budget={"soft": 60000, "hard": 120000} if variant != "resume" else None)
        w.permit_runner(runner)
        w.cfg.hub["post_command"] = (f"{sys.executable} -m ga --config {w.tmp}/ga.json --ga-dir {w.ga} "
                                     "post --channel {session} --from {session} <보고 파일>")
        if variant != "resume":
            w.cfg.sessions["A"].context = "fresh"
            w.cfg.sessions["A"].pack_max_tokens = 8000
        turns = []
        for d in directives():
            post, findings, gates = w.hub.send(d)
            res = w.hub.tick()
            st = w.hub.load_state()
            t = st["turns"][-1]
            r = runner.results[-1] if runner.results else None
            turns.append({"directive": d["id"], "sent": post is not None, "error": t.get("error"), "seconds": t.get("seconds"),
                          "cost": t.get("cost"), "usage": t.get("usage"), "model": t.get("model"), "pack": t.get("pack"),
                          "resumed": t.get("resumed"), "claude_p": r.raw if r else None,
                          "answer": r.answer if r and variant != "resume" else None,
                          "findings": [str(f) for f in findings], "gates": [g.reason if hasattr(g, "reason") else str(g) for g in gates],
                          "tick": {"integrated": res.integrated, "verdict": (res.verdict or {}).get("class"),
                                   "findings": [str(f) for f in res.findings][:10]}})
            print(json.dumps({"directive": d["id"], "error": t.get("error"), "usage": t.get("usage"),
                              "integrated": res.integrated}), flush=True)
        # the same checks for every variant, on the integration branch
        alpha = w.repos["alpha"]
        chk = Path(tempfile.mkdtemp(prefix="ga29-check-"))
        subprocess.run(["git", "-C", str(alpha), "worktree", "add", "-q", "--detach", str(chk), w.INTEG], check=True)
        hidden = subprocess.run([sys.executable, "-c", CHECK, str(chk)], capture_output=True, text=True)
        suite = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=chk, capture_output=True, text=True)
        readme = (chk / "README.md").read_text() if (chk / "README.md").exists() else ""
        checks = {"integration_head": subprocess.run(["git", "-C", str(alpha), "rev-parse", w.INTEG], capture_output=True,
                                                     text=True).stdout.strip(),
                  "hidden_check": hidden.returncode == 0, "hidden_tail": (hidden.stderr or hidden.stdout).strip()[-300:],
                  "suite": suite.returncode == 0, "suite_tail": suite.stderr.strip().splitlines()[-1:] if suite.stderr else [],
                  "readme_lists": all(f in readme for f in ("mean", "median", "describe"))}
        checks["passed"] = checks["hidden_check"] and checks["suite"] and checks["readme_lists"]
        (out / "checks.json").write_text(json.dumps(checks, indent=1) + "\n")
        (out / "turns.json").write_text(json.dumps(turns, indent=1, ensure_ascii=False) + "\n")
        shutil.copy(w.ga / "telemetry" / "A.jsonl", out / "l0_turns.jsonl")
        ev = calls_l0(runner.home)
        with (out / "l0_calls.jsonl").open("w") as f:
            for e in ev:
                f.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
        resp = [e["data"] for e in ev if e["type"] == "llm.response"]
        ctx = [sum(d.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")) for d in resp]
        tools = {e["data"].get("tool_use_id") or e["id"] for e in ev if e["type"] == "tool.start"}
        u = [t["usage"] or {} for t in turns]
        summary = {
            "variant": variant, "turns": len(turns), "claude_p_runs": len(runner.results),
            "model_calls": len(resp), "tool_calls": len(tools),
            "tokens_read_calls": sum(ctx), "max_context_per_call": max(ctx) if ctx else None,
            "output_tokens_calls": sum(d.get("output_tokens") or 0 for d in resp),
            "tokens_read_reported": sum(x.get("input", 0) + x.get("cache_read", 0) + x.get("cache_creation", 0) for x in u),
            "output_tokens_reported": sum(x.get("output", 0) for x in u),
            "cost_usd": round(sum(t["cost"] or 0 for t in turns), 6),
            "served_models": sorted({t["model"] for t in turns if t["model"]}),
            "turn_errors": [t["error"] for t in turns], "integrated_each_turn": [bool(t["tick"]["integrated"]) for t in turns],
            "passed_checks": checks["passed"],
        }
        (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
        print(json.dumps(summary))
        return 0
    finally:
        w.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], Path(sys.argv[2])))
