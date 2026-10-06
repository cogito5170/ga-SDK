"""``ga act`` (CMD-GA38): run one work item with the executor loop.

    ga act --item item.json --backend claude_cli --model claude-haiku-4-5 [--ladder m1,m2] [--repo .] [--options '{...}']
           [--max-turns 10] [--max-tokens 400000] [--cap 6000] [--config .ga-act.json] [--state DIR]
    ga act --item item.json --backend agv --route [--climb 1] [--triage-model M] [--triage-compare m1,m2]
    ga act routes [--state DIR] [--repo .] [--json]      the outcome ledger per source and per bucket (CMD-GA47)

The item is work/1-shaped: {"id", "goal", "files": [globs], "done_when"?: command name or argv}. Prints the act/1
result as JSON; exit 0 when the item is done, 1 when blocked, 2 on a config error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parser(sub) -> None:
    p = sub.add_parser("act", help="execute one work item: action format, state card, named commands (CMD-GA38)")
    p.add_argument("what", nargs="?", choices=["routes"], help="routes: print the route outcome ledger (CMD-GA47)")
    p.add_argument("--item", help="work item JSON file, or - for stdin")
    p.add_argument("--repo", default=".", help="the worktree to work in (default: .)")
    p.add_argument("--backend")
    p.add_argument("--model", help="the model (the first rung when --ladder is given)")
    p.add_argument("--ladder", help="m1,m2,...: start on m1, the next rung only when a run ends blocked by a cap or "
                   "no progress (CMD-GA45); models from the models list (.ga-act.json `models`)")
    p.add_argument("--route", action="store_true", help="choose the start model and turn cap once: the item's route, "
                   "else the outcome ledger, else one triage turn (CMD-GA47); also options {\"route\": true}")
    p.add_argument("--climb", type=int, help="rungs a routed run may climb when blocked by a cap or no progress "
                   "(0-2, default 1)")
    p.add_argument("--triage-model", dest="triage_model", help="the triage model (default gemini-3.1-pro-high)")
    p.add_argument("--triage-compare", dest="triage_compare", help="m1,m2: triage on both, record both, use m1")
    p.add_argument("--json", action="store_true", help="routes: print JSON")
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
    if args.what == "routes":
        from . import route as RT
        state = Path(args.state) if args.state else Path(args.repo) / ".ga" / "act"
        s = RT.summary(RT.read(state))
        print(json.dumps(s, sort_keys=True) if args.json else RT.render(s))
        return 0
    try:
        if not args.item or not args.backend:
            raise ValueError("give --item and --backend")
        raw = json.loads(sys.stdin.read() if args.item == "-" else Path(args.item).read_text(encoding="utf-8"))
        opts = json.loads(args.options)
        if not isinstance(opts, dict):
            raise ValueError("--options must be a JSON object")
        ladder = args.ladder if args.ladder else opts.get("ladder")
        routed = bool(args.route or opts.get("route"))
        model = args.model or (ladder.split(",")[0] if isinstance(ladder, str) else (ladder or [None])[0])
        if not model and not routed:
            raise ValueError("give --model, --ladder or --route")
        rk = {k: v for k, v in (("climb", args.climb), ("triage_model", args.triage_model),
                                ("triage_compare", args.triage_compare)) if v is not None}
        if routed:
            opts = dict(opts, route=True, **rk)
        res = run_item(Path(args.repo), raw, backend=args.backend, model=model or "", options=opts,
                       config=args.config, ladder=None if routed else (args.ladder or None),
                       state_dir=Path(args.state) if args.state else None, max_turns=args.max_turns,
                       max_tokens=args.max_tokens, cap=args.cap)
    except (OSError, ValueError, KeyError, ActConfigError, ConfigError, CardError) as e:
        print(f"ga act: {type(e).__name__}: {e}"[:500], file=sys.stderr)
        return 2
    print(json.dumps(res.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if res.status == "done" else 1
