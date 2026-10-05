"""``ga ask`` (CMD-GA36): GA CLI for people who do not want to remember commands.

    ga ask "뭐 할 수 있어?"                 a question -> one ga action, by local rules (ga.ask.intents; no model)
    ga ask --model "..."                     one agy turn, tools off, question + <=1500 tokens of context
    ga ask --solve "..."                     one ga supervise loop with ga outside (ga.ask.solve)
    ga ask ... --yes                         skips the y/N for free and mail actions only — never for a model action

Cost classes: free runs at once; mail (sends a form) and model (spends tokens) print what will run and the estimate
first and ask y/N. A daily cap counts agy turns (default 10). Waiting is code reading the run log (ga.runlog).
GA UI (``ga ui``) is a browser page over this same engine.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..runlog import RunLog, redact
from . import intents as I
from .store import DayTurns, Ledger, home as default_home, settings as load_settings

EXIT = {"ok": 0, "unmatched": 1, "declined": 1, "error": 2, "cap": 3}


class Refused(Exception):
    def __init__(self, msg: str, code: int = 2):
        super().__init__(msg)
        self.code = code


def needs_confirm(cost: str, yes: bool = False) -> bool:
    """A model action always asks; a mail action asks unless --yes; a free action never."""
    return cost == "model" or (cost == "mail" and not yes)


class Engine:
    """One engine for GA CLI (``ga ask``) and GA UI (``ga ui``). Every model or mail action goes through ``execute``,
    which refuses it without a confirmation, and every agy turn through the daily cap."""

    def __init__(self, home: Path | None = None, *, out: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 bridge_runner: Callable[..., dict] | None = None, solve_cli: Any = None, box: Any = None):
        self.home = Path(home) if home else default_home()
        self.out, self.clock, self.sleep = out, clock, sleep
        self.settings = load_settings(self.home)
        self.day = DayTurns(self.home, int(self.settings.get("daily_agy_turns", 10)), clock)
        self.ledger = Ledger(self.home, clock)
        self.bridge_runner, self.solve_cli, self.box = bridge_runner, solve_cli, box
        self.runs = self.home / "runs"
        self.log: RunLog | None = None

    # -- output: the terminal (or the ui) and the run log ------------------------------------------------------------
    def show(self, text: str) -> None:
        if self.log is not None:
            self.log.write(text)
        self.out(redact(text))

    def new_log(self, kind: str) -> RunLog:
        rid = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock())) + f"-{kind}-" + uuid.uuid4().hex[:6]
        return RunLog(self.runs, rid)

    # -- the bridge config (mail, run, next, report) ----------------------------------------------------------------
    def bridge_cfg(self) -> dict[str, Any]:
        from ..bridge import load_config
        p = Path(str(self.settings.get("bridge_config"))).expanduser()
        if not p.is_file():
            raise Refused(f"브리지 설정 {p} 이(가) 없습니다 — ~/agy-bridge.json을 만들거나 GA_BRIDGE_CONFIG를 지정하세요")
        try:
            return load_config(str(p))
        except SystemExit as e:
            raise Refused(str(e)) from None

    def mailbox(self, cfg: dict[str, Any]) -> Any:
        if self.box is not None:
            return self.box
        from ..mailbox import Mailbox
        return Mailbox(cfg["mailbox_repo"])

    def directives(self, cfg: dict[str, Any]) -> list[Any]:
        return [m for m in self.mailbox(cfg).unread(cfg["name"]) if m.sender == cfg["hub"] and m.schema == "directive/2"]

    # -- plan: what will run, what it costs ---------------------------------------------------------------------------
    def per_turn(self) -> tuple[int, str]:
        return self.ledger.per_turn_input("agv")

    def supervise_turns(self, cfg: dict[str, Any]) -> int:
        try:
            raw = json.loads((Path(cfg["workdir"]) / cfg["supervise_config"]).read_text(encoding="utf-8"))
            return int(raw.get("max_model_steps") or 20)
        except (OSError, ValueError):
            return 20

    def prepare(self, name: str) -> dict[str, Any]:
        """{intent, cost, label, lines, refuse}: the lines shown before anything runs."""
        it = I.BY_NAME[name]
        p: dict[str, Any] = {"intent": name, "cost": it.cost, "label": it.label, "lines": [], "refuse": None,
                             "argv": " ".join(it.argv)}
        if it.cost == "model":
            left = self.day.left()
            per, src = self.per_turn()
            try:
                cfg = self.bridge_cfg()
                turns = min(self.supervise_turns(cfg), left)
                waiting = self.directives(cfg)
            except Refused as e:
                p["refuse"] = str(e)
                return p
            except Exception as e:  # the mailbox could not be read
                p["refuse"] = f"메일함을 읽지 못했습니다: {type(e).__name__}: {str(e)[:150]}"
                return p
            if left < 1:
                p["refuse"] = f"오늘 agy 턴 한도({self.day.cap})를 다 썼습니다 — 내일 다시 하세요"
                return p
            if not waiting:
                p["refuse"] = "실행할 새 지시가 없습니다"
                return p
            p["turns"], p["estimate"] = turns, turns * per
            p["lines"] = [f"실행할 것: {p['argv']} — {waiting[0].form} 를 ga supervise로 한 번",
                          f"agy 턴: 최대 {turns} (오늘 남은 턴 {left} / 한도 {self.day.cap})",
                          f"예상 입력 토큰: ~{turns} × {per:,} = {turns * per:,} ({src})"]
        elif it.cost == "mail":
            try:
                cfg = self.bridge_cfg()
                f = self.prepared_report(cfg)
            except Refused as e:
                p["refuse"] = str(e)
                return p
            p["file"] = str(f)
            p["lines"] = [f"보낼 것: {f} → {cfg['hub']} (ga check 후 ga mail send)", "모델 토큰: 0 (메일만)"]
        return p

    # -- the guard -------------------------------------------------------------------------------------------------
    def execute(self, name: str, *, confirmed: bool = False, yes: bool = False) -> int:
        it = I.BY_NAME[name]
        if needs_confirm(it.cost, yes) and not confirmed:
            self.show(f"확인하지 않아 실행하지 않았습니다: {it.label}")
            return EXIT["declined"]
        fn = getattr(self, f"do_{name}")
        try:
            return int(fn() or 0)
        except Refused as e:
            self.show(str(e))
            return e.code

    # -- free actions ------------------------------------------------------------------------------------------------
    def do_help(self) -> int:
        self.show("ga ask — 이렇게 물어보세요 (모델 없이 규칙으로 고릅니다):")
        for it in I.TABLE:
            cost = {"free": "무료", "mail": "메일·확인", "model": "토큰·확인"}[it.cost]
            self.show(f"  {it.name:7} [{cost}] {it.label} — 예: \"{it.examples[0]}\"")
        self.show("  ga ask --model \"질문\"   agy 1턴 (도구 끔), 확인 후")
        self.show("  ga ask --solve \"문제\"   ga supervise 루프 (턴 한도 8), 확인 후")
        self.show("  ga ui                    같은 기능을 브라우저에서")
        return 0

    def do_status(self) -> int:
        d = self.ledger.day()
        try:
            cfg = self.bridge_cfg()
            n = sum(1 for _ in self.mailbox(cfg).unread(cfg["name"]))
            self.show(f"새 메일: {n}통 ({cfg['name']})")
        except Refused as e:
            self.show(f"새 메일: 알 수 없음 — {e}")
        except Exception as e:
            self.show(f"새 메일: 알 수 없음 — {type(e).__name__}")
        last = self.last_run()
        self.show("마지막 실행: " + (f"{last['id']} ({last['status']}) — {last['tail']}" if last else "없음"))
        self.show(f"오늘: agy 턴 {self.day.used()} / {self.day.cap} · 입력 {d['input']:,} · 출력 {d['output']:,} 토큰 · "
                  f"{d['seconds']:g}초 · 실행 {d['runs']}회")
        return 0

    def last_run(self) -> dict[str, str] | None:
        if not self.runs.exists():
            return None
        logs = sorted(self.runs.glob("*.log"), key=lambda p: (p.stat().st_mtime, p.name))
        if not logs:
            return None
        p = logs[-1]
        lines = p.read_text(encoding="utf-8").splitlines()
        done = p.with_suffix(".done")
        return {"id": p.stem, "status": done.read_text(encoding="utf-8").strip() if done.exists() else "running",
                "tail": lines[-1] if lines else ""}

    def do_next(self) -> int:
        from ..bridge import task_text
        from ..forms import parse_text
        cfg = self.bridge_cfg()
        ds = self.directives(cfg)
        if not ds:
            self.show("새 지시가 없습니다")
            return 0
        head, _ = parse_text(ds[0].text)
        text = task_text(head)
        self.show(text if len(text) <= 800 else text[:800] + " …")
        if len(ds) > 1:
            self.show(f"(그 뒤로 {len(ds) - 1}개 더)")
        return 0

    def do_usage(self) -> int:
        rows = self.ledger.rows()
        if not rows:
            self.show("아직 기록이 없습니다 (ledger 비어 있음)")
            return 0
        self.show("실행별 (최근 10):")
        for r in rows[-10:]:
            self.show(f"  {r.get('date')} {r.get('kind', '?'):5} {r.get('backend', '')} {r.get('model', '')}: 턴 "
                      f"{r.get('turns', 0)} · 입력 {r.get('input', 0):,} · 출력 {r.get('output', 0):,} · "
                      f"{r.get('seconds', 0):g}초")
        self.show("날짜별:")
        for date in sorted({r.get("date") for r in rows if r.get("date")})[-7:]:
            d = self.ledger.day(date)
            self.show(f"  {date}: 실행 {d['runs']} · 턴 {d['turns']} · 입력 {d['input']:,} · 출력 {d['output']:,} · "
                      f"{d['seconds']:g}초")
        return 0

    def do_doctor(self) -> int:
        from . import doctor
        rows = doctor.checks(self.settings, self.runs)
        for line in doctor.report(rows).splitlines():
            self.show(line)
        return 0 if all(ok for _, ok, _ in rows) else 1

    def do_stop(self) -> int:
        stopped = 0
        for name in ("bridge.pid", "solve.pid"):
            f = self.home / name
            if not f.exists():
                continue
            try:
                pid = int(f.read_text(encoding="utf-8").strip())
                if pid != os.getpid():
                    os.kill(pid, signal.SIGTERM)
                    stopped += 1
                    self.show(f"멈춤: {name[:-4]} (pid {pid})")
            except (ValueError, ProcessLookupError, PermissionError):
                pass
            f.unlink(missing_ok=True)
        if not stopped:
            self.show("실행 중인 브리지가 없습니다")
        return 0

    # -- mail ------------------------------------------------------------------------------------------------------
    def prepared_report(self, cfg: dict[str, Any]) -> Path:
        d = Path(cfg["workdir"]) / "reports"
        files = sorted(d.glob("*.md"), key=lambda p: (p.stat().st_mtime, p.name)) if d.is_dir() else []
        if not files:
            raise Refused(f"준비된 보고서가 없습니다 ({d}/*.md)")
        return files[-1]

    def do_report(self) -> int:
        from ..forms import FormError, hard, parse_text, validate
        cfg = self.bridge_cfg()
        f = self.prepared_report(cfg)
        text = f.read_text(encoding="utf-8")
        try:
            probs = validate(parse_text(text)[0])
        except FormError as e:
            probs = e.problems
        if hard(probs):
            for p in probs:
                self.show(f"{f.name}: {p}")
            raise Refused("ga check에서 문제가 있어 보내지 않았습니다", 2)
        path = self.mailbox(cfg).send(cfg["hub"], text, cfg["name"])
        self.show(f"보냄: {f.name} → {cfg['hub']} ({path})")
        return 0

    # -- model: the bridge, once ------------------------------------------------------------------------------------
    def do_run(self) -> int:
        from .. import bridge
        cfg = self.bridge_cfg()
        left = self.day.left()
        if left < 1:
            raise Refused(f"오늘 agy 턴 한도({self.day.cap})를 다 썼습니다", 3)
        turns = min(self.supervise_turns(cfg), left)
        runs: list[dict] = []
        base = self.bridge_runner or bridge.run_supervise

        def runner(c: dict, conf: Path, task: str) -> dict:
            r = base(c, conf, task, self.show)
            runs.append(r)
            return r
        bridge.one_pass(cfg, box=self.mailbox(cfg), runner=runner, log=self.show, sleep=self.sleep, max_steps=turns,
                        limit=1)
        evs = [e for r in runs for e in r.get("events") or [] if e.get("event") == "turn"]
        self.day.add(len(evs))
        tot = {"turns": len(evs), "input": sum(int(e.get("input_tokens") or 0) for e in evs),
               "output": sum(int(e.get("output_tokens") or 0) for e in evs),
               "seconds": round(sum(float(e.get("seconds") or 0) for e in evs), 3), "tool_steps": 0}
        self.ledger.add({"kind": "run", "backend": "agv", "model": next((e.get("model") for e in evs), "?"), **tot})
        self.show(f"합계: 턴 {tot['turns']} · 입력 {tot['input']:,} · 출력 {tot['output']:,} 토큰 · {tot['seconds']:g}초")
        return 0 if runs else 1

    # -- model: one turn, tools off -----------------------------------------------------------------------------------
    def model_context(self) -> list[tuple[str, str]]:
        d = self.ledger.day()
        parts = [("today", json.dumps({**d, "agy_turns": self.day.used(), "cap": self.day.cap}, ensure_ascii=False)),
                 ("intents", ", ".join(f"{i.name}: {i.label}" for i in I.TABLE))]
        last = self.last_run()
        if last:
            lines = (self.runs / f"{last['id']}.log").read_text(encoding="utf-8").splitlines()[-30:]
            parts.append(("last run log", "\n".join(lines)))
        return parts

    def prepare_model(self, question: str) -> dict[str, Any]:
        from .model import PROMPT_MAX, prompt
        from ..ctxpack import tokens
        text, dropped = prompt(question, self.model_context())
        per, src = self.per_turn()
        p = {"intent": "model", "cost": "model", "text": text, "prompt_tokens": tokens(text), "refuse": None,
             "lines": [f"실행할 것: agy 1턴, 도구 끔 (--mode plan) — 프롬프트 {tokens(text):,} 토큰 (≤{PROMPT_MAX:,})"
                       + (f", 뺀 문맥: {', '.join(dropped)}" if dropped else ""),
                       f"agy 턴: 1 (오늘 남은 턴 {self.day.left()} / 한도 {self.day.cap})",
                       f"예상 입력 토큰: ~{per:,} ({src})"]}
        if self.day.left() < 1:
            p["refuse"] = f"오늘 agy 턴 한도({self.day.cap})를 다 썼습니다 — 내일 다시 하세요"
        return p

    def run_model(self, p: dict[str, Any], *, confirmed: bool) -> int:
        from .model import ModelTurnError, one_turn
        if not confirmed:
            self.show("확인하지 않아 실행하지 않았습니다: agy 1턴")
            return EXIT["declined"]
        if self.day.left() < 1:
            self.show(f"오늘 agy 턴 한도({self.day.cap})를 다 썼습니다")
            return EXIT["cap"]
        self.day.add(1)  # counted before the call: a failed turn is spent too
        try:
            r = one_turn(p["text"], cli=self.settings.get("agy_cli"), model=self.settings.get("ask_model"))
        except ModelTurnError as e:
            self.show(f"agy 1턴 실패: {e.reason}")
            return EXIT["error"]
        u = r["usage"]
        inp, outp = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
        self.ledger.add({"kind": "model", "backend": "agv", "model": self.settings.get("ask_model") or "?", "turns": 1,
                         "input": inp, "output": outp, "seconds": r["seconds"], "tool_steps": 0})
        self.show(redact(r["text"].strip()) or "(빈 답)")
        if r["denied"]:
            self.show(f"agy가 도구를 쓰려다 거부됨: {', '.join(r['denied'][:8])}")
        self.show(f"입력 {inp:,} · 출력 {outp:,} 토큰 · {r['seconds']:g}초")
        return 0

    # -- solve -------------------------------------------------------------------------------------------------------
    def solve(self, question: str, confirm: Callable[[list[str]], bool], **kw: Any) -> dict[str, Any]:
        from . import solve
        return solve.run(question, home=self.home, settings=self.settings, line=self.out, confirm=confirm,
                         cli=self.solve_cli, clock=self.clock, sleep=self.sleep, **kw)


def ask_yes_no(lines: list[str], *, input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print) -> bool:
    for line in lines:
        out(line)
    try:
        return input_fn("계속할까요? [y/N] ").strip().lower() in ("y", "yes", "예", "네", "ㅇ")
    except EOFError:
        return False


def main(argv: list[str] | None = None, *, input_fn: Callable[[str], str] = input, engine: Engine | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ga ask", description="GA CLI: ask in Korean or English; ga picks the action by "
                                 "local rules (no model). Model and mail actions show the cost and ask y/N first.")
    ap.add_argument("question", nargs="*")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--model", action="store_true", help="one agy turn, tools off, question + <=1500 tokens of context")
    g.add_argument("--solve", action="store_true", help="one ga supervise loop with ga outside (turn cap, ledger)")
    ap.add_argument("--yes", "-y", action="store_true", help="skip y/N for free and mail actions (never for a model)")
    ap.add_argument("--backend", help="--solve: agv (default) or openai_http, ...")
    ap.add_argument("--model-name", dest="model_name", help="--solve: the model (an agv slug from `agy models`)")
    ap.add_argument("--base-url", dest="base_url", help="--solve with openai_http: e.g. http://127.0.0.1:8080/v1")
    ap.add_argument("--cap", type=int, help="--solve: turn cap (default 8)")
    a = ap.parse_args(argv)
    q = " ".join(a.question).strip()
    eng = engine or Engine()

    def confirm(lines: list[str]) -> bool:
        if a.yes:
            eng.out("(--yes는 모델을 쓰는 실행에는 적용되지 않습니다 — 확인이 필요합니다)")
        return ask_yes_no(lines, input_fn=input_fn, out=eng.out)
    if a.solve:
        from .solve import SolveRefused
        if not q:
            ap.error("--solve needs a question")
        try:
            r = eng.solve(q, confirm, backend=a.backend, model=a.model_name, cap=a.cap, base_url=a.base_url)
        except SolveRefused as e:
            eng.out(str(e))
            return e.code
        return 0 if r["status"] == "done" else 1
    if a.model:
        if not q:
            ap.error("--model needs a question")
        p = eng.prepare_model(q)
        if p["refuse"]:
            eng.out(p["refuse"])
            return EXIT["cap"]
        eng.log = eng.new_log("model")
        try:
            return eng.run_model(p, confirmed=confirm(p["lines"]))
        finally:
            eng.log.close()
    m = I.route(q or "help")
    if m.intent is None:
        eng.out("무슨 뜻인지 정하지 못했습니다 (추측하지 않습니다). 가까운 것:")
        for n in m.suggestions:
            it = I.BY_NAME[n]
            eng.out(f"  {n}: {it.label} — 예: ga ask \"{it.examples[0]}\"")
        return EXIT["unmatched"]
    p = eng.prepare(m.intent.name)
    if p["refuse"]:
        eng.out(p["refuse"])
        return EXIT["cap"] if "한도" in p["refuse"] else EXIT["error"]
    confirmed = False
    if needs_confirm(p["cost"], a.yes):
        confirmed = confirm(p["lines"])
    elif p["lines"]:
        for line in p["lines"]:
            eng.out(line)
    if p["cost"] != "free":
        eng.log = eng.new_log(m.intent.name)
    try:
        return eng.execute(m.intent.name, confirmed=confirmed, yes=a.yes)
    finally:
        if eng.log is not None:
            eng.log.close()
            eng.log = None
