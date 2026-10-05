"""GA Actions, the static check (CMD-GA42 S2): is a proposed named command safe to trial-run and to show a human?

A proposal is ``PROPOSE <name>`` plus one JSON line ``{argv, why, example, cwd, timeout_s, writes}``. The check is code
only and refuses with a reason; nothing here runs anything:

- argv[0] is an executable on the human's allowlist (``~/.ga/actions-policy.json`` ``executables``) or a file inside
  the project; never a shell (sh, bash, zsh, cmd, ...), never ``-c`` / ``-e`` / ``eval`` / ``exec`` anywhere;
- no shell metacharacter in any argument; placeholders only from a closed set ({path}, {name});
- no secret path (GA38 ``secret_path`` plus ~/.ssh-style folders), symlinks resolved; nothing outside the project;
- a network-looking command is flagged ``network``: it never trial-runs and only an approval that says so admits it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..act.apply import secret_path
from ..act.commands import NAME

PLACEHOLDERS = ("{path}", "{name}")
FIELDS = {"argv", "why", "example", "cwd", "timeout_s", "writes"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "ash", "cmd", "cmd.exe", "powershell",
          "powershell.exe", "pwsh", "busybox"}
RUNS_ANYTHING = {"env", "xargs", "sudo", "su", "doas", "nohup", "timeout", "nice", "setsid", "exec", "eval", "source",
                 "strace", "watch", "script", "expect"}
EVAL_FLAGS = {"-c", "-e", "--eval", "--command", "eval", "exec", "-exec", "--exec", "-execdir", "-ok"}
META = set(";|&$`<>(){}[]*?!~#'\"\\\n\r\t")
SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".docker", ".kube", ".azure", ".config", ".ga-ask", "secrets"}
NET_TOOLS = {"curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "telnet", "ftp", "ping", "dig",
             "nslookup", "gh", "aws", "gcloud", "az", "kubectl", "docker", "http", "https"}
NET_SUBCMDS = {"git": {"push", "pull", "fetch", "clone", "ls-remote", "remote", "submodule"},
               "pip": {"install", "download"}, "pip3": {"install", "download"},
               "npm": {"install", "i", "ci", "publish", "update", "add"}, "npx": None, "yarn": None, "pnpm": None,
               "cargo": {"install", "fetch", "update", "publish"}, "go": {"get", "install", "mod"},
               "uv": {"pip", "add", "sync"}}
SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]{1,200}$")   # GA rlo SAFE_PATH: what a {path} value may look like
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_PH = re.compile(r"\{[^{}]*\}")


def _secretish(rel: str) -> bool:
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    return secret_path(rel) or any(p in SECRET_DIRS for p in parts)


def confined(root: Path, rel: str) -> str | None:
    """None when ``rel`` is a project-relative path inside ``root``, not secret, also after resolving symlinks;
    else the reason."""
    if not rel or rel.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", rel):
        return f"path {rel!r} is not project-relative"
    if _secretish(rel):
        return f"path {rel!r} is a secret or internal path"
    root = Path(root).resolve()
    try:
        q = (root / rel).resolve()
    except (OSError, RuntimeError):
        return f"path {rel!r} cannot be resolved"
    if q != root and root not in q.parents:
        return f"path {rel!r} escapes the project"
    if q != root and _secretish(str(q.relative_to(root))):
        return f"path {rel!r} resolves to a secret path"
    return None


def placeholder_value(key: str, value: Any, root: Path) -> str | None:
    """None when a placeholder value is allowed (SAFE_PATH rule for {path}), else the reason."""
    if not isinstance(value, str) or value.startswith("-"):
        return f"{{{key}}} value {value!r} must be a string not starting with '-'"
    if key == "path":
        if not SAFE_PATH.match(value):
            return f"{{path}} value {value!r} breaks the SAFE_PATH rule"
        return confined(root, value)
    if key == "name":
        return None if SAFE_NAME.match(value) else f"{{name}} value {value!r} must match [A-Za-z0-9_.-]{{1,64}}"
    return f"placeholder {{{key}}} is not in the closed set {', '.join(PLACEHOLDERS)}"


def _pathlike(s: str) -> bool:
    return "/" in s or s.startswith(".") or bool(re.search(r"\.(env|pem|key|p12|pfx|netrc|npmrc|pypirc)\b", s, re.I)) \
        or s.startswith(("~", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"))


def _exists(root: Path, v: str) -> bool:
    try:
        q = root / v
        return q.is_symlink() or q.exists()
    except (OSError, ValueError):
        return False


def network(argv: list[str]) -> bool:
    exe = Path(argv[0]).name
    if exe in NET_TOOLS or any("://" in a or a.startswith("git@") for a in argv):
        return True
    subs = NET_SUBCMDS.get(exe, set())
    if subs is None:
        return True
    if exe.startswith("python") and len(argv) > 2 and argv[1] == "-m" and argv[2] in ("pip", "http.server"):
        return True
    return any(a in subs for a in argv[1:3])


def check(p: dict[str, Any], root: Path, executables: list[str] | tuple[str, ...] = ()) -> tuple[list[str], bool]:
    """(reasons it is refused, needs network). No reasons = it may trial-run (unless it needs network)."""
    why: list[str] = []
    root = Path(root).resolve()
    if not isinstance(p, dict):
        return ["the proposal is not a JSON object"], False
    if set(p) - FIELDS - {"name"}:
        why.append(f"unknown field(s): {', '.join(sorted(set(p) - FIELDS - {'name'}))}")
    if not NAME.match(str(p.get("name", ""))):
        why.append("name must match [a-z][a-z0-9_-], up to 32")
    argv = p.get("argv")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)):
        return why + ["argv must be a non-empty list of strings (no shell)"], False
    exe = argv[0]
    base = Path(exe).name.lower()
    if base in SHELLS or base.rstrip("0123456789.") in SHELLS:
        why.append(f"argv[0] {exe!r} is a shell")
    if base in RUNS_ANYTHING:
        why.append(f"argv[0] {exe!r} runs any command")
    for i, a in enumerate(argv):
        if a in EVAL_FLAGS or Path(a).name in SHELLS and i > 0:
            why.append(f"argv[{i}] {a!r} evaluates code or starts a shell")
        phs = _PH.findall(a)
        bad_ph = [x for x in phs if x not in PLACEHOLDERS]
        if bad_ph:
            why.append(f"argv[{i}]: placeholder(s) {', '.join(bad_ph)} not in the closed set {', '.join(PLACEHOLDERS)}")
        if phs and (i == 0 or a not in PLACEHOLDERS and not re.fullmatch(r"--?[A-Za-z0-9_-]+=\{(path|name)\}", a)):
            why.append(f"argv[{i}] {a!r}: a placeholder must be a whole argument (or --opt={{path}}), never argv[0]")
        rest = _PH.sub("", a)
        meta = sorted({c for c in rest if c in META})
        if meta:
            why.append(f"argv[{i}] {a!r} has shell metacharacter(s) {''.join(meta)!r}")
        for v in ([rest.split("=", 1)[1]] if rest.startswith("-") and "=" in rest else [rest]):
            if v and not v.startswith("-") and (_pathlike(v) or i == 0 or _exists(root, v)):
                if i == 0 and "/" not in v:
                    continue  # a bare executable name: the allowlist decides
                r = confined(root, v)
                if r:
                    why.append(f"argv[{i}]: {r}")
    if "/" in exe:
        q = (root / exe)
        if confined(root, exe) is None and not q.is_file():
            why.append(f"argv[0] {exe!r} is not a file in the project")
    elif exe not in executables:
        why.append(f"argv[0] {exe!r} is not on the executable allowlist ({', '.join(executables) or 'empty'}) "
                   "and not a file inside the project")
    cwd = p.get("cwd", ".")
    if not isinstance(cwd, str):
        why.append("cwd must be a project-relative path")
    elif cwd not in (".", ""):
        r = confined(root, cwd)
        if r:
            why.append(f"cwd: {r}")
    t = p.get("timeout_s", 60)
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= 3600:
        why.append("timeout_s must be a number in (0, 3600]")
    writes = p.get("writes", [])
    if not (isinstance(writes, list) and all(isinstance(g, str) and g for g in writes)):
        why.append("writes must be a list of project globs ([] = writes nothing)")
    else:
        for g in writes:
            r = confined(root, g.replace("*", "x").replace("?", "x"))
            if r:
                why.append(f"writes: {r}")
    ex = p.get("example", {})
    if isinstance(ex, str):
        ex = {"path": ex} if "{path}" in argv else {"name": ex}
    if not isinstance(ex, dict):
        why.append("example must map placeholder names to values")
    else:
        for k, v in ex.items():
            r = placeholder_value(str(k).strip("{}"), v, root)
            if r:
                why.append(f"example: {r}")
        missing = [x for x in PLACEHOLDERS if any(x in a for a in argv) and x.strip("{}") not in
                   {str(k).strip("{}") for k in ex}]
        if missing:
            why.append(f"example has no value for {', '.join(missing)}")
    return list(dict.fromkeys(why)), network(argv)


def fill(argv: list[str], values: dict[str, Any], root: Path) -> tuple[list[str] | None, str]:
    """argv with {path}/{name} filled from ``values`` (each value checked); (None, reason) when refused."""
    vals = {str(k).strip("{}"): v for k, v in (values or {}).items()}
    used = {x.strip("{}") for a in argv for x in _PH.findall(a)}
    extra = set(vals) - used
    if extra:
        return None, f"unknown placeholder(s): {', '.join(sorted(extra))}"
    for k in used:
        if k not in vals:
            return None, f"missing value for {{{k}}}"
        r = placeholder_value(k, vals[k], root)
        if r:
            return None, r
    return [_PH.sub(lambda m: str(vals[m.group(0).strip("{}")]), a) for a in argv], ""
