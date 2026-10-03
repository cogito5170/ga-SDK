"""The closed forms of METHOD §3 and the question form of §6.

Each form is a list of Field specs plus an optional cross-field check.
``validate(doc)`` never raises; ``load(doc)`` raises FormError on hard problems.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .core import (
    DECISION_ID_RE,
    DIRECTIVE_ID_RE,
    HARD,
    SHA_RE,
    SOFT,
    Field,
    FormError,
    Problem,
    any_value,
    check_fields,
    hard,
    is_int,
    is_str,
    list_of,
    matches,
    obj,
    one_of,
    parse_sections,
    parse_text,
)

VERDICT_CLASSES = ("success", "partial", "failure", "blocked", "insufficient")
CAUSES = ("implementation", "requirement", "dependency", "measurement", "environment", "hub_directive")
NEXT_CHOICES = ("continue", "refine", "verify", "handoff", "change_direction", "wait", "ask_user")
HANDLED_STATUS = ("done", "paused", "declined")
CHANGE_SIZES = ("implementation", "component", "interface", "architecture", "baseline")
NEEDS = ("credential", "budget", "new_repo", "new_session")
EVIDENCE_MARKS = ("verified", "partially verified", "not verified", "assumption", "blocked")
ISSUE_GRADES = ("blocking", "current", "future", "optional")
REPORT_SECTIONS = ("Task", "Execution", "Result", "Evidence", "Deviation", "Blocker", "Proposal", "Request")

# Korean labels used by renderers (the vocabulary of GUIDANCE / PROTOCOL)
LABELS = {
    "success": "성공", "partial": "부분 성공", "failure": "실패", "blocked": "막힘", "insufficient": "정보 부족",
    "implementation": "구현", "requirement": "요구사항", "dependency": "의존성", "measurement": "측정",
    "environment": "환경", "hub_directive": "허브 지시",
    "continue": "continue", "refine": "refine", "verify": "verify", "handoff": "handoff",
    "change_direction": "change direction", "wait": "wait", "ask_user": "사용자에게 묻기",
    "user": "사용자", "hub": "허브",
}

directive_id = matches(DIRECTIVE_ID_RE, "CMD-<PREFIX><number>")
decision_id = matches(DECISION_ID_RE, "BD-<number>")
sha = matches(SHA_RE, "a 7-40 char lowercase hex sha")
full_sha = matches(re.compile(r"^[0-9a-f]{40}$"), "a full 40 char sha")


def _rev_seen(v: Any) -> str | None:
    if isinstance(v, int) and not isinstance(v, bool):
        return None if v >= 1 else "must be >= 1"
    if isinstance(v, list) and 1 <= len(v) <= 2 and all(isinstance(x, int) and not isinstance(x, bool) and x >= 1 for x in v):
        return None if v == sorted(v) else "start rev must not exceed end rev"
    return "must be a rev (int) or [rev at start, rev at end]"


def _budget(v: Any) -> str | None:
    if not isinstance(v, dict) or not v:
        return "must be a non-empty object of numeric limits"
    for k, x in v.items():
        if isinstance(x, bool) or not isinstance(x, (int, float)) or x < 0:
            return f"{k} must be a non-negative number"
    return None


def _counts(v: Any) -> str | None:
    if not isinstance(v, dict):
        return "must be an object"
    for k in ("passed", "failed", "skipped"):
        if k not in v:
            return f"{k} is required"
    for k, x in v.items():
        if k not in ("passed", "failed", "skipped", "errors"):
            return f"unknown count {k}"
        if isinstance(x, bool) or not isinstance(x, int) or x < 0:
            return f"{k} must be an integer >= 0"
    return None


def _str_map(v: Any) -> str | None:
    if not isinstance(v, dict) or not all(isinstance(k, str) and isinstance(x, str) for k, x in v.items()):
        return "must be an object of strings"
    return None


DIRECTIVE = [
    Field("id", directive_id),
    Field("rev", is_int(1)),
    Field("supersedes", obj([Field("id", directive_id), Field("rev", is_int(1))]), required=False),
    Field("to", is_str),
    Field("goal", is_str),
    Field("why", is_str),
    Field("scope", is_str),
    # §3.1 marks done_when as required, §5 R7 makes its absence a soft warning to the hub.
    Field("done_when", is_str, missing=SOFT, rule="R7"),
    Field("after", list_of(directive_id), required=False),
    Field("budget", _budget, required=False),
    Field("change_size", one_of(*CHANGE_SIZES), required=False),
    Field("contradicts", list_of(decision_id), required=False),
]

REPORT = [
    Field("from", is_str),
    Field(
        "handled",
        list_of(
            obj(
                [
                    Field("id", directive_id),
                    Field("rev_seen", _rev_seen),
                    Field("status", one_of(*HANDLED_STATUS)),
                    Field("reason", is_str, required=False),
                ]
            )
        ),
    ),
    Field("commits", list_of(obj([Field("repo", is_str), Field("branch", is_str), Field("sha", sha)])), required=False),
    Field("tests", _counts, required=False),
    Field("change_size", one_of(*CHANGE_SIZES), required=False),
    Field("needs", list_of(one_of(*NEEDS)), required=False),
    # exchanges this session took part in since its last report (§3.5): post ids or one-line notes
    Field("exchanges", list_of(is_str), required=False),
]

EXCHANGE = [
    Field("from", is_str),
    Field("to", is_str),
    Field("why", is_str),
    Field("asked", is_str),
    Field("got", is_str),
    Field("proposal", is_str, required=False),
]

VERDICT = [
    Field("class", one_of(*VERDICT_CLASSES)),
    Field("subclass", is_str, required=False),
    Field("cause", one_of(*CAUSES), required=False),
    Field(
        "evidence",
        obj(
            [
                Field("heads", _str_map),
                Field("tests", lambda v: None if isinstance(v, dict) and all(_counts(c) is None for c in v.values()) else "must map repo to counts"),
                Field("notes", list_of(is_str), required=False),
            ]
        ),
    ),
    Field("claims_vs_evidence", list_of(is_str)),
    Field("next", obj([Field("choice", one_of(*NEXT_CHOICES)), Field("reason", is_str)])),
    Field("report_ref", is_str, required=False),
    Field("round", is_int(1), required=False),
]

ROUND = [
    Field("n", is_int(1)),
    Field("date", is_str),
    Field("repos", list_of(obj([Field("repo", is_str), Field("sha", sha), Field("tests", _counts, required=False)]))),
    Field("directives", list_of(directive_id)),
    Field("verdict", one_of(*VERDICT_CLASSES)),
    Field("summary", is_str),
    Field("next", one_of(*NEXT_CHOICES)),
    Field("notices", list_of(is_str), required=False),
    Field("decisions", list_of(decision_id), required=False),
]

DECISION = [
    Field("id", decision_id),
    Field("date", is_str),
    Field("decision", is_str),
    Field("basis", is_str),
    Field("by", one_of("user", "hub")),
    Field("supersedes", list_of(decision_id), required=False),
]

STAGE = [
    Field("name", is_str),
    Field("date", is_str),
    Field("repos", list_of(obj([Field("repo", is_str), Field("sha", full_sha), Field("tests", is_int(0))]), min_len=1)),
    Field("established", list_of(is_str)),
    Field("deferred", list_of(is_str)),
    Field("decision", decision_id, required=False),
]

# METHOD rev 10 §3.3b (BD-147): an outside verdict on an integrated result (a person, an upper hub). The hub folds
# it into the next round (Judge context, draft) and only ever makes the reviewed round stricter; records only grow.
REVIEW = [
    Field("id", matches(re.compile(r"^RV-\d+$"), "RV-<number>")),
    Field("date", is_str),
    Field("by", is_str),
    Field("repo", is_str),
    Field("sha", sha),
    Field("class", one_of(*VERDICT_CLASSES)),
    Field("cause", one_of(*CAUSES), required=False),
    Field("why", is_str),
    Field("round", is_int(1), required=False),  # the round that integrated the sha, when the hub found it
    Field("amends", obj([Field("round", is_int(1)), Field("from", one_of(*VERDICT_CLASSES)), Field("to", one_of(*VERDICT_CLASSES))]),
          required=False),
]

QUESTION = [
    Field("gate", is_int(1)),
    Field("about", is_str),
    Field("A", is_str),
    Field("B", is_str),
    Field("C", is_str),
    Field("options", list_of(obj([Field("label", is_str), Field("effect", is_str)]), min_len=2)),
    Field("recommendation", is_str),
    Field("refs", list_of(is_str), required=False),
]


def _verdict_cross(doc: dict[str, Any]) -> list[Problem]:
    if doc.get("class") not in (None, "success") and not doc.get("cause"):
        return [Problem("$.cause", "is required when class is not success")]
    return []


def _question_cross(doc: dict[str, Any]) -> list[Problem]:
    g = doc.get("gate")
    if isinstance(g, int) and not 1 <= g <= 7:
        return [Problem("$.gate", "must be 1..7 (METHOD §6)")]
    return []


def _directive_cross(doc: dict[str, Any]) -> list[Problem]:
    sup = doc.get("supersedes")
    if isinstance(sup, dict) and sup.get("id") == doc.get("id") and isinstance(sup.get("rev"), int) and isinstance(doc.get("rev"), int):
        if sup["rev"] >= doc["rev"]:
            return [Problem("$.supersedes.rev", "must be lower than rev when superseding the same directive")]
    return []


def _review_cross(doc: dict[str, Any]) -> list[Problem]:
    if doc.get("class") not in (None, "success") and not doc.get("cause"):
        return [Problem("$.cause", "is required when class is not success")]
    return []


def _exchange_cross(doc: dict[str, Any]) -> list[Problem]:
    if doc.get("from") == doc.get("to"):
        return [Problem("$.to", "an exchange is between two different sessions")]
    return []


SCHEMAS: dict[str, tuple[list[Field], Callable[[dict[str, Any]], list[Problem]] | None]] = {
    "directive/1": (DIRECTIVE, _directive_cross),
    "report/1": (REPORT, None),
    "verdict/1": (VERDICT, _verdict_cross),
    "round/1": (ROUND, None),
    "decision/1": (DECISION, None),
    "stage/1": (STAGE, None),
    "question/1": (QUESTION, _question_cross),
    "exchange/1": (EXCHANGE, _exchange_cross),
    "review/1": (REVIEW, _review_cross),
}


def validate(doc: Any, expect: str | None = None) -> list[Problem]:
    """All problems of a form document. Never raises."""
    if not isinstance(doc, dict):
        return [Problem("$", "must be an object")]
    schema = doc.get("schema")
    if expect is not None and schema != expect:
        return [Problem("$.schema", f"expected {expect}, got {schema!r}")]
    if schema not in SCHEMAS:
        return [Problem("$.schema", f"unknown schema {schema!r}; known: {', '.join(sorted(SCHEMAS))}")]
    fields, cross = SCHEMAS[schema]
    problems = check_fields(doc, fields)
    if cross and not hard(problems):
        problems += cross(doc)
    return problems


def load(doc: Any, expect: str | None = None) -> dict[str, Any]:
    """Return the document if it has no hard problems, else raise FormError."""
    problems = validate(doc, expect)
    if hard(problems):
        raise FormError(hard(problems))
    return doc


def report_body_notes(body: str) -> list[Problem]:
    """Soft notices on the human body of a report (METHOD §3.2). Empty sections are fine."""
    notes: list[Problem] = []
    sections = parse_sections(body)
    ev = sections.get("Evidence", "")
    if ev and not any(m in ev for m in EVIDENCE_MARKS):
        notes.append(Problem("Evidence", "claims carry no mark (verified / partially verified / not verified / assumption / blocked)", SOFT))
    prop = sections.get("Proposal", "")
    if prop and not any(g in prop for g in ISSUE_GRADES):
        notes.append(Problem("Proposal", "found issues carry no grade (blocking / current / future / optional)", SOFT))
    res = sections.get("Result", "")
    if re.search(r"(하겠다|할 것이다|\bwill\b|\bplan to\b)", res):
        notes.append(Problem("Result", "reads like a plan; plans belong in Proposal", SOFT))
    return notes


def parse_post(text: str, expect: str | None = None) -> tuple[dict[str, Any], str, list[Problem]]:
    """Parse a posted text. Raises FormError on a missing/invalid head or hard problems.

    Returns (head, body, soft problems)."""
    head, body = parse_text(text)
    problems = validate(head, expect)
    if hard(problems):
        raise FormError(hard(problems))
    if head.get("schema") == "report/1":
        problems += report_body_notes(body)
    return head, body, [p for p in problems if p.strength != HARD]
