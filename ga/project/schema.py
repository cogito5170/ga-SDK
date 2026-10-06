"""project/1 (CMD-GA54 S1): one file that binds a project's name, repos, environment, instructions, routines, mailbox
and model ladder. Stored at ``<GA_HOME>/projects/<name>.json`` (mode 0600; GA_HOME is ``$GA_HOME`` or ``~/.ga``).

    {"schema": "project/1", "name": "ga",
     "repos": [{"name", "url", "branch", "path"}],
     "environment": {"kind": "local|vm", "python", "venv", "services": ["console", "bridge", "hub", "token-api", "token-web"]},
     "instructions": {"file": "~/baseline/CLAUDE.md"} | {"text": "... (8 KB at most)"},
     "routines": [{"name", "every": "<systemd OnCalendar> | 30min", "action": "<an approved GA Action id>"}],
     "mailbox": {"repo", "branch"},
     "models": {"ladder": "<ref>"}}

``validate`` refuses unknown keys, secret-shaped keys and values (rule R6's patterns, the same ``ga.mailbox.secrets_in``
the mail and the action trial use), URLs that carry credentials, and a routine whose action is not a registered GA
Action id (a shell string never is).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..act.commands import NAME as ACTION_ID
from ..actions import registry as AR
from ..bridge.tools import _SECRETISH
from ..mailbox import secrets_in

SCHEMA = "project/1"
NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")
SERVICES = ("console", "bridge", "hub", "token-api", "token-web")
KINDS = ("local", "vm")
INSTRUCTIONS_MAX = 8 * 1024
TOP = ("schema", "name", "repos", "environment", "instructions", "routines", "mailbox", "models")
KEYS = {"repo": ("name", "url", "branch", "path"), "environment": ("kind", "python", "venv", "services"),
        "instructions": ("file", "text"), "routine": ("name", "every", "action"), "mailbox": ("repo", "branch"),
        "models": ("ladder",)}
INTERVAL = re.compile(r"^[1-9][0-9]{0,4}(s|sec|min|m|h|d)$")             # '30min': every 30 min after the last run
CALENDAR = re.compile(r"^[A-Za-z0-9*:,./~ -]{1,64}$")                     # systemd OnCalendar, one line
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,99}$")
PATHISH = re.compile(r"^[A-Za-z0-9_./~+-]{1,200}$")                       # goes into a unit file's ExecStart as is
SHELLISH = re.compile(r"[\s;|&$`<>(){}\[\]*?!'\"\\]")


class ProjectError(ValueError):
    pass


def home() -> Path:
    return AR.home()


def path_of(name: str, h: Path | None = None) -> Path:
    if not NAME.match(name or ""):
        raise ProjectError(f"project name {name!r}: [a-z0-9][a-z0-9_.-], up to 40")
    return (h or home()) / "projects" / f"{name}.json"


def credential_url(s: str) -> bool:
    """True for a URL that carries a user name with a password, or any user name over http(s) (a token in the
    user part). ``git@host:owner/repo`` and ``ssh://git@host/...`` carry no secret and pass."""
    try:
        u = urlsplit(s)
    except ValueError:
        return True
    if not u.scheme or not u.netloc:
        return False
    return bool(u.password) or (u.scheme.lower() in ("http", "https") and u.username is not None)


def _strings(obj: Any, where: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, f"{where}.{k}" if where else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{where}[{i}]")
    elif isinstance(obj, str):
        yield where, obj


def _keys(obj: Any, where: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{where}.{k}" if where else str(k), str(k)
            yield from _keys(v, f"{where}.{k}" if where else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _keys(v, f"{where}[{i}]")


def _only(obj: Any, allowed: tuple[str, ...], where: str, out: list[str]) -> bool:
    if not isinstance(obj, dict):
        out.append(f"{where}: an object")
        return False
    for k in obj:
        if k not in allowed:
            out.append(f"{where}.{k}: unknown key (allowed: {', '.join(allowed)})")
    return True


def _str(v: Any, where: str, out: list[str], pat: re.Pattern[str] | None = None, *, need: bool = True) -> None:
    if v is None and not need:
        return
    if not isinstance(v, str) or not v or (pat is not None and not pat.match(v)):
        out.append(f"{where}: {'a string' if pat is None else 'not allowed here: ' + repr(v)[:60]}")


def routine_problem(r: dict[str, Any], registered: set[str] | None) -> str | None:
    """Why this routine's action is refused, else None. A routine names a GA Action; it never carries a command."""
    a = r.get("action")
    if not isinstance(a, str) or not a:
        return "action: a registered GA Action id"
    if SHELLISH.search(a) or "/" in a:
        return f"action {a[:40]!r}: a shell string is never a routine; name a registered GA Action id"
    if not ACTION_ID.match(a):
        return f"action {a[:40]!r}: not a GA Action id"
    if registered is not None and a not in registered:
        return f"action {a!r}: not a registered (approved) GA Action — `ga actions list`"
    return None


def every_ok(s: Any) -> bool:
    return isinstance(s, str) and bool(INTERVAL.match(s) or (CALENDAR.match(s) and not s.startswith("-")))


def validate(p: Any, *, registered: set[str] | None = None, check_files: bool = True) -> list[str]:
    """Every reason ``p`` is not a valid project/1; [] when it is. ``registered``: the approved GA Action ids (default:
    the verified registry under GA_HOME)."""
    out: list[str] = []
    if not isinstance(p, dict):
        return ["a project is a JSON object"]
    for where, k in _keys(p):
        if _SECRETISH.search(k):
            out.append(f"{where}: secret-shaped key; a project never holds credentials")
    for where, s in _strings(p):
        if secrets_in(s):
            out.append(f"{where}: looks like a secret; a project never holds credentials")
        elif credential_url(s):
            out.append(f"{where}: URL carries credentials; use a credential helper, not the URL")
    _only(p, TOP, "project", out)
    if p.get("schema") != SCHEMA:
        out.append(f"schema: {SCHEMA!r}")
    _str(p.get("name"), "name", out, NAME)
    repos = p.get("repos", [])
    if not isinstance(repos, list):
        out.append("repos: a list")
        repos = []
    seen: set[str] = set()
    for i, r in enumerate(repos):
        w = f"repos[{i}]"
        if not _only(r, KEYS["repo"], w, out):
            continue
        _str(r.get("name"), f"{w}.name", out, NAME)
        _str(r.get("url"), f"{w}.url", out, need=False)
        _str(r.get("branch"), f"{w}.branch", out, BRANCH)
        _str(r.get("path"), f"{w}.path", out, PATHISH)
        if r.get("name") in seen:
            out.append(f"{w}.name: {r.get('name')!r} twice")
        seen.add(r.get("name"))
    env = p.get("environment")
    if env is not None and _only(env, KEYS["environment"], "environment", out):
        if env.get("kind") not in KINDS:
            out.append(f"environment.kind: one of {', '.join(KINDS)}")
        _str(env.get("python"), "environment.python", out, PATHISH, need=False)
        _str(env.get("venv"), "environment.venv", out, PATHISH, need=False)
        sv = env.get("services", [])
        if not isinstance(sv, list) or any(s not in SERVICES for s in sv):
            out.append(f"environment.services: a list of {', '.join(SERVICES)}")
    ins = p.get("instructions")
    if ins is not None and _only(ins, KEYS["instructions"], "instructions", out):
        if len(ins) != 1:
            out.append("instructions: exactly one of file, text")
        if "text" in ins:
            if not isinstance(ins["text"], str) or len(ins["text"].encode("utf-8")) > INSTRUCTIONS_MAX:
                out.append(f"instructions.text: a string of {INSTRUCTIONS_MAX} bytes at most")
        if "file" in ins:
            _str(ins["file"], "instructions.file", out, PATHISH)
            f = Path(str(ins["file"])).expanduser()
            if check_files and isinstance(ins["file"], str) and f.is_file():
                if f.stat().st_size > INSTRUCTIONS_MAX:
                    out.append(f"instructions.file: {INSTRUCTIONS_MAX} bytes at most")
                elif secrets_in(f.read_text(encoding="utf-8", errors="replace")):
                    out.append("instructions.file: looks like it holds a secret")
    routines = p.get("routines", [])
    if not isinstance(routines, list):
        out.append("routines: a list")
        routines = []
    if registered is None and routines:
        registered = set(AR.verified())
    names: set[str] = set()
    for i, r in enumerate(routines):
        w = f"routines[{i}]"
        if not _only(r, KEYS["routine"], w, out):
            continue
        _str(r.get("name"), f"{w}.name", out, NAME)
        if r.get("name") in names:
            out.append(f"{w}.name: {r.get('name')!r} twice")
        names.add(r.get("name"))
        if not every_ok(r.get("every")):
            out.append(f"{w}.every: a systemd OnCalendar expression or an interval like 30min")
        why = routine_problem(r, registered)
        if why:
            out.append(f"{w}.{why}")
    mb = p.get("mailbox")
    if mb is not None and _only(mb, KEYS["mailbox"], "mailbox", out):
        _str(mb.get("repo"), "mailbox.repo", out)
        _str(mb.get("branch"), "mailbox.branch", out, BRANCH)
    mo = p.get("models")
    if mo is not None and _only(mo, KEYS["models"], "models", out):
        _str(mo.get("ladder"), "models.ladder", out)
    return out


def load(name: str, h: Path | None = None) -> dict[str, Any] | None:
    try:
        obj = json.loads(path_of(name, h).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def names(h: Path | None = None) -> list[str]:
    d = (h or home()) / "projects"
    return sorted(f.stem for f in d.glob("*.json") if NAME.match(f.stem)) if d.is_dir() else []


def save(p: dict[str, Any], h: Path | None = None, *, registered: set[str] | None = None) -> Path:
    """Validate, then write 0600 (created 0600, never readable by others for a moment). Raises ProjectError."""
    bad = validate(p, registered=registered)
    if bad:
        raise ProjectError("; ".join(bad)[:800])
    path = path_of(p["name"], h)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(p, ensure_ascii=False, indent=1) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path
