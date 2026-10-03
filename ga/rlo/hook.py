"""`python -m ga.rlo.hook` -- the local profile's guard command (CMD-GR3 S3).

It is `python -m rlo.hooks` with the same arguments and the same verdict, plus one thing: after a PreToolUse verdict it
appends a state line to the `--record` file,

    {"kind": "state", "labels": {"execution_health": "NO_FAILURE_OBSERVED", ...}}

so ga's own `guard_summary` (ga af904fe, GA19) carries the Sensor state into `turns[].guards[].state`. Only labels that
match `[A-Za-z0-9_.:+-]{1,40}` are written: no paths, URLs, command text or prose.

The verdict is rlo's alone. rlo's output is passed through unchanged; the state line is written after it and any problem
there drops the line, never the verdict. rlo 0.5.1 already closes on malformed input and unexpected errors; if rlo itself
raises anyway, an enforce PreToolUse is denied here (fail closed).
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time

LABEL = re.compile(r"^[A-Za-z0-9_.:+-]{1,40}$")


def clean(labels: dict) -> dict[str, str]:
    """Only short label names and values; anything else is dropped."""
    return {k: v for k, v in labels.items()
            if isinstance(k, str) and isinstance(v, str) and LABEL.match(k) and LABEL.match(v)}


def _args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--model")
    ap.add_argument("--mode", default="shadow")
    ap.add_argument("--record")
    ap.add_argument("--now-ms", type=float)
    return ap.parse_known_args(argv)[0]


def state_line(data: object, a: argparse.Namespace) -> dict | None:
    """The state line for one PreToolUse input, or None (not a PreToolUse, no record, unreadable transcript)."""
    if not a.record or not a.model or not isinstance(data, dict) or data.get("hook_event_name") != "PreToolUse":
        return None
    path = data.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    from . import bridge, preset

    now = a.now_ms if a.now_ms is not None else time.time() * 1000  # the guard's own view of now
    labels = clean(bridge.transcript_state(path, preset.load_model(a.model), now_ms=now))
    return {"kind": "state", "labels": labels} if labels else None


def main(argv: list[str] | None = None, stdin=None, stdout=None) -> int:
    from rlo.hooks import deny
    from rlo.hooks import main as rlo_main

    argv = list(sys.argv[1:] if argv is None else argv)
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    raw = stdin.read()
    a = _args(argv)
    buf = io.StringIO()
    try:
        rc = rlo_main(argv, io.StringIO(raw), buf)
    except Exception as e:  # noqa: BLE001 -- rlo 0.5.1 should not get here; if it does, close
        rc, buf = 0, io.StringIO()
        if a.mode == "enforce":
            json.dump(deny(f"ga-rlo hook error: {type(e).__name__}"), buf)
    stdout.write(buf.getvalue())
    try:
        line = state_line(json.loads(raw), a)
        if line is not None:
            with open(a.record, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 -- no state line; the verdict above stands
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
