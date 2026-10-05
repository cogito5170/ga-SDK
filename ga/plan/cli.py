"""``ga plan`` (CMD-GA40 S3). Cost is printed before the model turn and asked y/N unless --yes; the draft is written,
never sent (no mail, no session).

    ga plan "<request>" [--repo R] [--to ROLE] [--yes] [--backend B --model M] [--home H] [--in-flight CMD-A,CMD-B]
    ga plan compare <draft.md> <final.md> [--json]

Config (optional) ``<repo>/ga-plan.json``: {"schema": "ga-plan/1", "backend": "agv", "model": "gpt-oss-120b-medium",
"decision_log": "path/to/DECISION_LOG.md", "in_flight": ["CMD-GA39"], "l7": true}. Exit 0 a clean draft, 1 a draft
with errors or a failed plan, 2 a config error or a refusal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from ..ctxpack import tokens
from . import card as cardmod
from .compare import compare_files
from .draft import PlanFailed, plan, write

DEFAULT_BACKEND, DEFAULT_MODEL = "agv", "gpt-oss-120b-medium"  # ga/backends default (GA ask solve)


def home(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    if os.environ.get("GA_HOME"):
        return Path(os.environ["GA_HOME"]).expanduser()
    from ..ask.store import home as ask_home
    return ask_home()


def load_cfg(repo: Path) -> dict[str, Any]:
    p = repo / "ga-plan.json"
    if not p.exists():
        return {}
    cfg = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or cfg.get("schema", "ga-plan/1") != "ga-plan/1":
        raise ValueError(f"{p.name}: not a ga-plan/1 config")
    return cfg


def cost_lines(backend: str, model: str, card_text: str) -> list[str]:
    from ..ask.store import AGV_OVERHEAD
    t = tokens(card_text)
    fixed = AGV_OVERHEAD if backend in ("agv", "agy") else 0
    return [f"ga plan: 1 model turn on {backend} {model} (+1 repair turn only on a bad format)",
            f"ga plan: card {len(card_text.encode('utf-8')):,} bytes (<= {cardmod.CARD_MAX:,}) ~{t:,} tokens"
            + (f"; + ~{fixed:,} fixed input per {backend} turn" if fixed else ""),
            f"ga plan: estimated input ~{t + fixed:,} tokens per turn; the draft is written, never sent"]


def make_runner(backend: str, model: str, repo: Path) -> Any:
    from .. import backends
    return backends.create(backend, model, {}, {"cwd": str(repo), "timeout_s": 600.0})


def compare_main(argv: list[str], out: Callable[[str], None]) -> int:
    ap = argparse.ArgumentParser(prog="ga plan compare", description="field-level changes of a final vs a draft")
    ap.add_argument("draft")
    ap.add_argument("final")
    a = ap.parse_args(argv)
    try:
        r = compare_files(Path(a.draft), Path(a.final))
    except (OSError, ValueError) as e:
        out(f"ga plan compare: {e}")
        return 2
    out(json.dumps(r, ensure_ascii=False, separators=(",", ":")))
    return 0


def main(argv: list[str] | None = None, *, runner: Any = None, input_fn: Callable[[str], str] = input,
         out: Callable[[str], None] = print) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["compare"]:
        return compare_main(argv[1:], out)
    ap = argparse.ArgumentParser(prog="ga plan", description="GA Planner (shadow): a request -> a directive/2 draft, "
                                 "checked against the planning lessons; written, never sent (CMD-GA40)")
    ap.add_argument("request", nargs="+")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--to", help="the role the directive is for (e.g. GA)")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the y/N before the model turn")
    ap.add_argument("--backend")
    ap.add_argument("--model")
    ap.add_argument("--home", help="the ga home (default: $GA_HOME, else the GA CLI home)")
    ap.add_argument("--decision-log", help="a DECISION_LOG.md: its last 10 titles go on the card")
    ap.add_argument("--in-flight", help="comma-separated directive ids in flight (lesson L7)")
    a = ap.parse_args(argv)
    request, repo = " ".join(a.request).strip(), Path(a.repo).resolve()
    if not request:
        out("ga plan: an empty request")
        return 2
    try:
        cfg = load_cfg(repo)
    except ValueError as e:
        out(f"ga plan: {e}")
        return 2
    backend = a.backend or cfg.get("backend") or DEFAULT_BACKEND
    model = a.model or cfg.get("model") or DEFAULT_MODEL
    dlog = a.decision_log or cfg.get("decision_log")
    dlog = (repo / dlog if not Path(dlog).is_absolute() else Path(dlog)) if dlog else None
    in_flight = [x for x in (a.in_flight.split(",") if a.in_flight else cfg.get("in_flight") or []) if x]
    c = cardmod.build(request, repo, decision_log=dlog, to=a.to)
    for line in cost_lines(backend, model, c.text):
        out(line)
    if not a.yes and input_fn("계속할까요? / continue? [y/N] ").strip().lower() not in ("y", "yes", "예", "네", "ㅇ"):
        out("ga plan: stopped before the model turn (0 tokens)")
        return 2
    try:
        runner = runner or make_runner(backend, model, repo)
        d = plan(request, repo, runner, to=a.to, decision_log=dlog,
                 ctx={"in_flight": in_flight, "l7": cfg.get("l7", True)})
    except PlanFailed as e:
        out(f"ga plan: failed after {e.turns} turn(s), no draft written: {e}")
        return 1
    except (ValueError, KeyError, RuntimeError) as e:
        out(f"ga plan: {e}")
        return 2
    path = write(d, home(a.home))
    out(f"ga plan: draft {path} - {d.errors} error(s) ({len(d.findings)} lesson finding(s), {len(d.check)} ga check)")
    return 1 if d.errors else 0


__all__ = ["main", "compare_main", "cost_lines", "home"]
