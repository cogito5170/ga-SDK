"""Local headless Runner (METHOD §4c, 2nd edition first): one session turn = one ``claude -p`` run.

- The prompt goes in on stdin; ``--output-format json`` gives the end of the turn, the session id and the cost.
- The next turn of the same session continues with ``--resume <session id>``.
- The child runs in a clean environment: only PATH, locale, proxy / CA variables and ANTHROPIC_BASE_URL are
  passed (plus ``extra_env``). HOME and CLAUDE_CONFIG_DIR point at the runner's own directory, never at the
  person's ``~/.claude``; this process's CLAUDE_CODE_* variables are not passed.
- Permissions are narrowed with ``--permission-mode`` and ``--allowedTools`` / ``--disallowedTools``; the
  working directory is the session's worktree directory. ``--max-budget-usd`` caps one turn when set.
- Nothing from the turn (transcript, answer text) is kept: only numbers and the error kind.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import sandbox
from .base import TurnRequest, TurnResult

KEEP_ENV = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "ANTHROPIC_BASE_URL",
)

DEFAULT_ALLOWED = (
    "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git add:*)", "Bash(git commit:*)",
    "Bash(git push origin:*)",
    # harmless or read-only commands a turn reaches for first; refusing them made ordinary turns give up
    # quietly (GA5 rev 2 B, GA6 run 1 A — CMD-GA7). Writes stay inside the sandbox; the guard still runs.
    "Bash(git -C:*)", "Bash(git rev-parse:*)", "Bash(git show:*)", "Bash(git branch:*)",
    "Bash(ls:*)", "Bash(pwd)", "Bash(cat:*)", "Bash(head:*)", "Bash(wc:*)", "Bash(mkdir:*)", "Bash(cd:*)",
)
# best effort: the pre-push hook is client side, so keep the turn away from the switches that skip it
DEFAULT_DISALLOWED = ("Bash(git push --no-verify:*)", "Bash(git config:*)", "Bash(git -c:*)", "Bash(git commit --no-verify:*)")


class HeadlessRunner:
    kind = "headless"

    def __init__(
        self,
        home: str | Path,
        *,
        executable: str = "claude",
        model: str | None = None,
        permission_mode: str = "acceptEdits",
        allowed_tools: Iterable[str] = DEFAULT_ALLOWED,
        disallowed_tools: Iterable[str] = DEFAULT_DISALLOWED,
        timeout: float = 900,
        max_budget_usd: float | None = None,
        extra_env: dict[str, str] | None = None,
        extra_args: Iterable[str] = (),
        guard: bool = True,
        sandbox: str = "auto",
    ):
        self.home = Path(home)
        self.executable = executable
        self.model = model
        self.permission_mode = permission_mode
        self.allowed_tools = list(allowed_tools)
        self.disallowed_tools = list(disallowed_tools)
        self.timeout = timeout
        self.max_budget_usd = max_budget_usd
        self.extra_env = dict(extra_env or {})
        self.extra_args = list(extra_args)
        self.guard = guard
        if sandbox not in ("auto", "require", "off"):
            raise ValueError("sandbox must be auto, require or off")
        self.sandbox = sandbox

    def session_home(self, session: str) -> Path:
        """HOME / CLAUDE_CONFIG_DIR of one session: per session, so a turn cannot touch another session's state."""
        return self.home / session

    def guard_log(self, session: str) -> Path:
        return self.session_home(session) / "ga-guard.jsonl"

    def settings_file(self, session: str, sandboxed: bool = False) -> Path:
        """Turn settings with the PreToolUse guard (bash_guard.py) — only in the runner's own directory."""
        import shlex
        import sys

        from . import bash_guard

        home = self.session_home(session)
        home.mkdir(parents=True, exist_ok=True)
        parts = [sys.executable, bash_guard.__file__, "--log", str(self.guard_log(session))] + (["--sandboxed"] if sandboxed else [])
        cmd = " ".join(shlex.quote(x) for x in parts)
        settings = {"hooks": {"PreToolUse": [{"matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
                                              "hooks": [{"type": "command", "command": cmd}]}]}}
        path = home / "ga-settings.json"
        path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        return path

    def env(self, session: str) -> dict[str, str]:
        home = self.session_home(session)
        cfg = home / ".claude"
        cfg.mkdir(parents=True, exist_ok=True)
        env = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
        env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(cfg))
        env.update(self.extra_env)
        return env

    def argv(self, req: TurnRequest, sandboxed: bool = False) -> list[str]:
        argv = [self.executable, "-p", "--output-format", "json", "--permission-mode", self.permission_mode]
        if self.model:
            argv += ["--model", self.model]
        allowed = self.allowed_tools + list(req.permissions.get("allow", []))
        if allowed:
            argv += ["--allowedTools", ",".join(allowed)]
        if self.disallowed_tools:
            argv += ["--disallowedTools", ",".join(self.disallowed_tools)]
        cap = req.budget.get("max_budget_usd", self.max_budget_usd) if req.budget else self.max_budget_usd
        if isinstance(cap, (int, float)) and not isinstance(cap, bool):
            argv += ["--max-budget-usd", f"{cap:g}"]
        if req.resume_id:
            argv += ["--resume", req.resume_id]
        if self.guard:
            argv += ["--settings", str(self.settings_file(req.session, sandboxed))]
        return argv + self.extra_args

    def run_turn(self, req: TurnRequest) -> TurnResult:
        env = self.env(req.session)
        protect = list(req.permissions.get("protect", []))
        sandboxed = False
        if self.sandbox != "off" and protect:
            if sandbox.available():
                sandboxed = True
            elif self.sandbox == "require":
                return TurnResult(ended=True, error="sandbox_unavailable", seconds=0.0, sandboxed=False)
        argv = self.argv(req, sandboxed)
        if sandboxed:
            writable = list(req.permissions.get("writable", [])) + [self.session_home(req.session)]
            argv = sandbox.wrap(argv, protect, writable)
        call = run_claude(argv, req.prompt, Path(req.workdir), env, self.timeout)
        data = call.data or {}
        return TurnResult(ended=True, session_id=call.session_id, cost=call.cost, error=call.error, seconds=call.seconds,
                          note=f"num_turns {data.get('num_turns')}" if "num_turns" in data else call.note, sandboxed=sandboxed)


@dataclass
class ClaudeCall:
    data: dict | None
    error: str
    seconds: float
    cost: float | None = None
    session_id: str | None = None
    note: str = ""


def run_claude(argv: list[str], stdin: str, cwd: Path, env: dict[str, str], timeout: float) -> ClaudeCall:
    """One ``claude -p --output-format json`` call. Never raises; the kind of failure is in ``error``."""
    start = time.monotonic()
    try:
        p = subprocess.run(argv, input=stdin, text=True, capture_output=True, cwd=str(cwd), env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ClaudeCall(None, "timeout", round(time.monotonic() - start, 3), note=f"killed after {timeout:g}s")
    except OSError as e:
        return ClaudeCall(None, "not_started", round(time.monotonic() - start, 3), note=type(e).__name__)
    secs = round(time.monotonic() - start, 3)
    try:
        data = json.loads(p.stdout)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except ValueError:
        return ClaudeCall(None, "bad_json" if p.returncode == 0 else f"exit {p.returncode}", secs,
                          note=f"stdout {len(p.stdout)} bytes, stderr {len(p.stderr)} bytes")
    cost = data.get("total_cost_usd")
    cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    sid = data.get("session_id") if isinstance(data.get("session_id"), str) else None
    error = ""
    if p.returncode != 0:
        error = f"exit {p.returncode}"
    elif data.get("is_error"):
        error = f"is_error:{data.get('subtype', '?')}"
    return ClaudeCall(data, error, secs, cost, sid)
