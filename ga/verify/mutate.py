"""CMD-GA39 S1: a mutation spec for ``ga judge`` from the code operators on the changed or added lines of a diff.

    ga verify mutations --base <ref> --head <ref> [--repo DIR] [--max N]

Operators (Python files only; each applies to a node that starts on a line the diff adds or changes):
  guard      an ``if`` whose body raises or returns: its test -> False (the guard dropped); any other ``if``: negated
  term       one operand of an and/or in an ``if``/``while``/``assert``/``return`` condition dropped
  limit      ``>= n``/``> n`` -> 1; ``<= n``/``< n`` -> n * 1000; a MAX/LIMIT/CAP/MIN constant widened the same way
  permissive ``return False`` -> ``return True``; ``return A if c else B`` with A (or B) a refusal -> the other branch
  flag       shell=False, check=True, allow_*/verify/strict/force keywords flipped; "--force-with-lease" -> "--force"
  raise      a ``raise`` line -> ``pass``
  check      compare_digest(..) / .is_relative_to(..) -> True; ``x.resolve()`` -> ``x``

Each mutation is ``{id, file, find, replace, tests, op, score}``: ``find`` is whole lines of the head's file, widened
with context until it occurs once; ``tests`` are the test modules that import or name the mutated module (static
scan, file paths). Ranked by a guard score: operator weight, plus a function whose name or docstring says
check/verify/guard/allow/approve/ok/secret/token/host/origin, plus guard words on the line itself.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any

GUARD_WORDS = re.compile(r"check|verif|guard|allow|approv|(?:^|_)ok(?:$|_)|secret|token|host|origin|auth|permit|"
                         r"safe|valid|forbid|refus|deny", re.I)
LINE_WORDS = re.compile(r"token|secret|digest|origin|host|network|needs|allow|approv|shell|parents|resolve|"
                        r"relative|withheld|forbid|refus|deny|accept|limit|max|min|cap", re.I)
OP_WEIGHT = {"guard": 3, "term": 3, "flag": 4, "check": 4, "permissive": 3, "raise": 2, "limit": 2, "negate": 0}
FLAG_TRUE_IS_SAFE = {"check", "verify", "strict", "secure", "follow_symlinks_safe"}  # True -> False weakens
FLAG_FALSE_IS_SAFE = {"shell", "force", "follow_symlinks", "insecure", "allow_query", "allow_network"}
REFUSALS = {"False", "None", "WITHHELD", "MASK", "REDACTED", "DENIED"}
REFUSAL_BODY = re.compile(r"refus|forbid|denied|not allow|tamper|ASK_HUMAN|SEND_BACK|reject|\b40[13]\b", re.I)
LIMIT_NAME = re.compile(r"(MAX|LIMIT|CAP|MIN)", re.I)


class MutateError(Exception):
    pass


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if p.returncode:
        raise MutateError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()[:200]}")
    return p.stdout


def changed_lines(repo: Path, base: str, head: str) -> dict[str, set[int]]:
    """{file: head line numbers added or changed} for the .py files of ``base...head`` (from their merge base)."""
    out: dict[str, set[int]] = {}
    cur = None
    for line in _git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", "--diff-filter=AM", f"{base}...{head}",
                     "--", "*.py").splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@") and cur:
            m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start, n = int(m.group(1)), int(m.group(2) or "1")
                out.setdefault(cur, set()).update(range(start, start + n))
    return {f: s for f, s in out.items() if s}


# ---- operators: each yields (op, node line, (start, end, new) byte edit, the text the edit weakens)

class _Src:
    def __init__(self, text: str):
        self.b = text.encode("utf-8")
        self.starts = [0]
        for i, c in enumerate(self.b):
            if c == 10:
                self.starts.append(i + 1)

    def off(self, line: int, col: int) -> int:
        return self.starts[line - 1] + col

    def span(self, node: ast.AST) -> tuple[int, int]:
        return self.off(node.lineno, node.col_offset), self.off(node.end_lineno, node.end_col_offset)

    def seg(self, node: ast.AST) -> str:
        s, e = self.span(node)
        return self.b[s:e].decode("utf-8")


def _num(node: ast.AST) -> int | float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    return None


def _guardy_body(body: list[ast.stmt]) -> bool:
    return bool(body) and isinstance(body[0], (ast.Raise, ast.Return, ast.Continue, ast.Break))


def _bool_terms(src: _Src, test: ast.AST):
    """one (edit, dropped operand, weakens) per operand of the condition's own and/or (through ``not`` and nesting, not into
    calls): that operand dropped. ``x or <constant>`` is a default, not a condition."""
    stack = [(test, True)]
    while stack:
        b, pos = stack.pop()
        if isinstance(b, ast.UnaryOp) and isinstance(b.op, ast.Not):
            stack.append((b.operand, not pos))
            continue
        if not isinstance(b, ast.BoolOp):
            continue
        if isinstance(b.op, ast.Or) and isinstance(b.values[-1], ast.Constant):
            continue
        stack += [(v, pos) for v in b.values]
        weakens = isinstance(b.op, ast.Or) == pos  # the condition fires less often without the operand
        sep = " and " if isinstance(b.op, ast.And) else " or "
        for i in range(len(b.values)):
            rest = [src.seg(v) if not isinstance(v, ast.BoolOp) else f"({src.seg(v)})"
                    for k, v in enumerate(b.values) if k != i]
            s, e = src.span(b)
            yield (s, e, sep.join(rest) if len(rest) > 1 else rest[0]), src.seg(b.values[i]), weakens


def operators(src: _Src, tree: ast.AST):
    """(op, line, edit, focus, bonus): an ``if`` whose body refuses (raises, returns early, or names a refusal) is a
    guard; dropping an operand that makes a guard fire less scores +2, one that makes it fire more -2."""
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            s, e = src.span(node.test)
            first = src.seg(node.body[0]) if node.body else ""
            guardish = _guardy_body(node.body) or bool(REFUSAL_BODY.search(first))
            bonus = 2 if REFUSAL_BODY.search(first) else 0
            if _guardy_body(node.body):
                yield "guard", node.lineno, (s, e, "False"), src.seg(node.test), bonus
            elif isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not):
                yield "negate", node.lineno, (s, e, src.seg(node.test.operand)), "", bonus
            else:
                yield "negate", node.lineno, (s, e, f"not ({src.seg(node.test)})"), "", bonus
            for ed, focus, weakens in _bool_terms(src, node.test):
                yield "term", node.lineno, ed, focus, bonus + ((2 if weakens else -2) if guardish else 0)
        elif isinstance(node, (ast.While, ast.Assert)):
            for ed, focus, weakens in _bool_terms(src, node.test):
                yield "term", node.lineno, ed, focus, (2 if weakens else -2) if isinstance(node, ast.Assert) else 0
        elif isinstance(node, ast.Return) and node.value is not None:
            v = node.value
            if isinstance(v, ast.Constant) and v.value is False:
                yield "permissive", node.lineno, (*src.span(v), "True"), src.seg(node), 0
            elif isinstance(v, ast.IfExp):
                a, b = src.seg(v.body), src.seg(v.orelse)
                if a in REFUSALS:
                    yield "permissive", node.lineno, (*src.span(v), b), src.seg(node), 0
                elif b in REFUSALS:
                    yield "permissive", node.lineno, (*src.span(v), a), src.seg(node), 0
            elif isinstance(v, ast.BoolOp):
                for ed, focus, _ in _bool_terms(src, v):
                    yield "term", node.lineno, ed, focus, 0
        elif isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators):
                n = _num(right)
                if n is None:
                    continue
                if isinstance(op, (ast.GtE, ast.Gt)) and n > 1:
                    yield "limit", node.lineno, (*src.span(right), "1"), src.seg(node), 0
                elif isinstance(op, (ast.LtE, ast.Lt)) and n > 0:
                    yield "limit", node.lineno, (*src.span(right), f"({src.seg(right)}) * 1000"), src.seg(node), 0
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name.isupper() and LIMIT_NAME.search(name) and _const_num(node.value):
                low = name.upper().startswith("MIN") or "_MIN" in name.upper()
                yield "limit", node.lineno, (*src.span(node.value), "1" if low else f"({src.seg(node.value)}) * 1000"), src.seg(node), 0
        elif isinstance(node, ast.keyword) and node.arg and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, bool):
            k, val = node.arg, node.value.value
            if (val is False and (k in FLAG_FALSE_IS_SAFE or k.startswith("allow"))) or \
                    (val is True and k in FLAG_TRUE_IS_SAFE):
                yield "flag", node.value.lineno, (*src.span(node.value), str(not val)), src.seg(node), 0
        elif isinstance(node, ast.Constant) and node.value == "--force-with-lease":
            yield "flag", node.lineno, (*src.span(node), '"--force"'), src.seg(node), 0
        elif isinstance(node, ast.Raise):
            yield "raise", node.lineno, (*src.span(node), "pass"), src.seg(node), 0
        elif isinstance(node, ast.Call):
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            if fname in ("compare_digest", "is_relative_to"):
                yield "check", node.lineno, (*src.span(node), "True"), src.seg(node), 0
            elif fname == "resolve" and isinstance(f, ast.Attribute) and not node.args:
                yield "check", node.lineno, (*src.span(node), src.seg(f.value)), src.seg(node), 0


def _const_num(v: ast.AST) -> bool:
    if _num(v) is not None:
        return True
    return isinstance(v, ast.BinOp) and _const_num(v.left) and _const_num(v.right)


def _functions(tree: ast.AST) -> list[tuple[int, int, int]]:
    """(start, end, guard score) for each function"""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(n) or ""
            score = 4 if GUARD_WORDS.search(n.name) else 0
            score += 2 if GUARD_WORDS.search(doc) else 0
            out.append((n.lineno, n.end_lineno or n.lineno, score))
    return out


def _unique_find(text: str, lines: list[str], a: int, b: int, new_mid: str) -> tuple[str, str] | None:
    """whole lines a..b (0-based, inclusive) widened with context until they occur once in ``text``"""
    lo, hi = a, b
    while True:
        find = "".join(lines[lo:hi + 1])
        if text.count(find) == 1:
            replace = "".join(lines[lo:a]) + new_mid + "".join(lines[b + 1:hi + 1])
            return find, replace
        if lo == 0 and hi == len(lines) - 1:
            return None
        if lo > 0:
            lo -= 1
        if hi < len(lines) - 1:
            hi += 1


def file_mutations(path: str, text: str, changed: set[int]) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    src = _Src(text)
    lines = text.splitlines(keepends=True)
    funcs = _functions(tree)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for op, lineno, (s, e, new), focus, bonus in operators(src, tree):
        if lineno not in changed:
            continue
        if src.b[s:e].decode("utf-8") == new:
            continue
        mutated = (src.b[:s] + new.encode("utf-8") + src.b[e:]).decode("utf-8")
        try:
            ast.parse(mutated)
        except SyntaxError:
            continue
        a = src.b.count(b"\n", 0, s)
        b = src.b.count(b"\n", 0, max(e - 1, s))
        line_start, line_end = src.starts[a], src.starts[b + 1] if b + 1 < len(src.starts) else len(src.b)
        mid = (src.b[line_start:s] + new.encode("utf-8") + src.b[e:line_end]).decode("utf-8")
        got = _unique_find(text, lines, a, min(b, len(lines) - 1), mid)
        if not got or got[0] == got[1] or got in seen:
            continue
        seen.add(got)
        fscore = max((sc for f0, f1, sc in funcs if f0 <= lineno <= f1), default=0)
        here = "".join(lines[a:b + 1])
        score = OP_WEIGHT[op] + bonus + fscore + (3 if LINE_WORDS.search(focus) else 1 if LINE_WORDS.search(here) else 0)
        out.append({"id": f"{op}:{path}:{lineno}", "file": path, "find": got[0], "replace": got[1], "op": op,
                    "line": lineno, "score": score})
    return out


# ---- which tests a mutation must break

_IMPORT_LINE = re.compile(r"^\s*(?:from\s+(\S+)\s+import\s+(.+)|import\s+(.+))$", re.M)


def _imports(text: str) -> set[str]:
    """the dotted names a Python file imports (``from a import b`` gives a, a.b)"""
    names: set[str] = set()
    for m in _IMPORT_LINE.finditer(text):
        if m.group(1):
            names.add(m.group(1))
            for part in re.split(r"[,()\s]+", m.group(2)):
                part = part.split(" as ")[0].strip()
                if part and part.isidentifier():
                    names.add(f"{m.group(1)}.{part}")
        else:
            for part in m.group(3).split(","):
                names.add(part.split(" as ")[0].strip())
    return names


def test_map(repo_files: dict[str, str]) -> dict[str, list[str]]:
    """{module path: [test files]}: a test file (tests/**/test_*.py) imports or names the module, directly or through
    one helper module in a tests directory that does."""
    test_dirs = [f for f in repo_files if re.search(r"(^|/)tests?/", f)]
    tests = sorted(f for f in test_dirs if Path(f).name.startswith("test_"))
    helpers = [f for f in test_dirs if f not in tests]
    imported = {f: _imports(repo_files[f]) for f in test_dirs}
    out: dict[str, list[str]] = {}

    def users(dotted: str, path: str) -> set[str]:
        hit = set()
        for f in test_dirs:
            if dotted in imported[f] or dotted in repo_files[f] or path in repo_files[f]:
                hit.add(f)
        return hit

    for f in repo_files:
        if f in test_dirs:
            continue
        dotted = f[:-3].replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        direct = users(dotted, f)
        via = set()
        for h in direct & set(helpers):
            stem = Path(h).stem
            via |= {t for t in tests if stem in imported[t]}
        out[f] = sorted((direct & set(tests)) | via)
    return out


def tests_for(repo: Path, head: str, files: list[str]) -> dict[str, list[str]]:
    all_py = [f for f in _git(repo, "ls-tree", "-r", "--name-only", head).splitlines() if f.endswith(".py")]
    texts = {}
    for f in all_py:
        if f in files or re.search(r"(^|/)tests?/", f):
            texts[f] = _git(repo, "show", f"{head}:{f}")
    m = test_map(texts)
    return {f: m.get(f, []) for f in files}


def mutations(repo: str | Path, base: str, head: str, max_n: int | None = None) -> list[dict[str, Any]]:
    """the ranked spec for ``base...head`` in ``repo`` (both refs local)"""
    repo = Path(repo)
    changed = changed_lines(repo, base, head)
    files = sorted(f for f in changed if not re.search(r"(^|/)tests?/", f))
    tmap = tests_for(repo, head, files) if files else {}
    out: list[dict[str, Any]] = []
    for f in files:
        text = _git(repo, "show", f"{head}:{f}")
        for mu in file_mutations(f, text, changed[f]):
            mu["tests"] = tmap.get(f, [])
            out.append(mu)
    out.sort(key=lambda m: (-m["score"], m["file"], m["line"], m["id"]))
    per_line: dict[tuple[str, int, str], int] = {}
    for mu in out:  # one operator's second and later mutations of a line rank lower: the top covers more guards
        key = (mu["file"], mu["line"], mu["op"])
        n = per_line.get(key, 0)
        per_line[key] = n + 1
        mu["score"] -= 3 * n
    out.sort(key=lambda m: (-m["score"], m["file"], m["line"], m["id"]))
    ids: dict[str, int] = {}
    for mu in out:  # ids stay unique when one line carries several mutations of one operator
        n = ids.get(mu["id"], 0)
        ids[mu["id"]] = n + 1
        if n:
            mu["id"] = f"{mu['id']}.{n}"
    return out[:max_n] if max_n else out


def main(args) -> int:
    try:
        spec = mutations(args.repo, args.base, args.head, args.max)
    except MutateError as e:
        print(f"ga verify mutations: {e}", file=__import__("sys").stderr)
        return 2
    text = json.dumps(spec, indent=1) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{len(spec)} mutations -> {args.out}")
    else:
        print(text, end="")
    return 0
