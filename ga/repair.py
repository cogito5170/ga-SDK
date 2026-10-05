"""One repair turn (CMD-GA41 S1): a model answer that breaks the form gets exactly one more turn, carrying only

    the checker's problem labels (e.g. ``say: must be a string``, ``not_json: ...``)
    the form's short reminder (a few lines, not the protocol)
    the model's own previous answer, capped

and never the task, the tool results or the history again. A second bad answer fails the step as before. ``ga do``
(ga.intake) has its own repair turn of the same shape (CMD-GA37); ``ga supervise`` / ``ga gemini`` plans and ``ga act``
action blocks use this one. Standard library only.
"""
from __future__ import annotations

import re

MAX_REPAIRS = 1          # exactly one repair turn per bad answer
PREVIOUS_CAP = 2000      # characters of the previous answer carried back

# check_plan's labels -> one line each (labels only: the plan text itself is never repeated in a label)
PLAN_LABELS = {
    "not_json": "the answer holds no JSON object (or it does not parse)",
    "plan: not an object": "the JSON must be one object",
    "schema": 'schema: must be "{form}"',
    "unknown_field": "only the fields schema, steps, next, say are allowed",
    "steps": "steps: must be a list of at most 16 steps",
    "next": "next: must be an object {prompt: non-empty string, after?: [step ids]}",
    "next.after": "next.after: must list ids of steps in this plan",
    "say": "say: must be a string",
}
_STEP = re.compile(r"^steps\[(\d+)\](?:\.(.+))?$")
_STEP_TEXT = {None: "must be {id, tool, args?, after?}", "id": "id: a new label [A-Za-z0-9_-]{1,12}",
              "tool_not_in_table": "tool: not in the tool table", "args": "args: must be an object",
              "after": "after: must list ids of earlier steps"}

PLAN_REMINDER = """Answer with ONE ```json block and nothing else:
{{"schema": "{form}", "steps": [{{"id": "s1", "tool": "<a tool from the table>", "args": {{}}, "after": []}}],
 "next": {{"prompt": "<what to decide next>", "after": ["s1"]}}, "say": "<one line for the user>"}}
steps, next and say are optional; say is a string; no other fields."""


def plan_problem(label: str, form: str) -> str:
    m = _STEP.match(label)
    if m:
        return f"steps[{m.group(1)}].{m.group(2) or ''}".rstrip(".") + ": " + _STEP_TEXT.get(m.group(2), "invalid")
    if label in PLAN_LABELS:
        text = PLAN_LABELS[label].format(form=form)
        return text if text.startswith(label) else f"{label}: {text}"
    return label[:120]


def cap(previous: str, limit: int = PREVIOUS_CAP) -> str:
    previous = (previous or "").strip()
    return previous if len(previous) <= limit else previous[:limit] + "\n(cut)"


def prompt(problems: list[str], reminder: str, previous: str, limit: int = PREVIOUS_CAP) -> str:
    """The repair turn's whole text: problems, the form's reminder, the previous answer (capped). Nothing else."""
    return ("Your previous answer broke the answer form. Answer again, in the form only.\nProblems:\n"
            + "\n".join(f"- {p}" for p in problems[:12])
            + f"\nThe form:\n{reminder.strip()}\nYour previous answer:\n{cap(previous, limit)}\n")


def plan_prompt(labels: list[str], form: str, previous: str) -> str:
    return prompt([plan_problem(x, form) for x in labels], PLAN_REMINDER.format(form=form), previous)


__all__ = ["MAX_REPAIRS", "PREVIOUS_CAP", "PLAN_REMINDER", "plan_problem", "plan_prompt", "prompt", "cap"]
