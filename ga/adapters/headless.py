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

    def env(self) -> dict[str, str]:
        self.home.mkdir(parents=True, exist_ok=True)
        cfg = self.home / ".claude"
        cfg.mkdir(exist_ok=True)
        env = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
        env.update(HOME=str(self.home), CLAUDE_CONFIG_DIR=str(cfg))
        env.update(self.extra_env)
        return env

    def argv(self, req: TurnRequest) -> list[str]:
        argv = [self.executable, "-p", "--output-format", "json", "--permission-mode", self.permission_mode]
        if self.model:
            argv += ["--model", self.model]
        if self.allowed_tools:
            argv += ["--allowedTools", ",".join(self.allowed_tools)]
        if self.disallowed_tools:
            argv += ["--disallowedTools", ",".join(self.disallowed_tools)]
        cap = req.budget.get("max_budget_usd", self.max_budget_usd) if req.budget else self.max_budget_usd
        if isinstance(cap, (int, float)) and not isinstance(cap, bool):
            argv += ["--max-budget-usd", f"{cap:g}"]
        if req.resume_id:
            argv += ["--resume", req.resume_id]
        return argv + self.extra_args

    def run_turn(self, req: TurnRequest) -> TurnResult:
        call = run_claude(self.argv(req), req.prompt, Path(req.workdir), self.env(), self.timeout)
        data = call.data or {}
        return TurnResult(ended=True, session_id=call.session_id, cost=call.cost, error=call.error, seconds=call.seconds,
                          note=f"num_turns {data.get('num_turns')}" if "num_turns" in data else call.note)


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
