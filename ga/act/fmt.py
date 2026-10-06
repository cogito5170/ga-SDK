"""The action format (CMD-GA38 S1): the only thing a model answers in ``ga act``. The spec text below is the same bytes
every turn (it opens the card's stable prefix); adding a command or a retriever never grows it.

    EDIT <path>                 then one or more blocks, each matched exactly once in the file:
    <<<<<<< SEARCH
    (existing lines, exactly)
    =======
    (new lines)
    >>>>>>> REPLACE
    NEW <path>                  a new file, or one of your files rewritten whole:
    <<<<<<< CONTENT
    (the whole file)
    >>>>>>> END
    RUN <name>                  one of the command names in the card; never a command line
    NEED symbol <dotted.name>   | NEED file <path> [lines a-b] | NEED grep <text>: shown in the next card
    DONE                        | BLOCKED <reason>

Lines outside these are ignored (counted as ``noise``); a fenced ``` line around the whole answer is dropped.
Standard library only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SPEC = """\
# ga act: answer ONLY with these actions (no prose, no history: this card is all you get)
EDIT <path>
<<<<<<< SEARCH
<lines copied exactly from the file; must match exactly once>
=======
<replacement lines>
>>>>>>> REPLACE
  (several SEARCH/REPLACE blocks may follow one EDIT; keep each SEARCH short but unique)
NEW <path>
<<<<<<< CONTENT
<whole content of a new file, or of one of your files you rewrite whole>
>>>>>>> END
RUN <name>                 run a command listed under "commands" (names only, never a command line)
RUN <action> path=<p>      run an approved action by name; values only for its placeholders (path=, name=)
PROPOSE <name>             propose a new named command; the next line is one JSON object:
{"argv": [...], "why": "...", "example": {"path": "..."}, "cwd": ".", "timeout_s": 60, "writes": []}
                           (no shell; a person approves it later, never in this answer)
NEED symbol <dotted.name>  code of a function/class (e.g. pkg.mod.func or Class.method), shown next turn
NEED file <path> [lines a-b]
NEED grep <text>
DONE                       the item is finished (code runs done_when; red means not done)
BLOCKED <reason>           you cannot finish; say why in one line
Rules: edit only the files listed under "files"; a rejected block shows the nearest lines next turn.
"""

_TOP = re.compile(r"^(EDIT|NEW|RUN|NEED|DONE|BLOCKED|PROPOSE)(?:[ \t]+(.*?))?[ \t]*$")
_KV = re.compile(r"^(path|name)=\S+$")
_NEED = re.compile(r"^(symbol|file|grep)[ \t]+(.+?)$")
_LINES = re.compile(r"^(?P<path>\S+)(?:[ \t]+lines?[ \t]+(?P<a>\d+)[ \t]*-[ \t]*(?P<b>\d+))?$")
S_OPEN, S_MID, S_CLOSE = "<<<<<<< SEARCH", "=======", ">>>>>>> REPLACE"
C_OPEN, C_CLOSE = "<<<<<<< CONTENT", ">>>>>>> END"


@dataclass
class Block:
    search: str
    replace: str


@dataclass
class Action:
    kind: str                      # EDIT NEW RUN NEED DONE BLOCKED PROPOSE
    arg: str = ""                  # path, command name, reason; NEED: the target
    need: str = ""                 # NEED: symbol file grep
    lines: tuple[int, int] | None = None
    blocks: list[Block] = field(default_factory=list)
    content: str = ""              # NEW: the file; PROPOSE: the JSON line
    values: dict[str, str] = field(default_factory=dict)  # RUN <action>: placeholder values


@dataclass
class Parsed:
    actions: list[Action]
    problems: list[str]            # format problems, shown in the next card
    noise: int = 0                 # lines outside any action


def _text(lines: list[str]) -> str:
    return "".join(ln + "\n" for ln in lines)


def _content(lines: list[str]) -> str:
    """NEW content: trailing empty lines collapse to one final newline; empty content stays empty."""
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return _text(lines)


def parse(answer: str) -> Parsed:
    lines = (answer or "").replace("\r\n", "\n").split("\n")
    acts: list[Action] = []
    probs: list[str] = []
    noise = 0
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = _TOP.match(ln)
        if not m:
            if ln.strip() and not ln.strip().startswith("```"):
                noise += 1
            i += 1
            continue
        kind, arg = m[1], (m[2] or "").strip()
        i += 1
        if kind in ("DONE",):
            acts.append(Action("DONE"))
        elif kind == "BLOCKED":
            acts.append(Action("BLOCKED", arg or "no reason given"))
        elif kind == "RUN":
            name, *kv = arg.split() or [""]
            vals = dict(x.split("=", 1) for x in kv if _KV.match(x))
            if not arg or len(vals) != len(kv):
                probs.append(f"RUN takes one command name (and key=value for an action), got {arg!r}"[:200])
            else:
                acts.append(Action("RUN", name, values=vals))
        elif kind == "PROPOSE":
            body = lines[i].strip() if i < len(lines) else ""
            if not arg or not body.startswith("{"):
                probs.append(f"PROPOSE <name> then one JSON line, got {arg!r}"[:200])
            else:
                acts.append(Action("PROPOSE", arg, content=body))
                i += 1
        elif kind == "NEED":
            n = _NEED.match(arg)
            if not n:
                probs.append(f"NEED symbol|file|grep <target>, got {arg!r}"[:200])
                continue
            a = Action("NEED", n[2].strip(), need=n[1])
            if n[1] == "file":
                f = _LINES.match(a.arg)
                if not f:
                    probs.append(f"NEED file <path> [lines a-b], got {arg!r}"[:200])
                    continue
                a.arg = f["path"]
                if f["a"]:
                    a.lines = (int(f["a"]), int(f["b"]))
            acts.append(a)
        elif kind == "EDIT":
            a = Action("EDIT", arg)
            while i < len(lines) and lines[i].strip() == S_OPEN:
                j = i + 1
                try:
                    mid = lines.index(S_MID, j)
                    end = lines.index(S_CLOSE, mid + 1)
                except ValueError:
                    probs.append(f"EDIT {arg}: a SEARCH block without ======= and >>>>>>> REPLACE")
                    i = len(lines)
                    break
                a.blocks.append(Block(_text(lines[j:mid]), _text(lines[mid + 1:end])))
                i = end + 1
                while i < len(lines) and not lines[i].strip():
                    i += 1
            if not arg:
                probs.append("EDIT without a path")
            elif not a.blocks:
                probs.append(f"EDIT {arg}: no SEARCH/REPLACE block")
            else:
                acts.append(a)
        elif kind == "NEW":
            if i < len(lines) and lines[i].strip() == C_OPEN:
                try:
                    end = lines.index(C_CLOSE, i + 1)
                except ValueError:
                    probs.append(f"NEW {arg}: no >>>>>>> END line")
                    i = len(lines)
                    continue
                if arg:
                    acts.append(Action("NEW", arg, content=_content(lines[i + 1:end])))
                else:
                    probs.append("NEW without a path")
                i = end + 1
            else:
                probs.append(f"NEW {arg}: content must follow in <<<<<<< CONTENT ... >>>>>>> END")
    return Parsed(acts, probs, noise)
