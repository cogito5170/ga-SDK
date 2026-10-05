"""Code-side retrievers (CMD-GA38 S2/S3): they serve NEED and the code slices of the state card, so a model never
spends a turn searching. Python symbols come from ``ast``; TS/JS symbols from a regex index with brace matching.
Whole files are shown only when small. Nothing outside the root, under .git/.ga or node_modules is read.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from .apply import readable as safe_rel, secret_path

SKIP = {".git", ".ga", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
PY = (".py",)
JS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
SMALL_FILE_LINES = 80


def files(root: Path, exts: tuple[str, ...] | None = PY + JS, limit: int = 2000) -> list[str]:
    """Repo-relative files (``exts`` None = any), sorted, skipping SKIP directories without walking into them."""
    import os
    out = []
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP)
        for n in sorted(names):
            if (exts is None or Path(n).suffix in exts) and not secret_path(n):
                out.append(str((Path(d) / n).relative_to(root)))
                if len(out) >= limit:
                    return sorted(out)
    return sorted(out)


def numbered(lines: list[str], a: int, b: int, width: int = 200) -> str:
    """Lines a..b (1-based, inclusive) with their numbers."""
    a, b = max(1, a), min(len(lines), b)
    return "\n".join(f"{k:>5}| {lines[k - 1][:width]}" for k in range(a, b + 1))


# ------------------------------------------------------------------ python
def _py_defs(tree: ast.AST) -> list[tuple[str, int, int]]:
    """(qualified name, first line, last line) of every def/class, outer first."""
    out: list[tuple[str, int, int]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}{ch.name}"
                first = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                out.append((q, first, ch.end_lineno or ch.lineno))
                walk(ch, q + ".")

    walk(tree, "")
    return out


def _parse(p: Path) -> tuple[list[str], ast.AST | None]:
    text = p.read_text(encoding="utf-8", errors="replace")
    try:
        return text.splitlines(), ast.parse(text)
    except SyntaxError:
        return text.splitlines(), None


# ------------------------------------------------------------------ ts / js
_JS_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?\s+(?P<f>[\w$]+)"
    r"|class\s+(?P<c>[\w$]+)"
    r"|(?:const|let|var)\s+(?P<v>[\w$]+)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[\w$]+)\s*(?::[^=]+)?=>"
    r"|(?:(?:public|private|protected|static|readonly|async)\s+)*(?P<m>[\w$]+)\s*\([^)]*\)\s*(?::[^{]+)?\{)")
_JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "else"}


def _js_end(lines: list[str], start: int) -> int:
    """The line (1-based) where the brace block opened on/after ``start`` closes (strings and comments ignored)."""
    depth, opened = 0, False
    for k in range(start - 1, len(lines)):
        s = re.sub(r"//.*$|'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"|`(?:\\.|[^`])*`", "", lines[k])
        for ch in s:
            if ch == "{":
                depth, opened = depth + 1, True
            elif ch == "}":
                depth -= 1
                if opened and depth == 0:
                    return k + 1
        if not opened and k > start - 1 and s.strip().endswith(";"):
            return k + 1
    return len(lines)


def _js_defs(lines: list[str]) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    cls: list[tuple[str, int]] = []
    for k, ln in enumerate(lines, 1):
        while cls and k > cls[-1][1]:
            cls.pop()
        m = _JS_DEF.match(ln)
        if not m:
            continue
        name = m["f"] or m["c"] or m["v"] or m["m"]
        if not name or name in _JS_KEYWORDS or (m["m"] and not cls):
            continue
        end = _js_end(lines, k)
        q = f"{cls[-1][0]}.{name}" if cls and m["m"] else name
        out.append((q, k, end))
        if m["c"]:
            cls.append((name, end))
    return out


# ------------------------------------------------------------------ index
def defs(root: Path, rel: str) -> list[tuple[str, int, int]]:
    p = safe_rel(root, rel)
    if p is None or not p.is_file():
        return []
    if p.suffix in PY:
        lines, tree = _parse(p)
        return _py_defs(tree) if tree else []
    if p.suffix in JS:
        return _js_defs(p.read_text(encoding="utf-8", errors="replace").splitlines())
    return []


def _module_file(root: Path, parts: list[str]) -> tuple[str, list[str]] | None:
    """The longest dotted prefix that is a python module or a TS/JS file -> (rel path, the rest of the name)."""
    for k in range(len(parts), 0, -1):
        base = "/".join(parts[:k])
        for cand in [base + e for e in PY + JS] + [base + "/__init__.py", base + "/index.ts", base + "/index.js"]:
            p = safe_rel(root, cand)
            if p is not None and p.is_file():
                return cand, parts[k:]
    return None


def symbol(root: Path, name: str, max_lines: int = 120) -> str | None:
    """The code of ``name`` (pkg.mod.func, mod.Class.method, a file path stem plus name, or a bare name found by the
    index), numbered, at most ``max_lines`` lines; None when not found."""
    name = name.strip().strip("`")
    parts = [x for x in re.split(r"[./:]", name) if x]
    if not parts:
        return None
    hit = _module_file(root, parts)
    cands: list[tuple[str, str]] = []
    if hit and hit[1]:
        cands.append((hit[0], ".".join(hit[1])))
    elif hit:
        p = safe_rel(root, hit[0])
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return f"{hit[0]} (whole module)\n" + numbered(lines, 1, min(len(lines), max_lines))
    for k in range(len(parts)):
        q = ".".join(parts[k:])
        cands += [(f, q) for f in files(root)]
        if len(parts) - k <= 2:
            break
    for f, q in cands:
        for dq, a, b in defs(root, f):
            if dq == q or dq.endswith("." + q):
                lines = (safe_rel(root, f)).read_text(encoding="utf-8", errors="replace").splitlines()
                more = f" (first {max_lines} of {b - a + 1} lines)" if b - a + 1 > max_lines else ""
                return f"{f}:{a}-{b} {dq}{more}\n" + numbered(lines, a, min(b, a + max_lines - 1))
    return None


def enclosing(root: Path, rel: str, line: int, max_lines: int = 60) -> str | None:
    """The innermost function or class of ``rel`` that holds ``line``; the whole file when it is small; else
    ``max_lines`` lines around the line."""
    p = safe_rel(root, rel)
    if p is None or not p.is_file():
        return None
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    inner = [(q, a, b) for q, a, b in defs(root, rel) if a <= line <= b]
    if inner:
        q, a, b = min(inner, key=lambda d: d[2] - d[1])
        if b - a + 1 <= max_lines:
            return f"{rel}:{a}-{b} {q}\n" + numbered(lines, a, b)
    if len(lines) <= SMALL_FILE_LINES:
        return f"{rel} (whole file)\n" + numbered(lines, 1, len(lines))
    a = max(1, line - max_lines // 2)
    return f"{rel}:{a}-{a + max_lines - 1}\n" + numbered(lines, a, a + max_lines - 1)


def file_slice(root: Path, rel: str, lines_ab: tuple[int, int] | None = None, max_lines: int = 150) -> str | None:
    p = safe_rel(root, rel)
    if p is None or not p.is_file():
        return None
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    a, b = lines_ab or (1, len(lines))
    if b < a:
        a, b = b, a
    cut = b - a + 1 > max_lines
    b = min(b, a + max_lines - 1)
    return f"{rel}:{a}-{min(b, len(lines))}" + (" (cut)" if cut else "") + "\n" + numbered(lines, a, b)


def grep(root: Path, text: str, max_hits: int = 30) -> str:
    out = []
    for f in files(root, PY + JS + (".json", ".md", ".toml", ".cfg", ".txt", ".yml", ".yaml")):
        p = root / f
        for k, ln in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if text in ln:
                out.append(f"{f}:{k}: {ln.strip()[:160]}")
                if len(out) >= max_hits:
                    return "\n".join(out + ["(more hits cut)"])
    return "\n".join(out) or "(no hits)"
