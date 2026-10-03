"""Agent SDK Runner (METHOD §4c, 2nd edition third): one session turn = one Claude Agent SDK ``query()``.

The SDK is an optional dependency (``pip install "ga-sdk[agent]"``); nothing in ga imports it unless this
runner is used. The SDK drives the Claude Code CLI as a subprocess, so the guarantees of the headless Runner
are kept by giving the SDK a per-session wrapper as ``cli_path``:

- the wrapper starts the CLI with ``env -i`` and only the variables ga allows (the SDK itself would pass this
  process's whole environment, CLAUDE_CODE_* included) — HOME / CLAUDE_CONFIG_DIR are the session's own;
- inside the OS write sandbox when the hub asks for one (``sandbox: auto | require | off``, as headless);
- the wrapper sits in the runner's ``_wrappers/`` directory, which the sandbox protects, and is rewritten
  before every turn, so a turn cannot change how its next turn starts;
- the PreToolUse guard goes in through ``settings``; permission mode, tools, model, resume and the turn's
  ``max_budget_usd`` through ``ClaudeAgentOptions``.

The turn ends with the SDK's ``ResultMessage`` (session id, total_cost_usd, is_error). Only numbers are kept.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from . import sandbox
from .base import TurnRequest, TurnResult
from .headless import DEFAULT_ALLOWED, DEFAULT_DISALLOWED, KEEP_ENV, HeadlessRunner


def load_sdk() -> Any:
    """The installed claude_agent_sdk, or None."""
    try:
        import claude_agent_sdk
    except ImportError:
        return None
    return claude_agent_sdk


class AgentSDKRunner:
    kind = "agent_sdk"

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
        guard: bool = True,
        sandbox: str = "auto",
        sdk: Any = None,
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
        self.sdk = sdk
        if sandbox not in ("auto", "require", "off"):
            raise ValueError("sandbox must be auto, require or off")
        self.sandbox = sandbox
        # the guard settings, session homes and guard log are shared with the headless runner's layout
        self._layout = HeadlessRunner(self.home, guard=guard, sandbox="off")
        self.guard = guard

    def session_home(self, session: str) -> Path:
        return self._layout.session_home(session)

    def guard_log(self, session: str) -> Path:
        return self._layout.guard_log(session)

    # ------------------------------------------------------------------ the CLI wrapper

    def env(self, session: str) -> dict[str, str]:
        return self._layout.env(session) | self.extra_env

    def wrapper(self, session: str, protect: list[str], writable: list[str]) -> Path:
        real = shutil.which(self.executable) or self.executable
        env = self.env(session)
        cmd = ["env", "-i", *[f"{k}={v}" for k, v in sorted(env.items())]]
        inner = [real]
        if protect:
            inner = sandbox.wrap(inner, protect, writable)
        body = " ".join(shlex.quote(x) for x in cmd + inner) + ' "$@"\n'
        d = self.home / "_wrappers"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{session}.sh"
        tmp = d / f".{session}.sh.tmp"
        tmp.write_text("#!/bin/sh\n# written by ga before every turn; edits do not survive\nexec " + body, encoding="utf-8")
        tmp.chmod(0o755)
        os.replace(tmp, path)
        return path

    # ------------------------------------------------------------------ one turn

    def options(self, req: TurnRequest, cli_path: Path, sandboxed: bool) -> Any:
        sdk = self.sdk
        cap = req.budget.get("max_budget_usd", self.max_budget_usd) if req.budget else self.max_budget_usd
        kw: dict[str, Any] = {
            "cwd": str(req.workdir),
            "cli_path": str(cli_path),
            "permission_mode": self.permission_mode,
            "allowed_tools": self.allowed_tools,
            "disallowed_tools": self.disallowed_tools,
            "setting_sources": [],  # no user / project / local settings files: only what ga passes
        }
        if self.model:
            kw["model"] = self.model
        if isinstance(cap, (int, float)) and not isinstance(cap, bool):
            kw["max_budget_usd"] = float(cap)
        if req.resume_id:
            kw["resume"] = req.resume_id
        if self.guard:
            kw["settings"] = str(self._layout.settings_file(req.session, sandboxed))
        return sdk.ClaudeAgentOptions(**kw)

    async def _run(self, req: TurnRequest, options: Any) -> Any:
        result = None
        async for msg in self.sdk.query(prompt=req.prompt, options=options):
            if isinstance(msg, self.sdk.ResultMessage):
                result = msg
        return result

    def run_turn(self, req: TurnRequest) -> TurnResult:
        if self.sdk is None:
            self.sdk = load_sdk()
            if self.sdk is None:
                return TurnResult(ended=True, error="sdk_not_installed", seconds=0.0, sandboxed=False)
        protect = list(req.permissions.get("protect", []))
        sandboxed = False
        if self.sandbox != "off" and protect:
            if sandbox.available():
                sandboxed = True
            elif self.sandbox == "require":
                return TurnResult(ended=True, error="sandbox_unavailable", seconds=0.0, sandboxed=False)
        writable = list(req.permissions.get("writable", [])) + [str(self.session_home(req.session))]
        cli = self.wrapper(req.session, protect + [str(self.home)] if sandboxed else [], writable if sandboxed else [])
        options = self.options(req, cli, sandboxed)
        start = time.monotonic()
        try:
            result = asyncio.run(asyncio.wait_for(self._run(req, options), timeout=self.timeout))
        except asyncio.TimeoutError:
            return TurnResult(ended=True, error="timeout", seconds=round(time.monotonic() - start, 3), sandboxed=sandboxed)
        except Exception as e:  # the SDK's CLINotFoundError, ProcessError, CLIJSONDecodeError, …: the kind only
            return TurnResult(ended=True, error=f"sdk_error:{type(e).__name__}", seconds=round(time.monotonic() - start, 3), sandboxed=sandboxed)
        secs = round(time.monotonic() - start, 3)
        if result is None:
            return TurnResult(ended=True, error="no_result", seconds=secs, sandboxed=sandboxed)
        cost = getattr(result, "total_cost_usd", None)
        cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        error = f"is_error:{getattr(result, 'subtype', '?')}" if getattr(result, "is_error", False) else ""
        return TurnResult(ended=True, session_id=getattr(result, "session_id", None), cost=cost, error=error, seconds=secs,
                          note=f"num_turns {getattr(result, 'num_turns', '?')}", sandboxed=sandboxed)


def fake_sdk(results: list[Any], record: list[dict] | None = None) -> SimpleNamespace:
    """A stand-in for claude_agent_sdk in tests: ``query`` yields the next planned item (a ResultMessage-like
    object, an exception to raise, or ("sleep", seconds)). ``record`` gets each call's options."""

    class ResultMessage(SimpleNamespace):
        pass

    plan = list(results)

    async def query(*, prompt: str, options: Any):
        if record is not None:
            record.append({"prompt": prompt, "options": options})
        step = plan.pop(0) if plan else ResultMessage(session_id="s", total_cost_usd=0.0, is_error=False, subtype="success", num_turns=1)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, tuple) and step[0] == "sleep":
            await asyncio.sleep(step[1])
            return
        if callable(step):  # a test's stand-in for the session's work, then a result
            step = step(options) or ResultMessage(session_id="s", total_cost_usd=0.01, is_error=False, subtype="success", num_turns=1)
        if isinstance(step, dict):
            step = ResultMessage(**step)
        yield SimpleNamespace(kind="assistant")
        yield step

    return SimpleNamespace(query=query, ClaudeAgentOptions=lambda **kw: SimpleNamespace(**kw), ResultMessage=ResultMessage)
