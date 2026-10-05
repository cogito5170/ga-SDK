"""Apply EDIT and NEW actions (CMD-GA38 S1) inside the item's owned files only.

A path must be relative, stay inside the root (symlinks resolved), not be under .git or .ga, and match one of the
item's ``files`` globs (the pool's ownership rule, ga.net.pool.owned). A SEARCH that does not occur exactly once
(byte for byte) rejects only that block; the nearest lines of the file go into the next card.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..net.pool import owned
from .fmt import Action


@dataclass
class Outcome:
    applied: list[str] = field(default_factory=list)    # "EDIT a.py block 1"
    rejected: list[str] = field(default_factory=list)   # "EDIT a.py block 2: no exact match"
    nearest: list[str] = field(default_factory=list)    # numbered file lines for each no-match block
    changed: list[str] = field(default_factory=list)    # paths written


def safe_rel(root: Path, rel: str) -> Path | None:
    """The path ``rel`` inside ``root`` or None (absolute, .., .git/.ga, or a symlink out)."""
    parts = rel.replace("\\", "/").split("/")
    if not rel or rel.startswith("/") or ".." in parts or any(x in (".git", ".ga") for x in parts) or "" in parts:
        return None
    p = root / rel
    try:
        rp, rr = p.resolve(), root.resolve()
    except OSError:
        return None
    return p if rp == rr or rr in rp.parents else None


SECRET_FILE = re.compile(r"^(\.env.*|.*\.pem|.*\.key|id_rsa.*|id_ed25519.*|id_ecdsa.*|id_dsa.*|.*\.p12|.*\.pfx|"
                         r"\.npmrc|\.pypirc|\.netrc|\.git-credentials)$", re.I)


def secret_path(rel: str) -> bool:
    """A path any part of which is .git, .ga, or a secret-shaped file name (.env*, *.pem, *.key, id_rsa*, ...)."""
    return any(x in (".git", ".ga") or SECRET_FILE.match(x) for x in rel.replace("\\", "/").split("/"))


def readable(root: Path, rel: str) -> Path | None:
    """The path a retriever may read (CMD-GA38 rev 2): inside the root and never a secret or internal file."""
    p = safe_rel(root, rel)
    if p is None or secret_path(rel):
        return None
    try:
        if secret_path(str(p.resolve().relative_to(Path(root).resolve()))):  # a symlink to a secret file
            return None
    except (OSError, ValueError):
        return None
    return p


def occurrences(text: str, search: str) -> list[int]:
    """Offsets of every exact occurrence of ``search`` (overlapping ones too)."""
    out, i = [], text.find(search)
    while i >= 0:
        out.append(i)
        i = text.find(search, i + 1)
    return out


def nearest(text: str, search: str, extra: int = 2, width: int = 160) -> str:
    """The window of the file most like ``search`` (difflib ratio), numbered, with ``extra`` lines around it."""
    fl = text.splitlines()
    sl = search.splitlines() or [""]
    n = max(1, len(sl))
    best, at = -1.0, 0
    want = "\n".join(s.strip() for s in sl)
    for i in range(0, max(1, len(fl) - n + 1)):
        r = difflib.SequenceMatcher(None, "\n".join(s.strip() for s in fl[i:i + n]), want, autojunk=False).ratio()
        if r > best:
            best, at = r, i
    a, b = max(0, at - extra), min(len(fl), at + n + extra)
    return "\n".join(f"{k + 1:>5}| {fl[k][:width]}" for k in range(a, b))


def apply(root: Path, actions: list[Action], globs: list[str]) -> Outcome:
    out = Outcome()
    for a in actions:
        if a.kind not in ("EDIT", "NEW"):
            continue
        p = safe_rel(root, a.arg)
        if p is None:
            out.rejected.append(f"{a.kind} {a.arg}: path outside the repository")
            continue
        if not owned(a.arg, globs):
            out.rejected.append(f"{a.kind} {a.arg}: not in the item's files (you may edit only those)")
            continue
        if a.kind == "NEW":
            if p.exists():
                out.rejected.append(f"NEW {a.arg}: the file exists (use EDIT)")
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(a.content, encoding="utf-8")
            out.applied.append(f"NEW {a.arg} ({len(a.content.splitlines())} lines)")
            out.changed.append(a.arg)
            continue
        if not p.is_file():
            out.rejected.append(f"EDIT {a.arg}: no such file (use NEW)")
            continue
        text = p.read_text(encoding="utf-8")
        before = text
        for k, b in enumerate(a.blocks, 1):
            if not b.search.strip():
                out.rejected.append(f"EDIT {a.arg} block {k}: empty SEARCH")
                continue
            hits = occurrences(text, b.search)
            if len(hits) != 1:
                why = "no exact match" if not hits else f"{len(hits)} matches (make SEARCH unique)"
                out.rejected.append(f"EDIT {a.arg} block {k}: {why}")
                out.nearest.append(f"{a.arg} block {k}, nearest lines:\n" + nearest(text, b.search))
                continue
            text = text[:hits[0]] + b.replace + text[hits[0] + len(b.search):]
            out.applied.append(f"EDIT {a.arg} block {k}")
        if text != before:
            p.write_text(text, encoding="utf-8")
            if a.arg not in out.changed:
                out.changed.append(a.arg)
    return out
