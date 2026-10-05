"""One planning turn (CMD-GA40 S1/S3): card -> one model turn -> parse (one repair on a bad format, GA41 style) ->
``ga check`` (ga.forms) -> the lessons (ga.plan.lessons) -> a draft file. A draft is only written: nothing here mails,
posts or creates a session. Standard library only.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import repair
from ..forms import FormError, hard, minify, validate
from . import card as cardmod
from . import lessons

MAX_REPAIRS = repair.MAX_REPAIRS  # exactly one repair turn; a second bad answer fails the plan
REMINDER = cardmod.TEMPLATE

# the worker rules baseline adds in the session prompt (code writes them; a model-written prompt is checked by L6)
WORKER_RULES = """Worker rules (one directive per session):
- Branch claude/{branch} from the base the directive names; commit and push there. No PRs.
- Context cap: past ~150k tokens, commit, push and report what is done (status paused).
- Before the final push, merge the integration branch named in the hub's config and rerun the full suite.
- Report report/2 at reports/{id}.md; then one notify/1 line to the hub."""

_JSON_BLOCK = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S)


class PlanFailed(RuntimeError):
    def __init__(self, problems: list[str], turns: int):
        super().__init__("; ".join(problems))
        self.problems, self.turns = problems, turns


@dataclass
class Draft:
    head: dict[str, Any]
    item: dict[str, Any] | None
    worker_prompt: str
    heads: int = 1
    findings: list[dict[str, str]] = field(default_factory=list)
    check: list[str] = field(default_factory=list)    # ga check's hard problems of the head
    turns: int = 1
    card_bytes: int = 0
    path: Path | None = None

    @property
    def errors(self) -> int:
        return len(self.check) + sum(f["severity"] == lessons.ERROR for f in self.findings)

    def as_lessons_input(self) -> dict[str, Any]:
        return {"head": self.head, "item": self.item, "worker_prompt": self.worker_prompt, "heads": self.heads}


def extract(answer: str) -> tuple[Any, list[str]]:
    """(the answer's JSON, format problems). Format only: ga check and the lessons judge the content later."""
    text = (answer or "").strip()
    m = _JSON_BLOCK.search(text)
    raw = m.group(1) if m else (text[text.find("{"):text.rfind("}") + 1] if "{" in text else "")
    try:
        doc = json.loads(raw)
    except ValueError as e:
        return None, [f"not_json: {str(e)[:80] or 'no JSON object'}"]
    probs = []
    if not isinstance(doc, dict):
        return None, ["answer: must be one JSON object {head, item?}"]
    heads = doc.get("heads")
    if isinstance(heads, list) and heads:  # several heads: kept, and L6 reports it
        doc = dict(doc, head=heads[0])
    if not isinstance(doc.get("head"), dict):
        probs.append("head: must be an object (the directive/2 head)")
    elif doc["head"].get("schema", "directive/2") != "directive/2":
        probs.append('head.schema: must be "directive/2"')
    if "item" in doc and doc["item"] is not None and not isinstance(doc["item"], dict):
        probs.append("item: must be an object {goal, files, done_when} or left out")
    if "worker_prompt" in doc and not isinstance(doc["worker_prompt"], (str, type(None))):
        probs.append("worker_prompt: must be a string or left out")
    return doc, probs


def turn(runner: Any, text: str) -> str:
    t = runner.run_turn(text, None)
    return getattr(t, "answer", t if isinstance(t, str) else "")


def plan(request: str, repo: Path, runner: Any, *, to: str | None = None, decision_log: Path | None = None,
         ctx: dict[str, Any] | None = None) -> Draft:
    """The draft of one request. Raises PlanFailed when the answer breaks the form after its one repair."""
    c = cardmod.build(request, Path(repo), decision_log=decision_log, to=to)
    answer = turn(runner, c.text)
    doc, probs = extract(answer)
    turns = 1
    for _ in range(MAX_REPAIRS):
        if not probs:
            break
        answer = turn(runner, repair.prompt(probs, REMINDER, answer))
        turns += 1
        doc, probs = extract(answer)
    if probs:
        raise PlanFailed(probs, turns)
    head = dict(doc["head"])
    head.setdefault("schema", "directive/2")
    head.setdefault("rev", 1)
    if to:
        head.setdefault("to", to)
    item = doc.get("item") if isinstance(doc.get("item"), dict) else None
    heads = len(doc["heads"]) if isinstance(doc.get("heads"), list) else 1
    hid = head.get("id") if isinstance(head.get("id"), str) else "CMD-X"
    wp = doc.get("worker_prompt") or WORKER_RULES.format(branch=hid.lower().replace("cmd-", ""), id=hid)
    d = Draft(head, item, wp, heads, turns=turns, card_bytes=c.size)
    d.check = [str(p) for p in hard(validate(head))]
    d.findings = lessons.check(d.as_lessons_input(), dict(ctx or {}, repo=Path(repo)))
    return d


def draft_id(head: dict[str, Any], clock: Callable[[], float] = time.time) -> str:
    hid = head.get("id")
    if isinstance(hid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", hid):
        return hid
    return "draft-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(clock()))


def render(d: Draft) -> str:
    """The draft file: the head in one ```ga block (minified), then findings, ga check, item draft, worker prompt."""
    L = ["```ga", minify(d.head), "```", "", f"Status: draft (never sent) - {d.errors} error(s); "
         f"{d.turns} model turn(s); card {d.card_bytes} bytes", "", "## Findings"]
    L += [f"- {f['lesson']} {f['severity']} {f['where']}: {f['message']}" for f in d.findings] or ["- none"]
    L += ["", "## ga check"] + ([f"- {p}" for p in d.check] or ["- 0 hard"])
    if d.item is not None:
        L += ["", "## Item draft", "```json", json.dumps(d.item, ensure_ascii=False, indent=1), "```"]
    L += ["", "## Worker prompt", d.worker_prompt, ""]
    return "\n".join(L)


def write(d: Draft, home: Path, clock: Callable[[], float] = time.time) -> Path:
    out = Path(home) / "plan" / "drafts"
    out.mkdir(parents=True, exist_ok=True)
    d.path = out / f"{draft_id(d.head, clock)}.md"
    d.path.write_text(render(d), encoding="utf-8")
    return d.path


__all__ = ["Draft", "MAX_REPAIRS", "PlanFailed", "WORKER_RULES", "draft_id", "extract", "plan", "render", "write"]
