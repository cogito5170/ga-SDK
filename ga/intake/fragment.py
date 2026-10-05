"""Short context-dependent requests and secret-looking text (CMD-GA37 rev 2 S7, S8). Code only, no model.

A fragment is a whole request such as '1번', 'option 2', '추천대로 해', '시작해', '어디까지 되었어?'. ``resolve`` reads the
state store (ga/intake/state.py) and says what it means:

    choose N     option N of the last offer                 -> how "state"
    recommend    the option marked recommended              -> how "state" (none marked: option 1, an assumption)
    start        the newest open work item, else the recommended option
    status       the open work and the recent ledger rows   -> kind hint "answer"

When the state cannot settle it, the meaning is an assumption (the most recent offer, or else 'a new request with no
prior offer'), recorded in the spec. A fragment never becomes a question to the person.

``withhold`` replaces every line that looks like a secret (ga.rules.SECRET_PATTERNS plus a loose ``name = value``
form) before any model sees the text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..rules import SECRET_PATTERNS
from .state import State

WITHHELD = "[withheld: secret-looking text]"
_LOOSE = r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*\S{8,}"
_SECRETS = [re.compile(p) for p in SECRET_PATTERNS + [_LOOSE]]

_CHOOSE = [re.compile(p, re.I) for p in (
    r"^\s*(\d{1,2})\s*(?:번|번으로|번으로 해(?:줘)?|번 해(?:줘)?|번째|번이요|번요)?\s*[.!]?\s*$",
    r"^\s*(?:(?:go with|pick|take|choose|use)\s+)?(?:the\s+)?(?:option|choice|number|no\.?)\s*#?\s*(\d{1,2})\s*[.!]?\s*$",
    r"^\s*(?:go with|pick|take|choose)\s+#?(\d{1,2})\s*[.!]?\s*$",
    r"^\s*(\d{1,2})\s*번\s*(?:으로)?\s*(?:가자|진행해(?:줘)?|하자)\s*[.!]?\s*$")]
_RECOMMEND = re.compile(r"(추천(?:한|하는)?\s*대로|권장(?:한|하는)?\s*대로|as you recommend(?:ed)?|"
                        r"(?:go with|take|use) (?:your|the) recommend(?:ation|ed(?: one)?)|your call)", re.I)
_START = re.compile(r"^\s*(시작해(?:줘|봐)?|진행해(?:줘)?|해\s*줘|고고|start|go ahead|go|proceed|let'?s go|begin)\s*[.!]?\s*$",
                    re.I)
_STATUS = re.compile(r"(어디까지\s*(?:되었|됐|했|진행)|진행\s*상황|지금\s*상태|^\s*status\s*\??\s*$|where are we|"
                     r"how far (?:are|is|did)|what'?s the status|progress\s*\?)", re.I)
MAX_FRAGMENT = 40  # characters: anything longer is a request in its own right


@dataclass
class Resolution:
    fragment: str
    form: str                          # choose | recommend | start | status
    to: str                            # what it means, in words
    how: str                           # state | assumption
    kind_hint: str | None = None
    assumption: dict[str, str] | None = None

    def spec(self) -> dict[str, str]:
        return {"fragment": self.fragment, "to": self.to, "how": self.how}

    def line(self) -> str:
        return f"'{self.fragment}' means: {self.to}" + (" (assumed)" if self.how == "assumption" else "")


def withhold(text: str) -> tuple[str, int]:
    """(text with secret-looking lines replaced, how many lines were replaced)."""
    out, n = [], 0
    for line in text.split("\n"):
        if any(p.search(line) for p in _SECRETS):
            out.append(WITHHELD)
            n += 1
        else:
            out.append(line)
    return "\n".join(out), n


def form_of(request: str) -> tuple[str, int | None] | None:
    r = request.strip()
    if not r or len(r) > MAX_FRAGMENT:
        return None
    for p in _CHOOSE:
        m = p.match(r)
        if m:
            return "choose", int(m.group(1))
    if _STATUS.search(r):
        return "status", None
    if _RECOMMEND.search(r):
        return "recommend", None
    if _START.match(r):
        return "start", None
    return None


def _option_line(o: dict[str, Any], offer: dict[str, Any]) -> str:
    return f"option {o['n']} of {offer['task']}: {o['text']}"


def resolve(request: str, state: State) -> Resolution | None:
    """What a fragment means, from the state; None for a request that is not a fragment."""
    f = form_of(request)
    if f is None:
        return None
    form, n = f
    frag = request.strip()
    offer = state.options()
    items = offer["items"] if offer else []
    rec = next((o for o in items if o.get("recommended")), None)
    open_work = [w for w in state.work() if w.get("status") != "done"]

    def assume(to: str, why: str) -> Resolution:
        return Resolution(frag, form, to, "assumption", None,
                          {"text": f"what '{frag}' refers to: {why}", "default": to})

    if form == "status":
        if open_work or state.recent():
            return Resolution(frag, form, "the status of the open work and recent requests in the state", "state",
                              "answer")
        r = assume("the status of the project as the repository shows it", "no open work is recorded")
        r.kind_hint = "answer"
        return r
    if form == "choose":
        hit = next((o for o in items if o["n"] == n), None)
        if hit:
            return Resolution(frag, form, _option_line(hit, offer), "state")
        if items:
            take = rec or items[0]
            return assume(_option_line(take, offer), f"the last offer has no option {n}; took option {take['n']}")
        return assume(f"a new request '{frag}' with no prior offer", "no options were offered before")
    if form == "recommend":
        if rec:
            return Resolution(frag, form, _option_line(rec, offer), "state")
        if items:
            return assume(_option_line(items[0], offer), "no option was marked recommended; took option 1")
        return assume(f"a new request '{frag}' with no prior offer", "no recommendation was offered before")
    # start
    if open_work:
        w = open_work[-1]
        return Resolution(frag, form, f"start open work {w['id']}: {w.get('title', '')}", "state")
    if items:
        take = rec or items[0]
        return Resolution(frag, form, _option_line(take, offer), "state" if rec else "assumption",
                          assumption=None if rec else {"text": f"what '{frag}' refers to: no open work and no "
                                                               "recommended option; took option 1",
                                                       "default": _option_line(take, offer)})
    return assume(f"a new request '{frag}' with no prior offer", "no open work and no offer in the state")


__all__ = ["Resolution", "resolve", "withhold", "form_of", "WITHHELD"]
