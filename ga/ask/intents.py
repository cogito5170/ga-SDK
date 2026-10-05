"""ga ask's intent table (CMD-GA36 S1): Korean and English phrasings -> one ga action. Local rules only — keywords
and regular expressions, no model. A question that matches nothing gets the three closest intents, never a guess.

Each intent: name, cost class (free | mail | model), the fixed command it stands for (shown before it runs), a
Korean label, example phrasings (for suggestions) and patterns. The score of an intent is the total length of its
patterns' matches (an action verb weighs 3), so the more specific phrase wins ("마지막 실행" is status, not run;
"다음 지시 실행해줘" is run, not next).
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

COSTS = ("free", "mail", "model")


@dataclass(frozen=True)
class Intent:
    name: str
    cost: str
    argv: tuple[str, ...]
    label: str
    examples: tuple[str, ...]
    patterns: tuple = field(default=())  # regex, or (regex, weight): an action verb weighs 3


TABLE: tuple[Intent, ...] = (
    Intent("help", "free", ("ga", "ask", "help"), "도움말 · 할 수 있는 것",
           ("뭐 할 수 있어?", "도움말", "사용법 알려줘", "what can you do", "help"),
           (r"뭐\s*할\s*수\s*있", r"도움말", r"도와\s*줘", r"사용\s*법", r"어떻게\s*(써|사용)", r"명령(어)?\s*(목록|뭐)",
            r"\bhelp\b", r"what can (you|ga|i) do", r"how do i use", r"\bcommands\b")),
    Intent("status", "free", ("ga", "ask", "status"), "상태 · 새 메일 · 마지막 실행 · 오늘 토큰",
           ("지금 상태 어때?", "새 메일 왔어?", "마지막 실행 어떻게 됐어?", "status", "any new mail?"),
           (r"상태", r"새\s*(메일|메시지|편지|지시)", r"메일\s*(왔|있)", (r"마지막\s*실행", 3), r"진행\s*상황", r"어떻게\s*돼\s*가",
            r"\bstatus\b", r"new (mail|messages?)", r"any mail", (r"last run", 3), r"what'?s going on", r"how are things")),
    Intent("next", "free", ("ga", "ask", "next"), "다음 지시 (짧게)",
           ("다음 할 일 뭐야?", "다음 지시 보여줘", "what's next", "next directive"),
           (r"다음\s*(지시|할\s*일|작업|디렉티브|일)", r"뭐\s*해야", r"할\s*일\s*(이\s*)?뭐", r"\bnext\b",
            r"what should i do", r"\bto-?do\b")),
    Intent("run", "model", ("ga", "bridge", "once"), "다음 지시를 브리지로 한 번 실행",
           ("다음 지시 실행해줘", "브리지 돌려줘", "run the next directive", "start the bridge"),
           ((r"실행\s*(해|시켜|하자)", 3), (r"돌려", 3), (r"시작해", 3), (r"처리해", 3), (r"\brun\b", 3), (r"\bstart\b", 3),
            (r"\bexecute\b", 3), (r"go ahead", 3))),
    Intent("report", "mail", ("ga", "check", "<report>", "&&", "ga", "mail", "send"), "준비된 보고서 검사 후 보내기",
           ("보고서 보내줘", "리포트 제출해줘", "send the report", "submit my report"),
           (r"보고서", r"리포트", r"보고\s*(보내|제출|올려)", r"\breport\b", r"\bsubmit\b")),
    Intent("usage", "free", ("ga", "ask", "usage"), "토큰 · 시간 사용량 (실행별, 날짜별)",
           ("오늘 얼마나 썼어?", "토큰 사용량 보여줘", "how many tokens did i use today", "usage"),
           (r"얼마나\s*(썼|사용|들었|나왔)", r"사용량", r"토큰", r"비용", r"얼마\s*들", r"\busage\b", r"\btokens?\b",
            r"how much (did|have) (i|we) (use|spen[dt])", r"\bcost\b", r"\bspent\b")),
    Intent("doctor", "free", ("ga", "ask", "doctor"), "진단 · 설치와 마지막 실패 원인",
           ("왜 안 돼?", "진단해줘", "뭐가 문제야?", "doctor", "why did it fail"),
           (r"진단", r"점검", r"고장", r"안\s*돼", r"안\s*되", r"오류", r"에러", r"왜\s*실패", r"뭐가\s*문제", r"\bdoctor\b",
            r"diagnos", r"\bbroken\b", r"not working", r"\berror\b", r"why did (it|the run) fail", r"check (my )?(setup|install)")),
    Intent("stop", "free", ("ga", "ask", "stop"), "실행 중인 브리지 멈추기",
           ("멈춰", "브리지 중지해줘", "그만해", "stop the bridge"),
           ((r"멈춰", 3), (r"중지", 3), (r"정지", 3), (r"그만", 3), (r"꺼\s*줘", 3), (r"\bstop\b", 3), (r"\bhalt\b", 3),
            (r"\bcancel\b", 3), (r"\bkill\b", 3))),
)
BY_NAME = {i.name: i for i in TABLE}
_COMPILED = {i.name: [(re.compile(p[0] if isinstance(p, tuple) else p, re.I), p[1] if isinstance(p, tuple) else 1)
                      for p in i.patterns] for i in TABLE}


@dataclass
class Match:
    intent: Intent | None
    score: int
    suggestions: list[str]   # three intent names when nothing matched


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text or "")).strip().lower()


def scores(text: str) -> dict[str, int]:
    q = normalize(text)
    out = {}
    for name, pats in _COMPILED.items():
        out[name] = sum(len(m.group(0)) * w for p, w in pats for m in p.finditer(q))
    return out


def closest(text: str, k: int = 3) -> list[str]:
    """The k intents whose example phrasings look most like the text (characters, not meaning)."""
    q = normalize(text)
    best = {i.name: max(difflib.SequenceMatcher(None, q, normalize(e)).ratio() for e in i.examples) for i in TABLE}
    return [n for n, _ in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]


def route(text: str) -> Match:
    s = scores(text)
    name, top = max(s.items(), key=lambda kv: kv[1])
    if top <= 0:
        return Match(None, 0, closest(text))
    tied = [n for n, v in s.items() if v == top]
    if len(tied) > 1:  # two intents equally sure: not a guess either — ask with those
        return Match(None, top, (tied + [n for n in closest(text) if n not in tied])[:3])
    return Match(BY_NAME[name], top, [])
