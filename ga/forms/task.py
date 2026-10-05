"""task/1 (CMD-GA37 S1, S6): a person's natural-language request as a checked task, before any work starts.

    {"schema": "task/1", "id": "T-<8 hex>", "request": "<verbatim, at most REQUEST_CAP chars>",
     "kind": "answer|investigate|change|decide", "goal": "...",
     "answer": {"text", "cites": [str]} (kind answer only), "decision": {"text", "affects": [work id]} (kind decide only),
     "options": [{"n": 1, "text", "recommended": bool}] (choices offered to the person; optional),
     "resolution": {"fragment", "to", "how": "state|assumption"} (filled by code for a short fragment; optional),
     "deliverables": [{"id": "V1", "what", "where"}], "constraints": [str],
     "acceptance": [{"id": "A1", "check", "kind": "command|file|observable|review", "rubric": [str] (review only)}],
     "non_goals": [str], "assumptions": [{"text", "default"}],
     "questions": [{"text", "needs": "credential|budget|new_repo|irreversible"}], "risks": [str],
     "repo": {"languages": {ext-name: files}, "test_cmds": [str], "build_cmds": [str]}}

Checks beyond the shape (all from ``task_cross``):
- kind investigate and change need at least one deliverable and one acceptance item; answer and decide may have none.
  kind answer needs ``answer``, kind decide needs ``decision``, and no other kind has either. Options are numbered
  1, 2, 3 ... (``task:kind``, ``task:options``).
- every acceptance item names a runnable command (kind command), a path (kind file) or an observable result with a
  measure: a number or a quoted literal (kind observable). kind review only with a rubric; it is a soft notice anyway,
  since a review is the last resort (``task:review``).
- a vague word (VAGUE) without a measure is flagged: hard in an acceptance check, soft in the goal, a deliverable or a
  constraint (``task:vague``).
- a question's ``needs`` must be one of NEEDS (the four things only a person can give); any other gap is an
  assumption with a default (S4).
"""
from __future__ import annotations

import re
from typing import Any

from .core import HARD, SOFT, Field, Problem, is_int, is_str, list_of, matches, obj, one_of

REQUEST_CAP = 2000
NEEDS = ("credential", "budget", "new_repo", "irreversible")
ACCEPTANCE_KINDS = ("command", "file", "observable", "review")
KINDS = ("answer", "investigate", "change", "decide")  # rev 2 S6: what the request is; investigate and change go on
WORK_KINDS = ("investigate", "change")                 # the kinds that need deliverables and acceptance
TASK_ID = re.compile(r"^T-[0-9a-f]{8}$")
ITEM_ID = re.compile(r"^[A-Z][0-9]{1,3}$")

# vague words (S1). English as whole words; Korean as written (잘 alone, so 잘못 is not one).
VAGUE_EN = ("good", "nice", "fast", "clean", "properly", "better", "robust", "user-friendly", "nicely", "quickly")
VAGUE_KO = ("적절히", "깔끔하게", "빠르게", "적당히", "좋게", "멋지게")
_VAGUE = re.compile(r"(?i)(?<![\w-])(" + "|".join(re.escape(w) for w in VAGUE_EN) + r")(?![\w-])"
                    + "|(" + "|".join(VAGUE_KO) + r")|(?<![가-힣])(잘)(?![가-힣])")
# a measure: a number, or a quoted literal ("..", '..', `..`, 「..」, “..”)
_MEASURE = re.compile(r"\d|\"[^\"]+\"|'[^']+'|`[^`]+`|「[^」]+」|“[^”]+”")
# a command line: its first word is a program or a path, not a prose word
_CMD_HEAD = re.compile(r"^[A-Za-z0-9_./:+=-]+$")
_PROSE = {"the", "a", "an", "it", "ensure", "verify", "check", "should", "must", "all", "user", "users",
          "when", "if", "that", "this", "tests", "test", "run", "build"}
_PATH = re.compile(r"(?:^|[\s`'\"(])((?:[\w.-]+/)*[\w-]+\.[A-Za-z0-9]{1,8}|(?:[\w.-]+/)+[\w.-]*)(?=$|[\s`'\"),:])")


def vague_words(text: str) -> list[str]:
    """The vague words in ``text`` that stand without a measure (a number or a quoted literal); [] if it has one."""
    if not isinstance(text, str) or _MEASURE.search(text):
        return []
    return [next(g for g in m.groups() if g) for m in _VAGUE.finditer(text)]


def is_command(check: str) -> bool:
    """A runnable command line: ``npm test``, ``python -m pytest tests/test_x.py``, ``./run.sh``."""
    words = check.strip().strip("`").split()
    if not words:
        return False
    head = words[0]
    if not _CMD_HEAD.match(head) or head.lower() in _PROSE:
        return False
    # "run tests" style prose has its own words; "pytest -k login" has a program word
    return not (head[0].isupper() and len(words) > 2 and not head.startswith("./"))


def names_path(check: str) -> bool:
    return bool(_PATH.search(" " + check.strip() + " "))


def _bool(v: Any) -> str | None:
    return None if isinstance(v, bool) else "must be true or false"


def _rubric(v: Any) -> str | None:
    return list_of(is_str, 1)(v)


def _repo(v: Any) -> str | None:
    if not isinstance(v, dict):
        return "must be an object"
    langs = v.get("languages")
    if not isinstance(langs, dict) or not all(isinstance(k, str) and isinstance(n, int) and not isinstance(n, bool)
                                               for k, n in langs.items()):
        return "languages must be a {name: file count} object"
    for k in ("test_cmds", "build_cmds"):
        if list_of(is_str)(v.get(k)):
            return f"{k} must be a list of strings"
    extra = sorted(set(v) - {"languages", "test_cmds", "build_cmds"})
    return f"unknown field(s) {', '.join(extra)}" if extra else None


def _request(v: Any) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return "must be a non-empty string"
    return f"must be at most {REQUEST_CAP} characters" if len(v) > REQUEST_CAP else None


TASK = [
    Field("id", matches(TASK_ID, "T-<8 hex>")),
    Field("request", _request),
    Field("kind", one_of(*KINDS)),
    Field("goal", is_str),
    Field("answer", obj([Field("text", is_str), Field("cites", list_of(is_str, 1))]), required=False),
    Field("decision", obj([Field("text", is_str), Field("affects", list_of(is_str))]), required=False),
    Field("options", list_of(obj([Field("n", is_int(1)), Field("text", is_str),
                                  Field("recommended", _bool, required=False)])), required=False),
    Field("resolution", obj([Field("fragment", is_str), Field("to", is_str), Field("how", one_of("state", "assumption"))]),
          required=False),
    Field("deliverables", list_of(obj([Field("id", matches(ITEM_ID, "a letter and a number")), Field("what", is_str),
                                       Field("where", is_str)]))),
    Field("constraints", list_of(is_str)),
    Field("acceptance", list_of(obj([Field("id", matches(ITEM_ID, "a letter and a number")), Field("check", is_str),
                                     Field("kind", one_of(*ACCEPTANCE_KINDS)),
                                     Field("rubric", _rubric, required=False)]))),
    Field("non_goals", list_of(is_str)),
    Field("assumptions", list_of(obj([Field("text", is_str), Field("default", is_str)]))),
    Field("questions", list_of(obj([Field("text", is_str), Field("needs", one_of(*NEEDS))]))),
    Field("risks", list_of(is_str)),
    Field("repo", _repo),
]


def task_cross(doc: dict[str, Any]) -> list[Problem]:
    out: list[Problem] = []
    kind = doc.get("kind")
    if kind in WORK_KINDS:
        for key in ("deliverables", "acceptance"):
            if not doc.get(key):
                out.append(Problem(f"$.{key}", f"kind {kind} needs at least one item", HARD, "task:kind"))
    for key, owner in (("answer", "answer"), ("decision", "decide")):
        if kind == owner and key not in doc:
            out.append(Problem(f"$.{key}", f"kind {owner} needs {key}", HARD, "task:kind"))
        elif kind != owner and key in doc:
            out.append(Problem(f"$.{key}", f"only kind {owner} has {key}", HARD, "task:kind"))
    ns = [o.get("n") for o in doc.get("options", [])]
    if ns and ns != list(range(1, len(ns) + 1)):
        out.append(Problem("$.options", "options are numbered 1, 2, 3 ... in order", HARD, "task:options"))
    for key in ("deliverables", "acceptance"):
        ids = [x.get("id") for x in doc.get(key, [])]
        for i in sorted({i for i in ids if ids.count(i) > 1}):
            out.append(Problem(f"$.{key}", f"id {i} is used twice", HARD, "task:ids"))
    for i, a in enumerate(doc.get("acceptance", [])):
        path, check, kind = f"$.acceptance[{i}]", a["check"], a["kind"]
        if kind == "command" and not is_command(check):
            out.append(Problem(path + ".check", "kind command must be a runnable command line (e.g. `npm test`)",
                               HARD, "task:check"))
        elif kind == "file" and not names_path(check):
            out.append(Problem(path + ".check", "kind file must name a path (e.g. src/app.ts)", HARD, "task:check"))
        elif kind == "observable" and not _MEASURE.search(check):
            out.append(Problem(path + ".check", "kind observable must state a measured result: a number or a quoted "
                               "literal (e.g. 'exits 0', 'shows \"Saved\"')", HARD, "task:check"))
        elif kind == "review":
            if not a.get("rubric"):
                out.append(Problem(path + ".rubric", "kind review needs a rubric (the points a reviewer checks)",
                                   HARD, "task:review"))
            else:
                out.append(Problem(path + ".kind", "review: only for what no command, file or observable can check",
                                   SOFT, "task:review"))
        if kind != "review":
            if "rubric" in a:
                out.append(Problem(path + ".rubric", "only kind review takes a rubric", HARD, "task:review"))
        words = vague_words(check)
        if words:
            out.append(Problem(path + ".check", f"vague word(s) {', '.join(words)} without a measure", HARD,
                               "task:vague"))
    soft_texts = [("$.goal", doc.get("goal", ""))]
    soft_texts += [(f"$.deliverables[{i}].what", d.get("what", "")) for i, d in enumerate(doc.get("deliverables", []))]
    soft_texts += [(f"$.constraints[{i}]", c) for i, c in enumerate(doc.get("constraints", []))]
    for path, text in soft_texts:
        words = vague_words(text)
        if words:
            out.append(Problem(path, f"vague word(s) {', '.join(words)} without a measure", SOFT, "task:vague"))
    return out


__all__ = ["TASK", "task_cross", "NEEDS", "KINDS", "WORK_KINDS", "ACCEPTANCE_KINDS", "REQUEST_CAP", "VAGUE_EN", "VAGUE_KO", "vague_words",
           "is_command", "names_path"]
