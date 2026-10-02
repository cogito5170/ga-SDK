"""Judges (METHOD §4.6). 1st edition: a person answers in a file. No LLM.

``FileJudge`` reads ``<dir>/round-<n>.json`` (the proposal). When it is not there yet it writes
``<dir>/round-<n>.request.md`` with the machine evidence and raises ``NeedJudgement``; the hub stops
the tick there without recording the round, and the next tick picks the answer up.

``CallableJudge`` wraps a function. Tests use it to stand in for the person; a 2nd-edition LLM judge
plugs into the same interface.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..forms import canonical_json
from .base import JudgeContext


class NeedJudgement(Exception):
    def __init__(self, request_path: Path, wrote: bool = True):
        self.request_path = request_path
        self.wrote = wrote
        super().__init__(f"waiting for a judgement: {request_path}")


def describe(ctx: JudgeContext) -> str:
    lines = [f"# {ctx.round} 회차 판정 요청", ""]
    lines.append(f"- 기계가 정한 클래스: {ctx.machine_class or '없음'}")
    for r in ctx.reports:
        lines.append(f"- 보고 {r['post'].id} ({r['head'].get('from')}): handled {json.dumps(r['head'].get('handled'), ensure_ascii=False)}")
    lines += ["", "## 증거", "", "```json", canonical_json(ctx.evidence).rstrip(), "```", "", "## 규칙 알림", ""]
    lines += [f"- {p}" for p in ctx.findings] or ["- 없음"]
    lines += [
        "",
        "## 답 꼴",
        "",
        f'`round-{ctx.round}.json` = {{"verdict": verdict/1, "summary": "...", "directive": directive/1 | null, '
        '"decision": decision/1 | null, "action": null}}',
    ]
    return "\n".join(lines) + "\n"


class FileJudge:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def propose(self, ctx: JudgeContext) -> dict[str, Any]:
        answer = self.dir / f"round-{ctx.round}.json"
        if answer.exists():
            return json.loads(answer.read_text(encoding="utf-8"))
        self.dir.mkdir(parents=True, exist_ok=True)
        req = self.dir / f"round-{ctx.round}.request.md"
        text = describe(ctx)
        if req.exists() and req.read_text(encoding="utf-8") == text:
            raise NeedJudgement(req, wrote=False)
        req.write_text(text, encoding="utf-8")
        raise NeedJudgement(req)


class CallableJudge:
    def __init__(self, fn: Callable[[JudgeContext], dict[str, Any]]):
        self.fn = fn

    def propose(self, ctx: JudgeContext) -> dict[str, Any]:
        return self.fn(ctx)
