"""``ga ops tick [--dry-run]`` (CMD-GA57 S5): one ops tick over the hub's ga dir. Prints nothing when nothing happened."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parser(sub) -> None:
    p = sub.add_parser("ops", help="the ops tick: observe the hub's ga dir -> rule -> guarded action -> VERIFY -> "
                       "escalate; a model only when no rule matches (CMD-GA57; docs/OPS.md)")
    p.add_argument("ops_cmd", choices=["tick", "last"])
    p.add_argument("--config", default=".ga-hub.json", help="the hub config (JSON), as for ga hub")
    p.add_argument("--ga-dir", default=".ga")
    p.add_argument("--dry-run", action="store_true", help="decide and print; nothing executed, mailed or written")
    p.add_argument("-n", type=int, default=20, help="last: how many decision rows")
    p.set_defaults(fn=cmd_ops)


def cmd_ops(args) -> int:
    from .core import Ops, last_decisions
    if args.ops_cmd == "last":
        print(json.dumps(last_decisions(args.ga_dir, args.n), ensure_ascii=False, indent=1))
        return 0
    cf = Path(args.config).expanduser()
    if not cf.is_file():
        print(f"ga ops: no config at {cf} (the hub's hub.json) — give --config", file=sys.stderr)
        return 2
    try:
        conf = json.loads(cf.read_text(encoding="utf-8"))
    except ValueError as e:
        print(f"ga ops: {cf} is not JSON ({e})", file=sys.stderr)
        return 2
    rows = Ops(conf, ga_dir=args.ga_dir).tick(dry_run=args.dry_run)
    for r in rows:
        print(json.dumps(r, ensure_ascii=False, sort_keys=True), flush=True)
    return 0
