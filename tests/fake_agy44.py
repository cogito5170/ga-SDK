"""Fake agy for CMD-GA44 (on PATH as `agy`). Reads FAKE44_DIR: calls.jsonl (argv rows), cfg.json
{"input": N, "installed": [names], "models": [slugs], "text": "..."}."""
import json
import os
import sys
from pathlib import Path

d = Path(os.environ["FAKE44_DIR"])
cfg = json.loads((d / "cfg.json").read_text())
argv = sys.argv[1:]
with open(d / "calls.jsonl", "a") as f:
    f.write(json.dumps(argv) + "\n")
if argv[:2] == ["plugin", "list"]:
    print("\n".join(cfg.get("installed", [])))
    sys.exit(0)
if argv[:1] == ["models"]:
    if cfg.get("models") is None:
        sys.exit(1)
    print("\n".join(f"{m}  (fake)" for m in cfg["models"]))
    sys.exit(0)
if "--agent" in argv and cfg.get("unknown_agent"):
    print("Error: unknown agent: " + argv[argv.index("--agent") + 1], file=sys.stderr)
    sys.exit(1)
print(json.dumps({"response": cfg.get("text", "ok"), "usage": {"input_tokens": cfg.get("input", 2600), "output_tokens": 20}}))
