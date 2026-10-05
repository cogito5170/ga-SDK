"""Bulk context: the working repository's files put into the prompt whole (FINAL_TASK 5.1: 50k-150k tokens per call).
Token estimate is ga.ctxpack.tokens (ceil(bytes / 4)); the real count is read back from claude -p usage and checked."""
from __future__ import annotations

from pathlib import Path

from ga.ctxpack import tokens

MIN_TOKENS = 50_000
TARGET_TOKENS = 70_000
MAX_TOKENS = 150_000
ROOT = Path(__file__).resolve().parents[3]
_SKIP = {".git", "bench", "__pycache__", "results", "reports", ".ga"}
_EXT = {".py", ".md", ".toml"}


def repo_files(root: Path = ROOT) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and p.suffix in _EXT and not (set(rel.parts) & _SKIP):
            out.append(p)
    return out


def bulk_context(root: Path = ROOT, target: int = TARGET_TOKENS) -> dict:
    """-> {"text", "files": [rel], "tokens": est}. Whole files, in path order, until the target is reached (never cut
    inside a file, never below MIN_TOKENS: a repository too small for that is an error, not a smaller prompt)."""
    parts, files, total = [], [], 0
    for p in repo_files(root):
        body = p.read_text(encoding="utf-8", errors="replace")
        part = f"=== {p.relative_to(root)} ===\n{body}\n"
        if total + tokens(part) > MAX_TOKENS:
            continue
        parts.append(part)
        files.append(str(p.relative_to(root)))
        total += tokens(part)
        if total >= target:
            break
    if total < MIN_TOKENS:
        raise ValueError(f"bulk context is {total} est. tokens, under the {MIN_TOKENS} minimum")
    return {"text": "## project files (whole)\n" + "".join(parts) + "\n", "files": files, "tokens": total}
