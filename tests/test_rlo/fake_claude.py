"""A fake ``claude`` for ga_rlo tests: no model, no network, no cost.

It plays one headless turn the way Claude Code would, as far as the guards can tell:
- writes a Claude Code transcript under ``$CLAUDE_CONFIG_DIR/projects/<cwd>/<session id>.jsonl`` with the current time;
- for each planned tool call, appends the ``tool_use`` line and runs the ``PreToolUse`` hooks of the ``--settings`` file
  (matcher, then each command through the shell, JSON on stdin, JSON on stdout); a ``deny`` (or exit 2) blocks the call
  and its ``tool_result`` is an error, otherwise the call "runs" and its result is ok;
- then does the work: a commit in ``<cwd>/<repo>`` and a report/1 in the session's mailbox (ga's FileMailbox);
- prints the ``--output-format json`` result.

Driven by the JSON file named in ``$GA_RLO_FAKE``:
    {"tools": [{"name": "Bash", "input": {...}}, ...], "repo": "work", "file": "x.txt", "text": "...",
     "mailbox": "<.ga/mailbox>", "session": "W", "directive": "CMD-W1", "branch": "sess-w"}
Each hook decision is appended to ``$HOME/fake-hooks.jsonl`` (tool, hook index, decision) for the test to read.
"""
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

plan = json.loads(Path(os.environ["GA_RLO_FAKE"]).read_text(encoding="utf-8"))
argv = sys.argv[1:]
sys.stdin.read()
settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8")) if "--settings" in argv else {}
pre = (settings.get("hooks") or {}).get("PreToolUse", [])
sid = argv[argv.index("--resume") + 1] if "--resume" in argv else str(uuid.uuid4())
transcript = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()) / f"{sid}.jsonl"
transcript.parent.mkdir(parents=True, exist_ok=True)
log = Path(os.environ["HOME"]) / "fake-hooks.jsonl"
n = 0


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def line(kind: str, **kw) -> None:
    global n
    n += 1
    with transcript.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": kind, "sessionId": sid, "uuid": f"u{n:04d}", "timestamp": now(), **kw}) + "\n")


def run_hooks(tool: str, tool_input: dict, tid: str) -> bool:
    """True when a hook blocks the call."""
    data = {"session_id": sid, "transcript_path": str(transcript), "cwd": os.getcwd(), "permission_mode": "acceptEdits",
            "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "tool_use_id": tid}
    blocked = False
    for gi, group in enumerate(pre):
        m = group.get("matcher", "*")
        if m not in ("*", "") and not re.fullmatch(m, tool):
            continue
        for h in group.get("hooks", []):
            p = subprocess.run(h["command"], shell=True, input=json.dumps(data), capture_output=True, text=True)
            try:
                out = json.loads(p.stdout) if p.stdout.strip() else {}
            except ValueError:
                out = {}
            dec = (out.get("hookSpecificOutput") or {}).get("permissionDecision") or ("deny" if p.returncode == 2 else "")
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"tool": tool, "hook": gi, "decision": dec, "exit": p.returncode}) + "\n")
            blocked = blocked or dec == "deny"
    return blocked


line("user", message={"role": "user", "content": "go"})
for i, call in enumerate(plan["tools"], 1):
    tid = f"tu{i}"
    line("assistant", message={"id": f"m{i}", "model": "fake", "role": "assistant", "stop_reason": "tool_use",
                               "usage": {"input_tokens": 10, "output_tokens": 1},
                               "content": [{"type": "tool_use", "id": tid, "name": call["name"], "input": call["input"]}]})
    blocked = run_hooks(call["name"], call["input"], tid)
    line("user", toolUseResult={}, message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": "blocked" if blocked else "ok"}],
         "is_error": blocked}]})

repo = Path(os.getcwd()) / plan["repo"]
(repo / plan["file"]).write_text(plan["text"], encoding="utf-8")
git = ["git", "-c", "user.name=fake", "-c", "user.email=fake@test", "-c", "commit.gpgsign=false"]
subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
subprocess.run(git + ["commit", "-q", "-m", "fake work"], cwd=repo, check=True)
sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

from ga.adapters.mailbox import FileMailbox  # noqa: E402
from ga.forms import dump_text  # noqa: E402

head = {"schema": "report/1", "from": plan["session"],
        "handled": [{"id": plan["directive"], "rev_seen": 1, "status": "done"}],
        "commits": [{"repo": plan["repo"], "branch": plan["branch"], "sha": sha}]}
FileMailbox(plan["mailbox"]).post(plan["session"], plan["session"], dump_text(head, "## Result\n가짜 턴이 일했다\n"))
line("assistant", message={"id": "mz", "model": "fake", "role": "assistant", "stop_reason": "end_turn",
                           "usage": {"input_tokens": 10, "output_tokens": 1}, "content": [{"type": "text", "text": "done"}]})
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": sid,
                  "total_cost_usd": 0.0, "num_turns": len(plan["tools"]) + 1}))
