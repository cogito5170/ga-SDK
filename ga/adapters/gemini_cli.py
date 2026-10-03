"""Gemini CLI, headless (CMD-GA21 S1, BD-221): one Gemini turn is one short-lived ``gemini`` process.

    gemini -p <prompt> --output-format stream-json -m <model> [--resume <session id>]

Never the interactive UI: no TTY, stdin closed, so the CLI's quota dialog (Keep trying / Switch model / Stop) cannot
open. The model is fixed by the caller; the served model is read from the stream, and a turn served by any other model,
or by a model the stream does not name, is a failed turn (``ModelMismatch``) — no silent fallback is accepted.
A 429 / RESOURCE_EXHAUSTED raises ``GeminiRateLimited`` with ``status`` and ``body`` the way a provider error carries
them, so rlo's Governor reads the server's ``retryDelay`` (or the quota hints) as the earliest next time.

The event shapes read here (JSONL, one object per line: ``init`` {session_id, model}, ``message`` {role, content,
delta}, ``tool_use``, ``tool_result``, ``error`` {severity, message}, ``result`` {status, error?, stats{…}}) are the
headless stream-json format as ga reads it; the parsing is all in ``parse_stream``. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_MODEL = "gemini-3-flash-preview"


class GeminiError(RuntimeError):
    """A failed Gemini turn. ``status`` · ``body`` · ``headers`` as a provider error carries them (may be None)."""

    def __init__(self, reason: str, status: int | None = None, body: dict | None = None, headers: dict | None = None):
        super().__init__(reason)
        self.reason, self.status, self.body, self.headers = reason, status, body, headers


class GeminiRateLimited(GeminiError):
    """429 / RESOURCE_EXHAUSTED. Not a failure of the step: the scheduler parks it until ``retryDelay``."""


class ModelMismatch(GeminiError):
    """The turn was served by another model than the one asked for (or the stream did not say which)."""


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
    served: list[str] = field(default_factory=list)
    text: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)       # error texts (kept only to find a 429; never logged)
    status: str | None = None
    events: int = 0


def _served_add(s: Stream, m: Any) -> None:
    if isinstance(m, str) and m and m not in s.served:
        s.served.append(m)


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
            _served_add(s, ev.get("model"))
        elif t == "message" and ev.get("role") == "assistant" and isinstance(ev.get("content"), str):
            s.text = s.text + ev["content"] if ev.get("delta") else ev["content"]
        elif t == "error":
            s.errors.append(json.dumps(ev))
        elif t == "result":
            s.status = ev.get("status")
            if ev.get("error") is not None:
                s.errors.append(json.dumps(ev["error"]))
            stats = ev.get("stats") if isinstance(ev.get("stats"), dict) else {}
            for k in ("input_tokens", "output_tokens", "total_tokens"):
                if isinstance(stats.get(k), int):
                    s.usage[k] = stats[k]
            models = stats.get("models")
            if isinstance(models, dict):  # a per-model breakdown names every model that served this turn
                for m in models:
                    _served_add(s, m)
            _served_add(s, ev.get("model"))
    return s


_RETRY_IN = re.compile(r"(?i)retry(?:Delay)?\W{0,4}(?:in\s+)?(\d+(?:\.\d+)?)\s*s\b")


def _json_objects(text: str) -> Iterable[dict]:
    """Every JSON object embedded in ``text`` (an error message may carry the API error body as text)."""
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():  # the API error may sit, as text, inside the event's own message
                if isinstance(v, str) and "{" in v:
                    yield from _json_objects(v)
        i = text.find("{", end)


def rate_limit_body(texts: Iterable[str]) -> dict | None:
    """The Gemini API 429 body found in error texts, or one built from a "retry in Ns" hint; None when no 429."""
    hint = None
    for text in texts:
        for obj in _json_objects(text):
            err = obj.get("error") if isinstance(obj.get("error"), dict) else obj
            if err.get("code") == 429 or err.get("status") == "RESOURCE_EXHAUSTED":
                return {"error": err} if "error" not in obj else obj
        is_429 = "429" in text or "RESOURCE_EXHAUSTED" in text
        m = _RETRY_IN.search(text)
        if is_429:
            hint = hint or {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": []}}
            if m and not hint["error"]["details"]:
                hint["error"]["details"].append({"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                 "retryDelay": f"{m.group(1)}s"})
    return hint


def clean_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The CLI's environment: the caller's, without Claude Code's own variables."""
    env = dict(os.environ if base is None else base)
    return {k: v for k, v in env.items() if not k.startswith("CLAUDE_CODE_")}


class GeminiCLI:
    def __init__(self, command: list[str] | None = None, model: str = DEFAULT_MODEL, *, cwd: str | None = None,
                 env: dict[str, str] | None = None, timeout_s: float = 600.0):
        self.command = list(command or ["gemini"])
        self.model, self.cwd, self.timeout_s = model, cwd, timeout_s
        self.env = clean_env() if env is None else dict(env)

    def argv(self, prompt: str, session_id: str | None) -> list[str]:
        a = self.command + ["-p", prompt, "--output-format", "stream-json", "-m", self.model]
        return a + (["--resume", session_id] if session_id else [])

    def run_turn(self, prompt: str, session_id: str | None = None) -> GeminiTurn:
        """One turn. Raises GeminiRateLimited (429), ModelMismatch, or GeminiError; returns the turn otherwise."""
        import time
        t0 = time.monotonic()
        try:
            p = subprocess.run(self.argv(prompt, session_id), cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=self.timeout_s)
        except FileNotFoundError:
            raise GeminiError("cli_not_found") from None
        except subprocess.TimeoutExpired:
            raise GeminiError("timeout") from None
        s = parse_stream(p.stdout.splitlines())
        body = rate_limit_body(s.errors + [p.stderr])
        if body is not None:
            raise GeminiRateLimited("rate_limited", 429, body)
        if p.returncode != 0 or s.status not in (None, "success") or s.errors:
            raise GeminiError(f"exit_{p.returncode}" if p.returncode else f"status_{s.status or 'error'}")
        if not s.served:
            raise ModelMismatch("served_model_unknown")
        if any(m != self.model for m in s.served):
            raise ModelMismatch("served_model_mismatch")
        return GeminiTurn(s.session_id or session_id, list(s.served), s.text, dict(s.usage),
                          round(time.monotonic() - t0, 3), s.events)
