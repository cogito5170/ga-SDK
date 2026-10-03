"""LLM Judge (METHOD §4.6, 2nd edition second): proposes a verdict and the next step with ``claude -p``.

It only proposes. The hub keeps the machine's class as a floor (§3.3 rev 5: the judge can only make it
stricter) and the gates still stop (§6). Whatever the model returns is checked against verdict/1 and
directive/1; a reply that does not check, a failed call, or an exhausted budget gives a fallback proposal
that asks the user instead of guessing.

The call runs with no tools (``--tools ""``), no session persistence, a clean environment and its own
HOME, like the headless Runner. The context goes in on stdin as JSON; nothing of the reply is kept but
the proposal itself and the call's numbers.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..forms import CAUSES, NEXT_CHOICES, VERDICT_CLASSES, canonical_json, hard, validate
from .base import JudgeContext
from .headless import KEEP_ENV, run_claude

SYSTEM = """너는 허브의 판정 보조다. 작업 세션의 보고와 허브가 재현한 증거를 읽고, 판정과 다음 걸음을 **제안**만 한다. 결정은 허브와 사용자가 한다.

판정 class (하나):
- success: 지시의 끝난 기준을 증거가 보인다.
- partial: 일부만 됐거나, 낡은 판의 지시로 일했다(subclass "crossed": 보고의 rev_seen 이 지시의 현재 판보다 낮거나, 이미 대체된 지시를 수행함), 또는 건너뛴 시험이 있다(subclass "skipped").
- failure: 시도했으나 끝난 기준을 못 맞췄다(시험 실패 등).
- blocked: 진행할 수 없다(규칙 위반으로 통합 안 됨, 권한 · 자격 없음 등).
- insufficient: 판정할 정보가 모자란다.
cause (success 가 아니면 하나): implementation(구현) · requirement(요구사항) · dependency(의존성) · measurement(측정) · environment(환경) · hub_directive(허브의 지시 자체가 틀렸거나 서로 어긋남 — 허브도 틀린다).
next.choice: continue · refine · verify · handoff · change_direction · wait · ask_user. 할 일이 없으면 wait. 단계 마감 · PR · 태그 · 기준선 변경 · 예산 · 비밀값 · 새 저장소는 정하지 말고 ask_user.
ask_user 일 때는 "gate" 에 근거가 되는 사용자 게이트 번호를 적는다(근거가 없으면 ask_user 는 질문이 되지 않고 알림만 남는다):
1 단계 마감 · PR · 기본 브랜치 합치기 · 태그 / 2 기준선 수준 변경 · 계약 동결 · 판 올림 / 3 앞선 사용자 결정과 부딪힘 / 4 소유가 겹치거나 소유표를 바꿔야 함 / 5 열린 질문의 값 · 설계 갈림 / 6 예산 초과 · 비밀값 · 자격 증명 / 7 새 저장소 · 새 세션.
허브가 이미 기계적으로 하는 일(ff 가 아닌 세션에 rev+1 보내기 등)은 묻지 않는다.

규칙: 계획("하겠다")과 결과("했고 측정됐다")를 섞지 않는다. 증거가 보고의 주장과 다르면 증거를 따른다. 기계가 정한 class(machine_class)가 있으면 그보다 너그럽게 판정하지 않는다.
다음 지시 초안은 정말 필요할 때만 쓰고, 쓰면 id · rev · to · goal · why · scope · done_when 을 모두 채운다(방법은 쓰지 않는다).

출력은 JSON 객체 하나뿐이다. 다른 글을 쓰지 않는다:
{"class": "...", "subclass": null, "cause": null, "next": {"choice": "...", "reason": "한 줄"}, "gate": null, "summary": "한 줄", "claims_vs_evidence": ["..."], "directive": null}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "class": {"type": "string", "enum": list(VERDICT_CLASSES)},
        "subclass": {"type": ["string", "null"]},
        "cause": {"type": ["string", "null"], "enum": [*CAUSES, None]},
        "next": {"type": "object", "properties": {"choice": {"type": "string", "enum": list(NEXT_CHOICES)}, "reason": {"type": "string"}},
                 "required": ["choice", "reason"]},
        "summary": {"type": "string"},
        "claims_vs_evidence": {"type": "array", "items": {"type": "string"}},
        "directive": {"type": ["object", "null"]},
    },
    "required": ["class", "next", "summary"],
}

FENCE_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def context_json(ctx: JudgeContext, body_limit: int = 4000) -> str:
    reports = []
    for r in ctx.reports:
        body = r.get("body", "")
        reports.append({"head": r["head"], "body": body[:body_limit] + ("…" if len(body) > body_limit else "")})
    doc = {
        "round": ctx.round,
        "machine_class": ctx.machine_class,
        "evidence": ctx.evidence,
        "rule_findings": [str(p) for p in ctx.findings],
        "reports": reports,
        "open_directives": ctx.open_directives,
        "exchanges": ctx.exchanges,
        "outside_reviews": [{k: v for k, v in r.items() if k != "schema"} for r in getattr(ctx, "reviews", [])],
        "user_answers": [{"gate": a.get("gate"), "label": a.get("label"), "decision": a.get("decision")} for a in ctx.answers],
    }
    return canonical_json(doc)


def extract(data: dict[str, Any]) -> dict[str, Any] | None:
    """The proposal object from a claude JSON result (structured output, plain JSON, or a fenced block)."""
    so = data.get("structured_output")
    if isinstance(so, dict):
        return so
    text = data.get("result")
    if not isinstance(text, str):
        return None
    for cand in [text.strip()] + FENCE_JSON.findall(text):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None
    return None


class LLMJudge:
    calls_model = True  # METHOD rev 14 §4c 6: needs the person's permission for measurement calls
    def __init__(
        self,
        home: str | Path,
        *,
        executable: str = "claude",
        model: str | None = None,
        timeout: float = 300,
        max_runs: int | None = None,
        max_budget_usd: float | None = None,
        use_json_schema: bool = False,  # --json-schema with --tools "" is not verified against the real CLI yet
        extra_env: dict[str, str] | None = None,
    ):
        self.home = Path(home)
        self.executable = executable
        self.model = model
        self.timeout = timeout
        self.max_runs = max_runs
        self.max_budget_usd = max_budget_usd
        self.use_json_schema = use_json_schema
        self.extra_env = dict(extra_env or {})
        self.calls: list[dict[str, Any]] = []  # numbers only

    # ------------------------------------------------------------------ call

    def env(self) -> dict[str, str]:
        import os

        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / ".claude").mkdir(exist_ok=True)
        env = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
        env.update(HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.home / ".claude"))
        env.update(self.extra_env)
        return env

    def argv(self) -> list[str]:
        argv = [self.executable, "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
                "--system-prompt", SYSTEM]
        if self.model:
            argv += ["--model", self.model]
        if self.max_budget_usd is not None:
            argv += ["--max-budget-usd", f"{self.max_budget_usd:g}"]
        if self.use_json_schema:
            argv += ["--json-schema", json.dumps(SCHEMA, ensure_ascii=False)]
        return argv

    # ------------------------------------------------------------------ propose

    def fallback(self, ctx: JudgeContext, why: str) -> dict[str, Any]:
        cls = ctx.machine_class or "insufficient"
        verdict = {"schema": "verdict/1", "class": cls, "evidence": ctx.evidence, "claims_vs_evidence": [],
                   "next": {"choice": "ask_user", "reason": f"LLM 판정을 쓸 수 없음: {why}"}}
        if cls != "success":
            verdict["cause"] = "measurement"
        return {"verdict": verdict, "summary": f"판정 보조 실패({why}) — 기계 클래스로 판정, 다음 tick 에 다시 부른다",
                "directive": None, "judge": {"fallback": why}, "judge_failed": why}

    def propose(self, ctx: JudgeContext) -> dict[str, Any]:
        if self.max_runs is not None and len(self.calls) >= self.max_runs:
            return self.fallback(ctx, "budget: judge runs exhausted")
        self.home.mkdir(parents=True, exist_ok=True)
        call = run_claude(self.argv(), context_json(ctx), self.home, self.env(), self.timeout)
        rec = {"round": ctx.round, "error": call.error, "cost": call.cost, "seconds": call.seconds, "valid": False}
        self.calls.append(rec)
        if call.error or call.data is None:
            return dict(self.fallback(ctx, call.error or "no reply"), judge=rec)
        obj = extract(call.data)
        if obj is None:
            return dict(self.fallback(ctx, "reply is not JSON"), judge=rec)
        verdict = {"schema": "verdict/1", "class": obj.get("class"), "evidence": ctx.evidence,
                   "claims_vs_evidence": [c for c in obj.get("claims_vs_evidence") or [] if isinstance(c, str) and c.strip()],
                   "next": obj.get("next")}
        if obj.get("subclass"):
            verdict["subclass"] = obj["subclass"]
        if obj.get("cause"):
            verdict["cause"] = obj["cause"]
        problems = hard(validate(verdict, "verdict/1"))
        if problems:
            return dict(self.fallback(ctx, "reply does not check as verdict/1: " + "; ".join(p.message for p in problems[:3])), judge=rec)
        rec["valid"] = True
        out = {"verdict": verdict, "summary": obj.get("summary") or verdict["next"]["reason"], "directive": None, "judge": rec}
        g = obj.get("gate")
        if isinstance(g, int) and not isinstance(g, bool) and 1 <= g <= 7:
            out["gate"] = g  # the §6 ground of an ask_user (METHOD rev 11); anything else is no ground
        draft = obj.get("directive")
        if isinstance(draft, dict):
            kind = "directive/2" if isinstance(draft.get("scope"), list) or draft.get("changes") else "directive/1"
            draft = dict(draft, schema=kind)
            dprobs = hard(validate(draft, kind))
            if dprobs:
                rec["directive_dropped"] = "; ".join(p.message for p in dprobs[:3])
            else:
                out["directive"] = draft
        return out
