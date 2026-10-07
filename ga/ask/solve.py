"""``ga ask --solve "<question>"`` and ga ui's Solve box (CMD-GA36 S6): one ga supervise loop with ga outside.

- the backend and model the person chose (default agv gpt-oss-120b-medium; any agv slug from ``agy models``, or
  openai_http with a base_url such as a local server); tools only from ga's table (``ga.bridge.tools``); a turn cap
  (default 8) and, on agv, the daily agy cap
- before it starts: backend, model, cap, estimate (turns x the backend's measured fixed overhead), y/N — nothing runs
  without a yes, ``--yes`` included
- while it runs: one line per turn from the supervise log (turn n, input, output tokens, seconds, tool steps); the
  waiting is code reading that log, no model turn is spent on it
- at the end: the answer, then the totals (the sums of the turn lines), the share of input that was the backend's
  fixed overhead, one row in the day ledger. TOOL_NEEDED lines and tools outside the table are listed, never installed.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..bridge.tools import TOOLS, table as tools_table
from ..runlog import RunLog, Tail, TurnMeter, follow, redact
from .store import AGV_OVERHEAD, DayTurns, Ledger

TOOL_NEEDED = re.compile(r"^\s*TOOL_NEEDED:\s*(.+?)\s*$", re.M)


class SolveRefused(Exception):
    """Not started: a bad choice, the daily cap, or no confirmation. ``code`` is the exit code."""

    def __init__(self, msg: str, code: int = 2):
        super().__init__(msg)
        self.code = code


def overhead_of(backend: str, model: str, ledger: Ledger) -> tuple[int, str]:
    """The backend's fixed input tokens per turn and where the number comes from."""
    if backend in ("agv", "agy"):
        return AGV_OVERHEAD, "measured: AGY_FACTS V0 (9,852 input tokens per agy turn)"
    from .. import backends
    try:
        plug = backends.get(backend)
    except KeyError:
        plug = None
    ov = dict(getattr(plug, "overhead", None) or {})
    tok = ov.get("tokens")
    if isinstance(tok, dict):
        tok = next((v for k, v in tok.items() if k in model), None)
    if isinstance(tok, int):
        return tok, str(ov.get("source") or "the backend's overhead")
    n, src = ledger.per_turn_input(backend)
    return n, f"not measured for {backend}; per-turn input {src}"


def build_config(home: Path, run_id: str, backend: str, model: str, cap: int, opts: dict[str, Any]) -> Path:
    d = home / "solve" / run_id
    d.mkdir(parents=True, exist_ok=True)
    conf = {"schema": "ga-supervise/1", "backend": backend, "model": model, "options": opts,
            "tools": tools_table(), "max_model_steps": int(cap), "state_dir": "state"}
    path = d / "ga-supervise.json"
    path.write_text(json.dumps(conf, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


class Watch:
    """Wraps the backend runner: keeps, per turn, the plan's tool names outside the table and TOOL_NEEDED lines
    (labels only). The supervisor's own plan check is still the authority that refuses them."""

    def __init__(self, inner: Any, root: Any = None):
        self.inner, self.outside, self.needed, self.proposed, self.root = inner, [], [], [], root
        for k in ("resumes", "bare", "usage_format", "quota_family"):
            if hasattr(inner, k):
                setattr(self, k, getattr(inner, k))
        if callable(getattr(inner, "usage", None)):
            self.usage = inner.usage

    def run_turn(self, *a: Any, **kw: Any) -> Any:
        turn = self.inner.run_turn(*a, **kw)  # llm-scan: inside a turn the caller already took through ga.llm.run_turn
        from ..gemini import PlanError, TOOL_NAME, extract_plan
        try:
            plan = extract_plan(turn.text)
            for s in plan.get("steps", []) if isinstance(plan, dict) else []:
                t = s.get("tool") if isinstance(s, dict) else None
                if isinstance(t, str) and t not in TOOLS and TOOL_NAME.fullmatch(t) and t not in self.outside:
                    self.outside.append(t)
        except (PlanError, AttributeError, TypeError):
            pass
        self.needed += [x[:200] for x in TOOL_NEEDED.findall(turn.text or "") if x[:200] not in self.needed]
        if self.root is not None:  # CMD-GA42 S2: PROPOSE blocks and TOOL_NEEDED lines become proposals (never tools)
            from .. import actions
            try:
                self.proposed += [r["name"] for r in actions.from_text(turn.text or "", self.root, source="ga supervise")]
            except (OSError, ValueError):
                pass
        return turn


def plan_lines(p: dict[str, Any]) -> list[str]:
    return [f"백엔드: {p['backend']} · 모델: {p['model']}",
            f"턴 한도: {p['cap']}" + (f" (오늘 남은 agy 턴 {p['left']} / 한도 {p['daily_cap']})" if p.get("left") is not None else ""),
            f"도구: ga 도구 표만 ({', '.join(TOOLS)})",
            f"예상 입력 토큰: 최대 {p['cap']} 턴 × 고정 오버헤드 {p['overhead']:,} = ~{p['estimate']:,} "
            f"({p['overhead_source']})"]


def run(question: str, *, home: Path, settings: dict[str, Any], line: Callable[[str], None],
        confirm: Callable[[list[str]], bool], backend: str | None = None, model: str | None = None,
        cap: int | None = None, base_url: str | None = None, cli: Any = None, clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep, poll_s: float = 0.2, runlog: RunLog | None = None) -> dict[str, Any]:
    """One solve loop. ``confirm(lines)`` sees the plan and says yes or no; ``line`` gets every line shown."""
    from .. import gemini as G
    from ..forms import FormError
    s = settings.get("solve") or {}
    backend = backend or s.get("backend") or "agv"
    model = model or s.get("model") or "gpt-oss-120b-medium"
    cap = int(cap or s.get("cap") or 8)
    base_url = base_url or s.get("base_url")
    if cap < 1:
        raise SolveRefused("턴 한도는 1 이상이어야 합니다")
    ledger = Ledger(home, clock)
    day = DayTurns(home, int(settings.get("daily_agy_turns", 10)), clock)
    agv = backend in ("agv", "agy")
    left = day.left() if agv else None
    if agv and left == 0:
        raise SolveRefused(f"오늘 agy 턴 한도({day.cap})를 다 썼습니다 — 내일 다시 하거나 ask.json의 daily_agy_turns를 "
                           "바꾸세요", 3)
    turns_cap = min(cap, left) if agv else cap
    opts: dict[str, Any] = {}
    if agv and settings.get("agy_cli"):
        opts["cli"] = list(settings["agy_cli"])
    if backend in ("openai_http", "anthropic_http"):
        if not base_url:
            raise SolveRefused(f"{backend}에는 base_url이 필요합니다 (예: http://127.0.0.1:8080/v1)")
        opts["base_url"] = base_url
        if s.get("key_env"):
            opts["key_env"] = s["key_env"]
    run_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock())) + "-solve-" + uuid.uuid4().hex[:6]
    conf_path = build_config(home, run_id, backend, model, turns_cap, opts)
    try:
        cfg = G.load_config(conf_path)
    except FormError as e:
        raise SolveRefused("설정 오류: " + "; ".join(str(p) for p in e.problems)[:300]) from None
    overhead, src = overhead_of(backend, model, ledger)
    plan = {"backend": backend, "model": model, "cap": turns_cap, "left": left, "daily_cap": day.cap,
            "overhead": overhead, "overhead_source": src, "estimate": turns_cap * overhead}
    lines = plan_lines(plan)
    if not confirm(lines):
        raise SolveRefused("취소했습니다 (아무것도 실행하지 않음)", 1)
    log = runlog or RunLog(home / "runs", run_id)

    def show(text: str) -> None:
        log.write(text)
        line(redact(text))
    show(f"[solve {run_id}] 시작: {backend} {model}, 턴 한도 {turns_cap}")
    pidf = home / "solve.pid"
    pidf.write_text(str(os.getpid()), encoding="utf-8")
    if "AGY_BRIDGE_COMMANDS" not in os.environ and settings.get("commands"):
        os.environ["AGY_BRIDGE_COMMANDS"] = json.dumps(settings["commands"])
    sup_out = io.StringIO()
    try:
        sup = G.Supervisor(cfg, cli=cli, out=sup_out)
    except FormError as e:
        raise SolveRefused("설정 오류: " + "; ".join(str(p) for p in e.problems)[:300]) from None
    watch = Watch(sup.cli, Path.cwd())
    sup.cli = watch
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["ok"] = sup.start(question)
        except Exception as e:  # reported below, never retried by asking a model
            box["error"] = type(e).__name__
    th = threading.Thread(target=work, daemon=True)
    meter = TurnMeter()
    th.start()
    follow(lambda: not th.is_alive(), Tail(sup.log_file, start=0), meter, show, sleep=sleep, poll_s=poll_s)
    th.join()
    try:
        pidf.unlink()
    except OSError:
        pass
    t = meter.totals()
    if agv:
        day.add(t["turns"])
    share = round(min(t["turns"] * overhead / t["input"], 1.0), 4) if t["input"] else None
    status = meter.end.get("status") or ("error" if "error" in box else "?")
    answer = redact((sup.st.get("say") or "").strip()) or "(답 없음)"
    show("── 답 ──")
    show(answer)
    refused = list(watch.outside)
    needed = list(watch.needed)
    for name in refused:
        show(f"거부된 도구 (ga 도구 표에 없음, 설치하지 않음): {name}")
    for n in needed:
        show(f"필요한 도구 (TOOL_NEEDED, 설치하지 않음): {n}")
    for n in watch.proposed:
        show(f"제안된 도구 (GA Actions, 사람이 승인하기 전에는 쓸 수 없음): {n} — ga actions show {n}")
    for d in sorted(set(meter.denied)):
        show(f"agy가 거부한 동작: {d}")
    show(f"── 합계 ── 턴 {t['turns']} · 입력 {t['input']:,} · 출력 {t['output']:,} 토큰 · {t['seconds']:g}초 · "
         f"도구 {t['tool_steps']}단계 · 고정 오버헤드 비중 "
         + ("?" if share is None else f"{share * 100:.1f}% ({t['turns']} × {overhead:,} / {t['input']:,})")
         + f" · 상태 {status}")
    row = ledger.add({"kind": "solve", "run": run_id, "backend": backend, "model": model, **t,
                      "overhead_tokens": overhead, "overhead_share": share, "status": status,
                      "refused_tools": refused, "tool_needed": len(needed)})
    log.close(status)
    return {"run": run_id, "status": status, "answer": answer, "totals": t, "turns": list(meter.turns),
            "overhead": overhead, "overhead_share": share, "refused": refused, "needed": needed, "ledger": row,
            "plan": plan, "error": box.get("error")}
