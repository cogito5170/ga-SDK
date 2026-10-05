"""The approved tools of ga's tool table (CMD-GA36 S3; baseline's ops/agy_bridge/agy_tools.py, BD-356). ga supervise
calls these as Python tools; the model only plans.

Rules for every tool here: read-only, confined to the project folder (the process cwd), output capped, no network,
no shell. A new tool comes in only through review (code + test), never installed because a model asked for it.
"""
from __future__ import annotations

import re
from pathlib import Path

CAP = 20000          # characters returned per call
SKIP = {".git", ".ga", ".ga-supervise", "node_modules", ".venv", "__pycache__"}

# the table `ga bridge` and `ga ask --solve` hand to ga supervise: these names and nothing else
TOOLS = {
    "list_dir": {"python": "ga.bridge.tools:list_dir", "about": "list one project folder; args: path (default '.')"},
    "read_file": {"python": "ga.bridge.tools:read_file", "about": "read numbered lines of one project text file; args: "
                  "path, start (default 1), lines (default 400)"},
    "search": {"python": "ga.bridge.tools:search", "about": "regex search under a project folder; args: pattern, path "
               "(default '.'), max_hits (default 50)"},
    "run_check": {"python": "ga.bridge.tools:run_check", "about": "run one command the project owner allowed, by name "
                  "(e.g. 'test'); args: name"},
}


def _root() -> Path:
    return Path.cwd().resolve()


def _safe(path: str) -> Path:
    root = _root()
    q = (root / str(path)).resolve()
    if q != root and root not in q.parents:
        raise ValueError("outside the project")
    if any(part in SKIP or part.startswith(".env") for part in q.relative_to(root).parts):
        raise ValueError("not readable through the bridge")
    return q


def list_dir(path: str = ".") -> str:
    """Names in one project folder; folders end with '/'. Hidden and vendored folders are left out."""
    q = _safe(path)
    names = [x.name + ("/" if x.is_dir() else "") for x in q.iterdir() if not x.name.startswith(".") and x.name not in SKIP]
    return "\n".join(sorted(names))[:CAP]


def read_file(path: str, start: int = 1, lines: int = 400) -> str:
    """Lines [start, start+lines) of one text file in the project, numbered."""
    q = _safe(path)
    rows = q.read_text(encoding="utf-8", errors="replace").splitlines()
    s = max(int(start), 1)
    part = rows[s - 1: s - 1 + max(int(lines), 1)]
    return "\n".join(f"{s + i}\t{r}" for i, r in enumerate(part))[:CAP]


def search(pattern: str, path: str = ".", max_hits: int = 50) -> str:
    """Lines matching a regular expression under a project folder: 'file:line: text'."""
    rx = re.compile(pattern)
    base = _safe(path)
    root = _root()
    hits: list[str] = []
    files = [base] if base.is_file() else sorted(base.rglob("*"))
    for f in files:
        rel = f.relative_to(root)
        if not f.is_file() or any(p in SKIP or p.startswith(".") for p in rel.parts) or f.stat().st_size > 2_000_000:
            continue
        try:
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                    if len(hits) >= int(max_hits):
                        return "\n".join(hits)[:CAP]
        except (UnicodeDecodeError, OSError):
            continue
    return "\n".join(hits)[:CAP] or "(no match)"


_SECRETISH = re.compile(r"(?i)(key|token|secret|password|credential|auth)")


def run_check(name: str) -> str:
    """Run one command the project owner allowed by name (agy-bridge.json "commands": {"test": ["npm", "test"]}).

    The argv is fixed in the config: the model picks only the name. No shell, project cwd, 600 s cap, environment
    variables whose names look like secrets removed, output tail capped. Exit code first line.
    """
    import json
    import os
    import subprocess
    allowed = json.loads(os.environ.get("AGY_BRIDGE_COMMANDS") or "{}")
    if name not in allowed:
        raise ValueError(f"not an allowed command: {name!r} (allowed: {', '.join(sorted(allowed)) or 'none'})")
    env = {k: v for k, v in os.environ.items() if not _SECRETISH.search(k)}
    try:
        p = subprocess.run(list(allowed[name]), cwd=_root(), env=env, capture_output=True, text=True, timeout=600)
        out, code = (p.stdout or "") + (p.stderr or ""), p.returncode
    except subprocess.TimeoutExpired:
        out, code = "timed out after 600 s", 124
    return f"exit {code}\n" + out[-CAP:]
