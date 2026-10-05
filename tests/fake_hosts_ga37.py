"""Fake model hosts for CMD-GA37 (0 network): claude (claude -p --output-format json), codex (codex exec --json) and
agy (agy -p --output-format stream-json), each in the output shape ga's real runner parses.

    python fake_hosts_ga37.py <claude|codex|agy> <state dir> <the host's own argv...>

The n-th call answers with the n-th string of <state dir>/answers.json (the last one again past the end) and appends
{mode, argv, prompt, system} to <state dir>/calls.jsonl. <state dir>/sleep (seconds) delays the answer.
Every host reports the same usage: input 100, output 50, cache read 10.
"""
import json
import sys
import time
from pathlib import Path

mode, d, argv = sys.argv[1], Path(sys.argv[2]), sys.argv[3:]


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


calls = d / "calls.jsonl"
n = len(calls.read_text().splitlines()) if calls.exists() else 0
prompt = argv[-1] if mode == "codex" else opt("-p")
with open(calls, "a") as f:
    f.write(json.dumps({"mode": mode, "argv": argv, "prompt": prompt, "system": opt("--system-prompt")}) + "\n")
answers = json.loads((d / "answers.json").read_text())
ans = answers[min(n, len(answers) - 1)]
if (d / "sleep").exists():
    time.sleep(float((d / "sleep").read_text()))
model = opt("--model")
if mode == "claude":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": ans,
                      "modelUsage": {model: {"inputTokens": 100}},
                      "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10,
                                "cache_creation_input_tokens": 0}}))
elif mode == "codex":
    for ev in ({"type": "thread.started", "model": model},
               {"type": "item.completed", "item": {"type": "agent_message", "text": ans}},
               {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 10,
                                                    "output_tokens": 50}}):
        print(json.dumps(ev))
elif mode == "agy":
    for ev in ({"type": "init", "model": model},
               {"type": "message", "role": "assistant", "content": ans},
               {"type": "result", "status": "success",
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 10}}}):
        print(json.dumps(ev))
else:
    sys.exit(2)
