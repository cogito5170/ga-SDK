"""Gemini CLI, headless (CMD-GA21 S1, BD-221/222): one Gemini turn is one short-lived ``gemini`` process.

    gemini -p <prompt> --output-format stream-json -m <model> [--resume <session id>]

Never the interactive UI: no TTY, stdin closed, so the CLI's quota dialog cannot open.

The stream (Gemini CLI 0.62.0, as baseline read it from the source — baseline#12 5970108172): JSONL on stdout, every event
with ``type`` and ``timestamp``:
- ``init`` {session_id, model} — ``model`` is the model *asked for*, not the one that served;
- ``message`` {role, content, delta?}; ``tool_use``; ``tool_result``;
- ``error`` {severity warning|error, message} — loop detected (warning) or max session turns (error) only;
- ``result`` {status success|error, error?{type, message}, stats{…, models{<model>: …}}}.
A thrown API error (429 included) ends the run with one ``result`` {status: error, error.type = the error class:
``RetryableQuotaError`` per minute, ``TerminalQuotaError`` daily or hard}. The exit code is the error's status (429 exits
as 173) — never read. No ``result`` at all is a crashed turn. ``retryDelay`` is not in the stream: a "retry in N s" in
``error.message`` is kept as a hint only.

The served model is every key of ``result.stats.models``: any other key than the asked model fails the turn
(``ModelMismatch``) — 0.62 can switch silently even headless. The CLI would retry a per-minute 429 inside the process
(``general.maxAttempts``, default 10) and sleep there; ga writes private CLI settings with ``maxAttempts: 1`` so the CLI
fails fast and ga does the waiting. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

DEFAULT_MODEL = "gemini-3-flash-preview"
SETTINGS_ENV = "GEMINI_CLI_SYSTEM_SETTINGS_PATH"  # system settings override the user's and the workspace's
MINUTE_ERROR, DAY_ERROR = "RetryableQuotaError", "TerminalQuotaError"


class GeminiError(RuntimeError):
    """A failed Gemini turn. ``reason`` is a label."""

    def __init__(self, reason: str, status: int | None = None, body: dict | None = None, headers: dict | None = None):
        super().__init__(reason)
        self.reason, self.status, self.body, self.headers = reason, status, body, headers


class GeminiRateLimited(GeminiError):
    """A quota error. ``kind`` is "minute" or "day"; ``hint_s`` a "retry in N s" from the message, or None. ``status`` and
    ``body`` are the provider-error shape rlo's Governor reads (the supervisor may set the body's wait)."""

    def __init__(self, kind: str, hint_s: float | None, via: str = "type"):
        body = quota_body(kind, hint_s)
        super().__init__(f"rate_limited_{kind}", 429, body)
        self.kind, self.hint_s, self.via = kind, hint_s, via


class ModelMismatch(GeminiError):
    """The turn was served by another model than the one asked for (or the stream did not say which)."""


def quota_body(kind: str, wait_s: float | None) -> dict:
    """A Gemini 429 body for the Governor: a RetryInfo when a wait is known, else the per-minute or per-day quota id."""
    details: list[dict] = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaId": f"GenerateRequestsPer{'Day' if kind == 'day' else 'Minute'}PerProjectPerModel"}]}]
    if wait_s is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{wait_s:.3f}s"})
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": details}}


@dataclass
class GeminiTurn:
    session_id: str | None
    served: list[str]
    text: str
    usage: dict[str, int]
    seconds: float = 0.0
    events: int = 0


@dataclass
class Stream:
    session_id: str | None = None
    asked: str | None = None
    served: list[str] = field(default_factory=list)
    text: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    has_result: bool = False
    status: str | None = None
    error_type: str | None = None
    error_message: str = ""          # kept only to find a quota hint; never logged
    cli_errors: int = 0               # error events of severity error (max session turns)
    warnings: int = 0                 # error events of severity warning (loop detected)
    events: int = 0


def parse_stream(lines: Iterable[str]) -> Stream:
    """stream-json lines -> Stream. Lines that are not JSON objects are ignored (the CLI may print notices)."""
    s = Stream()
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        s.events += 1
        t = ev.get("type")
        if t == "init":
            s.session_id = ev.get("session_id") or s.session_id
            s.asked = ev.get("model") or s.asked
        elif t == "message" and ev.get("role") == "assistant" and isinstance(ev.get("content"), str):
            s.text = s.text + ev["content"] if ev.get("delta") else ev["content"]
        elif t == "error":
            if ev.get("severity") == "error":
                s.cli_errors += 1
            else:
                s.warnings += 1
        elif t == "result":
            s.has_result, s.status = True, ev.get("status")
            err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            s.error_type = err.get("type") or s.error_type
            s.error_message = str(err.get("message") or "")
            stats = ev.get("stats") if isinstance(ev.get("stats"), dict) else {}
            for k in ("input_tokens", "output_tokens", "total_tokens"):
                if isinstance(stats.get(k), int):
                    s.usage[k] = stats[k]
            models = stats.get("models")
            if isinstance(models, dict):
                s.served = [m for m in models if isinstance(m, str)]
    return s


_RETRY_IN = re.compile(r"(?i)\bretry\s+(?:in|after)\s+(\d+(?:\.\d+)?)\s*s")
_PER_DAY = re.compile(r"(?i)per[\s_-]?day|PerDay|daily")
_QUOTA_TEXT = re.compile(r"(?i)\b429\b|RESOURCE_EXHAUSTED|exceeded your current quota|exhausted your (?:capacity|daily quota)")


def quota_of(s: Stream) -> tuple[str, float | None, str] | None:
    """(kind, hint_s, via) when the run ended on a quota error, else None. The first path is the result's error type;
    the error text (a 429 / quota sentence) is a second path only, for an error class this table does not name."""
    if s.error_type in (MINUTE_ERROR, DAY_ERROR):
        via = "type"
        kind = "day" if s.error_type == DAY_ERROR or _PER_DAY.search(s.error_message) else "minute"
    elif s.status == "error" and _QUOTA_TEXT.search(s.error_message):
        via = "text"
        kind = "day" if _PER_DAY.search(s.error_message) else "minute"
    else:
        return None
    m = _RETRY_IN.search(s.error_message)
    return kind, (float(m.group(1)) if m and kind == "minute" else None), via


def default_system_settings() -> Path:
    if sys.platform == "darwin":
        return Path("/Library/Application Support/GeminiCli/settings.json")
    if os.name == "nt":
        return Path(r"C:\ProgramData\gemini-cli\settings.json")
    return Path("/etc/gemini-cli/settings.json")


def clean_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The CLI's environment: the caller's, without Claude Code's own variables."""
    env = dict(os.environ if base is None else base)
    return {k: v for k, v in env.items() if not k.startswith("CLAUDE_CODE_")}


class GeminiCLI:
    def __init__(self, command: list[str] | None = None, model: str = DEFAULT_MODEL, *, cwd: str | None = None,
                 env: dict[str, str] | None = None, timeout_s: float = 600.0, settings_dir: str | Path | None = None):
        self.command = list(command or ["gemini"])
        self.model, self.cwd, self.timeout_s = model, cwd, timeout_s
        self.env = clean_env() if env is None else dict(env)
        self.settings_dir = Path(settings_dir) if settings_dir else None
        self.system_settings = Path(self.env.get(SETTINGS_ENV) or default_system_settings())  # read, never written

    def argv(self, prompt: str, session_id: str | None) -> list[str]:
        a = self.command + ["-p", prompt, "--output-format", "stream-json", "-m", self.model]
        return a + (["--resume", session_id] if session_id else [])

    def write_settings(self) -> Path | None:
        """The private CLI settings: the system settings in force (if any), with general.maxAttempts = 1, written where
        ga keeps its state and handed to the CLI by SETTINGS_ENV. The person's own settings files are not touched."""
        if self.settings_dir is None:
            return None
        src, own = self.system_settings, self.settings_dir / "gemini-cli-settings.json"
        data: dict = {}
        if src.exists():
            try:
                data = json.loads(src.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise GeminiError("system_settings_unreadable") from None  # never drop them silently
        if not isinstance(data, dict):
            raise GeminiError("system_settings_unreadable")
        general = data.get("general") if isinstance(data.get("general"), dict) else {}
        data["general"] = {**general, "maxAttempts": 1}
        self.settings_dir.mkdir(parents=True, exist_ok=True)
        tmp = own.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, own)
        self.env[SETTINGS_ENV] = str(own)
        return own

    def run_turn(self, prompt: str, session_id: str | None = None) -> GeminiTurn:
        """One turn. Raises GeminiRateLimited (quota), ModelMismatch, or GeminiError; returns the turn otherwise."""
        self.write_settings()
        t0 = time.monotonic()
        try:
            p = subprocess.run(self.argv(prompt, session_id), cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=self.timeout_s)
        except FileNotFoundError:
            raise GeminiError("cli_not_found") from None
        except subprocess.TimeoutExpired:
            raise GeminiError("timeout") from None
        s = parse_stream(p.stdout.splitlines())  # the exit code is not read: the result event decides
        if not s.has_result:
            raise GeminiError("no_result")
        q = quota_of(s)
        if q is not None:
            raise GeminiRateLimited(*q)
        if s.status != "success":
            raise GeminiError(f"result_{s.error_type or 'error'}"[:60])
        if s.cli_errors:
            raise GeminiError("cli_error")
        if not s.served:
            raise ModelMismatch("served_model_unknown")
        if any(m != self.model for m in s.served):
            raise ModelMismatch("served_model_mismatch")
        return GeminiTurn(s.session_id or session_id, list(s.served), s.text, dict(s.usage),
                          round(time.monotonic() - t0, 3), s.events)
