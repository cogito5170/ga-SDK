"""G1 가드 프리셋 -- rlo 행동 모형과 grant 로 ga 설정의 `runner.guards` 항목을 만든다(GA_RLO.md §2).

작업 턴 기본값(BD-163):
- `python -m rlo.hooks` 를 **enforce** 로 건다. 판정이 ALLOW 가 아니면 막고, ALLOW 에도 `allow` 를 내지 않는다(P3).
- grant 는 **Bash 하나**다. cc_tools_model 에서 Bash 는 `external` 이라 grant 가 없으면 모든 Bash 가 A7 로 막힌다.
  더 넓히지도(다른 도구) 빼지도 않는다.
- 모형에 없는 도구(Agent · WebFetch …)는 A1 로 막힌다.
- matcher 는 `*` 다. 좁히면 나머지 도구가 가드 밖으로 나간다.
- 기록은 `{home}/rlo-{session}.jsonl`(ga 가 턴마다 늘어난 줄의 수와 라벨만 증거에 싣는다, METHOD rev 15 §4c 7).

ga 는 rlo 에 의존하지 않고 명령 문자열만 받는다. 이 모듈이 두 쪽을 잇는다: 문자열을 짓고(`rlo_guard`), 읽고(`parse`),
닫는 쪽 기본값에서 벗어난 것을 찾는다(`problems`). 판정 자체는 rlo 의 것이다.
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

NAME = "rlo"
MODE = "enforce"
GRANTS = ("Bash",)
RECORD = "{home}/rlo-{session}.jsonl"
MODEL_FILE = "cc_tools_model.json"
# the local guard command: rlo.hooks' verdict plus Sensor state lines in the record (CMD-GR3 S3, ga/rlo/hook.py)
HOOK_MODULE = "ga.rlo.hook"
MODULES = ("rlo.hooks", HOOK_MODULE, "ga_rlo.hook")  # ga_rlo.hook: configs written by ga_rlo <= 0.4
STATE_DIR = ".ga-rlo"  # ga_rlo 가 쓰는 곳(설정 디렉터리 아래). 모형 사본 · 턴 증거


def packaged_model() -> Any:
    """rlo 가 함께 내는 시작 모형(`rlo/data/cc_tools_model.json`)."""
    return resources.files("rlo") / "data" / MODEL_FILE


def load_model(path: str | Path) -> Any:
    """`action-model/1` 로 읽는다(틀리면 예외). 틀린 모형을 걸면 enforce 의 모든 PreToolUse 가 막힌다."""
    from action.spec import ActionModel
    from rlo.react import split_model  # rlo >= 0.6.0: 'substitutes' sits beside action-model/1 (K11)

    model, _ = split_model(json.loads(Path(path).read_text(encoding="utf-8")))
    return ActionModel.from_dict(model)


def write_model(dest: str | Path) -> Path:
    """시작 모형을 `dest` 에 쓴다. 이미 있으면 그대로 둔다(운영자가 넓히거나 좁힌 모형을 덮지 않는다)."""
    dest = Path(dest)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(packaged_model().read_bytes())
    load_model(dest)
    return dest


def rlo_guard(model: str | Path, *, python: str | None = None, mode: str = MODE, grants=GRANTS,
              record: str = RECORD) -> dict[str, str]:
    """ga `runner.guards` 항목 하나. python 은 rlo 가 깔린 해석기(기본: 지금 이것) -- 턴은 깨끗한 PATH 로 돈다."""
    argv = [python or sys.executable, "-m", HOOK_MODULE, "--model", str(Path(model).resolve()), "--mode", mode]
    for g in grants:
        argv += ["--grant", g]
    argv += ["--record", record]
    return {"name": NAME, "command": " ".join(shlex.quote(x) for x in argv), "record": record}


@dataclass
class Parsed:
    """rlo.hooks 명령 하나를 읽은 것."""

    python: str
    model: str | None = None
    mode: str = "shadow"  # rlo.hooks 의 기본
    grants: list[str] = field(default_factory=list)
    record: str | None = None
    purpose: str | None = None
    now_ms: float | None = None
    unknown: list[str] = field(default_factory=list)


def is_rlo(command: str) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return any(argv[i] == "-m" and argv[i + 1] in MODULES for i in range(len(argv) - 1))


def parse(command: str) -> Parsed | None:
    """rlo.hooks 명령이면 그 인자, 아니면 None."""
    if not is_rlo(command):
        return None
    argv = shlex.split(command)
    at = next(i for i in range(len(argv) - 1) if argv[i] == "-m" and argv[i + 1] in MODULES)
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--model")
    ap.add_argument("--mode", default="shadow")
    ap.add_argument("--grant", action="append", default=[])
    ap.add_argument("--record")
    ap.add_argument("--purpose")
    ap.add_argument("--stall-threshold")
    ap.add_argument("--now-ms", type=float)
    a, unknown = ap.parse_known_args(argv[at + 2:])
    return Parsed(argv[0] if at else "", a.model, a.mode, list(a.grant), a.record, a.purpose, a.now_ms, unknown)


def rlo_guards(runner: dict[str, Any]) -> list[tuple[int, dict[str, Any], Parsed]]:
    out = []
    for i, g in enumerate(runner.get("guards") or []):
        p = parse(g.get("command", "")) if isinstance(g, dict) else None
        if p is not None:
            out.append((i, g, p))
    return out


def problems(raw: dict[str, Any]) -> list[str]:
    """설정(ga-config/1 원본 dict)이 작업 턴 기본값에서 벗어난 곳. 빈 목록이면 기본값 그대로다.

    잡는 것(§4 3): 가드 빠짐 · enforce 가 shadow 로 · grant 넓어짐(또는 빠짐) · matcher 좁아짐 · ga 가드 꺼짐 ·
    샌드박스 require 아님 · 모형이 action-model/1 로 읽히지 않음."""
    out: list[str] = []
    runner = raw.get("runner") or {}
    kind = runner.get("kind", "manual")
    if kind not in ("headless", "agent_sdk"):
        out.append(f"runner.kind is {kind!r}: only the headless and agent_sdk Runners write a turn's own settings (P4)")
    if runner.get("guard") is False:
        out.append("runner.guard is false: ga's own PreToolUse guard (bash_guard) is off (P5)")
    if runner.get("sandbox", "auto") != "require":
        out.append(f"runner.sandbox is {runner.get('sandbox', 'auto')!r}, not 'require': a turn may run without the "
                   "OS write sandbox (P5)")
    found = rlo_guards(runner)
    if not found:
        out.append("runner.guards has no rlo.hooks guard: the turn would run without rlo")
        return out
    if len(found) > 1:
        out.append(f"runner.guards has {len(found)} rlo.hooks guards: one is expected")
    for i, g, p in found:
        where = f"runner.guards[{i}]"
        if p.mode != MODE:
            out.append(f"{where}: mode is {p.mode!r}, not {MODE!r} (shadow only records)")
        if sorted(p.grants) != sorted(GRANTS):
            more = sorted(set(p.grants) - set(GRANTS))
            if more:
                out.append(f"{where}: grants {more} beyond {list(GRANTS)} (wider than the preset)")
            if not set(GRANTS) <= set(p.grants):
                out.append(f"{where}: no --grant Bash: every Bash call is denied by A7 in enforce (BD-163)")
            if len(p.grants) != len(set(p.grants)):
                out.append(f"{where}: a grant is repeated")
        if g.get("matcher", "*") != "*":
            out.append(f"{where}: matcher {g.get('matcher')!r} is not '*': other tools are not guarded")
        if not g.get("record") or p.record != g.get("record"):
            out.append(f"{where}: record must be set and equal the command's --record (ga reads it as turn evidence)")
        if p.now_ms is not None:
            out.append(f"{where}: --now-ms fixes the clock: only for replaying recorded transcripts, never in a turn")
        if p.unknown:
            out.append(f"{where}: unknown rlo.hooks arguments {p.unknown}")
        if not p.model:
            out.append(f"{where}: no --model")
        else:
            try:
                load_model(p.model)
            except Exception as e:  # noqa: BLE001 -- 무엇이 틀렸든 닫는다
                out.append(f"{where}: model {p.model} is not a readable action-model/1 ({type(e).__name__})")
        prog = p.python
        if not prog or ("/" in prog and not Path(prog).is_file()) or ("/" not in prog and shutil.which(prog) is None):
            out.append(f"{where}: python {prog!r} is not there")
    return out
