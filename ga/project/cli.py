"""``ga project init|show|status|list|apply`` (CMD-GA54). ``init --yes`` and ``apply --yes`` are human acts: they need a
TTY, the same rule as ``ga actions approve``; without one nothing is written."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import core as K
from . import schema as S


def _tty(stdin) -> bool:
    return hasattr(stdin, "isatty") and stdin.isatty()


def _pick(name: str | None, h: Path) -> dict | None:
    have = S.names(h)
    if not name and len(have) == 1:
        name = have[0]
    if not name:
        print("ga project: give the project name (" + (", ".join(have) or "none saved — ga project init") + ")",
              file=sys.stderr)
        return None
    p = S.load(name, h)
    if p is None:
        print(f"ga project: no project {name!r} under {h / 'projects'}", file=sys.stderr)
    return p


def main(argv: list[str] | None = None, stdin=None, out=None) -> int:
    stdin, out = stdin or sys.stdin, out or sys.stdout
    ap = argparse.ArgumentParser(prog="ga project", description="one project file: repos, environment, instructions, "
                                 "routines, threads (CMD-GA54; docs/PROJECT.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("init", help="propose a project from what exists; writes only with --yes at a terminal")
    q.add_argument("--name"); q.add_argument("--from-session-file", dest="session_file")
    q.add_argument("--json", action="store_true"); q.add_argument("--yes", action="store_true")
    for name, h in (("show", "the saved project file"), ("status", "repo heads, threads, routines (read-only)")):
        q = sub.add_parser(name, help=h)
        q.add_argument("name", nargs="?"); q.add_argument("--json", action="store_true")
        if name == "status":
            q.add_argument("--fetch", action="store_true", help=f"git fetch first (at most every {K.FETCH_EVERY_S} s)")
    sub.add_parser("list", help="saved projects")
    q = sub.add_parser("apply", help="make reality match the file (dry run unless --yes at a terminal)")
    q.add_argument("name", nargs="?"); q.add_argument("--dry-run", action="store_true"); q.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    h = S.home()
    if a.cmd == "list":
        for n in S.names(h):
            p = S.load(n, h) or {}
            print(f"{n}  repos {len(p.get('repos') or [])}  routines {len(p.get('routines') or [])}", file=out)
        return 0
    if a.cmd == "init":
        prop = K.proposal(name=a.name, session_file=a.session_file, h=h)
        print(json.dumps(prop, ensure_ascii=False, indent=1) if a.json else K.render_proposal(prop), file=out)
        if not a.yes:
            print("nothing written (ga project init --yes at a terminal writes it)", file=sys.stderr)
            return 0
        if not _tty(stdin):
            print("ga project init --yes: a person approves at a terminal (TTY); nothing written", file=sys.stderr)
            return 2
        if prop["problems"]:
            print("ga project init: not written: " + "; ".join(prop["problems"])[:600], file=sys.stderr)
            return 1
        print(f"wrote {K.approve(prop, h)}", file=out)
        return 0
    p = _pick(a.name, h)
    if p is None:
        return 1
    if a.cmd == "show":
        print(json.dumps(p, ensure_ascii=False, indent=1), file=out)
        bad = S.validate(p)
        for b in bad:
            print(f"problem: {b}", file=out)
        return 1 if bad else 0
    if a.cmd == "status":
        st = K.status(p, fetch=a.fetch, h=h)
        if a.json:
            print(json.dumps(st, ensure_ascii=False, indent=1), file=out)
            return 0
        print(f"project {st['name']}  ({(st['environment'] or {}).get('kind', '?')})", file=out)
        for r in st["repos"]:
            head = (r.get("head") or "")[:7] or "missing"
            print(f"  repo {r['name']}  {r.get('branch')}  {head}{'  dirty' if r.get('dirty') else ''}  {r['path']}", file=out)
        for t in st["threads"]:
            where = f"  {t['repo']}:{t['branch']}" if t["kind"] == "branch" else ""
            print(f"  thread {t['id']}  {t['state']}{where}", file=out)
        for rt in st["routines"]:
            print(f"  routine {rt['name']}  every {rt['every']}  action {rt['action']}  last {rt['last']}"
                  f"  next {rt['next'] or '-'}", file=out)
        if st["fetch"]:
            print(f"  fetch: {st['fetch']}", file=out)
        return 0
    if a.cmd == "apply":
        yes = a.yes and not a.dry_run
        if yes and not _tty(stdin):
            print("ga project apply --yes: a person applies at a terminal (TTY); dry run instead", file=sys.stderr)
            yes = False
        return K.apply(p, yes=yes, h=h, say=lambda m: print(m, file=out))
    return 2


if __name__ == "__main__":
    sys.exit(main())
