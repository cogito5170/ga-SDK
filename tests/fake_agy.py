"""A fake Antigravity CLI (agy) for tests (CMD-GA23). Its shapes are the ASSUMPTIONS of ga/adapters/agy_cli.py (baseline#12
5971559909: V verified, A assumption, U unknown); the user's Mac run settles them.

    agy -p /usage            -> prints FAKE_AGY_DIR/usage.txt (V: spends no quota); not a turn
    agy -p <prompt> ...      -> plays FAKE_AGY_DIR/script.json, one entry per turn:
      {"plan": {...}, "model": "<slug>" | {"display_name": ...} | null, "denied": [...], "format": "stream-json"|"json"}
      {"quota": true}                     -> "AGY_ERROR: ... quota ..." on stderr, exit 3 (A)
      {"credits": "offer" | "low"}        -> the AI-credits prompt or message (V), exit 3
      {"error": true}                     -> "AGY_ERROR: agent failed", exit 3 (V)
      {"crash": true}                     -> no output, exit 1
      {"set_usage": "<text>"}             -> (with any entry) usage.txt becomes <text> after this turn
Every call appends a row to calls.jsonl.
"""
import json
import os
import sys
from pathlib import Path

d = Path(os.environ["FAKE_AGY_DIR"])
argv = sys.argv[1:]


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


prompt = opt("-p") or ""
calls = d / "calls.jsonl"
rows = [json.loads(x) for x in calls.read_text().splitlines()] if calls.exists() else []
row = {"kind": "usage" if prompt.strip() == "/usage" else "turn", "model": opt("--model"),
       "format": opt("--output-format"), "cwd": os.getcwd(), "pid": os.getpid(),
       "stdin_eof": (sys.stdin.read() == "") if not sys.stdin.isatty() else False,
       "credits_arg": any("credit" in a.lower() for a in argv), "resume_arg": "--resume" in argv,
       "has_task": "Task:" in prompt, "has_protocol": "ga-gemini-plan/1" in prompt, "has_results": "Results:" in prompt}
with open(calls, "a") as f:
    f.write(json.dumps(row) + "\n")
if row["kind"] == "usage":
    u = d / "usage.txt"
    print(u.read_text() if u.exists() else "")
    sys.exit(0)
n = sum(1 for r in rows if r["kind"] == "turn")
script = json.loads((d / "script.json").read_text())
entry = script[n] if n < len(script) else {"crash": True}
if "set_usage" in entry:
    (d / "usage.txt").write_text(entry["set_usage"])
if entry.get("crash"):
    sys.exit(1)
if entry.get("quota"):
    print("AGY_ERROR: You have reached the quota limit for Gemini models.", file=sys.stderr)
    sys.exit(3)
if entry.get("credits"):
    print("Your plan quota is used up. Use AI Credits to continue? [y/N]" if entry["credits"] == "offer"
          else "Your AI credits balance is too low to continue.")
    sys.exit(3)
if entry.get("error"):
    print("AGY_ERROR: agent failed", file=sys.stderr)
    sys.exit(3)
model = entry["model"] if "model" in entry else opt("--model")
text = "```json\n" + json.dumps(entry["plan"]) + "\n```\n"
if entry.get("format") == "json":
    out = {"response": text}
    if model is not None:
        out["model"] = model
    if entry.get("denied"):
        out["denied_actions"] = entry["denied"]
    print(json.dumps(out))
    sys.exit(0)
ev = [{"type": "init"} | ({"model": model} if model is not None else {})]
ev += [{"type": "message", "role": "assistant", "content": text[i:i + 30], "delta": True} for i in range(0, len(text), 30)]
ev.append({"type": "result", "status": "success"} | ({"denied_actions": entry["denied"]} if entry.get("denied") else {}))
for e in ev:
    print(json.dumps(e))
