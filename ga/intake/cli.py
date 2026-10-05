"""``ga do "<request>"`` (CMD-GA37 S5): the entry of the autonomy machine. Until the planner (GA40) exists it stops
after the checked task/1 spec.

    ga do "가격 페이지에 연간 요금 토글을 추가해줘"            the backend from ga-do.json, else the router
    ga do --backend codex_cli --model gpt-5.1-codex "..."   override
    ga do --dry-run "..."                                   the repo summary and its token count; no model

Writes ``<ga_dir>/tasks/<id>/task.json`` (the spec), ``summary.md`` (for a person; Korean if the request is) and
``report.json`` (a report/2-shaped summary: status paused, next stage planner; blocked with the problems otherwise),
and prints the tokens used. Exit 0 ready, 1 blocked, 2 a config error.

ga-do.json (optional, in the repo): {"schema": "ga-do/1", "backend": "claude_cli", "model": "claude-haiku-4-5-20251001",
"options": {...}, "router": {"backends": ["claude_cli", "openai_http"]}}. No backend and no model anywhere: the GA31
router picks the cheapest R0 text entry among ``router.backends`` (default: claude_cli).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from .. import backends
from ..backends.base import ConfigError
from ..forms import canonical_json
from .engine import Intake, Result, overhead_of
from .summary import summarize

DEFAULT_ROUTED = ["claude_cli"]
_HANGUL = re.compile(r"[가-힣]")


def resolve(cli_backend: str | None, cli_model: str | None, cfg: dict[str, Any]) -> tuple[str, str, dict, str]:
    """(backend, model, options, how): CLI flags, then ga-do.json, then the router."""
    backend = cli_backend or cfg.get("backend")
    model = cli_model or cfg.get("model")
    options = dict(cfg.get("options") or {}) if (cli_backend is None or cli_backend == cfg.get("backend")) else {}
    if backend and model:
        return backend, model, options, "flag" if cli_backend or cli_model else "config"
    from ..net.router import Router, catalog_for
    names = [backend] if backend else list((cfg.get("router") or {}).get("backends") or DEFAULT_ROUTED)
    entries = catalog_for(names, None, backends.get)
    if model:
        entries = [e for e in entries if e["model"] == model] or entries
    ch = Router(entries).pick("intake", {"tier": "R0", "capabilities": ["text"]})
    return ch.backend, model or ch.model, dict(options, **ch.options), "router"


def make_runner(backend: str, model: str, options: dict, root: Path, timeout_s: float) -> Any:
    opts = dict(options)
    if backend == "claude_cli":
        opts["bare"] = True  # S3: tools off, INSTRUCTION as the system prompt; never ctx tools
    return backends.create(backend, model, opts, {"cwd": str(root), "timeout_s": timeout_s})


def is_korean(text: str) -> bool:
    return bool(_HANGUL.search(text))


def human_summary(res: Result, request: str) -> str:
    ko = is_korean(request)
    s, tk = res.spec or {}, res.tokens()
    L: list[str] = []
    if ko:
        L.append(f"# 과제 {res.task_id} — {'준비됨' if res.status == 'ready' else '막힘'}")
        L.append(f"\n요청: {request}\n")
        if res.status == "ready":
            L.append(f"목표: {s.get('goal', '')}\n\n## 산출물")
            L += [f"- {d['id']} {d['what']} ({d['where']})" for d in s.get("deliverables", [])]
            L.append("\n## 수용 기준")
            L += [f"- {a['id']} [{a['kind']}] {a['check']}" for a in s.get("acceptance", [])]
            L.append("\n## 가정(기본값)")
            L += [f"- {a['text']} → {a['default']}" for a in s.get("assumptions", [])] or ["- 없음"]
            L.append("\n## 사람에게 묻는 것")
            L += [f"- [{q['needs']}] {q['text']}" for q in res.human_questions()] or ["- 없음"]
            L.append("\n다음 단계: 계획기(GA40) — 아직 없어 여기서 멈춤.")
        else:
            L.append("## 문제")
            L += [f"- {p}" for p in res.problems]
        L.append(f"\n토큰: 입력 {tk['input']} · 출력 {tk['output']} · 캐시 읽기 {tk['cache_read']} · 턴 {len(res.turns)}")
    else:
        L.append(f"# Task {res.task_id} — {res.status}")
        L.append(f"\nRequest: {request}\n")
        if res.status == "ready":
            L.append(f"Goal: {s.get('goal', '')}\n\n## Deliverables")
            L += [f"- {d['id']} {d['what']} ({d['where']})" for d in s.get("deliverables", [])]
            L.append("\n## Acceptance")
            L += [f"- {a['id']} [{a['kind']}] {a['check']}" for a in s.get("acceptance", [])]
            L.append("\n## Assumptions (defaults)")
            L += [f"- {a['text']} -> {a['default']}" for a in s.get("assumptions", [])] or ["- none"]
            L.append("\n## Asked of a person")
            L += [f"- [{q['needs']}] {q['text']}" for q in res.human_questions()] or ["- none"]
            L.append("\nNext stage: planner (GA40) — not built yet, so ga stops here.")
        else:
            L.append("## Problems")
            L += [f"- {p}" for p in res.problems]
        L.append(f"\nTokens: input {tk['input']} · output {tk['output']} · cache read {tk['cache_read']} · "
                 f"turns {len(res.turns)}")
    return "\n".join(L) + "\n"


def report(res: Result) -> dict[str, Any]:
    """A report/2-shaped summary of the intake stage (S5)."""
    ready = res.status == "ready"
    return {"shape": "report/2", "from": "ga do", "task": res.task_id, "stage": "intake",
            "status": "paused" if ready else "blocked", "next": "planner" if ready else None,
            "items": [{"id": "intake", "state": "done" if ready else "blocked",
                       "evidence": [f"tasks/{res.task_id}/task.json"]}],
            "blockers": [] if ready else [{"kind": "problem", "what": p} for p in res.problems],
            "questions": res.human_questions(), "assumptions": res.assumptions(), "notes": res.notes,
            "tokens": res.tokens(), "turns": len(res.turns),
            "summary_tokens": res.summary.tokens}


def write(res: Result, request: str, ga_dir: Path) -> Path:
    d = ga_dir / "tasks" / res.task_id
    d.mkdir(parents=True, exist_ok=True)
    if res.spec is not None:
        (d / "task.json").write_text(canonical_json(res.spec), encoding="utf-8")
    (d / "summary.md").write_text(human_summary(res, request), encoding="utf-8")
    (d / "report.json").write_text(canonical_json(report(res)), encoding="utf-8")
    return d


def _progress(ev: dict) -> None:
    if ev["event"] == "started":
        print(f"ga do: turn {ev['turn']} ({ev['kind']}) on {ev['backend']} {ev['model']} ...", file=sys.stderr)
    elif ev["event"] == "waiting":
        print(f"ga do: ... still waiting on {ev['backend']} ({ev['seconds']}s)", file=sys.stderr)
    elif ev["event"] == "done":
        print(f"ga do: turn {ev['turn']} " + (f"failed: {ev['error']}" if ev.get("error")
                                              else f"in {ev.get('input')} / out {ev.get('output')} tokens"),
              file=sys.stderr)


def do_main(args: Any) -> int:
    request = " ".join(args.request).strip()
    if not request:
        print("ga do: an empty request", file=sys.stderr)
        return 2
    root = Path(args.repo).resolve()
    if args.dry_run:
        s = summarize(root)
        sys.stdout.write(s.text)
        print(json.dumps({"summary_tokens": s.tokens, "cap": 2000, "dropped": s.dropped}, ensure_ascii=False))
        return 0
    cfg_path = Path(args.do_config) if args.do_config else root / "ga-do.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        if not isinstance(cfg, dict) or cfg.get("schema", "ga-do/1") != "ga-do/1":
            raise ConfigError(f"{cfg_path.name}: not a ga-do/1 config")
        backend, model, options, how = resolve(args.backend, args.model, cfg)
        runner = make_runner(backend, model, options, root, args.timeout)
        plugin = backends.get(backend)
    except (ConfigError, KeyError, ValueError, RuntimeError) as e:
        print(f"ga do: {e}", file=sys.stderr)
        return 2
    ga_dir = Path(args.ga_dir).resolve() if getattr(args, "ga_dir", None) else root / ".ga"
    print(f"ga do: {backend} {model} (by {how}); repo summary first", file=sys.stderr)
    res = Intake(runner, backend=backend, model=model, ga_dir=ga_dir, overhead=overhead_of(backend, plugin),
                 on_progress=_progress).run(request, root)
    d = write(res, request, ga_dir)
    tk = res.tokens()
    print(json.dumps(report(res), ensure_ascii=False, separators=(",", ":")))
    print(f"ga do: {res.status}; {d / 'summary.md'}; tokens in {tk['input']} out {tk['output']} "
          f"cache_read {tk['cache_read']} over {len(res.turns)} turn(s)", file=sys.stderr)
    return 0 if res.status == "ready" else 1


def add_parser(sub: Any) -> None:
    p = sub.add_parser("do", help="a natural-language request -> a checked task/1 spec, on any backend (CMD-GA37)")
    p.add_argument("request", nargs="+")
    p.add_argument("--backend", help="agv, claude_cli, codex_cli, openai_http, anthropic_http, or a plugin")
    p.add_argument("--model")
    p.add_argument("--repo", default=".", help="the repository the request is about (default: here)")
    p.add_argument("--do-config", help="a ga-do/1 config (default: <repo>/ga-do.json)")
    p.add_argument("--dry-run", action="store_true", help="the repo summary and its token count; no model")
    p.add_argument("--timeout", type=float, default=600.0)
    p.set_defaults(fn=do_main)
