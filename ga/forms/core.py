"""Shared machinery for the closed forms (METHOD §3).

A form document is a dict with a ``schema`` key such as ``"directive/1"``.
On the wire it is a Markdown text whose first block is a fenced ``ga`` block
holding the JSON head; everything after that block is the human body.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

HARD = "hard"
SOFT = "soft"

FENCE_RE = re.compile(r"\A\s*```ga[ \t]*\n(.*?)\n```[ \t]*(?:\n|\Z)", re.DOTALL)
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
DIRECTIVE_ID_RE = re.compile(r"^CMD-([A-Z]+)(\d+)$")
DECISION_ID_RE = re.compile(r"^BD-(\d+)$")


@dataclass(frozen=True)
class Problem:
    """One finding of a validator. ``hard`` rejects the document, ``soft`` only notifies."""

    path: str
    message: str
    strength: str = HARD
    rule: str | None = None

    def __str__(self) -> str:
        tag = f"{self.rule} " if self.rule else ""
        return f"[{self.strength}] {tag}{self.path}: {self.message}"


class FormError(ValueError):
    """Raised when a document has hard problems."""

    def __init__(self, problems: list[Problem]):
        self.problems = problems
        super().__init__("; ".join(str(p) for p in problems))


def hard(problems: Iterable[Problem]) -> list[Problem]:
    return [p for p in problems if p.strength == HARD]


def soft(problems: Iterable[Problem]) -> list[Problem]:
    return [p for p in problems if p.strength == SOFT]


# --------------------------------------------------------------------------- wire format


def parse_text(text: str) -> tuple[dict[str, Any], str]:
    """Split a posted text into (head, body). Raises FormError when the head is missing or not JSON."""
    m = FENCE_RE.match(text)
    if not m:
        raise FormError([Problem("$", "no leading ```ga fenced JSON head")])
    try:
        head = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise FormError([Problem("$", f"head is not JSON: {e.msg} (line {e.lineno})")]) from None
    if not isinstance(head, dict):
        raise FormError([Problem("$", "head must be a JSON object")])
    return head, text[m.end():]


def dump_text(head: dict[str, Any], body: str = "") -> str:
    """Inverse of parse_text. The head is written compactly on one line so posts stay readable."""
    out = "```ga\n" + json.dumps(head, ensure_ascii=False, sort_keys=True) + "\n```\n"
    if body:
        out += body if body.endswith("\n") else body + "\n"
    return out


def canonical_json(obj: Any) -> str:
    """Byte-stable JSON for records (G2)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


SECTION_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def parse_sections(body: str) -> dict[str, str]:
    """Read ``## <Name>`` sections of a body. Unknown sections are kept; text before the first heading is ``_preamble``."""
    out: dict[str, str] = {}
    matches = list(SECTION_RE.finditer(body))
    pre = body[: matches[0].start()] if matches else body
    if pre.strip():
        out["_preamble"] = pre.strip()
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out[m.group(1).strip()] = body[m.end():end].strip()
    return out


# --------------------------------------------------------------------------- field specs

Check = Callable[[Any], "str | None"]


@dataclass(frozen=True)
class Field:
    name: str
    check: Check
    required: bool = True
    # strength of the "missing" problem; a malformed present value is always hard
    missing: str = HARD
    rule: str | None = None


def is_str(v: Any) -> str | None:
    return None if isinstance(v, str) and v.strip() else "must be a non-empty string"


def is_int(minimum: int = 0) -> Check:
    def check(v: Any) -> str | None:
        if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
            return f"must be an integer >= {minimum}"
        return None

    return check


def one_of(*allowed: str) -> Check:
    def check(v: Any) -> str | None:
        return None if v in allowed else f"must be one of {', '.join(allowed)}"

    return check


def matches(regex: re.Pattern[str], what: str) -> Check:
    def check(v: Any) -> str | None:
        return None if isinstance(v, str) and regex.match(v) else f"must be {what}"

    return check


def list_of(item: Check, min_len: int = 0) -> Check:
    def check(v: Any) -> str | None:
        if not isinstance(v, list):
            return "must be a list"
        if len(v) < min_len:
            return f"must have at least {min_len} item(s)"
        for i, x in enumerate(v):
            err = item(x)
            if err:
                return f"[{i}] {err}"
        return None

    return check


def obj(fields: list[Field]) -> Check:
    """Check a nested object; only hard problems turn into an error string here."""

    def check(v: Any) -> str | None:
        if not isinstance(v, dict):
            return "must be an object"
        probs = hard(check_fields(v, fields, ""))
        return "; ".join(f"{p.path.lstrip('.')} {p.message}" for p in probs) or None

    return check


def any_value(_: Any) -> str | None:
    return None


def check_fields(doc: dict[str, Any], fields: list[Field], prefix: str = "$") -> list[Problem]:
    problems: list[Problem] = []
    known = {f.name for f in fields}
    for f in fields:
        path = f"{prefix}.{f.name}"
        if f.name not in doc or doc[f.name] is None:
            if f.required:
                problems.append(Problem(path, "is required", f.missing, f.rule))
            continue
        err = f.check(doc[f.name])
        if err:
            problems.append(Problem(path, err, HARD, f.rule))
    for k in doc:
        if k not in known and k != "schema":
            problems.append(Problem(f"{prefix}.{k}", "unknown field", HARD))
    return problems
