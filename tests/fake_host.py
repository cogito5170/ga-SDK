#!/usr/bin/env python3
"""A recorded host for the GA28 backend tests: the n-th call prints FAKE_HOST_FIXTURE/<n>.out; an agy ``-p /usage`` call
prints usage.out and is not counted. Each argv goes to FAKE_HOST_DIR/argv.jsonl (the bare-call assertions read it)."""
import json
import os
import sys
from pathlib import Path

fixture, d = Path(os.environ["FAKE_HOST_FIXTURE"]), Path(os.environ["FAKE_HOST_DIR"])
argv = sys.argv[1:]
if argv == ["-p", "/usage"]:
    sys.stdout.write((fixture / "usage.out").read_text(encoding="utf-8"))
    sys.exit(0)
log = d / "argv.jsonl"
n = len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(argv) + "\n")
sys.stdout.write((fixture / f"{n + 1}.out").read_text(encoding="utf-8"))
