"""``python -m ga`` (or ``ga``) — the local, no-infrastructure front of the hub loop.

    ga setup                                    install the receiving-side R3 hook on local bare remotes
    ga sandbox SESSION [CMD…]                    manual mode: a shell for SESSION that can write only its own clone
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
import importlib
import json
import sys
from pathlib import Path

from . import config as gacfg
from . import rules
from .adapters.git import GitVcs
from .adapters.human import FileJudge
from .adapters.llm_judge import LLMJudge
from .adapters.mailbox import FileMailbox
from .adapters.headless import HeadlessRunner
from .adapters.runner import ManualRunner
from .forms import FormError, Problem, deprecated, hard, parse_post, parse_text, validate
from .hub import Hub
from .prompts import hub_prompt, worker_prompt
from .records import RecordStore


def _read(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def _hub(args) -> Hub:
    cfg = gacfg.load(args.config)
    ga_dir = Path(args.ga_dir).resolve() if args.ga_dir else Path(args.config).resolve().parent / ".ga"
    return Hub(cfg, ga_dir=ga_dir, channel=FileMailbox(ga_dir / "mailbox"), vcs=GitVcs(cfg, ga_dir),
               judge=make_judge(cfg, ga_dir), runner=make_runner(cfg, ga_dir))


def make_judge(cfg, ga_dir: Path):
    """config "judge": {"kind": "file"} (default: a person answers in .ga/judge) or
    {"kind": "llm", "model", "timeout", "max_runs", "max_budget_usd", "executable"}."""
    j = dict(cfg.judge)
    kind = j.pop("kind", "file")
    if kind == "file":
        return FileJudge(ga_dir / "judge")
    if kind == "llm":
        return LLMJudge(ga_dir / "judge" / "home", **j)
    raise SystemExit(f"unknown judge kind {kind!r}")


def make_runner(cfg, ga_dir: Path):
    """config "runner": {"kind": "manual"} (default) or {"kind": "headless", "model", "timeout", "max_budget_usd",
    "executable", "permission_mode", "allowed_tools", "disallowed_tools"}."""
    r = dict(cfg.runner)
    kind = r.pop("kind", "manual")
    r.pop("permission", None)  # read by the hub (§4c), not a Runner argument
    if kind == "manual":
        return ManualRunner(ga_dir / "outbox")
    if kind == "headless":
        return HeadlessRunner(ga_dir / "headless" / "home", **r)
    if kind == "agent_sdk":
        from .adapters.agent_sdk import AgentSDKRunner

        return AgentSDKRunner(ga_dir / "agent_sdk" / "home", **r)
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


def cmd_sandbox(args) -> int:
    """Manual mode: run a person's shell (or a command) for one session inside the same write sandbox."""
    import os

    from .adapters import sandbox

    hub = _hub(args)
    if not sandbox.available():
        print("ga sandbox: unprivileged user namespaces are not available here; R3 then rests on the hub's pull "
              "and its checks only (see README)", file=sys.stderr)
        return 3
    paths = hub.sandbox_paths(args.session)
    for r in hub.cfg.sessions[args.session].repos:
        hub.vcs.ensure_session_worktree(args.session, r)
    cmd = args.cmd or [os.environ.get("SHELL", "/bin/sh")]
    argv = sandbox.wrap(cmd, paths["protect"], paths["writable"])
    os.chdir(hub.ga / "worktrees" / args.session)
    os.execvp(argv[0], argv)


def cmd_setup(args) -> int:
    for hook in _hub(args).setup():
        print(hook)
    return 0


def cmd_post(args) -> int:
    cfg = gacfg.load(args.config)
    ga_dir = Path(args.ga_dir).resolve() if args.ga_dir else Path(args.config).resolve().parent / ".ga"
    text = _read(args.file)
    try:
        head, _, notes = parse_post(text)
        if head.get("schema") not in ("report/1", "report/2"):
            raise FormError([Problem("$.schema", f"a session posts report/2 (or report/1), not {head.get('schema')!r}")])
    except FormError as e:
        print(f"not a valid report/2 or report/1: {e}", file=sys.stderr)
        return 2
    notes = deprecated(head["schema"]) + notes
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


def cmd_review(args) -> int:
    """METHOD rev 10 §3.3b: an outside verdict (review/1) on an integrated result."""
    hub = _hub(args)
    try:
        doc = hub.review(args.by, args.repo, args.sha, args.cls, args.why, cause=args.cause)
    except FormError as e:
        print("\n".join(str(p) for p in e.problems), file=sys.stderr)
        return 2
    print(json.dumps({k: doc[k] for k in ("id", "repo", "sha", "class", "round", "amends") if k in doc}, ensure_ascii=False))
    return 0


def cmd_permit(args) -> int:
    """METHOD rev 13 §4c: the person permits a Runner that opens model turns, with its scope (decision/1 by user)."""
    budget = {k: float(v) if "." in v else int(v) for k, v in (x.split("=", 1) for x in args.budget or [])}
    if not args.judge_only and not args.runner:
        print("--runner or --judge-only is required", file=sys.stderr)
        return 2
    d = _hub(args).permit("manual" if args.judge_only else args.runner, model=args.model, sandbox=args.sandbox, budget=budget or None,
                          measurement_calls=args.measurement_calls, note=args.note or "")
    print(d["id"])
    print(f'설정에 넣는다: "runner": {{..., "permission": "{d["id"]}"}}', file=sys.stderr)
    return 0


def cmd_answer(args) -> int:
    d = _hub(args).answer(args.question, args.label, args.note or "")
    print(d["id"])
    return 0


def cmd_prompt(args) -> int:
    cfg = gacfg.load(args.config)
    if not args.hub and args.session not in cfg.sessions:
        print(f"$.sessions: no session {args.session!r} (known: {', '.join(cfg.sessions) or '-'})", file=sys.stderr)
        return 2
    try:
        text = hub_prompt(cfg) if args.hub else worker_prompt(cfg, args.session)
    except FormError as e:  # CMD-GA19 (GR1 request 2): a config error, not a KeyError traceback
        for p in e.problems:
            print(f"config: {p}", file=sys.stderr)
        return 2
    sys.stdout.write(text)
    return 0


def cmd_check(args) -> int:
    cfg = gacfg.load(args.config) if Path(args.config).exists() else None
    status = 0
    for f in args.files:
        text = _read(f)
        try:
            head, body = parse_text(text)
            probs = validate(head) + deprecated(head.get("schema"))
        except FormError as e:
            probs = e.problems
        if cfg is not None:
            probs = probs + rules.r13_commands(cfg, text) + rules.r6_secrets(cfg, text, f)
        for p in probs:
            print(f"{f}: {p}")
        if hard(probs):
            status = 2
    return status


def cmd_notify(args) -> int:
    """METHOD rev 16 §3.6: the one-line notify/1 that wakes a session; the content stays at ``ref``."""
    doc = {"schema": "notify/1", "to": args.to, "kind": args.kind, "ref": args.ref, **({"id": args.id} if args.id else {})}
    probs = validate(doc)
    if hard(probs):
        for p in probs:
            print(p, file=sys.stderr)
        return 2
    print(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    return 0


def cmd_render(args) -> int:
    cfg = gacfg.load(args.config)
    ga_dir = Path(args.ga_dir).resolve() if args.ga_dir else Path(args.config).resolve().parent / ".ga"
    store = RecordStore(ga_dir / "records")
    for name in store.write_rendered(store.root, {r: s.slug for r, s in cfg.repos.items() if s.slug}):
        print(name)
    return 0


RLO_CLI = "ga.rlo.cli"


def cmd_rlo(argv: list[str]) -> int:
    """``ga rlo ...`` (CMD-GA20): hand the rest of the command line to ``ga.rlo.cli.main`` (GR's module). The import is
    here, not at the top, so ga runs without rlo. A missing module is exit 2 naming it, never a silent pass."""
    try:
        cli = importlib.import_module(RLO_CLI)
    except ModuleNotFoundError as e:
        missing = e.name or "?"
        why = "is not there yet" if missing in (RLO_CLI, "ga.rlo") else f"is missing ({RLO_CLI} needs it)"
        print(f"ga rlo: module {missing} {why}", file=sys.stderr)
        return 2
    return int(cli.main(list(argv)) or 0)


def _rlo_argv(argv: list[str]) -> list[str] | None:
    """The arguments after ``rlo`` when it is the command (past ga's own --config / --ga-dir), else None."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--config", "--ga-dir"):
            i += 2
        elif a.startswith(("--config=", "--ga-dir=")):
            i += 1
        else:
            return argv[i + 1:] if a == "rlo" else None
    return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    rest = _rlo_argv(argv)
    if rest is not None:  # before argparse, so `ga rlo --help` and every other option reach ga.rlo.cli unchanged
        return cmd_rlo(rest)
    ap = argparse.ArgumentParser(prog="ga", description="ga-SDK hub loop (local edition)")
    ap.add_argument("--config", default="ga.json")
    ap.add_argument("--ga-dir", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tick"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(fn=cmd_tick)
    p = sub.add_parser("setup"); p.set_defaults(fn=cmd_setup)
    p = sub.add_parser("sandbox"); p.add_argument("session"); p.add_argument("cmd", nargs=argparse.REMAINDER); p.set_defaults(fn=cmd_sandbox)
    p = sub.add_parser("post"); p.add_argument("--channel", required=True); p.add_argument("--from", dest="author", required=True)
    p.add_argument("file"); p.set_defaults(fn=cmd_post)
    p = sub.add_parser("send"); p.add_argument("file"); p.set_defaults(fn=cmd_send)
    p = sub.add_parser("review", help="an outside verdict on an integrated result (review/1)")
    p.add_argument("--by", required=True); p.add_argument("--repo", required=True); p.add_argument("--sha", required=True)
    p.add_argument("--class", dest="cls", required=True); p.add_argument("--cause"); p.add_argument("--why", required=True)
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("permit", help="the person permits a model-turn Runner, with its scope (decision/1)")
    p.add_argument("--runner", choices=["headless", "agent_sdk", "remote"]); p.add_argument("--model")
    p.add_argument("--judge-only", dest="judge_only", action="store_true", help="permit the LLM Judge alone (manual Runner hub)")
    p.add_argument("--sandbox", choices=["auto", "require", "off"]); p.add_argument("--budget", action="append", help="name=number")
    p.add_argument("--measurement-calls", dest="measurement_calls", action="store_true"); p.add_argument("--note")
    p.set_defaults(fn=cmd_permit)
    p = sub.add_parser("answer"); p.add_argument("question"); p.add_argument("label"); p.add_argument("--note"); p.set_defaults(fn=cmd_answer)
    p = sub.add_parser("prompt"); p.add_argument("session", nargs="?"); p.add_argument("--hub", action="store_true"); p.set_defaults(fn=cmd_prompt)
    p = sub.add_parser("check"); p.add_argument("files", nargs="+"); p.set_defaults(fn=cmd_check)
    p = sub.add_parser("notify", help="the notify/1 line that wakes a session (METHOD rev 16)")
    p.add_argument("--to", required=True); p.add_argument("--kind", required=True, choices=["directive", "report", "verdict", "question", "ack"])
    p.add_argument("--ref", required=True); p.add_argument("--id"); p.set_defaults(fn=cmd_notify)
    p = sub.add_parser("render"); p.set_defaults(fn=cmd_render)
    sub.add_parser("rlo", add_help=False, help="rlo Autonomy commands (ga.rlo, owned by GR)")  # listed here, run above
    args = ap.parse_args(argv)
    if args.cmd == "prompt" and not args.hub and not args.session:
        ap.error("prompt needs SESSION or --hub")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
