"""The intake turn's text (CMD-GA37 S2/S3): one fixed instruction and one output form for every backend.

A bare backend (claude_cli bare, openai_http, anthropic_http) gets INSTRUCTION as its system prompt and the message as
the prompt; a backend with no system channel (agv, codex_cli, gemini_cli) gets ``whole(message)``: INSTRUCTION, SEP,
message. The bytes do not depend on the backend or the model: nothing here takes either as an argument.

The model writes only what code cannot know. ``schema``, ``id``, ``request`` and ``repo`` are filled by code.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..forms.task import NEEDS

SEP = "\n\n---\n\n"
OUTPUT_KEYS = ("kind", "goal", "answer", "decision", "options", "deliverables", "constraints", "acceptance", "non_goals",
               "assumptions", "questions", "risks")

INSTRUCTION = f"""You turn a person's request about a software project into a task spec. You do no work, call no tools,
ask nothing. Answer with one JSON object and nothing else:
{{"kind": "answer"|"investigate"|"change"|"decide",
 "goal": str,
 "answer": {{"text": str, "cites": [str]}},
 "decision": {{"text": str, "affects": [str]}},
 "options": [{{"n": 1, "text": str, "recommended": bool}}],
 "deliverables": [{{"id": "V1", "what": str, "where": "path or place in the repo"}}],
 "constraints": [str],
 "acceptance": [{{"id": "A1", "check": str, "kind": "command"|"file"|"observable"|"review", "rubric": [str]}}],
 "non_goals": [str],
 "assumptions": [{{"text": str, "default": str}}],
 "questions": [{{"text": str, "needs": {"|".join(f'"{n}"' for n in NEEDS)}}}],
 "risks": [str]}}
Kinds:
- answer: a question the material below can answer. Give answer (short); every cite is a path or a phrase copied
  from the material. No answer field for other kinds.
- decide: the person settles a choice. Give decision; affects lists ids of open work it changes (from the state).
  No decision field for other kinds.
- investigate: find a cause or compare; change: modify the repo. Both need deliverables and acceptance; answer and
  decide may leave them [].
- options: only when you offer the person numbered choices (1, 2, ...); mark at most one recommended.
Rules:
- acceptance: each item is checkable without you. kind command: check is a command line to run (use the repo's test
  or build commands when they fit). kind file: check names a path that must exist or change. kind observable: check
  states a result with a number or a quoted literal (e.g. exits 0, shows "Saved"). kind review only when none of these
  can check it, and then give rubric (the points to check); no rubric for other kinds.
- No vague words (good, nice, fast, clean, properly, 적절히, 잘, 깔끔하게, 빠르게) unless a number or literal measures them.
- Ask a question only when the work truly needs one of: credential (a key or login), budget (money or paid quota),
  new_repo (a repository that does not exist), irreversible (deleting data, publishing, payments). Every other gap is
  an assumption with the default you chose; do not ask about it.
- A line "Meaning (resolved by code)" says what a short request refers to: take it as the request.
- Keep the repo's languages and tools unless the request says otherwise. Ids: V1.., A1...
- Write the text fields in the request's language."""


def message(request: str, summary_text: str, meaning: str | None = None) -> str:
    head = f"Request:\n{request}\n"
    if meaning:
        head += f"Meaning (resolved by code): {meaning}\n"
    return head + f"\nMaterial (gathered by code):\n{summary_text.rstrip()}\n"


def repair(problems: list[str], previous: str) -> str:
    """The repair turn: the validator's problems and the answer they are about. Not the request, not the material."""
    return ("Your JSON has these problems. Answer with the corrected JSON object only.\nProblems:\n"
            + "\n".join(f"- {p}" for p in problems) + f"\nYour JSON:\n{previous.strip()}\n")


def whole(text: str) -> str:
    """The prompt for a backend with no system channel."""
    return INSTRUCTION + SEP + text


_FENCE = re.compile(r"```(?:json|JSON)?[ \t]*\n(.*?)```", re.DOTALL)


def parse(answer: str) -> dict[str, Any] | None:
    """The answer's JSON object: a fenced ```json block or bare JSON, with text around it tolerated."""
    if not isinstance(answer, str):
        return None
    cands = [m.group(1) for m in _FENCE.finditer(answer)] + [answer]
    dec = json.JSONDecoder()
    for c in cands:
        i = c.find("{")
        while i != -1:
            try:
                obj, _ = dec.raw_decode(c, i)
            except ValueError:
                i = c.find("{", i + 1)
                continue
            if isinstance(obj, dict):
                return obj
            i = c.find("{", i + 1)
    return None


__all__ = ["INSTRUCTION", "SEP", "OUTPUT_KEYS", "message", "repair", "whole", "parse"]
