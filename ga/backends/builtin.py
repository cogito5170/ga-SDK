"""ga's built-in CLI backends (CMD-GA28 S2/S3/S5): agv, gemini_cli, claude_cli, codex_cli. Each is one short-lived host
process per turn, stdin closed, the exit code and the host's JSON read in one place.

Bare plan call (S5) — the host's own tools off and ga's protocol (the spec's ``once`` section) as the system prompt:
- claude_cli: yes. ``--tools ""`` (V: ``claude --help`` 2.1.289: "Use \"\" to disable all tools"), ``--strict-mcp-config``
  (no MCP server from any settings file), ``--system-prompt <once>``, ``--disable-slash-commands``,
  ``--no-session-persistence``. baseline measured one 'OK' turn at 945 (Haiku) / 1,197 (Sonnet) input tokens this way,
  against 29,287 / 32,287 by default (BD-302, results-2026-10-04.json).
- agv: no. The flags known for agy are ``-p``, ``--output-format``, ``--model`` (agy_cli.py's V facts); none turns tools
  off or replaces the system prompt, and agy is not installed here to read its ``--help``. Fixed overhead: not measured.
- gemini_cli: no. Gemini CLI 0.62 has no flag that turns its built-in tools off for one call; baseline measured one 'OK'
  turn at 11,822 input tokens (BD-302). Closest: ``GEMINI_SYSTEM_MD`` (the documented system-prompt override, a file)
  plus a ``tools.exclude`` list in the private settings ga already writes — not verified offline, so not on.
- codex_cli: no. ``codex exec`` has no documented switch known here that removes its shell tool or replaces its system
  prompt (codex is not installed here); closest: ``--sandbox read-only``. Fixed overhead: not measured.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any, Callable

from ..adapters import agy_cli
from ..adapters.gemini_cli import GeminiCLI, clean_env
from .catalog import CATALOG
from .base import API_VERSION, BackendError, BackendTurn, ConfigError, check_served, rate_limited


def _cli(options: dict[str, Any], default: list[str]) -> list[str]:
    cli = options.get("cli", default)
    if not (isinstance(cli, list) and cli and all(isinstance(x, str) and x for x in cli)):
        raise ConfigError("options.cli must be a non-empty list of strings")
    return list(cli)


def _known(options: dict[str, Any], allowed: set[str], backend: str) -> None:
    extra = sorted(set(options) - allowed)
    if extra:
        raise ConfigError(f"{backend}: unknown option(s) {', '.join(extra)}")


def run_process(argv: list[str], *, cwd: str | None, env: dict[str, str], timeout_s: float,
                on_wait: Callable[[float], None] | None = None, wait_every_s: float | None = None) -> tuple[str, str, int]:
    """One host process: (stdout, stderr, exit code). ``on_wait(seconds)`` every ``wait_every_s`` while it runs."""
    t0 = time.monotonic()
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        raise BackendError("cli_not_found") from None
    step = wait_every_s if on_wait is not None and wait_every_s else None
    while True:
        left = timeout_s - (time.monotonic() - t0)
        try:
            out, err = p.communicate(timeout=max(0.0, min(left, step) if step else left))
            return out, err, p.returncode
        except subprocess.TimeoutExpired:
            if time.monotonic() - t0 >= timeout_s:
                p.kill()
                p.communicate()
                raise BackendError("timeout") from None
            if step:
                on_wait(round(time.monotonic() - t0, 1))


# ---- agv (Antigravity; command agy) --------------------------------------------------------------------------------

class AgvRunner(agy_cli.AgyCLI):
    """agy_cli.AgyCLI on any known family. Not bare (see the module doc); no --resume (U), so every prompt is whole."""

    bare = False

    def __init__(self, command: list[str] | None, model: str, **kw: Any):
        self.family = agy_cli.slug_family(model)  # ValueError for an unknown family: the caller's config error
        super().__init__(command, model, **kw)
        self.usage_format = agy_cli.FAMILIES[self.family]
        self.quota_family = agy_cli.family_of(model)

    def run_turn(self, prompt: str, session_id: str | None = None, *, system: str | None = None,
                 on_wait: Callable[[float], None] | None = None, wait_every_s: float | None = None) -> BackendTurn:
        if system is not None:
            raise BackendError("not_bare")  # agv has no system-prompt channel: the loop sends the whole prompt
        t = super().run_turn(prompt, session_id, on_wait=on_wait, wait_every_s=wait_every_s)
        usage = getattr(t, "raw_usage", None)
        if usage is not None and self.family == "gemini":  # usageMetadata (camelCase) -> Telemetry's snake_case names
            usage = {re.sub(r"(?<!^)([A-Z])", r"_\1", k).lower(): v for k, v in usage.items()}
        return BackendTurn(t.text, list(t.served), usage, self.usage_format if usage is not None else None, None,
                           t.seconds, t.events, list(getattr(t, "denied", []) or []))


class _Agv:
    catalog = CATALOG['agv']  # CMD-GA31 S3: what the router may pick
    name, version, api = "agv", "1", API_VERSION
    options = {"cli", "usage_floor_pct", "window", "reset_fallback_s", "usage_every_s"}
    overhead = {"bare": False, "tokens": None, "source": "not measured; agy is not installed here and its docs name no "
                "flag that turns tools off or replaces the system prompt",
                "closest": "none known: -p, --output-format, --model only"}

    def create(self, model: str, options: dict[str, Any], ctx: dict[str, Any]) -> AgvRunner:
        _known(options, self.options, self.name)
        try:
            return AgvRunner(_cli(options, ["agy"]), model, cwd=ctx.get("cwd"), timeout_s=ctx.get("timeout_s", 600.0))
        except ValueError as e:
            raise ConfigError(str(e)) from None


AGV = _Agv()


# ---- gemini_cli -----------------------------------------------------------------------------------------------------

class GeminiRunner(GeminiCLI):
    """gemini_cli.GeminiCLI as a backend: resumes with --resume; usage is the result event's stats (otel names)."""

    resumes, bare = True, False
    usage_format = "otel"

    def run_turn(self, prompt: str, session_id: str | None = None, *, system: str | None = None,
                 on_wait: Callable[[float], None] | None = None, wait_every_s: float | None = None) -> BackendTurn:
        if system is not None:
            raise BackendError("not_bare")
        t = super().run_turn(prompt, session_id, on_wait=on_wait, wait_every_s=wait_every_s)
        return BackendTurn(t.text, list(t.served), dict(t.usage) or None, "otel" if t.usage else None, t.session_id,
                           t.seconds, t.events)


class _GeminiCli:
    catalog = CATALOG['gemini_cli']  # CMD-GA31 S3: what the router may pick
    name, version, api = "gemini_cli", "1", API_VERSION
    overhead = {"bare": False, "tokens": 11822, "source": "measured: one 'OK' turn, baseline BD-302 "
                "(ops/model_smoke results-2026-10-04.json)",
                "closest": "GEMINI_SYSTEM_MD (system prompt file) + tools.exclude in the private settings; not verified"}

    def create(self, model: str, options: dict[str, Any], ctx: dict[str, Any]) -> GeminiRunner:
        _known(options, {"cli"}, self.name)
        return GeminiRunner(_cli(options, ["gemini"]), model, cwd=ctx.get("cwd"), timeout_s=ctx.get("timeout_s", 600.0),
                            settings_dir=ctx.get("state_dir"))


GEMINI_CLI = _GeminiCli()


# ---- claude_cli (claude -p) -----------------------------------------------------------------------------------------

_LIMIT = re.compile(r"(?i)\b(rate limit|usage limit|429|overloaded)\b")


class ClaudeRunner:
    """``claude -p`` with ``--output-format json``. Bare by default (S5). The served model is every key of the result's
    ``modelUsage``; the usage is the result's ``usage`` (anthropic shape). No session is kept (--no-session-persistence),
    so every prompt is whole and ``resumes`` is False."""

    resumes = False
    usage_format = "anthropic"

    def __init__(self, command: list[str], model: str, *, bare: bool = True, cwd: str | None = None,
                 timeout_s: float = 600.0, env: dict[str, str] | None = None):
        self.command, self.model, self.bare, self.cwd, self.timeout_s = command, model, bare, cwd, timeout_s
        self.env = clean_env() if env is None else dict(env)

    def argv(self, prompt: str, system: str | None = None) -> list[str]:
        a = self.command + ["-p", prompt, "--output-format", "json", "--model", self.model, "--no-session-persistence"]
        if self.bare:
            if system is None:
                raise BackendError("bare_without_system")
            a += ["--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--system-prompt", system]
        elif system is not None:
            raise BackendError("not_bare")
        return a

    def run_turn(self, prompt: str, session_id: str | None = None, *, system: str | None = None,
                 on_wait: Callable[[float], None] | None = None, wait_every_s: float | None = None) -> BackendTurn:
        t0 = time.monotonic()
        out, err, code = run_process(self.argv(prompt, system), cwd=self.cwd, env=self.env, timeout_s=self.timeout_s,
                                     on_wait=on_wait, wait_every_s=wait_every_s)
        return parse_claude(out, code, self.model, round(time.monotonic() - t0, 3))


def parse_claude(stdout: str, code: int, model: str, seconds: float = 0.0) -> BackendTurn:
    try:
        data = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise BackendError(f"exit_{code}" if code else "bad_json")
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        if data.get("api_error_status") == 429 or _LIMIT.search(str(data.get("result") or "")):
            raise rate_limited(None)
        raise BackendError(f"is_error:{str(data.get('subtype', '?'))[:40]}")
    mu = data.get("modelUsage")
    served = sorted(str(k) for k in mu) if isinstance(mu, dict) else []
    check_served(served, model)
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    return BackendTurn(str(data.get("result") or ""), served, usage, "anthropic" if usage is not None else None, None,
                       seconds, 1)


class _ClaudeCli:
    catalog = CATALOG['claude_cli']  # CMD-GA31 S3: what the router may pick
    name, version, api = "claude_cli", "1", API_VERSION
    overhead = {"bare": True, "tokens": {"haiku": 945, "sonnet": 1197},
                "source": "measured: one 'OK' turn with --tools '' and --system-prompt, baseline BD-302 "
                "(default host: 29,287 / 32,287)", "closest": "bare"}

    def create(self, model: str, options: dict[str, Any], ctx: dict[str, Any]) -> ClaudeRunner:
        _known(options, {"cli", "bare"}, self.name)
        if not isinstance(options.get("bare", True), bool):
            raise ConfigError("claude_cli: options.bare must be true or false")
        return ClaudeRunner(_cli(options, ["claude"]), model, bare=options.get("bare", True), cwd=ctx.get("cwd"),
                            timeout_s=ctx.get("timeout_s", 600.0))


CLAUDE_CLI = _ClaudeCli()


# ---- codex_cli (codex exec) -----------------------------------------------------------------------------------------

class CodexRunner:
    """``codex exec --json``: JSONL events. The answer is the last ``agent_message`` item's text; the usage is
    ``turn.completed``'s (input_tokens · cached_input_tokens · output_tokens: read as otel). U: whether the events name
    the served model — ga looks for a ``model`` at any depth and fails the turn (served_model_unknown) when there is
    none. Not bare (module doc); no resume."""

    resumes, bare, usage_format = False, False, "otel"

    def __init__(self, command: list[str], model: str, *, cwd: str | None = None, timeout_s: float = 600.0,
                 env: dict[str, str] | None = None):
        self.command, self.model, self.cwd, self.timeout_s = command, model, cwd, timeout_s
        self.env = clean_env() if env is None else dict(env)

    def argv(self, prompt: str) -> list[str]:
        return self.command + ["exec", "--json", "--model", self.model, "--sandbox", "read-only",
                               "--skip-git-repo-check", prompt]

    def run_turn(self, prompt: str, session_id: str | None = None, *, system: str | None = None,
                 on_wait: Callable[[float], None] | None = None, wait_every_s: float | None = None) -> BackendTurn:
        if system is not None:
            raise BackendError("not_bare")
        t0 = time.monotonic()
        out, err, code = run_process(self.argv(prompt), cwd=self.cwd, env=self.env, timeout_s=self.timeout_s,
                                     on_wait=on_wait, wait_every_s=wait_every_s)
        return parse_codex(out, code, self.model, round(time.monotonic() - t0, 3))


def _models(obj: Any, out: list[str]) -> None:
    if isinstance(obj, dict):
        m = obj.get("model")
        if isinstance(m, str) and m and m not in out:
            out.append(m)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _models(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _models(v, out)


def parse_codex(stdout: str, code: int, model: str, seconds: float = 0.0) -> BackendTurn:
    answer, usage, served, events, failed, limited = None, None, [], 0, None, False
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        events += 1
        _models(ev, served)
        t = ev.get("type")
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if t == "item.completed" and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            answer = item["text"]
        elif t == "turn.completed" and isinstance(ev.get("usage"), dict):
            usage = dict(ev["usage"])
        elif t in ("turn.failed", "error"):
            msg = str((ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else ev.get("message"))
            limited = limited or bool(_LIMIT.search(msg))
            failed = t
    if limited:
        raise rate_limited(None)
    if failed or answer is None:
        raise BackendError(failed or (f"exit_{code}" if code else "no_answer"))
    check_served(served, model)
    return BackendTurn(answer, served, usage, "otel" if usage is not None else None, None, seconds, events)


class _CodexCli:
    catalog = CATALOG['codex_cli']  # CMD-GA31 S3: what the router may pick
    name, version, api = "codex_cli", "1", API_VERSION
    overhead = {"bare": False, "tokens": None, "source": "not measured; codex is not installed here",
                "closest": "--sandbox read-only (its shell tool and system prompt stay)"}

    def create(self, model: str, options: dict[str, Any], ctx: dict[str, Any]) -> CodexRunner:
        _known(options, {"cli"}, self.name)
        return CodexRunner(_cli(options, ["codex"]), model, cwd=ctx.get("cwd"), timeout_s=ctx.get("timeout_s", 600.0))


CODEX_CLI = _CodexCli()
