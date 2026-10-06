"""``ga actions list|show|run|approve|revoke`` (CMD-GA42 S3). ``approve`` is a human act: it asks y/N on a TTY and refuses
without one (GA Console approves through ``ga.actions.approve(name, approver="console")`` after its token check)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import registry as R


def add_parser(sub) -> None:
    p = sub.add_parser("actions", help="GA Actions: proposed named commands, human approval, registry (CMD-GA42)")
    s = p.add_subparsers(dest="actions_cmd", required=True)
    s.add_parser("list", help="proposals and approved actions")
    for name, h in (("show", "one proposal with its check and trial"), ("revoke", "remove an approved action")):
        q = s.add_parser(name, help=h)
        q.add_argument("name")
    q = s.add_parser("run", help="run an approved action by name in this folder (what a ga project routine runs)")
    q.add_argument("name")
    q.add_argument("--root", default=".", help="the project folder (default: here)")
    q = s.add_parser("approve", help="approve a proposal (a person at a TTY, y/N)")
    q.add_argument("name")
    q.add_argument("--allow-network", action="store_true", help="the action may use the network")
    p.set_defaults(fn=cmd_actions)


def cmd_actions(args, stdin=None, out=None) -> int:
    stdin, out = stdin or sys.stdin, out or sys.stdout
    h = R.home()
    try:
        if args.actions_cmd == "list":
            reg, ok = R.registry(h), R.verified(h)
            for n in sorted(reg):
                print(f"approved  {n}  {'ok' if n in ok else 'TAMPERED (refused)'}  by {reg[n].get('approved_by')}"
                      f"  {reg[n].get('about', '')}", file=out)
            d = h / "actions" / "proposals"
            for f in sorted(d.glob("*.json")) if d.is_dir() else []:
                rec = R._read(f, {})
                if f.stem not in reg:
                    st = "ok" if rec.get("check", {}).get("ok") else "refused"
                    print(f"proposed  {f.stem}  {st}  from {rec.get('source', '?')}", file=out)
            return 0
        if args.actions_cmd == "show":
            rec = R._read(R.proposal_path(args.name, h), None)
            if rec is None:
                print(f"ga actions: no proposal {args.name!r}", file=sys.stderr)
                return 1
            print(json.dumps({"proposal": rec, "approved": R.registry(h).get(args.name)}, ensure_ascii=False,
                             indent=1), file=out)
            return 0
        if args.actions_cmd == "revoke":
            ok = R.revoke(args.name, h)
            print(f"revoked {args.name}" if ok else f"{args.name} was not approved", file=out)
            return 0 if ok else 1
        if args.actions_cmd == "run":
            code, text = R.run(args.name, {}, Path(args.root), h=h)
            print(text, end="" if text.endswith("\n") or not text else "\n", file=out)
            return code
        if args.actions_cmd == "approve":
            if not (hasattr(stdin, "isatty") and stdin.isatty()):
                print("ga actions approve: a person approves at a terminal (TTY); refused", file=sys.stderr)
                return 2
            rec = R._read(R.proposal_path(args.name, h), None)
            if rec is None:
                print(f"ga actions: no proposal {args.name!r}", file=sys.stderr)
                return 1
            print(json.dumps(rec, ensure_ascii=False, indent=1), file=out)
            print(f"approve {args.name}{' (network allowed)' if args.allow_network else ''}? [y/N] ", end="", file=out)
            out.flush()
            if stdin.readline().strip().lower() not in ("y", "yes"):
                print("not approved", file=out)
                return 1
            e = R.approve(args.name, "cli", allow_network=args.allow_network, h=h)
            print(f"approved {args.name} sha256 {e['sha256'][:12]}", file=out)
            return 0
    except R.ActionError as e:
        print(f"ga actions: {e}", file=sys.stderr)
        return 1
    return 2
