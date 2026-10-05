"""PreToolUse hook: the context-budget/1 backstop of a fresh turn (CMD-GA29 S4) -- rlo-sdk 0.11.1 ``rlo.ctxbudget``.

The headless Runner writes it only into the turn's own settings file in its per-session directory (HOME /
CLAUDE_CONFIG_DIR of that session), never into a person's ``~/.claude`` or another session's.

    python budget_hook.py --soft N --hard N [--mode shadow|enforce] [--state STATE.md ...] --log <jsonl>

Reads the hook input on stdin, measures the context from the transcript tail (``context_tokens``), decides
(``decide``) and appends one record to the log. shadow (the default): prints nothing -- record only. enforce: prints
rlo's PreToolUse output (a note past soft; past hard, a deny for anything but checkpoint tools). Unknown context, a
missing rlo or a broken input never blocks: it is recorded as such.
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None, stdin=None, stdout=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--soft", type=int, required=True)
    ap.add_argument("--hard", type=int, required=True)
    ap.add_argument("--mode", default="shadow", choices=("shadow", "enforce"))
    ap.add_argument("--state", action="append", default=[])
    ap.add_argument("--log", required=True)
    a = ap.parse_args(argv)
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    rec: dict = {"kind": "context_budget", "soft": a.soft, "hard": a.hard, "mode": a.mode}
    out: dict = {}
    try:
        data = json.loads(stdin.read() or "{}")
        rec.update(tool_name=data.get("tool_name"), tool_use_id=data.get("tool_use_id"))
        from rlo import ctxbudget as CB  # rlo-sdk 0.11.1 (ga/_pins.py)

        b = CB.Budget(a.soft, a.hard, tuple(a.state or ["STATE.md"]), a.mode)
        ctx = CB.context_tokens(data["transcript_path"]) if data.get("transcript_path") else None
        stage, out = CB.decide(ctx, data.get("tool_name"), data.get("tool_input"), b)
        rec.update(stage=stage, ctx=ctx, enforced=a.mode == "enforce" and bool(out))
    except Exception as e:  # never a block for want of a number
        rec.update(stage="unknown", error=type(e).__name__)
        out = {}
    with open(a.log, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    if a.mode == "enforce" and out:
        stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
