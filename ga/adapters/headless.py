"""Local headless Runner (METHOD §4c, 2nd edition first): one session turn = one ``claude -p`` run.

- The prompt goes in on stdin; ``--output-format json`` gives the end of the turn, the session id and the cost.
- The next turn of the same session continues with ``--resume <session id>`` -- unless the turn is fresh
  (CMD-GA29 S1): then there is never a ``--resume``, and the context-budget/1 hook (rlo-sdk 0.10.0) goes into this
  turn's settings when ``context_budget`` is set -- only in the runner's own per-session directory.
- The child runs in a clean environment: only PATH, locale, proxy / CA variables and ANTHROPIC_BASE_URL are
  passed (plus ``extra_env``). HOME and CLAUDE_CONFIG_DIR point at the runner's own directory, never at the
  person's ``~/.claude``; this process's CLAUDE_CODE_* variables are not passed.
- Permissions are narrowed with ``--permission-mode`` and ``--allowedTools`` / ``--disallowedTools``; the
  working directory is the session's worktree directory. ``--max-budget-usd`` caps one turn when set.
- The result gives numbers, the error kind, token usage, the served model and the answer text (CMD-GA29 S3); the
  hub keeps the numbers and, in fresh mode, reads the report and the state block from the answer.
- ``tools`` (``--tools``) and ``system_prompt`` (``--system-prompt``) make the child's fixed cost as small as the
  work allows (BD-302); both are off unless set.
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
        guards: Iterable[dict] = (),
        context_budget: dict | None = None,
        tools: Iterable[str] | None = None,
        system_prompt: str | None = None,
    ):
        self.home = Path(home)
        # METHOD rev 15 §4c 7: the operator's PreToolUse guards (e.g. rlo.hooks enforce), after ga's own
        self.guards = [dict(g) for g in guards]
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
        self.context_budget = budget_config(context_budget)
        self.tools = None if tools is None else list(tools)
        self.system_prompt = system_prompt

    def session_home(self, session: str) -> Path:
        """HOME / CLAUDE_CONFIG_DIR of one session: per session, so a turn cannot touch another session's state."""
        return self.home / session

    def guard_log(self, session: str) -> Path:
        return self.session_home(session) / "ga-guard.jsonl"

    def budget_log(self, session: str) -> Path:
        return self.session_home(session) / "ga-budget.jsonl"

    def settings_file(self, session: str, sandboxed: bool = False, fresh: bool = False) -> Path:
        """Turn settings with the PreToolUse guard (bash_guard.py) — only in the runner's own directory."""
        import shlex
        import sys

        from . import bash_guard, budget_hook

        home = self.session_home(session)
        home.mkdir(parents=True, exist_ok=True)
        parts = [sys.executable, bash_guard.__file__, "--log", str(self.guard_log(session))] + (["--sandboxed"] if sandboxed else [])
        cmd = " ".join(shlex.quote(x) for x in parts)
        pre = [{"matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": cmd}]}] if self.guard else []
        for g in self.guards:  # after ga's guard; only in this turn's settings, never the person's ~/.claude
            pre.append({"matcher": g.get("matcher", "*"), "hooks": [{"type": "command", "command": self.fill(g["command"], session)}]})
        if fresh and self.context_budget:  # S4: the budget backstop, only in this turn's settings
            b = self.context_budget
            parts = [sys.executable, budget_hook.__file__, "--soft", str(b["soft"]), "--hard", str(b["hard"]),
                     "--mode", b.get("mode", "shadow"), "--log", str(self.budget_log(session))]
            for sp in b.get("state_paths", ()):
                parts += ["--state", sp]
            pre.append({"matcher": "*", "hooks": [{"type": "command", "command": " ".join(shlex.quote(x) for x in parts)}]})
        settings = {"hooks": {"PreToolUse": pre}}
        path = home / "ga-settings.json"
        path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        return path

    def fill(self, text: str, session: str) -> str:
        """``{session}`` and ``{home}`` (the session's own, writable, home) in a guard's command or record path."""
        return text.replace("{session}", session).replace("{home}", str(self.session_home(session)))

    def guard_records(self, session: str) -> list[tuple[str, Path]]:
        """(name, record file) of each operator guard that keeps a record (JSONL)."""
        return [(g.get("name") or f"guard{i + 1}", Path(self.fill(g["record"], session)))
                for i, g in enumerate(self.guards) if g.get("record")]

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
        if self.tools is not None:
            argv += ["--tools", ",".join(self.tools)]
        if self.system_prompt is not None:
            argv += ["--system-prompt", self.system_prompt]
        if req.resume_id and not req.fresh:
            argv += ["--resume", req.resume_id]
        if self.guard or self.guards or (req.fresh and self.context_budget):
            argv += ["--settings", str(self.settings_file(req.session, sandboxed, req.fresh))]
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
                          note=f"num_turns {data.get('num_turns')}" if "num_turns" in data else call.note, sandboxed=sandboxed,
                          usage=usage_of(data), model=served_model(data),
                          answer=data["result"] if isinstance(data.get("result"), str) else None,
                          raw={k: v for k, v in data.items() if k != "result"} if call.data else None)


USAGE_KEYS = (("input", "input_tokens"), ("cache_read", "cache_read_input_tokens"),
              ("cache_creation", "cache_creation_input_tokens"), ("output", "output_tokens"))


def usage_of(data: dict) -> dict[str, int] | None:
    """``usage`` of a ``claude -p --output-format json`` result -> {input, cache_read, cache_creation, output}.
    Only the fields the result gave; None when it gave none (never 0 for unknown)."""
    u = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(u, dict):
        return None
    out = {k: u[src] for k, src in USAGE_KEYS if isinstance(u.get(src), int) and not isinstance(u.get(src), bool)}
    return out or None


def served_model(data: dict) -> str | None:
    """The model(s) that served the turn: the keys of ``modelUsage`` (sorted, comma-joined)."""
    mu = data.get("modelUsage") if isinstance(data, dict) else None
    if isinstance(mu, dict) and mu:
        return ",".join(sorted(str(k) for k in mu))
    return None


def budget_config(b: dict | None) -> dict | None:
    """``runner.context_budget`` {soft, hard, mode?, state_paths?} checked like rlo.ctxbudget.Budget; None = off."""
    if b is None:
        return None
    if not isinstance(b, dict) or set(b) - {"soft", "hard", "mode", "state_paths"} or not {"soft", "hard"} <= set(b):
        raise ValueError("context_budget: {soft, hard, mode?, state_paths?}")
    soft, hard_ = b["soft"], b["hard"]
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (soft, hard_)) or not 0 < soft <= hard_:
        raise ValueError("context_budget: integers with 0 < soft <= hard")
    if b.get("mode", "shadow") not in ("shadow", "enforce"):
        raise ValueError("context_budget mode: shadow or enforce")
    sp = b.get("state_paths", ["STATE.md"])
    if not isinstance(sp, list) or not sp or not all(isinstance(x, str) and x.strip() for x in sp):
        raise ValueError("context_budget state_paths: a non-empty list of paths")
    return {"soft": soft, "hard": hard_, "mode": b.get("mode", "shadow"), "state_paths": list(sp)}


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
    except PermissionError as e:  # the environment refused to start it (METHOD rev 13 §4c): never retried another way
        return ClaudeCall(None, f"refused:{type(e).__name__}", round(time.monotonic() - start, 3), note="permission refused")
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
