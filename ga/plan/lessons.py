"""The planning lessons as code (CMD-GA40 S2). Every send-back so far was a planning miss; each became a check here.

    L1  a code item goal names a typed field without its shape                         (AGA2: 8-turn cap)
    L2  TS/TSX in scope or files, no type-check / build in done_when                   (AGA4 rev 1: build broke)
    L3  a judge / verifier / guard scope whose planted-fault list misses a named check (CON1 rev 1: min-font, weight)
    L4  a scope that acts (integrates, mails, deletes, pushes, starts services) with no shadow / dry-run mode (GA42 r1)
    L5  defaults naming paths, ports, branches or venvs not read from the repo; the finding names the file to read (CON2)
    L6  more than one directive per session, or no context-cap rule in the worker prompt
    L7  a version bump while other directives are in flight (configurable)

A draft is ``{"head": directive/2 head, "item": {goal, files, done_when} | None, "worker_prompt": str | None,
"heads": n}``. ``check(draft, ctx)`` returns findings ``{lesson, severity, where, message}``; an error keeps the draft
a draft. Text heuristics only: no model, no network. Standard library only.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

ERROR = "error"

CHECKLIST = """L1 code item goal: every typed field named WITH its shape (nested too), e.g. turns: [{n: int, calls: [...]}]
L2 TS/TSX in scope or files: done_when runs the type-check / build (tsc --noEmit, npm run build)
L3 judge/verifier/guard scope: the planted-fault list covers EVERY check it names (checks: a, b -> planted: a, b)
L4 a scope that integrates, mails, deletes, pushes or starts services has a shadow or dry-run mode
L5 defaults (paths, ports, branches, venvs) are read from the repo (README / config), never assumed
L6 one directive per session; the worker prompt carries the context-cap rule (~150k)
L7 no version bump while other directives are in flight"""

_NEG = re.compile(r"\b(never|no|not|without|nor|don'?t|doesn'?t|won'?t)\b", re.I)
_CLAUSE = re.compile(r"[.;,:()\n]|\bbut\b")


def negated(text: str, start: int) -> bool:
    """True when the clause holding position ``start`` says never / not / no before it."""
    before = text[:start]
    cuts = [m.end() for m in _CLAUSE.finditer(before)]
    return bool(_NEG.search(before[cuts[-1] if cuts else 0:]))


def _hits(rx: re.Pattern[str], text: str) -> list[re.Match[str]]:
    return [m for m in rx.finditer(text or "") if not negated(text, m.start())]


def _f(lesson: str, where: str, message: str) -> dict[str, str]:
    return {"lesson": lesson, "severity": ERROR, "where": where, "message": message}


def _items(head: dict[str, Any], key: str) -> list[tuple[str, str]]:
    out = []
    for i, it in enumerate(head.get(key) or []):
        if isinstance(it, dict) and isinstance(it.get("text"), str):
            out.append((f"$.{key}[{i}]" + (f" ({it.get('id')})" if it.get("id") else ""), it["text"]))
    return out


def _item(draft: dict[str, Any]) -> dict[str, Any]:
    it = draft.get("item")
    return it if isinstance(it, dict) else {}


def _strs(v: Any) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else ([v] if isinstance(v, str) else [])


# ---- L1 ------------------------------------------------------------------------------------------------------------
_TYPED = re.compile(r"\btyped\b|\bfields?\b|\bschema\b|\bkeys?\b|\bnested\b|\b(lists?|arrays?|dicts?|maps?|objects?|"
                    r"records?)\s+of\b", re.I)
_SHAPE = re.compile(r"[\[{]\s*[\[{]?\s*[\"'`]?\w+[\"'`]?\s*:|\b\w+\s*:\s*(str|int|float|bool|number|string|list|dict|"
                    r"array|object)\b|\bshape\s*:\s*[\[{]", re.I)


def l1(draft: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, str]]:
    goal = _item(draft).get("goal")
    if not isinstance(goal, str):
        return []
    if _TYPED.search(goal) and not _SHAPE.search(goal):
        return [_f("L1", "$.item.goal", "the item goal names a typed field without its shape: write the nested shape "
                   "in the goal, e.g. field: [{key: type, ...}] (AGA2 hit the 8-turn cap without it)")]
    return []


# ---- L2 ------------------------------------------------------------------------------------------------------------
_TS = re.compile(r"\.(tsx?|mts|cts)\b|\bTypeScript\b|\bTSX?\b", re.I)
_TYPECHECK = re.compile(r"\btsc\b|type-?check|\b(npm|pnpm|yarn|bun|vite|next|turbo)\b[^.;\n]{0,20}\bbuild\b|"
                        r"\bbuild\s+(passes|is green|green|succeeds|clean)\b", re.I)


def l2(draft: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, str]]:
    head, it = draft.get("head") or {}, _item(draft)
    sources = [t for _, t in _items(head, "scope")] + _strs(it.get("files")) + _strs(it.get("goal"))
    if not any(_TS.search(t) for t in sources):
        return []
    checks = [t for _, t in _items(head, "done_when")] + _strs(it.get("done_when"))
    if any(_TYPECHECK.search(t) for t in checks):
        return []
    return [_f("L2", "$.done_when", "TS/TSX is in scope or files but no done_when runs the type-check or build "
               "(add e.g. `tsc --noEmit` / `npm run build` passes; AGA4 rev 1 broke the build)")]


# ---- L3 ------------------------------------------------------------------------------------------------------------
_JUDGE = re.compile(r"\b(judge|verifier|verify|guard|checker)s?\b", re.I)
_CHECKS = re.compile(r"\bchecks?\b\s*(?:\(|:|—|=)\s*([^.;)\n]+)", re.I)
_PLANTED = re.compile(r"\bplant(?:ed|s)?\b[^:(\n]{0,30}(?:\(|:|—)\s*([^.;\n]+)", re.I)
_SPLIT = re.compile(r",|\band\b|/|\+|&", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", s.lower())


def _names(listing: str) -> list[str]:
    out = []
    for part in _SPLIT.split(listing):
        n = _norm(re.sub(r"^\s*(a|an|the|every|each)\s+", "", part.strip(), flags=re.I))
        if n:
            out.append(n)
    return out


def l3(draft: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, str]]:
    head = draft.get("head") or {}
    texts = [t for _, t in _items(head, "scope")] + [t for _, t in _items(head, "done_when")] \
        + _strs(_item(draft).get("done_when"))
    planted = _norm(" ".join(m.group(1) for t in texts for m in _PLANTED.finditer(t)))
    out = []
    for where, text in _items(head, "scope"):
        if not _JUDGE.search(text):
            continue
        named = [n for m in _CHECKS.finditer(text) for n in _names(m.group(1))]
        if not named:
            continue
        if not planted:
            out.append(_f("L3", where, f"a judge/verifier/guard scope names checks ({', '.join(named)}) but no "
                          "planted-fault list exists: plant one fault per check"))
            continue
        missing = [n for n in named if n not in planted]
        if missing:
            out.append(_f("L3", where, f"the planted-fault list does not cover: {', '.join(missing)} (each named check "
                          "needs a planted fault; CON1 rev 1 left min-font and weight untested)"))
    return out


# ---- L4 ------------------------------------------------------------------------------------------------------------
_ACTS = re.compile(r"\b(integrat\w*|merg(?:e|es|ed|ing)\b|mail(?:s|ed|ing)?\b|send(?:s|ing)?\b|delet\w*|"
                   r"remov(?:e|es|ing)\b|push(?:es|ed|ing)?\b|deploy\w*|launch\w*|restart\w*|kill\w*|"
                   r"start(?:s|ing)?\s+(?:the\s+|a\s+)?(?:\w+\s+){0,2}(?:services?|servers?|daemons?|stack|processes?))",
                   re.I)
_SHADOW = re.compile(r"\bshadow\b|dry[- ]?run|\bpreview\b|\bplan[- ]only\b", re.I)


def l4(draft: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, str]]:
    head = draft.get("head") or {}
    goal = head.get("goal") if isinstance(head.get("goal"), str) else ""
    out = []
    for where, text in _items(head, "scope"):
        acts = _hits(_ACTS, text)
        if acts and not _SHADOW.search(text) and not _SHADOW.search(goal):
            verbs = sorted({m.group(1).split()[0].lower() for m in acts})
            out.append(_f("L4", where, f"this scope acts ({', '.join(verbs)}) with no shadow or dry-run mode: add one, "
                          "default on (GA42 rev 1 shipped without it)"))
    return out


# ---- L5 ------------------------------------------------------------------------------------------------------------
_DEFAULT = re.compile(r"\bdefaults?\b|\bdefaulting\b|기본값", re.I)
_LITERAL = re.compile(r"(?<![\w/])(~/[\w.\-/]*|\.{1,2}/[\w.\-/]+|/(?:home|usr|opt|tmp|var|Users|etc|srv)/[\w.\-/]*)"
                      r"|(?<![\w/])\.?venv\b|\bport\s+\d{2,5}\b|(?<![\w/]):\d{2,5}\b|\b(?:origin/)?(?:main|master|develop)"
                      r"\b(?=\s*(?:branch|$|[,.;)]))|\bbranch\s+(?:[\w.\-]+/[\w./\-]+|main|master|develop|trunk)\b|\bclaude/[\w\-]+", re.I)
_SOURCED = re.compile(r"\b(read|reads|reading|per|from|parsed)\b[^.;\n]{0,40}\b(README|config|\.ga-judge\.json|"
                      r"pyproject|package\.json|[\w\-]+\.(json|toml|ya?ml|ini|cfg|md|env))", re.I)
_CONFIGS = ("pyproject.toml", "package.json", ".ga-judge.json", "ga.json", "setup.cfg")


def file_to_read(repo: Path | None) -> str:
    """The file a default should come from: the repo's README, else its first config file, else README.md."""
    if repo:
        for name in ("README.md", "README.rst", "README.txt", "README") + _CONFIGS:
            if (Path(repo) / name).is_file():
                return name
    return "README.md"


def l5(draft: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, str]]:
    head = draft.get("head") or {}
    out = []
    texts = _items(head, "scope") + ([("$.item.goal", _item(draft)["goal"])]
                                     if isinstance(_item(draft).get("goal"), str) else [])
    for where, text in texts:
        if not _DEFAULT.search(text) or _SOURCED.search(text):
            continue
        lits = sorted({m.group(0).strip() for m in _LITERAL.finditer(text)})
        if lits:
            out.append(_f("L5", where, f"defaults name {', '.join(lits)} without reading them from the repo: read "
                          f"{file_to_read(ctx.get('repo'))} (and its config) and say so (CON2 assumed the paths)"))
    return out


# ---- L6 ------------------------------------------------------------------------------------------------------------
_DIRECTIVE_BLOCK = re.compile(r"\"schema\"\s*:\s*\"directive/2\"")
_CAP = re.compile(r"context[- ](cap|budget|limit)|~?\d{2,3}\s?k\s+tokens|\bpast\s+~?\d{2,3}\s?k\b", re.I)


def l6(draft: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    wp = draft.get("worker_prompt") or ""
    n = max(int(draft.get("heads") or 1), len(_DIRECTIVE_BLOCK.findall(wp)))
    if n > 1:
        out.append(_f("L6", "$", f"{n} directives for one session: one directive per session"))
    if not _CAP.search(wp):
        out.append(_f("L6", "$.worker_prompt", "the worker prompt has no context-cap rule (e.g. past ~150k tokens: "
                      "commit, push, report paused)"))
    return out


# ---- L7 ------------------------------------------------------------------------------------------------------------
_BUMP = re.compile(r"\bbump\w*\b[^.;\n]{0,30}\bversion\b|\bversion\b[^.;\n]{0,20}\bbump\w*|__version__\s*=|"
                   r"\bversion\s+(?:to\s+)?\d+\.\d+", re.I)


def l7(draft: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, str]]:
    if ctx.get("l7") is False:
        return []
    in_flight = [x for x in ctx.get("in_flight") or [] if x != (draft.get("head") or {}).get("id")]
    if not in_flight:
        return []
    head, it = draft.get("head") or {}, _item(draft)
    texts = _items(head, "scope") + _items(head, "done_when") + [("$.item", t) for t in
                                                                   _strs(it.get("goal")) + _strs(it.get("done_when"))]
    for where, text in texts:
        if _hits(_BUMP, text):
            return [_f("L7", where, f"a version bump while {len(in_flight)} other directive(s) are in flight "
                       f"({', '.join(map(str, in_flight[:5]))}): leave the version to landing")]
    return []


LESSONS: dict[str, Callable[[dict[str, Any], dict[str, Any]], list[dict[str, str]]]] = {
    "L1": l1, "L2": l2, "L3": l3, "L4": l4, "L5": l5, "L6": l6, "L7": l7}


def check(draft: dict[str, Any], ctx: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Every lesson's findings on a draft, in lesson order."""
    ctx = ctx or {}
    return [f for name, fn in LESSONS.items() for f in fn(draft, ctx)]


__all__ = ["CHECKLIST", "LESSONS", "check", "file_to_read", "negated", "l1", "l2", "l3", "l4", "l5", "l6", "l7"]
