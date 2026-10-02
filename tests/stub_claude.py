"""A fake ``claude`` executable for headless Runner tests (no network, no cost).

Driven by ``plan.json`` in $GA_STUB_DIR: a list, one step per call. Every call is recorded in calls.jsonl
(argv, cwd, stdin size, environment variable *names*, HOME, CLAUDE_CONFIG_DIR). Steps:
  {"do": "ok", "cost": 0.01}                 normal JSON result (session id kept on --resume)
  {"do": "exit", "code": 3}                  non-zero exit, no JSON
  {"do": "sleep", "seconds": 5}              hang (for the timeout)
  {"do": "badjson"}                          broken JSON on stdout
  {"do": "is_error"}                         JSON with is_error (e.g. max turns)
  {"do": "work", "cost": .., "repo": "alpha", "file": "x.txt", "text": "..", "report": {...}}
      commit in <cwd>/<repo>, then post a report/1 claiming that commit (like a session would)
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

d = Path(os.environ["GA_STUB_DIR"])
plan = json.loads((d / "plan.json").read_text(encoding="utf-8"))
calls = d / "calls.jsonl"
n = len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
step = plan[n] if n < len(plan) else {"do": "ok"}
argv = sys.argv[1:]
stdin = sys.stdin.read()
with calls.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "stdin_len": len(stdin), "stdin_head": stdin[:200],
                        "env": sorted(os.environ), "HOME": os.environ.get("HOME"),
                        "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR")}, ensure_ascii=False) + "\n")

sid = argv[argv.index("--resume") + 1] if "--resume" in argv else str(uuid.uuid4())


def result(cost, is_error=False, subtype="success"):
    print(json.dumps({"type": "result", "subtype": subtype, "is_error": is_error, "result": "stub answer",
                      "session_id": sid, "total_cost_usd": cost, "num_turns": 2, "duration_ms": 10}))


do = step["do"]
if do == "ok":
    result(step.get("cost", 0.01))
elif do == "exit":
    print("boom", file=sys.stderr)
    sys.exit(step.get("code", 1))
elif do == "sleep":
    time.sleep(step["seconds"])
    result(0.0)
elif do == "badjson":
    print('{"type": "result", "session_id": ')
elif do == "is_error":
    result(0.02, True, "error_max_turns")
elif do == "reply":
    out = {"type": "result", "subtype": "success", "is_error": False, "result": step.get("text", ""), "session_id": sid,
           "total_cost_usd": step.get("cost", 0.004), "num_turns": 1}
    if "structured" in step:
        out["structured_output"] = step["structured"]
    print(json.dumps(out, ensure_ascii=False))
elif do == "cmd":
    p = subprocess.run(step["argv"], cwd=os.getcwd(), capture_output=True, text=True)
    (d / f"cmd-{n}.json").write_text(json.dumps({"code": p.returncode, "stderr": p.stderr[-2000:]}), encoding="utf-8")
    result(step.get("cost", 0.01))
elif do == "work":
    sys.path.insert(0, step["ga_root"])
    from ga.adapters.mailbox import FileMailbox
    from ga.forms import dump_text

    repo = Path(os.getcwd()) / step["repo"]
    (repo / step["file"]).write_text(step["text"], encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "stub work"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    r = step["report"]
    head = {"schema": "report/1", "from": r["session"], "handled": [{"id": r["directive"], "rev_seen": 1, "status": "done"}],
            "commits": [{"repo": step["repo"], "branch": r["branch"], "sha": sha}]}
    FileMailbox(step["mailbox"]).post(r["session"], r["session"], dump_text(head, "## Result\nstub 이 일했다\n"))
    result(step.get("cost", 0.01))
