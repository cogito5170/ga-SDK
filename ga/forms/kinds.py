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


def _note(v: Any) -> str | None:
    """A human-only note on a wire form (CMD-GA27 S1): a string of at most 280 characters."""
    return None if isinstance(v, str) and v.strip() and len(v) <= NOTE_MAX else f"must be a string of 1-{NOTE_MAX} chars"


NOTE_MAX = 280


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

# ---- METHOD rev 16 §3.6 (BD-173): version 2 forms — items, only what changed, results in fields
S_ID = re.compile(r"^S\d+$")
D_ID = re.compile(r"^D\d+$")
ITEM_STATES = ("met", "unmet", "blocked", "na")
BLOCKER_KINDS = ("env", "permission", "credential", "budget", "dependency", "design")
NOTIFY_KINDS = ("directive", "report", "verdict", "question", "ack", "shadow")


def _items(prefix: re.Pattern[str], what: str) -> Check:
    return list_of(obj([Field("id", matches(prefix, what)), Field("text", is_str)]))


DIRECTIVE2 = [
    Field("id", directive_id),
    Field("rev", is_int(1)),
    Field("supersedes", obj([Field("id", directive_id), Field("rev", is_int(1))]), required=False),
    Field("to", is_str),
    Field("goal", is_str),
    Field("why", is_str),
    # required at rev 1; at rev > 1 the receiver builds them from the previous revision and `changes`
    Field("scope", _items(S_ID, "S<number>"), required=False),
    Field("done_when", _items(D_ID, "D<number>"), required=False),
    Field("refs", list_of(is_str), required=False),
    Field("changes", list_of(obj([Field("item", matches(re.compile(r"^[SD]\d+$"), "S<n> or D<n>")),
                                  Field("op", one_of("add", "edit", "drop")),
                                  Field("text", is_str, required=False)])), required=False),
    Field("after", list_of(directive_id), required=False),
    Field("budget", _budget, required=False),
    Field("change_size", one_of(*CHANGE_SIZES), required=False),
    Field("contradicts", list_of(decision_id), required=False),
    Field("note", _note, required=False),
    # VM-BRIDGE-MODEL-1: the model this one directive runs on (the receiver checks it against what it can run)
    Field("model", lambda v: None if isinstance(v, str) and v.strip() else "must be a non-empty string", required=False),
]


def _evidence(v: Any) -> str | None:
    return None if (isinstance(v, str) and v.strip()) or (isinstance(v, list) and all(isinstance(x, str) for x in v)) else \
        "must be a string or a list of strings"


def _value(v: Any) -> str | None:
    return None if (isinstance(v, (int, float, str)) and not isinstance(v, bool)) else "must be a number or a string"


def _ci(v: Any) -> str | None:
    ok = isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)
    return None if ok and v[0] <= v[1] else "must be [lo, hi] numbers with lo <= hi"


REPORT2_ONLY = [
    Field("items", list_of(obj([Field("id", matches(D_ID, "D<number>")), Field("state", one_of(*ITEM_STATES)),
                                Field("evidence", list_of(is_str), required=False)]))),
    Field("results", list_of(obj([Field("name", is_str), Field("value", _value), Field("unit", is_str, required=False),
                                  Field("ci", _ci, required=False), Field("evidence", _evidence, required=False)])), required=False),
    Field("blockers", list_of(obj([Field("kind", one_of(*BLOCKER_KINDS)), Field("what", is_str),
                                   Field("gate", is_int(1), required=False)])), required=False),
    Field("deviations", list_of(is_str), required=False),
    Field("proposals", list_of(is_str), required=False),
    Field("note", _note, required=False),
]

def _shadow_row(v: Any) -> str | None:
    """CMD-GA45 S2: one shadow decision {id, rev, sha, decision, judge_class, input, output, mail, error, served, asks}; nulls allowed."""
    keys = {"id": str, "rev": int, "sha": str, "decision": str, "judge_class": str, "input": int, "output": int,
            "mail": str, "error": str, "served": str}  # CMD-GA49 S1: error, served (and asks) are additive
    if isinstance(v, dict) and v.get("asks") is not None:
        a = v["asks"]
        if not isinstance(a, list) or len(a) > 3 or not all(isinstance(x, str) and len(x) <= 300 for x in a):
            return "asks must be at most 3 strings of at most 300 characters"
        v = {k: x for k, x in v.items() if k != "asks"}
    if not isinstance(v, dict) or set(v) - set(keys) or not isinstance(v.get("decision"), str):
        return f"must be an object with decision and only {', '.join(keys)}"
    for k, t in keys.items():
        x = v.get(k)
        if x is not None and (not isinstance(x, t) or isinstance(x, bool) or (isinstance(x, str) and len(x) > 200)):
            return f"{k} must be {t.__name__} or null"
    return None


NOTIFY = [
    Field("to", is_str),
    Field("kind", one_of(*NOTIFY_KINDS)),
    Field("ref", matches(re.compile(r"^https?://\S+$"), "a URL")),
    Field("id", is_str, required=False),
    Field("shadow", _shadow_row, required=False),
    Field("note", _note, required=False),
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

REPORT2 = REPORT + REPORT2_ONLY

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
    Field("note", _note, required=False),
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

RUNNER_KINDS = ("headless", "agent_sdk", "remote")  # the Runners that open model turns (METHOD rev 13 §4c)

DECISION = [
    Field("id", decision_id),
    Field("date", is_str),
    Field("decision", is_str),
    Field("basis", is_str),
    Field("by", one_of("user", "hub")),
    Field("supersedes", list_of(decision_id), required=False),
    # §4c: a person's permission for a Runner that opens model turns, and how far it goes
    Field("scope", obj([
        Field("runner", one_of(*RUNNER_KINDS, "manual")),  # manual: the Judge alone is permitted (rev 14)
        Field("model", is_str, required=False),
        Field("sandbox", one_of("auto", "require", "off"), required=False),
        Field("budget", _budget, required=False),
        Field("measurement_calls", lambda v: None if isinstance(v, bool) else "must be true or false", required=False),
    ]), required=False),
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


def _unique(items: Any, path: str) -> list[Problem]:
    ids = [i.get("id") for i in items or [] if isinstance(i, dict)]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    return [Problem(path, f"item ids repeat: {', '.join(dup)}")] if dup else []


def _directive2_cross(doc: dict[str, Any]) -> list[Problem]:
    out = _directive_cross(doc)
    rev = doc.get("rev")
    if rev == 1:
        for k in ("scope", "done_when"):
            if not doc.get(k):
                out.append(Problem(f"$.{k}", "is required at rev 1 (a list of items)"))
        if doc.get("changes"):
            out.append(Problem("$.changes", "rev 1 has nothing to change"))
    elif isinstance(rev, int) and not doc.get("changes"):
        out.append(Problem("$.changes", "is required when rev > 1: write only what changed (METHOD rev 16 §3.6)"))
    out += _unique(doc.get("scope"), "$.scope") + _unique(doc.get("done_when"), "$.done_when")
    for i, c in enumerate(doc.get("changes") or []):
        if isinstance(c, dict) and c.get("op") in ("add", "edit") and not c.get("text"):
            out.append(Problem(f"$.changes[{i}].text", f"is required for {c.get('op')}"))
    return out


def _report2_cross(doc: dict[str, Any]) -> list[Problem]:
    return _unique(doc.get("items"), "$.items")


def apply_changes(prev: dict[str, Any] | None, doc: dict[str, Any]) -> dict[str, Any]:
    """The full directive/2 of this revision: the previous revision's scope and done_when items with this revision's
    ``changes`` put on them (add · edit · drop), the other fields from this revision. Hub records and turn prompts
    use the same computation. Raises FormError on a change to an item that is not there, or an add that is."""
    if doc.get("rev", 1) == 1 or not doc.get("changes"):
        return dict(doc)
    if prev is None:
        raise FormError([Problem("$.changes", f"rev {doc.get('rev')} needs the previous revision to apply its changes to")])
    lists = {"S": [dict(x) for x in prev.get("scope") or []], "D": [dict(x) for x in prev.get("done_when") or []]}
    problems = []
    for i, c in enumerate(doc["changes"]):
        items = lists[c["item"][0]]
        at = next((k for k, x in enumerate(items) if x["id"] == c["item"]), None)
        if c["op"] == "add":
            if at is not None:
                problems.append(Problem(f"$.changes[{i}]", f"{c['item']} is already there"))
            else:
                items.append({"id": c["item"], "text": c["text"]})
        elif at is None:
            problems.append(Problem(f"$.changes[{i}]", f"{c['item']} is not in the previous revision"))
        elif c["op"] == "edit":
            items[at] = {"id": c["item"], "text": c["text"]}
        else:
            items.pop(at)
    if problems:
        raise FormError(problems)
    full = {k: v for k, v in doc.items() if k != "changes"}
    full["scope"], full["done_when"] = lists["S"], lists["D"]
    return full


def deprecated(schema: str | None) -> list[Problem]:
    """Version 1 forms are still taken while moving, with a soft notice (METHOD rev 16 §3.6)."""
    new = {"directive/1": "directive/2", "report/1": "report/2"}.get(schema or "")
    return [Problem("$.schema", f"{schema} is deprecated: use {new} (METHOD rev 16 §3.6)", SOFT)] if new else []


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
    "directive/2": (DIRECTIVE2, _directive2_cross),
    "report/2": (REPORT2, _report2_cross),
    "notify/1": (NOTIFY, None),
}

# CMD-GA37 S1: the intake form (ga/forms/task.py)
from .task import TASK, task_cross  # noqa: E402

SCHEMAS["task/1"] = (TASK, task_cross)


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
    if head.get("schema") in ("report/1", "report/2"):
        problems += report_body_notes(body)
    return head, body, [p for p in problems if p.strength != HARD]
