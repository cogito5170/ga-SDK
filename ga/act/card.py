"""The state card (CMD-GA38 S2): rebuilt from files every turn, never from chat history, in fixed sections:

    stable prefix   1 format spec · 2 item (id, goal) · 3 done_when · 4 files you may edit · 5 commands
                    (the same bytes every turn of an item, so a provider's prompt cache can hit it)
    turn part       6 failing tests (names + key stack lines) · 7 code (slices for failing frames, NEED results,
                    small owned files) · 8 last turn (applied / rejected, nearest lines, command results) · 9 budget

Over ``cap`` tokens (ctxpack.tokens) the units are dropped lowest priority first — small owned files, then frame
slices, then NEED results — and then the longest remaining section is cut; the budget line always stays.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..ctxpack import tokens
from ..rules import SECRET_PATTERNS
from .fmt import SPEC

DEFAULT_CAP = 6000
SECTIONS = ("failing", "code", "last", "budget")
TITLES = {"failing": "## 6 failing (done_when)", "code": "## 7 code", "last": "## 8 last turn", "budget": "## 9 budget"}


class CardError(ValueError):
    pass


def prefix(item_id: str, goal: str, done_when: list[str], globs: list[str], owned_files: list[str],
           commands: dict[str, list[str]], actions: dict[str, str] | None = None) -> str:
    """The stable prefix: built once per item. Approved GA Actions (CMD-GA42) show as name + one-line about."""
    cmd_lines = [f"- {n}" for n in sorted(commands)] or ["- (none)"]
    cmd_lines += [f"- {n} (action): {a}" for n, a in sorted((actions or {}).items()) if n not in commands]
    lines = [SPEC.rstrip("\n"), "", f"## 2 item {item_id}", goal.strip(), "", "## 3 done_when (code runs it)",
             json.dumps(done_when), "", "## 4 files you may edit (globs, then the files that exist now)",
             *[f"- {g}" for g in globs], *[f"  {f}" for f in owned_files[:60]],
             *(["  ..."] if len(owned_files) > 60 else []), "", "## 5 commands (RUN <name>)", *cmd_lines, ""]
    return "\n".join(lines) + "\n"


@dataclass
class Unit:
    section: str
    text: str
    rank: int  # higher = dropped first (0 = never dropped)


@dataclass
class Card:
    prefix: str
    body: str
    tokens: int
    cap: int
    dropped: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.prefix + self.body


def redact(text: str) -> tuple[str, int]:
    """Every span a secret pattern (rule R6, as ga.mailbox.secrets_in) flags -> "(withheld)"; (text, spans)."""
    n = 0
    for pat in SECRET_PATTERNS:
        text, k = re.subn(pat, "(withheld)", text)
        n += k
    return text, n


def _render(units: list[Unit]) -> str:
    out = []
    for s in SECTIONS:
        parts = [u.text.rstrip("\n") for u in units if u.section == s]
        out.append(TITLES[s] + "\n" + ("\n".join(parts) if parts else "(none)") + "\n")
    return "\n".join(out)


def build(pre: str, units: list[Unit], cap: int = DEFAULT_CAP) -> Card:
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        raise CardError("the card cap must be a positive integer")
    base = tokens(pre) + tokens(_render([u for u in units if u.rank == 0]))
    if base > cap:
        raise CardError(f"the stable prefix and budget alone are over the cap ({base} > {cap} tokens)")
    kept = list(units)
    dropped: list[str] = []
    body = _render(kept)
    while tokens(pre + body) > cap:
        cand = [u for u in kept if u.rank > 0]
        if not cand:
            break
        worst = max(cand, key=lambda u: (u.rank, kept.index(u)))
        kept.remove(worst)
        dropped.append(f"{worst.section}:{worst.text.splitlines()[0][:80]}" if worst.text else worst.section)
        body = _render(kept)
    while tokens(pre + body) > cap:  # still over: cut the longest unit that may be cut
        cut = max((u for u in kept if u.rank > 0 or u.section != "budget"), key=lambda u: len(u.text), default=None)
        if cut is None or len(cut.text) < 80:
            break
        over = (tokens(pre + body) - cap) * 4 + 64
        cut.text = cut.text[:max(0, len(cut.text) - over)].rstrip() + "\n(cut to the card cap)"
        dropped.append(f"{cut.section}:cut")
        body = _render(kept)
    t = tokens(pre + body)
    if t > cap:
        raise CardError(f"the card is over the cap ({t} > {cap} tokens)")
    return Card(pre, body, t, cap, dropped)
