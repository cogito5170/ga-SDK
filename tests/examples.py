"""Valid example of each form. Tests derive invalid ones by mutation."""
import copy

SHA40 = "d313414429ca097b02a545da92bbef9138927844"

VALID = {
    "directive/1": {
        "schema": "directive/1",
        "id": "CMD-GA1",
        "rev": 2,
        "supersedes": {"id": "CMD-GA1", "rev": 1},
        "to": "GA",
        "goal": "ga-SDK 0.1 을 짓는다",
        "why": "손으로 반복한 일을 기계가 하게 (BD-131)",
        "scope": "ga-SDK 전부. METHOD.md 는 baseline 소유",
        "done_when": "G1–G8 끝난 기준을 시험으로 보인다",
        "after": ["CMD-K8"],
        "budget": {"llm_runs": 6},
    },
    "report/1": {
        "schema": "report/1",
        "from": "GA",
        "handled": [{"id": "CMD-GA1", "rev_seen": [1, 2], "status": "paused", "reason": "설계를 rev 2 에 맞춤"}],
        "commits": [{"repo": "ga-SDK", "branch": "claude/x", "sha": "f2bdeab"}],
        "tests": {"passed": 10, "failed": 0, "skipped": 1},
        "change_size": "implementation",
    },
    "verdict/1": {
        "schema": "verdict/1",
        "class": "partial",
        "subclass": "crossed",
        "cause": "hub_directive",
        "evidence": {"heads": {"ga-SDK": "f2bdeab"}, "tests": {"ga-SDK": {"passed": 10, "failed": 0, "skipped": 1}}},
        "claims_vs_evidence": [],
        "next": {"choice": "refine", "reason": "rev 2 에 맞춘다"},
    },
    "round/1": {
        "schema": "round/1",
        "n": 91,
        "date": "2026-10-02",
        "repos": [{"repo": "ga-SDK", "sha": "f2bdeab", "tests": {"passed": 10, "failed": 0, "skipped": 0}}],
        "directives": ["CMD-GA1"],
        "verdict": "success",
        "summary": "설계 초안 받음",
        "next": "continue",
    },
    "decision/1": {
        "schema": "decision/1",
        "id": "BD-131",
        "date": "2026-10-02",
        "decision": "ga-SDK 를 짓는다",
        "basis": "사용자 결정 2026-10-02",
        "by": "user",
        "supersedes": [],
    },
    "stage/1": {
        "schema": "stage/1",
        "name": "stage-4",
        "date": "2026-10-02",
        "repos": [{"repo": "rlo-SDK", "sha": SHA40, "tests": 62}],
        "established": ["SDK 설계"],
        "deferred": ["PyPI"],
    },
    "exchange/1": {
        "schema": "exchange/1",
        "from": "Sensor",
        "to": "DC",
        "why": "export 꼴의 칸 뜻을 바로 물어야 했다",
        "asked": "action: 칸의 단위",
        "got": "ms 단위",
        "proposal": "Sensor 쪽 주석을 고친다",
    },
    "question/1": {
        "schema": "question/1",
        "gate": 1,
        "about": "stage-5 마감",
        "A": "왜", "B": "무엇이 달라졌나", "C": "다음",
        "options": [{"label": "마감", "effect": "sha 고정"}, {"label": "보류", "effect": "계속"}],
        "recommendation": "마감",
    },
}


def valid(kind):
    return copy.deepcopy(VALID[kind])
