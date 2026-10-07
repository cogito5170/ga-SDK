"""``ga do "<request>"`` (CMD-GA37 S5, rev 2 S6-S8): the GA CLI entry of the GA Engine. Until the planner (GA40) exists it stops
after the checked task/1 spec.

    ga do "가격 페이지에 연간 요금 토글을 추가해줘"            the backend from ga-do.json, else the router
    ga do --backend codex_cli --model gpt-5.1-codex "..."   override
    ga do --dry-run "..."                                   the repo summary and its token count; no model
    ga do --replay requests.txt                             one request per blank-line block, same state store

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
from .engine import Intake, Result, handle, overhead_of
from .fragment import withhold
from .state import State
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
    from ..llm import create_runner
    opts = dict(options)
    if backend == "claude_cli":
        opts["bare"] = True  # S3: tools off, INSTRUCTION as the system prompt; never ctx tools
    return create_runner(backend, model, opts, {"cwd": str(root), "timeout_s": timeout_s})


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
            L += _kind_lines(res, True)
            L.append(f"목표: {s.get('goal', '')}")
            if s.get("deliverables") or s.get("acceptance"):
                L.append("\n## 산출물")
                L += [f"- {d['id']} {d['what']} ({d['where']})" for d in s.get("deliverables", [])]
                L.append("\n## 수용 기준")
                L += [f"- {a['id']} [{a['kind']}] {a['check']}" for a in s.get("acceptance", [])]
            L.append("\n## 가정(기본값)")
            L += [f"- {a['text']} → {a['default']}" for a in s.get("assumptions", [])] or ["- 없음"]
            L.append("\n## 사람에게 묻는 것")
            L += [f"- [{q['needs']}] {q['text']}" for q in res.human_questions()] or ["- 없음"]
            if (res.outcome or {}).get("next") == "planner" or not res.outcome:
                L.append("\n다음 단계: 계획기(GA40) — 아직 없어 여기서 멈춤.")
        else:
            L.append("## 문제")
            L += [f"- {p}" for p in res.problems]
        L.append(f"\n토큰: 입력 {tk['input']} · 출력 {tk['output']} · 캐시 읽기 {tk['cache_read']} · 턴 {len(res.turns)}")
    else:
        L.append(f"# Task {res.task_id} — {res.status}")
        L.append(f"\nRequest: {request}\n")
        if res.status == "ready":
            L += _kind_lines(res, False)
            L.append(f"Goal: {s.get('goal', '')}")
            if s.get("deliverables") or s.get("acceptance"):
                L.append("\n## Deliverables")
                L += [f"- {d['id']} {d['what']} ({d['where']})" for d in s.get("deliverables", [])]
                L.append("\n## Acceptance")
                L += [f"- {a['id']} [{a['kind']}] {a['check']}" for a in s.get("acceptance", [])]
            L.append("\n## Assumptions (defaults)")
            L += [f"- {a['text']} -> {a['default']}" for a in s.get("assumptions", [])] or ["- none"]
            L.append("\n## Asked of a person")
            L += [f"- [{q['needs']}] {q['text']}" for q in res.human_questions()] or ["- none"]
            if (res.outcome or {}).get("next") == "planner" or not res.outcome:
                L.append("\nNext stage: planner (GA40) — not built yet, so ga stops here.")
        else:
            L.append("## Problems")
            L += [f"- {p}" for p in res.problems]
        L.append(f"\nTokens: input {tk['input']} · output {tk['output']} · cache read {tk['cache_read']} · "
                 f"turns {len(res.turns)}")
    return "\n".join(L) + "\n"


def _kind_lines(res: Result, ko: bool) -> list[str]:
    s, out = res.spec or {}, res.outcome or {}
    L = [("종류: " if ko else "Kind: ") + str(s.get("kind"))]
    r = s.get("resolution")
    if r:
        L.append(("해석: " if ko else "Read as: ") + f"'{r['fragment']}' → {r['to']}"
                 + ((" (가정)" if ko else " (assumed)") if r["how"] == "assumption" else ""))
    if s.get("kind") == "answer" and s.get("answer"):
        L.append(("\n## 답\n" if ko else "\n## Answer\n") + s["answer"]["text"])
        L += [f"- {c}" for c in s["answer"].get("cites", [])]
    if s.get("kind") == "decide" and s.get("decision"):
        L.append(("\n## 결정 " if ko else "\n## Decision ") + str(out.get("decision", "")) + "\n"
                 + s["decision"]["text"])
        L += [("- 영향: " if ko else "- affects: ") + w for w in s["decision"].get("affects", [])]
    if s.get("options"):
        L.append("\n## 선택지" if ko else "\n## Options")
        L += [f"{o['n']}. {o['text']}" + ((" (추천)" if ko else " (recommended)") if o.get("recommended") else "")
              for o in s["options"]]
    return L + [""]


def report(res: Result) -> dict[str, Any]:
    """A report/2-shaped summary of the intake stage (S5; rev 2: the outcome by kind)."""
    ready = res.status == "ready"
    out = res.outcome or {"status": "paused" if ready else "blocked", "next": "planner" if ready else None,
                          "outcome": "paused" if ready else "blocked"}
    extra = {k: out[k] for k in ("answer", "decision", "affects") if k in out}
    return {"shape": "report/2", "from": "ga do", "task": res.task_id, "stage": "intake",
            "kind": (res.spec or {}).get("kind"), "outcome": out["outcome"],
            "status": out["status"], "next": out["next"], **extra,
            "resolution": (res.spec or {}).get("resolution"),
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


def _blocks(text: str) -> list[str]:
    """A replay file: one request per block, blocks separated by blank lines."""
    return [b.strip() for b in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if b.strip()]


def do_main(args: Any) -> int:
    request = " ".join(args.request).strip()
    if not request and not args.replay:
        print("ga do: an empty request (or give --replay FILE)", file=sys.stderr)
        return 2
    root = Path(args.repo).resolve()
    ga_dir = Path(args.ga_dir).resolve() if getattr(args, "ga_dir", None) else root / ".ga"
    state = State(ga_dir)
    if args.dry_run:
        s = summarize(root, state=state.lines())
        sys.stdout.write(s.text)
        print(json.dumps({"summary_tokens": s.tokens, "cap": 2000, "dropped": s.dropped}, ensure_ascii=False))
        return 0
    try:
        requests = _blocks(Path(args.replay).read_text(encoding="utf-8")) if args.replay else [request]
    except OSError as e:
        print(f"ga do: {e}", file=sys.stderr)
        return 2
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
    print(f"ga do: GA Engine intake on {backend} {model} (by {how}); repo summary first", file=sys.stderr)
    intake = Intake(runner, backend=backend, model=model, ga_dir=ga_dir, overhead=overhead_of(backend, plugin),
                    on_progress=_progress)
    blocked = 0
    for i, req in enumerate(requests, 1):
        if args.replay:
            print(f"ga do: replay {i}/{len(requests)}", file=sys.stderr)
        res = handle(intake, req, root, state)
        shown = res.spec["request"] if res.spec else withhold(req)[0]
        d = write(res, shown, ga_dir)
        tk = res.tokens()
        print(json.dumps(report(res), ensure_ascii=False, separators=(",", ":")))
        print(f"ga do: {res.outcome['outcome']}; {d / 'summary.md'}; tokens in {tk['input']} out {tk['output']} "
              f"cache_read {tk['cache_read']} over {len(res.turns)} turn(s)", file=sys.stderr)
        blocked += res.status != "ready"
    return 1 if blocked else 0


def add_parser(sub: Any) -> None:
    p = sub.add_parser("do", help="GA Engine entry: a natural-language request -> a checked task/1 spec, on any "
                                  "backend (CMD-GA37)")
    p.add_argument("request", nargs="*")
    p.add_argument("--replay", metavar="FILE", help="one request per blank-line-separated block, in order, with the "
                                                    "same project state (.ga/state/)")
    p.add_argument("--backend", help="agv, claude_cli, codex_cli, openai_http, anthropic_http, or a plugin")
    p.add_argument("--model")
    p.add_argument("--repo", default=".", help="the repository the request is about (default: here)")
    p.add_argument("--do-config", help="a ga-do/1 config (default: <repo>/ga-do.json)")
    p.add_argument("--dry-run", action="store_true", help="the repo summary and its token count; no model")
    p.add_argument("--timeout", type=float, default=600.0)
    p.set_defaults(fn=do_main)
