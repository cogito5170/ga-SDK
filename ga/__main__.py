"""``python -m ga`` (or ``ga``) — the local, no-infrastructure front of the hub loop.

    ga tick    [--dry-run]                      one pass of the loop (cron it for a safety net)
    ga post    --channel S --from S FILE         a session hands in a report (FILE: report/1 text, - = stdin)
    ga send    FILE                              the hub sends a directive (directive/1 text)
    ga answer  QUESTION-ID LABEL [--note TEXT]   the user answers a gate question
    ga prompt  SESSION | --hub                   print a session start prompt (§4b)
    ga check   FILE...                           validate form texts and the shell commands in them (R13)
    ga render                                    re-render the Markdown records

Common: --config PATH (default ga.json), --ga-dir PATH (default <config dir>/.ga).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as gacfg
from . import rules
from .adapters.git import GitVcs
from .adapters.human import FileJudge
from .adapters.mailbox import FileMailbox
from .adapters.headless import HeadlessRunner
from .adapters.runner import ManualRunner
from .forms import FormError, hard, parse_post, parse_text, validate
from .hub import Hub
from .prompts import hub_prompt, worker_prompt
from .records import RecordStore


def _read(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def _hub(args) -> Hub:
    cfg = gacfg.load(args.config)
    ga_dir = Path(args.ga_dir) if args.ga_dir else Path(args.config).resolve().parent / ".ga"
    return Hub(cfg, ga_dir=ga_dir, channel=FileMailbox(ga_dir / "mailbox"), vcs=GitVcs(cfg, ga_dir),
               judge=FileJudge(ga_dir / "judge"), runner=make_runner(cfg, ga_dir))


def make_runner(cfg, ga_dir: Path):
    """config "runner": {"kind": "manual"} (default) or {"kind": "headless", "model", "timeout", "max_budget_usd",
    "executable", "permission_mode", "allowed_tools", "disallowed_tools"}."""
    r = dict(cfg.runner)
    kind = r.pop("kind", "manual")
    if kind == "manual":
        return ManualRunner(ga_dir / "outbox")
    if kind == "headless":
        return HeadlessRunner(ga_dir / "headless" / "home", **r)
    raise SystemExit(f"unknown runner kind {kind!r}")


def cmd_tick(args) -> int:
    hub = _hub(args)
    res = hub.tick(dry_run=args.dry_run)
    if res.quiet:
        return 0  # R9: say nothing when there is nothing
    out = {
        "round": res.round, "integrated": res.integrated, "blocked": res.blocked, "sent": res.sent,
        "gates": [q["id"] for q in res.gates] if not args.dry_run else [], "waiting_for": res.waiting_for,
        "verdict": (res.verdict or {}).get("class") if not args.dry_run else res.verdict, "plan": res.plan,
        "findings": [str(p) for p in res.findings],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_post(args) -> int:
    cfg = gacfg.load(args.config)
    ga_dir = Path(args.ga_dir) if args.ga_dir else Path(args.config).resolve().parent / ".ga"
    text = _read(args.file)
    try:
        head, _, notes = parse_post(text, "report/1")
    except FormError as e:
        print(f"not a valid report/1: {e}", file=sys.stderr)
        return 2
    found = rules.r1_post(cfg, args.channel, args.author) + rules.r6_secrets(cfg, text, "report")
    for p in found + notes:
        print(p, file=sys.stderr)
    if hard(found):
        return 2
    post = FileMailbox(ga_dir / "mailbox").post(args.channel, args.author, text)
    print(post.id)
    return 0


def cmd_send(args) -> int:
    hub = _hub(args)
    head, body = parse_text(_read(args.file))
    post, findings, gates = hub.send(head, body)
    for p in findings:
        print(p, file=sys.stderr)
    for g in gates:
        print(f"gate {g.number} ({g.title}): {g.reason} — not sent; answer through the hub's tick", file=sys.stderr)
    return 0 if post else 3


def cmd_answer(args) -> int:
    d = _hub(args).answer(args.question, args.label, args.note or "")
    print(d["id"])
    return 0


def cmd_prompt(args) -> int:
    cfg = gacfg.load(args.config)
    sys.stdout.write(hub_prompt(cfg) if args.hub else worker_prompt(cfg, args.session))
    return 0


def cmd_check(args) -> int:
    cfg = gacfg.load(args.config) if Path(args.config).exists() else None
    status = 0
    for f in args.files:
        text = _read(f)
        try:
            head, body = parse_text(text)
            probs = validate(head)
        except FormError as e:
            probs = e.problems
        if cfg is not None:
            probs = probs + rules.r13_commands(cfg, text) + rules.r6_secrets(cfg, text, f)
        for p in probs:
            print(f"{f}: {p}")
        if hard(probs):
            status = 2
    return status


def cmd_render(args) -> int:
    cfg = gacfg.load(args.config)
    ga_dir = Path(args.ga_dir) if args.ga_dir else Path(args.config).resolve().parent / ".ga"
    store = RecordStore(ga_dir / "records")
    for name in store.write_rendered(store.root, {r: s.slug for r, s in cfg.repos.items() if s.slug}):
        print(name)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ga", description="ga-SDK hub loop (local edition)")
    ap.add_argument("--config", default="ga.json")
    ap.add_argument("--ga-dir", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tick"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(fn=cmd_tick)
    p = sub.add_parser("post"); p.add_argument("--channel", required=True); p.add_argument("--from", dest="author", required=True)
    p.add_argument("file"); p.set_defaults(fn=cmd_post)
    p = sub.add_parser("send"); p.add_argument("file"); p.set_defaults(fn=cmd_send)
    p = sub.add_parser("answer"); p.add_argument("question"); p.add_argument("label"); p.add_argument("--note"); p.set_defaults(fn=cmd_answer)
    p = sub.add_parser("prompt"); p.add_argument("session", nargs="?"); p.add_argument("--hub", action="store_true"); p.set_defaults(fn=cmd_prompt)
    p = sub.add_parser("check"); p.add_argument("files", nargs="+"); p.set_defaults(fn=cmd_check)
    p = sub.add_parser("render"); p.set_defaults(fn=cmd_render)
    args = ap.parse_args(argv)
    if args.cmd == "prompt" and not args.hub and not args.session:
        ap.error("prompt needs SESSION or --hub")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
