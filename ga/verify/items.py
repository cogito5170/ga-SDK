"""CMD-GA39 S3: lint a code item before it is sent, for the gaps that made baseline send work back.

    ga verify item <item.json> [--repo DIR]

The item is baseline's format (ops/agy_bridge/act_runner.py):
    {item: {id, goal, files, done_when}, tests: {path: text}, commands: {commands: {name: argv}, timeout_s}, base}

errors (exit 1):
  done-when    done_when is not one of commands
  typecheck    TypeScript files in item.files, and neither the done_when command nor any test type-checks or builds
               (the AGA4 rev 1 gap: vitest passed, tsc did not)
  no-tests     a code item with no acceptance tests
  not-imported a code file in item.files that no acceptance test imports
warnings (exit 0):
  shapes       the goal names a type from the repo's schema without any of its fields, and item.files use it
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from .mutate import _imports

TS = (".ts", ".tsx", ".mts", ".cts")
CODE = (".py", ".js", ".jsx", ".mjs", ".cjs") + TS
TYPECHECK_ARG = re.compile(r"^(?:tsc|vue-tsc|typecheck|type-check|check-types|build)$|(?:^|[/:])(?:tsc|typecheck)$")
TYPECHECK_TEXT = re.compile(r"""['"`](?:npx\s+)?(?:tsc|vue-tsc)\b|['"`]--noEmit['"`]|\btsc\s+--noEmit|"""
                            r"""(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:build|typecheck|type-check)\b|['"`]typecheck['"`]""")
JS_SPEC = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)['"]([^'"]+)['"]""")
SCHEMA_PATH = re.compile(r"(^|/)(schemas?|types?|models?|api)(/|\.|$)", re.I)
TS_TYPE = re.compile(r"(?:interface|type)\s+([A-Z]\w*)\s*(?:<[^>{]*>)?\s*(?:extends[^{]*)?=?\s*\{([^{}]*)\}", re.S)
TS_FIELD = re.compile(r"^\s*(?:readonly\s+)?([A-Za-z_]\w*)\??\s*:", re.M)
PY_CLASS = re.compile(r"^class\s+([A-Z]\w*)\((?:[\w.]*TypedDict|[\w.]*BaseModel|[\w.]*NamedTuple)\):\n((?:[ \t]+.*\n?)+)",
                      re.M)
PY_DATACLASS = re.compile(r"^@(?:dataclasses\.)?dataclass[^\n]*\nclass\s+([A-Z]\w*)[^\n]*:\n((?:[ \t]+.*\n?)+)", re.M)
PY_FIELD = re.compile(r"^\s+([a-z_]\w*)\s*:", re.M)


def _argvs(item: dict[str, Any], commands: dict[str, Any]) -> list[list[str]]:
    dw = item.get("done_when")
    names = dw if isinstance(dw, list) else [dw]
    return [list(commands[n]) for n in names if isinstance(n, str) and isinstance(commands.get(n), list)]


def _imported_by(path: str, tests: dict[str, str]) -> bool:
    p = PurePosixPath(path)
    if p.suffix == ".py":
        dotted = str(p.with_suffix("")).replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        tails = {".".join(dotted.split(".")[i:]) for i in range(len(dotted.split(".")))}
        return any(tails & _imports(t) or dotted in t for t in tests.values())
    stem = p.name[: -len(p.suffix)] if p.suffix else p.name
    for tpath, text in tests.items():
        for spec in JS_SPEC.findall(text):
            last = PurePosixPath(spec).name
            last = re.sub(r"\.(?:[cm]?[jt]sx?)$", "", last)
            if last == stem or (stem == "index" and PurePosixPath(spec).name == p.parent.name):
                return True
    return False


def schema_types(repo: Path) -> dict[str, set[str]]:
    """{type name: field names} from the repo's schema-like files (schema/types/models/api paths), TS and Python"""
    out: dict[str, set[str]] = {}
    if not repo.is_dir():
        return out
    for f in repo.rglob("*"):
        rel = f.relative_to(repo).as_posix()
        if "node_modules" in rel or rel.startswith(".") or f.suffix not in (".ts", ".tsx", ".py") \
                or not SCHEMA_PATH.search(rel) or not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if f.suffix == ".py":
            for rx in (PY_CLASS, PY_DATACLASS):
                for name, body in rx.findall(text):
                    out.setdefault(name, set()).update(PY_FIELD.findall(body))
        else:
            for name, body in TS_TYPE.findall(text):
                out.setdefault(name, set()).update(TS_FIELD.findall(body))
    return {k: v for k, v in out.items() if v}


def lint(doc: dict[str, Any], repo: Path | None = None) -> list[tuple[str, str, str]]:
    """[(level, code, message)], level "error" or "warning"."""
    item = doc.get("item") if isinstance(doc.get("item"), dict) else {}
    files = [f for f in item.get("files") or [] if isinstance(f, str)]
    tests = {k: v for k, v in (doc.get("tests") or {}).items() if isinstance(v, str)}
    cmds = (doc.get("commands") or {}).get("commands") or {}
    found: list[tuple[str, str, str]] = []

    dw = item.get("done_when")
    for n in (dw if isinstance(dw, list) else [dw]):
        if not isinstance(n, str) or n not in cmds:
            found.append(("error", "done-when", f"done_when {n!r} is not one of commands ({', '.join(sorted(cmds)) or 'none'})"))

    ts = [f for f in files if f.endswith(TS) and not f.endswith(".d.ts")]
    if ts:
        by_cmd = any(TYPECHECK_ARG.search(a) for argv in _argvs(item, cmds) for a in argv)
        by_test = any(TYPECHECK_TEXT.search(t) for t in tests.values())
        if not (by_cmd or by_test):
            found.append(("error", "typecheck", f"TypeScript files ({', '.join(ts[:3])}{'…' if len(ts) > 3 else ''}) but "
                          "neither the done_when command nor any test runs a type-check or build (add a tsc --noEmit "
                          "case or a typecheck/build command)"))

    code = [f for f in files if f.endswith(CODE) and f not in tests and not f.endswith(".d.ts")]
    if code and not tests:
        found.append(("error", "no-tests", "a code item with no acceptance tests"))
    elif code:
        for f in code:
            if not _imported_by(f, tests):
                found.append(("error", "not-imported", f"no acceptance test imports {f}"))

    goal = str(item.get("goal") or "")
    if repo is not None and goal:
        types = schema_types(repo)
        texts = list(tests.values())
        for f in files:
            try:
                texts.append((repo / f).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                pass
        for name, fields in sorted(types.items()):
            if not re.search(rf"\b{re.escape(name)}\b", goal):
                continue
            if not any(re.search(rf"\b{re.escape(name)}\b", t) for t in texts):
                continue
            if not any(re.search(rf"\b{re.escape(x)}\b", goal) for x in fields):
                found.append(("warning", "shapes", f"the goal names {name} but none of its fields "
                              f"({', '.join(sorted(fields)[:6])}); state the shapes the code must produce or read"))
    return found


def main(args) -> int:
    try:
        doc = json.loads(Path(args.item).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"ga verify item: {args.item}: {e}", file=sys.stderr)
        return 2
    if not isinstance(doc, dict) or not isinstance(doc.get("item"), dict):
        print(f"ga verify item: {args.item}: not an item ({{item: {{id, goal, files, done_when}}, tests, commands}})",
              file=sys.stderr)
        return 2
    found = lint(doc, Path(args.repo) if args.repo else None)
    for level, code, msg in found:
        print(f"{level} {code}: {msg}")
    errors = sum(1 for f in found if f[0] == "error")
    print(f"{doc['item'].get('id', '?')}: {errors} error(s), {len(found) - errors} warning(s)")
    return 1 if errors else 0
