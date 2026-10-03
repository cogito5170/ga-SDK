"""A fake headless Gemini CLI for tests (CMD-GA21 rev 2). Its output follows the Gemini CLI 0.62.0 stream-json shapes
baseline read from the source (baseline#12 5970108172). Plays FAKE_GEMINI_DIR/script.json, one entry per call:

    {"plan": {...}, "served": ["<model>", ...]?, "sleep": s?}
                                                  -> (after sleeping s seconds) init (asked model), message deltas
                                                    (```json plan```), result success with stats.models keyed by the
                                                    served models (default: the asked one)
    {"fixture": "<name>", "exit": n?}             -> replays tests/fixtures/gemini/<name>.jsonl as recorded, exit n
                                                    (default 0; a quota error exits 173 = 429 & 255, as 0.62 does)

Every call appends a row to calls.jsonl: argv parts, pid, whether stdin was a TTY / at EOF, and the general.maxAttempts
of the settings file named by GEMINI_CLI_SYSTEM_SETTINGS_PATH.
"""
import json
import os
import sys
from pathlib import Path

d = Path(os.environ["FAKE_GEMINI_DIR"])
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gemini"
argv = sys.argv[1:]


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


calls = d / "calls.jsonl"
n = sum(1 for _ in open(calls)) if calls.exists() else 0
script = json.loads((d / "script.json").read_text())
entry = script[n] if n < len(script) else {"fixture": "crash", "exit": 1}
stdin_eof = sys.stdin.read() == "" if not sys.stdin.isatty() else False
settings = os.environ.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH")
try:
    max_attempts = json.loads(Path(settings).read_text()).get("general", {}).get("maxAttempts") if settings else None
except (OSError, ValueError):
    max_attempts = "unreadable"
with open(calls, "a") as f:
    f.write(json.dumps({"n": n, "model": opt("-m"), "resume": opt("--resume"), "format": opt("--output-format"),
                        "has_p": "-p" in argv, "cwd": os.getcwd(), "prompt_len": len(opt("-p") or ""), "pid": os.getpid(),
                        "stdin_tty": sys.stdin.isatty(), "stdin_eof": stdin_eof, "settings": settings,
                        "max_attempts": max_attempts, "prompt_has_results": "Results:" in (opt("-p") or "")}) + "\n")


def emit(ev):
    print(json.dumps(dict(ev, timestamp="2026-10-03T14:00:00.000Z")), flush=True)


if "fixture" in entry:
    sys.stdout.write((FIXTURES / f"{entry['fixture']}.jsonl").read_text())
    sys.exit(entry.get("exit", 0))
model = opt("-m")
if entry.get("sleep"):  # a long turn (the CLI retrying inside the process, as 0.62 may for the preview model)
    import time
    time.sleep(entry["sleep"])
emit({"type": "init", "session_id": opt("--resume") or "s-1", "model": model})  # the asked model, as 0.62 does
text = "Here is the plan.\n```json\n" + json.dumps(entry["plan"]) + "\n```\n"
for i in range(0, len(text), 40):
    emit({"type": "message", "role": "assistant", "content": text[i:i + 40], "delta": True})
per = {"total_tokens": 1280, "input_tokens": 1200, "output_tokens": 80, "cached": 0, "input": 1200}
emit({"type": "result", "status": "success", "stats": dict(per, duration_ms=900, tool_calls=0,
                                                            models={m: per for m in entry.get("served", [model])})})
