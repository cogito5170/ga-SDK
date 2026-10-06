"""The planner's card (CMD-GA40 S1): one fixed-size text (<= CARD_MAX bytes, UTF-8) for the one model turn.

    the request                       (capped)
    the target repo's map             top-level dirs, package names, test commands (.ga-judge.json), README run heads
    the last 10 decision titles       only when a DECISION_LOG.md path is configured
    the directive/2 template
    the lessons checklist             (ga.plan.lessons.CHECKLIST)

The fixed parts (template, checklist) always go in whole; the request and the repo map share what is left, the map
losing lines first. Code reads the repo; nothing runs and nothing is sent. Standard library only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..act.route import LADDER, LEVELS
from .lessons import CHECKLIST

CARD_MAX = 8 * 1024          # bytes, UTF-8: the whole card
REQUEST_MAX = 1500           # bytes of the request carried
DECISIONS = 10               # last N decision titles
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build"}
_RUN_HEAD = re.compile(r"\b(run|running|install|setup|set up|start|usage|quick ?start|develop|test|build|dev)\b|실행|설치",
                       re.I)

TEMPLATE = """Answer with ONE ```json block and nothing else:
{"head": {"schema": "directive/2", "id": "CMD-<ROLE><n>", "rev": 1, "to": "<role>", "after": [],
  "goal": "<one paragraph; name every typed field WITH its shape, e.g. turns: [{n: int, calls: [{name: str}]}]>",
  "why": "<why now>", "refs": ["<repo@branch (sha): what to read>"],
  "scope": [{"id": "S1", "text": "<what to build; acting scopes have a shadow / dry-run mode>"}],
  "done_when": [{"id": "D1", "text": "<a test or command that proves it>"}],
  "budget": {"claude_p_runs": 0}},
 "item": {"goal": "<code work only: the item goal>", "files": ["<paths>"], "done_when": ["<checks>"],
  "route": {"difficulty": 1-5, "start": "<model>", "max_turns": 1-10}}}
Leave "item" out when the request is not code work.
route (CMD-GA47): the cheapest model likely to finish the item; models, cheapest first: """ + ", ".join(LADDER) + """
difficulty, one line per level:\n""" + "\n".join(LEVELS)


@dataclass
class Card:
    text: str
    sections: dict[str, int] = field(default_factory=dict)   # name -> bytes
    dropped: list[str] = field(default_factory=list)          # what was cut to fit

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8"))


def _b(s: str) -> int:
    return len(s.encode("utf-8"))


def _cut(s: str, limit: int) -> str:
    """``s`` cut to at most ``limit`` UTF-8 bytes (on a character boundary), marked when cut."""
    if _b(s) <= limit:
        return s
    mark = " ...(cut)"
    raw = s.encode("utf-8")[:max(0, limit - _b(mark))]
    return raw.decode("utf-8", "ignore") + mark


def _read(p: Path, limit: int = 256 * 1024) -> str:
    try:
        with p.open("rb") as f:
            return f.read(limit).decode("utf-8", "ignore")
    except OSError:
        return ""


def readme(repo: Path) -> Path | None:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        if (repo / name).is_file():
            return repo / name
    return None


def run_heads(text: str) -> list[str]:
    """README section heads about running, installing or testing (and the heads nested under one)."""
    out: list[str] = []
    under = 0  # level of the run section we are inside, 0 = none
    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if not m:
            continue
        level, title = len(m.group(1)), m.group(2)
        if under and level > under:
            out.append("  " * (level - under) + title)
            continue
        under = level if _RUN_HEAD.search(title) else 0
        if under:
            out.append(title)
    return out


def packages(repo: Path) -> list[str]:
    """Package names: pyproject.toml [project] name, package.json name (top level and one level down)."""
    out: list[str] = []
    m = re.search(r'(?ms)^\[project\].*?^name\s*=\s*"([^"]+)"', _read(repo / "pyproject.toml"))
    if m:
        out.append(f"{m.group(1)} (pyproject.toml)")
    cands = [repo / "package.json"]
    try:
        cands += sorted(d / "package.json" for d in repo.iterdir() if d.is_dir() and d.name not in _SKIP_DIRS)
    except OSError:
        pass
    for p in cands:
        if p.is_file():
            try:
                name = json.loads(_read(p)).get("name")
            except (ValueError, AttributeError):
                name = None
            if name:
                out.append(f"{name} ({p.relative_to(repo).as_posix()})")
    return out


def test_commands(repo: Path) -> list[str]:
    try:
        cfg = json.loads(_read(repo / ".ga-judge.json"))
    except ValueError:
        return []
    out = []
    for k in ("test", "test_named", "typecheck", "build", "lint"):
        v = cfg.get(k) if isinstance(cfg, dict) else None
        if isinstance(v, list):
            out.append(f"{k}: " + " ".join(map(str, v)))
        elif isinstance(v, str):
            out.append(f"{k}: {v}")
    return out


def repo_map(repo: Path) -> list[str]:
    """The map's lines, most useful first (each section can be cut from its end)."""
    L: list[str] = []
    try:
        dirs = sorted(d.name for d in repo.iterdir() if d.is_dir() and d.name not in _SKIP_DIRS)
    except OSError:
        dirs = []
    rd = readme(repo)
    L.append(f"repo: {repo.name}" + (f" (readme: {rd.name})" if rd else " (no README)"))
    L += [f"test command - {c}" for c in test_commands(repo)]
    L += [f"package - {p}" for p in packages(repo)]
    L += [f"readme run head - {h}" for h in (run_heads(_read(rd)) if rd else [])]
    L += [f"dir - {d}/" for d in dirs]
    return L


def decisions(log: Path | None, n: int = DECISIONS) -> list[str]:
    if not log:
        return []
    titles = [m.group(1).strip() for m in re.finditer(r"(?m)^#{2,3}\s+(.+)$", _read(Path(log), 4 * 1024 * 1024))]
    return titles[-n:]


def build(request: str, repo: Path, *, decision_log: Path | None = None, to: str | None = None,
          limit: int = CARD_MAX) -> Card:
    """The card. Never larger than ``limit`` bytes: the repo map loses lines from its end, then the request is cut."""
    repo = Path(repo)
    fixed_tail = "## Template\n" + TEMPLATE + "\n\n## Lessons checklist (code checks your draft)\n" + CHECKLIST + "\n"
    head = "# GA Planner card (directive/2 draft, one turn)\n" + (f"Directive for role: {to}\n" if to else "")
    req = "## Request\n" + _cut(request.strip(), REQUEST_MAX) + "\n\n"
    dec = decisions(decision_log)
    dec_txt = ("## Last decisions\n" + "".join(f"- {_cut(t, 160)}\n" for t in dec) + "\n") if dec else ""
    dropped: list[str] = []
    room = limit - _b(head) - _b(req) - _b(dec_txt) - _b(fixed_tail) - _b("## Repo map\n\n")
    if room < 0:  # decisions go first, then the request shrinks
        dec_txt, dropped = "", dropped + ["decisions"]
        room = limit - _b(head) - _b(req) - _b(fixed_tail) - _b("## Repo map\n\n")
    if room < 0:
        req = "## Request\n" + _cut(request.strip(), max(64, _b(req) + room - 16)) + "\n\n"
        room = limit - _b(head) - _b(req) - _b(fixed_tail) - _b("## Repo map\n\n")
        dropped.append("request")
    lines, used, rmap = [], 0, repo_map(repo)
    for ln in rmap:
        ln = _cut(ln, 200) + "\n"
        if used + _b(ln) > max(room, 0):
            dropped.append(f"repo map lines ({len(rmap) - len(lines)})")
            break
        lines.append(ln)
        used += _b(ln)
    mp = "## Repo map\n" + "".join(lines) + "\n"
    text = head + req + mp + dec_txt + fixed_tail
    text = _cut(text, limit) if _b(text) > limit else text  # a last guard; the budget above keeps it unused
    return Card(text, {"request": _b(req), "repo_map": _b(mp), "decisions": _b(dec_txt), "fixed": _b(fixed_tail)},
                dropped)


__all__ = ["CARD_MAX", "Card", "TEMPLATE", "build", "decisions", "packages", "readme", "repo_map", "run_heads",
           "test_commands"]
