"""A fake headless Gemini CLI for tests (CMD-GA21). Plays FAKE_GEMINI_DIR/script.json, one entry per call:

    {"plan": {...}, "served": "<model>"?, "session": "s-1"?, "no_model": false?}  -> init, message (```json plan```), result
    {"rate_limit": "7s"}            -> init, an error event carrying the API 429 body (RetryInfo), result error, exit 1
    {"rate_limit_text": "7"}        -> the same 429 as plain text only ("... retry in 7s"), exit 1
    {"fail": true}                  -> result error, exit 1

Every call appends a row to calls.jsonl: argv parts, pid, whether stdin was a TTY and whether it was at EOF.
"""
import json
import os
import sys
from pathlib import Path

d = Path(os.environ["FAKE_GEMINI_DIR"])
argv = sys.argv[1:]


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


calls = d / "calls.jsonl"
n = sum(1 for _ in open(calls)) if calls.exists() else 0
script = json.loads((d / "script.json").read_text())
entry = script[n] if n < len(script) else {"fail": True}
stdin_eof = sys.stdin.read() == "" if not sys.stdin.isatty() else False
with open(calls, "a") as f:
    f.write(json.dumps({"n": n, "model": opt("-m"), "resume": opt("--resume"), "format": opt("--output-format"),
                        "has_p": "-p" in argv, "prompt_len": len(opt("-p") or ""), "pid": os.getpid(),
                        "stdin_tty": sys.stdin.isatty(), "stdin_eof": stdin_eof,
                        "prompt_has_results": "Results:" in (opt("-p") or "")}) + "\n")
model = opt("-m")
session = entry.get("session", opt("--resume") or "s-1")


def emit(ev):
    print(json.dumps(ev), flush=True)


init = {"type": "init", "timestamp": "t", "session_id": session}
if not entry.get("no_model"):
    init["model"] = entry.get("served", model)
emit(init)
if "plan" in entry:
    text = "Here is the plan.\n```json\n" + json.dumps(entry["plan"]) + "\n```\n"
    for i in range(0, len(text), 40):
        emit({"type": "message", "timestamp": "t", "role": "assistant", "content": text[i:i + 40], "delta": True})
    emit({"type": "result", "timestamp": "t", "status": "success",
          "stats": {"input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280, "duration_ms": 900, "tool_calls": 0}})
    sys.exit(0)
if "rate_limit" in entry:
    body = {"error": {"code": 429, "message": "You exceeded your current quota.", "status": "RESOURCE_EXHAUSTED",
                      "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                   "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
                                  {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": entry["rate_limit"]}]}}
    emit({"type": "error", "timestamp": "t", "severity": "error", "message": "[API Error: " + json.dumps(body) + "]"})
    emit({"type": "result", "timestamp": "t", "status": "error", "error": {"type": "api", "message": "quota"}})
    sys.exit(1)
if "rate_limit_text" in entry:
    print(f"Error 429 RESOURCE_EXHAUSTED: quota exceeded. Please retry in {entry['rate_limit_text']}s.", file=sys.stderr)
    sys.exit(1)
emit({"type": "result", "timestamp": "t", "status": "error", "error": {"type": "api", "message": "boom"}})
sys.exit(1)
