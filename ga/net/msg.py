"""The peer message form (CMD-GA31 S1/S2): an ``exchange/1`` head (from · to, BD-133 'a reported fact, not an action')
plus exactly one ```peer block holding one item. No new schema: the head is checked by ``ga check`` like any form.

    request      {"kind": "request", "ref", "needs"?, "input"?}                 i asks j for a ref
    observation  {"kind": "observation", "ref", "value", "evidence", "re"?}      j observed X (evidence required)
    opinion      {"kind": "opinion", "ref", "value", "why", "re"?}               j thinks X: a Proposal, never State

The receiver reads a message as the observed fact 'j sent X' and X as an Observation reference or a Proposal
(NETWORK.md 3.1). Nothing in a peer message is ever a directive or an action.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from ..forms import FormError, hard, minify, parse_text, validate
from ..mailbox import secrets_in

KINDS = ("request", "observation", "opinion")
_BLOCK = re.compile(r"^```peer[ \t]*\n(.*?)\n```[ \t]*$", re.MULTILINE | re.DOTALL)
REF = re.compile(r"^[A-Za-z0-9_.\[\]:/-]{1,120}$")


def item_problems(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return ["the peer item is not an object"]
    k = item.get("kind")
    if k not in KINDS:
        return [f"kind must be one of {', '.join(KINDS)}"]
    out = []
    if not isinstance(item.get("ref"), str) or not REF.match(item["ref"]):
        out.append("ref must be a ref name")
    allowed = {"request": {"kind", "ref", "needs", "input"},
               "observation": {"kind", "ref", "value", "evidence", "re"},
               "opinion": {"kind", "ref", "value", "why", "re"}}[k]
    extra = set(item) - allowed
    if extra:  # a peer item carries data only: no command, directive or action field gets through
        out.append(f"unknown fields for {k}: {', '.join(sorted(extra))}")
    if k == "observation" and not (isinstance(item.get("evidence"), str) and item["evidence"].strip()):
        out.append("an observation needs evidence (an id)")
    if k in ("observation", "opinion") and not isinstance(item.get("value"), str):
        out.append("value must be text")
    if k == "opinion" and not isinstance(item.get("why"), str):
        out.append("an opinion needs why")
    return out


def build(sender: str, to: str, item: dict[str, Any], why: str) -> str:
    head = {"schema": "exchange/1", "from": sender, "to": to, "why": why[:200], "asked": item.get("ref", ""),
            "got": item.get("kind", "")}
    return "```ga\n" + minify(head) + "\n```\n```peer\n" + minify(item) + "\n```\n"


def parse(text: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """-> (head, item, problems). Any problem: the message is not used (it is still a received fact)."""
    try:
        head, body = parse_text(text)
    except FormError as e:
        return None, None, [str(p) for p in e.problems]
    probs = [str(p) for p in hard(validate(head))]
    if head.get("schema") != "exchange/1":
        probs.append(f"a peer message is exchange/1, not {head.get('schema')!r}")
    blocks = _BLOCK.findall(body)
    if len(blocks) != 1:
        return head, None, probs + [f"need exactly one ```peer block, found {len(blocks)}"]
    try:
        item = json.loads(blocks[0])
    except json.JSONDecodeError:
        return head, None, probs + ["the peer block is not JSON"]
    return head, item, probs + item_problems(item)


def check(text: str, cfg: Any = None, where: str = "peer message") -> list[str]:
    """Before a send: ga check (form) + R6 (secrets, the built-in patterns and the config's own)."""
    _, _, probs = parse(text)
    if secrets_in(text):
        probs.append("looks like a secret")
    if cfg is not None:
        from .. import rules
        probs += [str(p) for p in hard(rules.r6_secrets(cfg, text, where))]
    return probs


def blocks_in(answer: str) -> list[Any]:
    """The outgoing peer items a turn's answer holds (each ```peer block: one item or a list of items with ``to``)."""
    out: list[Any] = []
    for b in _BLOCK.findall(answer or ""):
        try:
            v = json.loads(b)
        except json.JSONDecodeError:
            out.append(None)
            continue
        out.extend(v if isinstance(v, list) else [v])
    return out


def strip_blocks(answer: str) -> str:
    return _BLOCK.sub("", answer or "")


def tokens_est(nbytes: int) -> int:
    return math.ceil(nbytes / 4)


def msg_id(path: str) -> str:
    """The id of a mailed message: its mailbox file name without ``.md`` (unique on the mailbox branch)."""
    return path.rsplit("/", 1)[-1][:-3] if path.endswith(".md") else path.rsplit("/", 1)[-1]
