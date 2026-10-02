"""User gates (METHOD §6, G7). The SDK stops at these and asks; it never decides them.

``detect`` looks at what the hub is about to do (report, proposal, rule findings) and returns the gates hit.
``question_for`` turns a gate into a question/1 document. Gates cannot be switched off.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Config
from .forms import Problem, load, parse_sections
from .rules import GATED_ACTIONS, requests_of

GATE_TITLES = {
    1: "단계 마감 · PR · 기본 브랜치 합치기 · 태그",
    2: "기준선(architecture) 수준의 변경 · 계약 동결 · 판 올림",
    3: "앞선 사용자 결정과 부딪힘",
    4: "소유가 겹치거나 소유표를 바꿔야 함",
    5: "열린 질문(OQ)의 값 · 설계 갈림",
    6: "예산 한도 초과 · 비밀값 · 자격 증명",
    7: "새 저장소 · 새 세션",
}

DEFAULT_OPTIONS = {
    1: [("진행", "허브가 sha 를 고정하고 명령을 사람에게 준다"), ("보류", "지금 상태를 유지한다")],
    2: [("받는다", "기준선을 바꾸고 BD 로 남긴다"), ("받지 않는다", "지금 기준선을 지킨다")],
    3: [("새 결정으로 바꾼다", "앞선 결정을 대체하는 BD 를 남긴다"), ("앞선 결정을 지킨다", "지시 · 보고를 고쳐 오게 한다")],
    4: [("소유표를 바꾼다", "새 소유 줄을 BD 로 남긴다"), ("지금 소유를 지킨다", "요청으로 소유 세션에 옮긴다")],
    5: [("제안대로 정한다", "값 · 갈래를 BD 로 남긴다"), ("더 알아본다", "작은 실험을 지시한다")],
    6: [("허락한다", "한도를 올리거나 자격 증명을 사람이 넣는다"), ("멈춘다", "그 일을 하지 않는다")],
    7: [("사람이 만든다", "만든 뒤 설정에 넣는다"), ("만들지 않는다", "지금 저장소 · 세션 안에서 한다")],
}

USER_BD_RE = re.compile(r"\bBD-(\d+)\b")
OQ_RE = re.compile(r"\bOQ-\d+\b")
BIG_SIZES = ("architecture", "baseline")


@dataclass
class Gate:
    number: int
    reason: str
    refs: list[str] = field(default_factory=list)
    session: str | None = None

    @property
    def title(self) -> str:
        return GATE_TITLES[self.number]


def detect(
    cfg: Config,
    *,
    report: dict[str, Any] | None = None,
    body: str = "",
    proposal: dict[str, Any] | None = None,
    findings: list[Problem] | None = None,
    user_decision: Callable[[str], bool] = lambda bd: False,
) -> list[Gate]:
    """All gates hit. ``proposal`` may hold ``directive``, ``action`` and ``next``.
    ``user_decision(bd)`` says whether a BD was decided by the user."""
    gates: list[Gate] = []
    session = (report or {}).get("from")
    proposal = proposal or {}
    directive = proposal.get("directive") or {}
    action = proposal.get("action")
    sections = parse_sections(body) if body else {}

    # 1 — stage close · PR · default merge · tag
    if action in GATED_ACTIONS:
        gates.append(Gate(1, f"proposed action {action}", session=session))

    # 2 — architecture / baseline size, contract freeze, version bump
    for src, doc in (("report", report or {}), ("directive", directive)):
        if doc.get("change_size") in BIG_SIZES:
            gates.append(Gate(2, f"{src} change_size {doc['change_size']}", session=session))
    if action in ("freeze_contract", "bump_contract"):
        gates.append(Gate(2, f"proposed action {action}", session=session))

    # 3 — clashes with a user decision
    clash = list(directive.get("contradicts", []))
    clash += [f"BD-{n}" for n in USER_BD_RE.findall(sections.get("Deviation", ""))]
    for bd in dict.fromkeys(clash):
        if user_decision(bd):
            gates.append(Gate(3, f"clashes with user decision {bd}", [bd], session))

    # 4 — ownership
    for p in findings or []:
        if p.rule == "R2" and "no row in the ownership table" in p.message:
            gates.append(Gate(4, f"{p.path} has no owner", [p.path], session))
    if action == "change_ownership":
        gates.append(Gate(4, "proposed ownership change", session=session))

    # 5 — open questions, design forks
    oqs = OQ_RE.findall(sections.get("Request", "") + "\n" + sections.get("Deviation", ""))
    if oqs:
        gates.append(Gate(5, "asks for the value of " + ", ".join(dict.fromkeys(oqs)), list(dict.fromkeys(oqs)), session))
    if action == "choose_design" or (proposal.get("next") == "ask_user" and not gates):
        gates.append(Gate(5, proposal.get("reason", "design fork"), session=session))

    # 6 — budget, secrets, credentials
    for p in findings or []:
        if p.rule in ("R6", "R12"):
            gates.append(Gate(6, f"{p.rule} {p.path}: {p.message}", [p.path], session))
    for need in (report or {}).get("needs", []):
        if need in ("credential", "budget"):
            gates.append(Gate(6, f"report needs {need}", session=session))

    # 7 — new repository / session
    for need in (report or {}).get("needs", []):
        if need in ("new_repo", "new_session"):
            gates.append(Gate(7, f"report needs {need}", session=session))
    for target, line in requests_of(sections.get("Request", "")):
        if target not in cfg.sessions and target != cfg.hub_name:
            gates.append(Gate(7, f"request to unknown session {target}: {line}", [target], session))
    if directive.get("to") and directive["to"] not in cfg.sessions:
        gates.append(Gate(7, f"directive to unknown session {directive['to']}", session=session))
    return gates


def question_for(gate: Gate, *, why: str, changed: str, next_: str, recommendation: str | None = None,
                 options: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    opts = options or DEFAULT_OPTIONS[gate.number]
    doc = {
        "schema": "question/1",
        "gate": gate.number,
        "about": f"[게이트 {gate.number} · {gate.title}] {gate.reason}",
        "A": why,
        "B": changed,
        "C": next_,
        "options": [{"label": a, "effect": b} for a, b in opts],
        # closing side by default (BD-07 spirit): the last default option keeps things as they are
        "recommendation": recommendation or opts[-1][0],
        "refs": list(gate.refs),
    }
    return load(doc, "question/1")
