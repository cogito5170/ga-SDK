"""``ga ask "왜 안 돼?"`` -> doctor (CMD-GA36 S1): is ga on PATH, in a venv, is agy there, are the configs valid, and
what the last failure was — read from the run logs with fixed rules, never asked of a model.

Failure kinds (first match wins, in this order):
  sandbox    the Antigravity terminal sandbox blocked a program ("operation not permitted")
  capacity   agy's 503 MODEL_CAPACITY_EXHAUSTED: the server, retry later (not the quota)
  quota      the plan quota (or AI credits) is spent: wait for the reset
  refused    agy refused a tool call inside a turn
  missing    the model asked for a tool ga's table does not have (TOOL_NEEDED)
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

KINDS = (
    ("sandbox", re.compile(r"(?i)operation not permitted"),
     "Antigravity 터미널 샌드박스가 ga를 막았습니다. 일반 터미널(Terminal.app)에서 ga를 실행하세요."),
    ("capacity", re.compile(r"MODEL_CAPACITY_EXHAUSTED|\b503\b.{0,40}capacity|\(capacity\)|reason\W+capacity|transient:503", re.I),
     "agy 서버 용량 부족(503)입니다. 할당량 문제가 아니니 잠시 후 다시 하세요."),
    ("quota", re.compile(r"(?i)\bquota\b|AI credits|usage limit|agy_quota"),
     "agy 할당량을 다 썼습니다. 초기화 시각까지 기다리세요 (`agy -p /usage`)."),
    ("refused", re.compile(r"agy refused \d+ action|\"event\": ?\"denied\"|denied_actions"),
     "agy가 도구 실행을 거부했습니다. ga의 도구 표에 있는 도구만 쓰게 하세요."),
    ("missing", re.compile(r"TOOL_NEEDED:"),
     "필요한 도구가 ga의 도구 표에 없습니다. 보고서의 blocker로 남겼습니다 (설치하지 않음)."),
)


def classify(text: str) -> tuple[str, str] | None:
    for kind, rx, advice in KINDS:
        if rx.search(text or ""):
            return kind, advice
    return None


def last_failure(runs_dir: Path) -> tuple[str, str, str] | None:
    """(run id, kind, advice) of the newest run log that holds a failure, or None."""
    if not runs_dir.exists():
        return None
    logs = sorted(runs_dir.glob("*.log"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    for p in logs:
        hit = classify(p.read_text(encoding="utf-8", errors="replace"))
        if hit:
            return p.stem, hit[0], hit[1]
    return None


def checks(settings: dict[str, Any], runs_dir: Path) -> list[tuple[str, bool, str]]:
    """[(name, ok, detail)]: ga on PATH, venv, agy found, bridge config, supervise config, last failure."""
    out: list[tuple[str, bool, str]] = []
    ga = shutil.which("ga")
    out.append(("ga on PATH", bool(ga), ga or "ga가 PATH에 없습니다: 가상환경을 켜거나 ~/ga-venv/bin을 PATH에 넣으세요"))
    venv = sys.prefix != sys.base_prefix
    out.append(("venv", venv, sys.prefix if venv else "가상환경 밖입니다 (python3 -m venv ~/ga-venv 권장)"))
    cli = list(settings.get("agy_cli") or ["agy"])
    agy = shutil.which(cli[0])
    out.append(("agy found", bool(agy), agy or f"{cli[0]}을(를) 찾지 못했습니다: Antigravity CLI를 설치하세요"))
    bpath = Path(str(settings.get("bridge_config") or "")).expanduser()
    bcfg: dict[str, Any] | None = None
    if not bpath.is_file():
        out.append(("bridge config", False, f"{bpath} 없음 (ga bridge와 메일 기능에 필요)"))
    else:
        try:
            from ..bridge import load_config
            bcfg = load_config(str(bpath))
            out.append(("bridge config", True, str(bpath)))
        except (SystemExit, ValueError, OSError) as e:
            out.append(("bridge config", False, f"{bpath}: {e}"[:200]))
    if bcfg is not None:
        sp = Path(bcfg["workdir"]) / bcfg["supervise_config"]
        try:
            from .. import gemini
            gemini.load_config(sp)
            out.append(("supervise config", True, str(sp)))
        except Exception as e:  # FormError, or a backend that cannot load
            probs = getattr(e, "problems", None)
            out.append(("supervise config", False, f"{sp}: " + ("; ".join(map(str, probs)) if probs else type(e).__name__)[:200]))
    lf = last_failure(runs_dir)
    out.append(("last failure", lf is None, "최근 실패 없음" if lf is None else f"{lf[0]}: {lf[1]} — {lf[2]}"))
    return out


def report(rows: list[tuple[str, bool, str]]) -> str:
    return "\n".join(f"{'✓' if ok else '✗'} {name}: {detail}" for name, ok, detail in rows)


def as_json(rows: list[tuple[str, bool, str]]) -> str:
    return json.dumps([{"check": n, "ok": ok, "detail": d} for n, ok, d in rows], ensure_ascii=False)
