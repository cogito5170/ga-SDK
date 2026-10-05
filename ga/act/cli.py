"""``ga act`` (CMD-GA38): run one work item with the executor loop.

    ga act --item item.json --backend claude_cli --model claude-haiku-4-5 [--repo .] [--options '{...}']
           [--max-turns 10] [--max-tokens 400000] [--cap 6000] [--config .ga-act.json] [--state DIR]

The item is work/1-shaped: {"id", "goal", "files": [globs], "done_when"?: command name or argv}. Prints the act/1
result as JSON; exit 0 when the item is done, 1 when blocked, 2 on a config error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parser(sub) -> None:
    p = sub.add_parser("act", help="execute one work item: action format, state card, named commands (CMD-GA38)")
    p.add_argument("--item", required=True, help="work item JSON file, or - for stdin")
    p.add_argument("--repo", default=".", help="the worktree to work in (default: .)")
    p.add_argument("--backend", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--options", default="{}", help="backend options as JSON")
    p.add_argument("--config", help="the commands file (default: <repo>/.ga-act.json)")
    p.add_argument("--state", help="ledger and L0 directory (default: <repo>/.ga/act)")
    p.add_argument("--max-turns", type=int, default=10)
    p.add_argument("--max-tokens", type=int, default=400_000)
    p.add_argument("--cap", type=int, default=6000, help="state card cap in tokens")
    p.set_defaults(fn=cmd_act)


def cmd_act(args) -> int:
    from ..backends.base import ConfigError
    from .card import CardError
    from .commands import ActConfigError
    from .loop import run_item
    try:
        raw = json.loads(sys.stdin.read() if args.item == "-" else Path(args.item).read_text(encoding="utf-8"))
        opts = json.loads(args.options)
        res = run_item(Path(args.repo), raw, backend=args.backend, model=args.model, options=opts, config=args.config,
                       state_dir=Path(args.state) if args.state else None, max_turns=args.max_turns,
                       max_tokens=args.max_tokens, cap=args.cap)
    except (OSError, ValueError, KeyError, ActConfigError, ConfigError, CardError) as e:
        print(f"ga act: {type(e).__name__}: {e}"[:500], file=sys.stderr)
        return 2
    print(json.dumps(res.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if res.status == "done" else 1
