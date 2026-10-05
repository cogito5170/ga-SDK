"""The repository summary the intake turn sees (CMD-GA37 S2): gathered by code, no model, under ``CAP`` tokens.

Units, highest priority first: languages (files per language), commands (test/build/lint detected from package.json,
pyproject.toml, Makefile, Cargo.toml, go.mod), project state (rev 2 S7, when given), ga config (backend/model keys only, never a value of any other key),
tree (top level, then one level of the biggest directories), README head. Over the cap the lowest unit is cut line by
line from its end, then the next; each cut is listed in ``dropped``. Same tree, same bytes: walk order is sorted, no
clock, no environment. Tokens are ctxpack's estimate (ceil(utf-8 bytes / 4)).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ctxpack import tokens

try:  # Python 3.11+; on 3.10 a pyproject is read by its text only
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

CAP = 2000
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".ga", ".venv", "venv", "env", "__pycache__", "dist", "build",
             ".next", ".nuxt", "target", ".tox", ".mypy_cache", ".pytest_cache", ".idea", ".vscode", "coverage",
             ".turbo", ".cache", "vendor", ".gradle", "out"}
LANGS = {".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
         ".mjs": "javascript", ".cjs": "javascript", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
         ".swift": "swift", ".rb": "ruby", ".php": "php", ".cs": "csharp", ".c": "c", ".h": "c", ".cpp": "cpp",
         ".cc": "cpp", ".hpp": "cpp", ".m": "objc", ".scala": "scala", ".sh": "shell", ".vue": "vue",
         ".svelte": "svelte", ".dart": "dart", ".lua": "lua", ".sql": "sql", ".css": "css", ".scss": "css",
         ".html": "html"}
CONFIGS = ("ga-do.json", "ga-supervise.json", "ga.json", "ga-gemini.json")
CONFIG_KEYS = ("schema", "backend", "model", "mode")  # the only keys a summary copies out of a ga config
MAX_FILES = 20000   # a walk stops counting here (the count is then a floor, said so)
README_LINES = 30
SCRIPT_KEYS = ("test", "build", "lint", "typecheck", "check", "e2e", "dev", "start")
_SAFE = re.compile(r"^[\w.:@/+-]{1,80}$")


@dataclass
class Summary:
    languages: dict[str, int]
    test_cmds: list[str]
    build_cmds: list[str]
    other_cmds: list[str]
    tree: list[str]
    readme: list[str]
    config: dict[str, Any]
    files: int
    capped: bool
    text: str = ""
    tokens: int = 0
    dropped: list[dict[str, Any]] = field(default_factory=list)

    def repo(self) -> dict[str, Any]:
        """The task/1 ``repo`` object."""
        return {"languages": dict(self.languages), "test_cmds": list(self.test_cmds),
                "build_cmds": list(self.build_cmds)}


def _walk(root: Path) -> tuple[dict[str, int], dict[str, int], int, bool]:
    """(files per language, files per top-level dir, files seen, stopped at MAX_FILES)."""
    langs: dict[str, int] = {}
    tops: dict[str, int] = {}
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        rel = Path(dirpath).relative_to(root)
        top = rel.parts[0] if rel.parts else None
        for f in sorted(filenames):
            n += 1
            if top:
                tops[top] = tops.get(top, 0) + 1
            lang = LANGS.get(Path(f).suffix.lower())
            if lang:
                langs[lang] = langs.get(lang, 0) + 1
            if n >= MAX_FILES:
                return langs, tops, n, True
    return langs, tops, n, False


def _read(p: Path, limit: int = 200_000) -> str | None:
    try:
        if p.is_file() and p.stat().st_size <= limit:
            return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return None


def commands(root: Path) -> tuple[list[str], list[str], list[str]]:
    """(test, build, other) command lines detected from the build files; names only, never a script body."""
    test: list[str] = []
    build: list[str] = []
    other: list[str] = []

    def add(kind: str, cmd: str) -> None:
        dst = {"test": test, "build": build}.get(kind, other)
        if cmd not in dst:
            dst.append(cmd)

    pkg = _read(root / "package.json")
    if pkg:
        try:
            scripts = (json.loads(pkg) or {}).get("scripts") or {}
        except (ValueError, AttributeError):
            scripts = {}
        runner = "pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() else "npm"
        if isinstance(scripts, dict):
            for name in sorted(scripts, key=lambda k: (SCRIPT_KEYS.index(k.split(":")[0])
                                                       if k.split(":")[0] in SCRIPT_KEYS else 99, k)):
                if not (isinstance(name, str) and _SAFE.match(name)):
                    continue
                base = name.split(":")[0]
                if base in ("test", "e2e") or base.startswith("test"):
                    add("test", f"{runner} test" if name == "test" else f"{runner} run {name}")
                elif base == "build":
                    add("build", f"{runner} run {name}")
                elif base in SCRIPT_KEYS:
                    add("other", f"{runner} run {name}")
    py = _read(root / "pyproject.toml")
    if py is not None:
        try:
            data = tomllib.loads(py) if tomllib else {"build-system": "[build-system]" in py or None}
        except ValueError:  # tomllib.TOMLDecodeError
            data = {}
        tool = data.get("tool") or {}
        if "pytest" in tool or "pytest" in py:
            add("test", "python -m pytest")
        elif (root / "tests").is_dir():
            add("test", "python -m unittest discover -s tests")
        if data.get("build-system"):
            add("build", "python -m build")
    elif (root / "setup.py").exists() and (root / "tests").is_dir():
        add("test", "python -m pytest")
    mk = _read(root / "Makefile")
    if mk:
        for t in re.findall(r"^([A-Za-z][\w-]*):(?!=)", mk, re.MULTILINE):
            if t in ("test", "check", "e2e") or t.startswith("test"):
                add("test", f"make {t}")
            elif t in ("build", "all"):
                add("build", f"make {t}")
            elif t in ("lint", "fmt", "typecheck"):
                add("other", f"make {t}")
    if (root / "Cargo.toml").exists():
        add("test", "cargo test")
        add("build", "cargo build")
    if (root / "go.mod").exists():
        add("test", "go test ./...")
        add("build", "go build ./...")
    return test, build, other


def ga_config(root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in CONFIGS:
        txt = _read(root / name, 100_000)
        if txt is None:
            continue
        try:
            data = json.loads(txt)
        except ValueError:
            out[name] = "unreadable"
            continue
        if isinstance(data, dict):
            out[name] = {k: data[k] for k in CONFIG_KEYS if isinstance(data.get(k), str) and _SAFE.match(data[k])}
    return out


def _tree(root: Path, tops: dict[str, int]) -> list[str]:
    lines = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for p in entries:
        if p.name.startswith(".") or p.name in SKIP_DIRS:
            continue
        if p.is_dir():
            lines.append(f"{p.name}/ ({tops.get(p.name, 0)} files)")
        else:
            lines.append(p.name)
    for top in sorted(tops, key=lambda k: (-tops[k], k))[:3]:  # one level more under the three biggest dirs
        try:
            kids = sorted(c.name + ("/" if c.is_dir() else "") for c in (root / top).iterdir()
                          if not c.name.startswith(".") and c.name not in SKIP_DIRS)
        except OSError:
            continue
        lines.append(f"{top}/: " + " ".join(kids[:25]) + (f" (+{len(kids) - 25})" if len(kids) > 25 else ""))
    return lines


def _readme(root: Path) -> list[str]:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        txt = _read(root / name, 2_000_000)
        if txt is not None:
            return [ln[:200] for ln in txt.splitlines()[:README_LINES]]
    return []


def render(units: list[tuple[str, list[str]]]) -> str:
    return "\n".join(f"[{name}]\n" + "\n".join(lines) for name, lines in units if lines) + "\n"


def summarize(root: str | Path, cap: int = CAP, state: list[str] | None = None) -> Summary:
    """``state``: the state store's summary lines (rev 2 S7), packed in the same cap, after the commands."""
    root = Path(root).resolve()
    langs, tops, n, capped = _walk(root)
    langs = dict(sorted(langs.items(), key=lambda kv: (-kv[1], kv[0])))
    test, build, other = commands(root)
    from .fragment import withhold
    readme = withhold("\n".join(_readme(root)))[0].split("\n") if _readme(root) else []
    s = Summary(langs, test, build, other, _tree(root, tops), readme, ga_config(root), n, capped)
    lang_line = ", ".join(f"{k} {v}" for k, v in langs.items()) or "none detected"
    units: list[tuple[str, list[str]]] = [
        ("languages", [f"{lang_line}; files {n}{'+' if capped else ''}"]),
        ("commands", [f"test: {' | '.join(test) or 'none detected'}", f"build: {' | '.join(build) or 'none detected'}"]
         + ([f"other: {' | '.join(other)}"] if other else [])),
        ("state", list(state or [])),
        ("ga config", [json.dumps(s.config, ensure_ascii=False, sort_keys=True)] if s.config else []),
        ("tree", list(s.tree)),
        ("readme head", list(s.readme)),
    ]
    text = render(units)
    # over the cap: cut from the lowest unit up, a line at a time, then shorten the unit's last line
    for i in range(len(units) - 1, -1, -1):
        name, lines = units[i]
        while tokens(text) > cap and lines:
            gone = lines.pop()
            s.dropped.append({"part": name, "tokens": tokens(gone + "\n")})
            text = render(units)
        if tokens(text) <= cap:
            break
    while tokens(text) > cap and len(units[0][1]) == 1 and units[0][1][0]:  # one huge first line: cut it by bytes
        name, lines = units[0]
        lines[0] = lines[0].encode("utf-8")[: max(0, len(lines[0].encode()) - 4 * (tokens(text) - cap) - 8)] \
            .decode("utf-8", "ignore")
        text = render(units)
    s.text, s.tokens = text, tokens(text)
    return s


__all__ = ["CAP", "Summary", "summarize", "commands", "ga_config"]
