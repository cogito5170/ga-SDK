"""Named commands (CMD-GA38 S3): the target repository's ``.ga-act.json`` names them; a model picks a name only.

    {"commands": {"test": ["python3", "-m", "unittest", "-v"], "lint": ["ruff", "check", "."]},
     "done_when": "test",            # a command name or an argv list (an item's own done_when wins)
     "timeout_s": 300}

Each argv is fixed: run without a shell, in the project directory, stdin closed, with a timeout, and with every
environment variable whose name looks like a secret removed. The output's tail is summarized for the card.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import judge as J

CONFIG = ".ga-act.json"
NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SECRET_ENV = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE|CERT)")


class ActConfigError(ValueError):
    pass


def clean_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The command's environment: the caller's without secret-named variables (and Claude Code's own)."""
    env = dict(os.environ if base is None else base)
    return {k: v for k, v in env.items() if not SECRET_ENV.search(k) and not k.startswith("CLAUDE_CODE_")}


def _argv(v: Any, where: str) -> list[str]:
    if not (isinstance(v, list) and v and all(isinstance(x, str) and x for x in v)):
        raise ActConfigError(f"{where} must be a non-empty list of strings (an argv, no shell)")
    return list(v)


# CMD-GA45 S3: the models a ladder may name (the agy list of 2026-10-06); ``models`` in .ga-act.json replaces it
MODELS = ("gpt-oss-120b-medium",
          *(f"gemini-3.{v}-flash-{e}" for v in (6, 7, 8) for e in ("low", "medium", "high")),
          "gemini-3.1-pro-low", "gemini-3.1-pro-high",
          *(f"claude-{f}-5-5-{e}" for f in ("sonnet", "opus") for e in ("low", "medium", "high")))
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")


def load(root: Path, path: str | None = None) -> dict[str, Any]:
    """{commands: {name: argv}, done_when: argv | None, timeout_s} from ``.ga-act.json`` (missing file = no commands)."""
    f = Path(path) if path else Path(root) / CONFIG
    raw: Any = {}
    if f.is_file():
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ActConfigError(f"{f.name}: not JSON ({e})") from None
    if not isinstance(raw, dict) or set(raw) - {"commands", "done_when", "timeout_s", "models"}:
        raise ActConfigError(f"{CONFIG} is {{commands, done_when?, timeout_s?, models?}}")
    cmds = raw.get("commands", {})
    if not isinstance(cmds, dict):
        raise ActConfigError("commands must map names to argv lists")
    out = {}
    for n, v in cmds.items():
        if not NAME.match(str(n)):
            raise ActConfigError(f"command name {n!r}: [a-z][a-z0-9_-], up to 32")
        out[n] = _argv(v, f"commands.{n}")
    t = raw.get("timeout_s", 300)
    if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
        raise ActConfigError("timeout_s must be a positive number")
    models = raw.get("models", list(MODELS))
    if not (isinstance(models, list) and all(isinstance(m, str) and MODEL.match(m) for m in models)):
        raise ActConfigError("models must be a list of model names ([A-Za-z0-9._:-], up to 80)")
    return {"commands": out, "done_when": resolve_done(raw.get("done_when"), out), "timeout_s": float(t),
            "models": list(models)}


def resolve_done(v: Any, commands: dict[str, list[str]]) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, str):
        if v not in commands:
            raise ActConfigError(f"done_when names {v!r}, which is not a command")
        return list(commands[v])
    return _argv(v, "done_when")


@dataclass
class Ran:
    name: str
    code: int | None                 # None: timed out or could not start
    out: str
    failing: list[str] = field(default_factory=list)
    counts: dict[str, int] | None = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0


def run(name: str, argv: list[str], cwd: Path, timeout_s: float) -> Ran:
    argv = [sys.executable if x == "{python}" else x for x in argv]
    try:
        env = clean_env()
        env["PYTHONDONTWRITEBYTECODE"] = "1"  # an edit of the same size in the same second must not hit a stale .pyc
        p = subprocess.run(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=timeout_s, shell=False)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        return Ran(name, None, out, note=f"timeout after {timeout_s:g}s")
    except OSError as e:
        return Ran(name, None, "", note=f"could not start: {type(e).__name__}")
    out = (p.stdout or "") + (p.stderr or "")
    return Ran(name, p.returncode, out, J.failing_tests(out), J.parse_counts(out))


_UT_HEAD = re.compile(r"^(?:FAIL|ERROR): .*$", re.M)
_PT_HEAD = re.compile(r"^_{3,} (.+?) _{3,}$", re.M)
_FRAME = re.compile(r'^\s*File "(?P<f>[^"]+)", line (?P<n>\d+), in (?P<fn>\S+)')
_JSFRAME = re.compile(r"^\s*at (?:(?P<fn>[\w$.<>]+) )?\(?(?P<f>[^():]+\.[cm]?[jt]sx?):(?P<n>\d+):\d+\)?")


def failure_blocks(out: str) -> list[str]:
    """unittest ``FAIL:``/``ERROR:`` sections or pytest ``____ name ____`` sections, in order."""
    for rx in (_UT_HEAD, _PT_HEAD):
        ms = list(rx.finditer(out))
        if ms:
            ends = [m.start() for m in ms[1:]] + [len(out)]
            return [out[m.start():e] for m, e in zip(ms, ends)]
    return []


def key_lines(block: str, root: Path, n: int) -> list[str]:
    """At most ``n`` lines: the frames inside the repository and the error line(s) at the end."""
    lines = [ln.rstrip() for ln in block.splitlines() if ln.strip() and not set(ln.strip()) <= set("=-_")]
    keep = []
    for k, ln in enumerate(lines):
        m = _FRAME.match(ln) or _JSFRAME.match(ln)
        if m and in_repo(m["f"], root):
            keep.append(ln.strip())
            if _FRAME.match(ln) and k + 1 < len(lines) and not _FRAME.match(lines[k + 1]):
                keep.append("    " + lines[k + 1].strip())
    tail = [ln.strip() for ln in lines[-3:] if not _FRAME.match(ln)]
    out = keep[-(max(1, n - len(tail))):] + [t for t in tail if t not in keep]
    return [x[:200] for x in out[:n]]


def in_repo(f: str, root: Path) -> bool:
    p = Path(f)
    try:
        rp = (p if p.is_absolute() else root / p).resolve()
    except OSError:
        return False
    rr = root.resolve()
    return rr in rp.parents and "site-packages" not in rp.parts and ".git" not in rp.parts


def frames(out: str, root: Path) -> list[tuple[str, int, str]]:
    """(repo-relative file, line, function) of every stack frame inside the repository, in order, each once."""
    seen, res = set(), []
    for ln in out.splitlines():
        m = _FRAME.match(ln) or _JSFRAME.match(ln)
        if not m or not in_repo(m["f"], root):
            continue
        p = Path(m["f"])
        rel = str(((p if p.is_absolute() else root / p).resolve()).relative_to(root.resolve()))
        key = (rel, int(m["n"]), m["fn"] or "")
        if key not in seen:
            seen.add(key)
            res.append(key)
    return res


def summarize(r: Ran, root: Path, *, max_tests: int = 8, stack_lines: int = 6, tail_lines: int = 12) -> str:
    """The card's text for one command run: exit, counts, failing tests with their key lines, else the output tail."""
    head = f"$ {r.name}: " + ("ok" if r.ok else (r.note or f"exit {r.code}"))
    if r.counts:
        head += " ({passed} passed, {failed} failed, {skipped} skipped)".format(**r.counts)
    lines = [head]
    blocks = failure_blocks(r.out)
    if r.failing or blocks:
        for t in r.failing[:max_tests]:
            lines.append(f"- {t}")
            blk = next((b for b in blocks if t.split(".")[-1] in b.splitlines()[0]), None)
            for k in key_lines(blk, root, stack_lines) if blk else []:
                lines.append(f"    {k}")
        if len(r.failing) > max_tests:
            lines.append(f"- ... {len(r.failing) - max_tests} more")
    elif not r.ok:
        tail = [ln.rstrip()[:200] for ln in r.out.strip().splitlines()[-tail_lines:]]
        lines += [f"    {t}" for t in tail]
    return "\n".join(lines)
