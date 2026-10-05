"""``ga gemini`` — a Gemini supervisor (CMD-GA21, BD-221). The user talks to ga; Gemini only directs.

- Every Gemini turn is one headless, short-lived CLI process (``ga.adapters.gemini_cli``) on the model fixed in the
  config (default gemini-3-flash-preview). No switch prompt, no fallback: a turn served by another model fails.
- A Gemini turn answers with a closed step list (``ga-gemini-plan/1``): tool steps from the configured tool table (an
  extension's MCP tools over stdio, or Python functions) and at most one next model step.
- rlo's Scheduler (CMD-K12) runs the list: tool steps whenever ready, model steps only when the Governor allows. When a
  model step is parked (the minute window, a quota error's "retry in N s" hint, or the daily quota) ga prints one
  status block (when it resumes, what is done / running / parked, what comes next, requests left today), saves the
  state file and sleeps until the window opens; at a daily reset it probes once. After a
  crash or a closed terminal, ``ga gemini --resume`` continues from the state file.
- ga's state stays on disk (state file, tool results, log); in memory only capped previews. The log holds labels and
  numbers only — no prompt, answer or result text.

rlo is imported only inside the functions that run a task, so ``import ga`` never loads it.
"""
from __future__ import annotations

import inspect
import json
import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

from .adapters.gemini_cli import DEFAULT_MODEL, GeminiError
from .forms import FormError, Problem

CONFIG_SCHEMA = "ga-gemini/1"          # `ga gemini`'s config (kept: an alias form, CMD-GA28 S4)
SUPERVISE_SCHEMA = "ga-supervise/1"    # `ga supervise --backend <name> --config <file>` (CMD-GA28)
PLAN_SCHEMA = "ga-plan/1"
LEGACY_PLAN_SCHEMA = "ga-gemini-plan/1"  # accepted as an alias of ga-plan/1; `ga gemini` still names it in its prompts
PLAN_SCHEMAS = (PLAN_SCHEMA, LEGACY_PLAN_SCHEMA)
STATE_SCHEMA = "ga-gemini-state/1"
MODEL_STEP = "gemini"  # the one model step name in the step table
STEP_ID = re.compile(r"^[A-Za-z0-9_-]{1,12}$")
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:+-]{1,40}$")  # a step-table name is a label (rlo LABEL)
MAX_PLAN_STEPS = 16
# the daily request cap is the integrating side's (BD-289): none unless `daily.requests` is configured — the 20 a day
# of the free tier is gone with billing on (CMD-GA26 S6, BD-294). reset_tz / reset_at say when a day ends: for a
# configured cap, and for the server's own daily quota (TerminalQuotaError), which ga still waits out; Google documents
# per-day limits as resetting at midnight Pacific
DAILY_DEFAULT = {"requests": None, "reset_tz": "America/Los_Angeles", "reset_at": "00:00"}
# requests per minute the Governor allows by default: meant to sit below the server's per-minute limit, so the CLI rarely
# meets a 429 it would retry inside the turn (BD-232). The free tier's exact minute limit for gemini-3-flash-preview is
# not known here — an assumption; set budget.rpm in ga-gemini.json to your key's limit minus a margin.
DEFAULT_BUDGET = {"rpm": 5}
HOSTS = ("gemini_cli", "agy")
# CMD-GA26: follow-up turns are compiled compact by default; verbatim (today's text, byte for byte) stays selectable.
# The first turn is always verbatim (S3: baseline decides on a compact first turn).
PROMPT_MODES = ("compact", "verbatim")
SPEC_FILE = Path(__file__).resolve().parent / "specs" / "supervisor-plan.pspec"
_SPEC: Any = None
# agy (CMD-GA23): the slug the user pinned (BD-255); a model step waits when the family's weekly share is under the
# floor; window and reset_fallback_s cover a /usage text that does not say them (its format is U)
AGY_DEFAULT = {"model": "gemini-3.8-flash-high", "cli": ["agy"], "usage_floor_pct": 5, "window": "weekly",
               "reset_fallback_s": 3600, "usage_every_s": 300}


# ---- config ---------------------------------------------------------------------------------------------------------

@dataclass
class GeminiConfig:
    root: Path
    model: str = DEFAULT_MODEL
    cli: list[str] = field(default_factory=lambda: ["gemini"])
    budget: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_BUDGET))
    tools: dict[str, dict[str, str]] = field(default_factory=dict)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    state_dir: str = ".ga-gemini"
    max_model_steps: int = 20
    result_cap: int = 2000
    turn_timeout_s: float = 600.0
    est_tokens: int = 2000
    daily: dict[str, Any] = field(default_factory=lambda: dict(DAILY_DEFAULT))
    turn_status_s: float = 20.0          # a turn running longer gets one status line per this many seconds (S7)
    max_parallel: int | None = None      # tool steps at once; handed to the Scheduler when it takes it (K12 rev 3, S8)
    host: str = "gemini_cli"             # gemini_cli | agy (CMD-GA23); `ga gemini --host` overrides it
    agy: dict[str, Any] = field(default_factory=lambda: dict(AGY_DEFAULT))
    prompt_mode: str = "compact"         # every turn: compact | verbatim (CMD-GA26; the first turn too since GA28)
    form: str = CONFIG_SCHEMA            # the config form it was read from: ga-gemini/1 or ga-supervise/1 (GA28)
    backend: str = ""                    # ga-supervise/1: the ga.backends plugin name
    options: dict[str, Any] = field(default_factory=dict)  # ga-supervise/1: that backend's options (no keys)
    transient_backoff_s: float = 20.0    # GA41 S3: the one retry of a transient server error waits this long

    @property
    def active_model(self) -> str:
        """The one model this host runs: fixed by the config, never switched (the same rule on every backend)."""
        if self.form == SUPERVISE_SCHEMA:
            return self.model
        return self.agy["model"] if self.host == "agy" else self.model

    @property
    def backend_name(self) -> str:
        if self.form == SUPERVISE_SCHEMA:
            return self.backend
        return "agv" if self.host == "agy" else "gemini_cli"

    @property
    def plan_schema(self) -> str:
        """The plan form the prompts name: ga gemini keeps ga-gemini-plan/1 (its verbatim text is unchanged)."""
        return PLAN_SCHEMA if self.form == SUPERVISE_SCHEMA else LEGACY_PLAN_SCHEMA

    @property
    def state_path(self) -> Path:
        return (self.root / self.state_dir).resolve()


def config_problems(raw: Any) -> list[Problem]:
    p: list[Problem] = []

    def bad(path: str, msg: str) -> None:
        p.append(Problem(path, msg))
    if not isinstance(raw, dict):
        return [Problem("$", "must be an object")]
    known = {"schema", "model", "cli", "budget", "tools", "mcp_servers", "state_dir", "max_model_steps", "result_cap",
             "turn_timeout_s", "est_tokens", "daily", "turn_status_s", "max_parallel", "host", "agy", "prompt_mode",
             "transient_backoff_s"}
    for k in sorted(set(raw) - known):
        bad(f"$.{k}", "unknown field")
    if raw.get("schema") != CONFIG_SCHEMA:
        bad("$.schema", f"must be {CONFIG_SCHEMA!r}")
    if "model" in raw and not (isinstance(raw["model"], str) and raw["model"]):
        bad("$.model", "must be a model name")
    if "cli" in raw and not (isinstance(raw["cli"], list) and raw["cli"] and all(isinstance(x, str) and x for x in raw["cli"])):
        bad("$.cli", "must be a non-empty list of strings (the gemini command)")
    b = raw.get("budget", DEFAULT_BUDGET)
    if not isinstance(b, dict) or set(b) - {"rpm", "tpm"} or not any(b.get(k) for k in ("rpm", "tpm")) or \
            any(b.get(k) is not None and not (isinstance(b[k], int) and not isinstance(b[k], bool) and b[k] > 0) for k in b):
        bad("$.budget", "must be {rpm?, tpm?} with at least one positive integer")
    servers = raw.get("mcp_servers", {})
    if not isinstance(servers, dict):
        bad("$.mcp_servers", "must be an object")
        servers = {}
    for name, s in servers.items():
        if not isinstance(s, dict) or set(s) - {"command", "cwd"} or not (isinstance(s.get("command"), list) and s["command"]
                                                                          and all(isinstance(x, str) for x in s["command"])):
            bad(f"$.mcp_servers.{name}", "must be {command: [..], cwd?}")
    tools = raw.get("tools", {})
    if not isinstance(tools, dict):
        bad("$.tools", "must be an object")
        tools = {}
    for name, t in tools.items():
        where = f"$.tools.{name}"
        if name == MODEL_STEP or not TOOL_NAME.match(name):
            bad(where, f"a tool name is a label ([A-Za-z0-9_.:+-], up to 40) and not {MODEL_STEP!r}")
        if not isinstance(t, dict) or set(t) - {"mcp", "tool", "python", "about"}:
            bad(where, "must be {mcp, tool, about?} or {python, about?}")
            continue
        if "python" in t:
            if not (isinstance(t["python"], str) and re.match(r"^[\w.]+:\w+$", t["python"])) or "mcp" in t:
                bad(where, "python must be 'module:function' (and alone)")
        elif not (isinstance(t.get("mcp"), str) and isinstance(t.get("tool"), str)) or t["mcp"] not in servers:
            bad(where, "mcp must name a server in mcp_servers, tool the MCP tool name")
    for k, lo in (("max_model_steps", 1), ("result_cap", 100), ("est_tokens", 0)):
        if k in raw and not (isinstance(raw[k], int) and not isinstance(raw[k], bool) and raw[k] >= lo):
            bad(f"$.{k}", f"must be an integer >= {lo}")
    for k in ("turn_timeout_s", "turn_status_s"):
        if k in raw and not (isinstance(raw[k], (int, float)) and not isinstance(raw[k], bool) and raw[k] > 0):
            bad(f"$.{k}", "must be a positive number")
    if "transient_backoff_s" in raw and not (isinstance(raw["transient_backoff_s"], (int, float))
                                             and not isinstance(raw["transient_backoff_s"], bool)
                                             and raw["transient_backoff_s"] >= 0):
        bad("$.transient_backoff_s", "must be a number >= 0")
    if "max_parallel" in raw and not (isinstance(raw["max_parallel"], int) and not isinstance(raw["max_parallel"], bool)
                                      and raw["max_parallel"] >= 1):
        bad("$.max_parallel", "must be an integer >= 1")
    if "state_dir" in raw and not (isinstance(raw["state_dir"], str) and raw["state_dir"]):
        bad("$.state_dir", "must be a path")
    if "host" in raw and raw["host"] not in HOSTS:
        bad("$.host", f"must be one of {', '.join(HOSTS)}")
    if "prompt_mode" in raw and raw["prompt_mode"] not in PROMPT_MODES:
        bad("$.prompt_mode", f"must be one of {', '.join(PROMPT_MODES)}")
    a = raw.get("agy", {})
    if not isinstance(a, dict) or set(a) - set(AGY_DEFAULT):
        bad("$.agy", "must be {model?, cli?, usage_floor_pct?, window?, reset_fallback_s?, usage_every_s?}")
    else:
        a = {**AGY_DEFAULT, **a}
        if not (isinstance(a["model"], str) and a["model"]):
            bad("$.agy.model", "must be an agy model slug")
        if not (isinstance(a["cli"], list) and a["cli"] and all(isinstance(x, str) and x for x in a["cli"])):
            bad("$.agy.cli", "must be a non-empty list of strings")
        if not (isinstance(a["usage_floor_pct"], (int, float)) and 0 <= a["usage_floor_pct"] < 100):
            bad("$.agy.usage_floor_pct", "must be a number in [0, 100)")
        if a["window"] not in ("weekly", "5-hour"):
            bad("$.agy.window", "must be weekly or 5-hour")
        for k in ("reset_fallback_s", "usage_every_s"):
            if not (isinstance(a[k], (int, float)) and not isinstance(a[k], bool) and a[k] > 0):
                bad(f"$.agy.{k}", "must be a positive number")
    d = raw.get("daily", DAILY_DEFAULT)
    if not isinstance(d, dict) or set(d) - set(DAILY_DEFAULT):
        bad("$.daily", "must be {requests?, reset_tz?, reset_at?}")
    else:
        d = {**DAILY_DEFAULT, **d}
        if d["requests"] is not None and not (isinstance(d["requests"], int) and not isinstance(d["requests"], bool)
                                              and d["requests"] >= 1):
            bad("$.daily.requests", "must be an integer >= 1, or absent for no daily cap")
        if not (isinstance(d["reset_at"], str) and re.match(r"^([01]\d|2[0-3]):[0-5]\d$", d["reset_at"])):
            bad("$.daily.reset_at", "must be HH:MM")
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(d["reset_tz"])
        except Exception:
            bad("$.daily.reset_tz", "must be an IANA time zone this system knows")
    return p


SUPERVISE_FIELDS = {"schema", "backend", "model", "options", "budget", "tools", "mcp_servers", "state_dir",
                    "max_model_steps", "result_cap", "turn_timeout_s", "est_tokens", "daily", "turn_status_s",
                    "max_parallel", "prompt_mode", "transient_backoff_s"}
_SECRET_NAME = re.compile(r"(?i)(api_?key|^key$|token|secret|password|credential|auth)")
_SECRET_VALUE = re.compile(r"^(sk-|sk_|AIza|xox[abp]-|ghp_|github_pat_|Bearer\s)")


def supervise_problems(raw: Any, backend: str | None = None) -> list[Problem]:
    """ga-supervise/1 (CMD-GA28 S4): the ga-gemini/1 fields that are not the host's, plus ``backend`` (a ga.backends
    name; ``--backend`` overrides it), ``model`` (required) and ``options`` (the backend's; never a key — a key is read
    from the environment variable ``options.key_env`` names). No budget or daily cap unless configured (BD-289)."""
    if not isinstance(raw, dict):
        return [Problem("$", "must be an object")]
    p = [Problem(f"$.{k}", "unknown field") for k in sorted(set(raw) - SUPERVISE_FIELDS)]
    if raw.get("schema") != SUPERVISE_SCHEMA:
        p.append(Problem("$.schema", f"must be {SUPERVISE_SCHEMA!r}"))
    common = {k: v for k, v in raw.items() if k in SUPERVISE_FIELDS - {"schema", "backend", "options"}}
    p += [q for q in config_problems({**common, "schema": CONFIG_SCHEMA}) if q.path != "$.schema"]
    name = backend or raw.get("backend")
    if not (isinstance(name, str) and name):
        p.append(Problem("$.backend", "must name a backend (or give --backend)"))
    if not (isinstance(raw.get("model"), str) and raw["model"]):
        p.append(Problem("$.model", "must be the model the backend runs"))
    opts = raw.get("options", {})
    if not isinstance(opts, dict):
        return p + [Problem("$.options", "must be an object")]
    for k, v in opts.items():
        if (_SECRET_NAME.search(k) and k != "key_env") or (isinstance(v, str) and _SECRET_VALUE.match(v)):
            p.append(Problem(f"$.options.{k}", "a config holds no keys: name an environment variable in key_env"))
    if p:
        return p
    from . import backends
    try:
        backends.create(name, raw["model"], opts, {"cwd": None})
    except KeyError as e:
        p.append(Problem("$.backend", str(e).strip("'\"")[:200]))
    except backends.ConfigError as e:
        p.append(Problem("$.options" if "option" in str(e) else "$.model", str(e)[:200]))
    return p


def load_config(path: str | Path, backend: str | None = None) -> GeminiConfig:
    """A ga-supervise/1 config, or a ga-gemini/1 one (``ga gemini``'s, read as before)."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise FormError([Problem("$", f"cannot read {path}: {type(e).__name__}")]) from None
    if isinstance(raw, dict) and raw.get("schema") == SUPERVISE_SCHEMA:
        probs = supervise_problems(raw, backend)
        if probs:
            raise FormError(probs)
        kw = {k: raw[k] for k in ("tools", "mcp_servers", "max_model_steps", "result_cap", "turn_timeout_s",
                                  "est_tokens", "turn_status_s", "max_parallel", "prompt_mode", "transient_backoff_s")
              if k in raw}
        return GeminiConfig(root=path.resolve().parent, model=raw["model"], budget=dict(raw.get("budget") or {}),
                            daily={**DAILY_DEFAULT, **raw.get("daily", {})}, state_dir=raw.get("state_dir", ".ga-supervise"),
                            form=SUPERVISE_SCHEMA, backend=backend or raw["backend"],
                            options=dict(raw.get("options") or {}), **kw)
    probs = config_problems(raw)
    if probs:
        raise FormError(probs)
    kw = {k: raw[k] for k in ("model", "cli", "budget", "tools", "mcp_servers", "state_dir", "max_model_steps",
                              "result_cap", "turn_timeout_s", "est_tokens", "turn_status_s", "max_parallel", "host",
                              "prompt_mode", "transient_backoff_s")
          if k in raw}
    return GeminiConfig(root=path.resolve().parent, daily={**DAILY_DEFAULT, **raw.get("daily", {})},
                        agy={**AGY_DEFAULT, **raw.get("agy", {})}, **kw)


# ---- the daily quota (S6, BD-222) -----------------------------------------------------------------------------------

def next_reset(now: float, tz: str, at: str) -> float:
    """The first time after ``now`` (epoch s) when the clock in ``tz`` reads ``at`` (HH:MM)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    zone, (h, m) = ZoneInfo(tz), map(int, at.split(":"))
    local = datetime.fromtimestamp(now, zone)
    cand = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if cand <= local:
        cand = (local + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
    return cand.timestamp()


class DayCount:
    """Requests sent today (this ga's count, on disk), against the configured daily cap if there is one (none by
    default, S6); a new day starts at the reset. ``spent``: the server said today's quota is spent."""

    def __init__(self, path: Path, daily: dict[str, Any]):
        cap = daily.get("requests")
        self.path, self.limit, self.tz, self.at = path, None if cap is None else int(cap), daily["reset_tz"], daily["reset_at"]
        d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.used, self.reset, self.spent = int(d.get("used", 0)), float(d.get("reset_at", 0.0)), bool(d.get("spent"))

    def _roll(self, now: float) -> None:
        if now >= self.reset:
            self.used, self.reset, self.spent = 0, next_reset(now, self.tz, self.at), False
            self._save()

    def _save(self) -> None:
        _atomic_write(self.path, {"used": self.used, "reset_at": self.reset, "limit": self.limit, "spent": self.spent})

    def left(self, now: float) -> int | None:
        """Requests left today: 0 once the server said the day is spent, None when no cap is configured."""
        self._roll(now)
        if self.spent:
            return 0
        return None if self.limit is None else max(0, self.limit - self.used)

    def reset_at(self, now: float) -> float:
        self._roll(now)
        return self.reset

    def add(self, now: float) -> None:
        self._roll(now)
        self.used += 1
        self._save()

    def exhaust(self, now: float) -> None:
        """The server says the day is spent (TerminalQuotaError): nothing is left until the reset, whatever ga counted."""
        self._roll(now)
        self.spent = True
        self._save()


def daily_governor(cfg: GeminiConfig, clock: Callable[[], float], day: DayCount, model: str | None = None) -> Any:
    """rlo's Governor (per-minute window, 429 waits) with the daily count on top: no request goes out when today's is
    spent — the step waits for the reset instead of spending a call on a known 429."""
    from rlo.governor import Governor

    class DailyGovernor(Governor):
        def wait_s(self, est_tokens: int = 0, model: str | None = None, calls: int = 1) -> float:
            w = super().wait_s(est_tokens, model, calls)
            now = self.clock()
            left = day.left(now)
            return max(w, day.reset_at(now) - now) if left is not None and left < calls else w

        def try_acquire(self, est_tokens: int = 0, model: str | None = None) -> Any:
            g = super().try_acquire(est_tokens, model)
            if g.ok:
                day.add(self.clock())
            return g

    return DailyGovernor({model or cfg.model: dict(cfg.budget)}, clock=clock, provider="gemini")


class AgyQuota:
    """agy's share for the pinned model's family (S2, BD-255): read from ``agy -p /usage`` (V: spends nothing), at most
    every ``usage_every_s`` and again once the held reset has come. Under the floor, or after a quota / credits error,
    model steps wait for the reset agy reports (or ``reset_fallback_s`` when it reports none)."""

    def __init__(self, cli: Any, model: str, agy: dict[str, Any], clock: Callable[[], float],
                 log: Callable[..., None] = lambda *a, **k: None):
        from .adapters.agy_cli import family_of
        self.cli, self.clock, self.log = cli, clock, log
        self.family = family_of(model)
        self.floor, self.window = float(agy["usage_floor_pct"]), agy["window"]
        self.fallback_s, self.every_s = float(agy["reset_fallback_s"]), float(agy["usage_every_s"])
        self.info: dict[str, Any] | None = None
        self.probed_at: float | None = None
        self.blocked_until: float | None = None
        self.probes = 0

    def probe(self) -> dict[str, Any] | None:
        now = self.clock()
        self.probes += 1
        self.probed_at = now
        try:
            self.info = self.cli.usage().get(self.family)
        except GeminiError:
            self.info = None
        pct = self.info["remaining_pct"] if self.info else None
        self.log("usage", family=self.family, remaining_pct=pct, known=self.info is not None)
        return self.info

    def _reset(self, now: float) -> float:
        at = self.info.get("reset_at") if self.info else None
        return at if isinstance(at, (int, float)) and at > now else now + self.fallback_s

    def wait(self, now: float) -> float:
        """Seconds a model step must still wait (no probe)."""
        return max(0.0, self.blocked_until - now) if self.blocked_until and now < self.blocked_until else 0.0

    def check(self, now: float) -> float:
        """Before a model step: the wait (0 when it may go). Probes when the last reading is stale or a held reset came."""
        if self.blocked_until and now < self.blocked_until:
            return self.blocked_until - now
        if self.blocked_until or self.probed_at is None or now - self.probed_at >= self.every_s:
            self.blocked_until = None
            self.probe()
        if self.info and self.info["remaining_pct"] < self.floor:
            self.blocked_until = self._reset(now)
            return self.blocked_until - now
        return 0.0

    def spent(self, now: float) -> float:
        """agy said the quota is spent (or offered credits): hold until the reset; return the wait."""
        self.probe()
        self.blocked_until = self._reset(now)
        return self.blocked_until - now

    @property
    def window_kind(self) -> str:
        return (self.info or {}).get("window") or self.window


def agy_governor(cfg: GeminiConfig, clock: Callable[[], float], quota: AgyQuota, model: str) -> Any:
    """rlo's Governor (per-minute window) with agy's share on top: no model step goes out while the share is under the
    floor or a quota stop is held."""
    from rlo.governor import Governor

    class AgyGovernor(Governor):
        def wait_s(self, est_tokens: int = 0, model_: str | None = None, calls: int = 1) -> float:
            return max(super().wait_s(est_tokens, model_, calls), quota.wait(self.clock()))

        def try_acquire(self, est_tokens: int = 0, model_: str | None = None) -> Any:
            from rlo.governor import Grant
            w = quota.check(self.clock())
            if w > 0:
                return Grant(False, w)
            return super().try_acquire(est_tokens, model_)

    return AgyGovernor({model: dict(cfg.budget)}, clock=clock, provider="gemini")


# ---- the closed step list -------------------------------------------------------------------------------------------

class PlanError(ValueError):
    """A Gemini turn whose answer is not a valid ga-gemini-plan/1 — the model step fails; nothing of it runs."""


def extract_plan(text: str) -> Any:
    """The JSON object of a turn: the last ```json fenced block, or the whole text."""
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    src = blocks[-1] if blocks else text.strip()
    try:
        return json.loads(src)
    except ValueError:
        raise PlanError("not_json") from None


def check_plan(plan: Any, tools: dict[str, Any]) -> list[str]:
    """Problems of a plan against the closed tool table (labels only — no plan text is repeated)."""
    if not isinstance(plan, dict):
        return ["plan: not an object"]
    out = []
    if plan.get("schema") not in PLAN_SCHEMAS:  # ga-plan/1, or its alias ga-gemini-plan/1
        out.append("schema")
    if set(plan) - {"schema", "steps", "next", "say"}:
        out.append("unknown_field")
    steps = plan.get("steps", [])
    if not isinstance(steps, list) or len(steps) > MAX_PLAN_STEPS:
        return out + ["steps"]
    seen: list[str] = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or set(s) - {"id", "tool", "args", "after"}:
            out.append(f"steps[{i}]")
            continue
        sid = s.get("id")
        if not (isinstance(sid, str) and STEP_ID.fullmatch(sid)) or sid in seen:  # fullmatch: "a\n" is refused, as by the spec
            out.append(f"steps[{i}].id")
        if s.get("tool") not in tools:
            out.append(f"steps[{i}].tool_not_in_table")
        if not isinstance(s.get("args", {}), dict):
            out.append(f"steps[{i}].args")
        after = s.get("after", [])
        if not isinstance(after, list) or any(a not in seen for a in after):
            out.append(f"steps[{i}].after")
        seen.append(sid)
    nxt = plan.get("next")
    if nxt is not None:
        if not isinstance(nxt, dict) or set(nxt) - {"prompt", "after"} or not (isinstance(nxt.get("prompt"), str)
                                                                               and nxt["prompt"].strip()):
            out.append("next")
        elif not isinstance(nxt.get("after", []), list) or any(a not in seen for a in nxt.get("after", [])):
            out.append("next.after")
    if "say" in plan and not isinstance(plan["say"], str):
        out.append("say")
    return out


# ---- the prompts: one prompt-spec/1 file (CMD-GA26, POL-2 T2) -------------------------------------------------------

def spec_tools(tools: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The spec's ``tools`` input: each about as protocol() always wrote it — ``str(about)`` without trailing ": " — so
    the spec's ``{% if t.about %}`` row is byte-identical for any about text (a blank or colon-ended one included)."""
    return {n: {"about": f"{t.get('about', '')}".rstrip(": ")} for n, t in tools.items()}


_TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)  # rlo.pspec's tag pattern


def stray_tags(spec: Any) -> list[str]:
    """Sections whose literal text holds a ``{{`` or ``{%`` that is not a whole tag. rlo-sdk 0.9.0 / 0.9.1 read a text piece
    that starts with one as a tag and drops its last two characters (``{{ ask }x`` renders as ``{{ ask }}``), so ga
    refuses such a spec rather than send what it did not say. ga's prompts hold no literal ``{{`` or ``{%``."""
    return [name for name, body in spec.sections.items() if "{{" in _TAG.sub("", body) or "{%" in _TAG.sub("", body)]


def plan_spec() -> Any:
    """ga/specs/gemini-plan.pspec, loaded once (rlo.pspec; rlo is imported here, never by ``import ga``)."""
    global _SPEC
    if _SPEC is None:
        from rlo import pspec
        spec = pspec.load_file(SPEC_FILE)
        bad = stray_tags(spec)
        if bad:
            raise pspec.SpecError(f"sections {bad}: a {{{{ or {{% that is not a whole tag", source=str(SPEC_FILE))
        _SPEC = spec
    return _SPEC


def prompt_text(section: str, tools: dict[str, Any], task: str = "", results: list[dict[str, str]] | None = None,
                ask: str = "", mode: str = "verbatim", form: str = LEGACY_PLAN_SCHEMA) -> str:
    """One prompt from the spec: ``once`` (the protocol), ``first``, ``turn`` (a host that resumes),
    ``turn_noresume`` (agy), and for a bare plan call ``task`` / ``turn_bare`` (the user side; ``once`` is the system
    prompt); ``results`` rows are {id, tool, text}; ``form`` the plan form the prompt names (``ga gemini``'s by
    default, so its text is unchanged)."""
    from rlo import pspec
    return pspec.compile(plan_spec(), section, {"tools": spec_tools(tools), "task": task, "results": results or [],
                                                "ask": ask, "form": form}, mode)


def spec_check(plan: Any, tools: dict[str, Any]) -> list[str]:
    """The answer check from the same spec (rlo.pspec.check). check_plan stays the authority; this runs beside it. A
    ga-gemini-plan/1 answer is read as ga-plan/1 (the alias)."""
    from rlo import pspec
    if isinstance(plan, dict) and plan.get("schema") == LEGACY_PLAN_SCHEMA:
        plan = {**plan, "schema": PLAN_SCHEMA}
    return pspec.check(plan_spec(), plan, {"tools": tools})


def protocol(cfg: GeminiConfig) -> str:
    return prompt_text("once", cfg.tools, form=cfg.plan_schema)


CONVERSATION_SCHEMA = "ga-gemini-conversation/1"


def token_report(conv: dict[str, Any]) -> dict[str, Any]:
    """S5, offline (no model call): the prompts of a recorded or labelled conversation —
    {schema, label, source, tools, task, turns: [{results: [{id, tool, text}], ask, answer}]}, turn 0 the first —
    compiled for each host type, tokens per turn (rlo.pspec estimate) in three columns: ``verbatim`` (today's text),
    ``compact`` (every turn compact) and ``ga`` (what ga sends: the first turn verbatim, follow-ups compact). Each
    turn's answer is checked by check_plan and by the same-spec check. Numbers and labels only, no text."""
    from rlo import pspec
    if conv.get("schema") != CONVERSATION_SCHEMA:
        raise FormError([Problem("$.schema", f"must be {CONVERSATION_SCHEMA!r}")])
    tools, task, turns = conv["tools"], conv["task"], conv["turns"]
    out: dict[str, Any] = {"schema": "ga-gemini-token-report/1", "label": conv.get("label"),
                           "source": conv.get("source"), "turns": len(turns), "estimate": "ceil(utf-8 bytes / 4)",
                           "spec": plan_spec().name, "spec_digest": plan_spec().digest[:12], "hosts": {}}
    for host, section in (("gemini_cli", "turn"), ("agy", "turn_noresume")):
        cols: dict[str, list[str]] = {"verbatim": [], "compact": [], "ga": []}
        for k, t in enumerate(turns):
            for col in cols:
                mode = "verbatim" if col == "verbatim" else "compact"  # GA28: ga's first turn is compact too
                cols[col].append(prompt_text("first", tools, task, mode=mode) if k == 0 else
                                 prompt_text(section, tools, task, t.get("results", []), t.get("ask", ""), mode))
        rep = {c: pspec.token_report(texts) for c, texts in cols.items()}
        v, c, ga = (rep[x]["estimate"] for x in ("verbatim", "compact", "ga"))
        out["hosts"][host] = {
            "per_turn": [{"turn": k, **{x: rep[x]["turns"][k]["estimate"] for x in cols}} for k in range(len(turns))],
            "verbatim": v, "compact": c, "ga": ga,
            "saved_compact_pct": round(100 * (1 - c / v), 1) if v else 0.0,
            "saved_ga_pct": round(100 * (1 - ga / v), 1) if v else 0.0}
    out["backends"] = backend_report(conv)
    answers = [t.get("answer") for t in turns]
    plan_ok = [not check_plan(a, tools) for a in answers]
    spec_ok = [not spec_check(a, tools) for a in answers]
    out["answers"] = {"n": len(answers), "check_plan_ok": sum(plan_ok), "spec_ok": sum(spec_ok),
                      "agree": sum(p == q for p, q in zip(plan_ok, spec_ok))}
    return out


def provider_prompt_tokens(usage: Any, fmt: str | None) -> int | None:
    """The prompt tokens a provider reported (cache reads included), read by Telemetry (rlo.pspec.usage_report); when
    Telemetry cannot split the input (an otel total, a Gemini count without the cached count) the whole prompt count
    the provider gave. None when it gave none — never 0."""
    if not isinstance(usage, dict) or not usage or fmt is None:
        return None
    from rlo.pspec import usage_report
    try:
        n = (usage_report(usage, fmt) or {}).get("prompt_tokens")
    except Exception:
        n = None
    if n is None:
        for k in ("total_input_tokens", "input_tokens", "prompt_token_count", "promptTokenCount", "prompt_tokens"):
            if isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool):
                return usage[k]
    return n


def backend_prompts(backend: Any, conv: dict[str, Any], mode: str = "compact") -> list[str]:
    """What ``ga supervise`` sends per turn on a backend (form ga-plan/1): a bare one gets ``once`` as the system prompt
    and ``task`` / ``turn_bare`` as the user message (counted together); a host that resumes gets ``first`` then
    ``turn``; any other gets ``first`` then ``turn_noresume``."""
    tools, task, turns, f = conv["tools"], conv["task"], conv["turns"], PLAN_SCHEMA
    out = []
    for k, t in enumerate(turns):
        res, ask = t.get("results", []), t.get("ask", "")
        if backend.bare:
            once = prompt_text("once", tools, mode=mode, form=f)
            user = (prompt_text("task", tools, task, mode=mode, form=f) if k == 0 else
                    prompt_text("turn_bare", tools, task, res, ask, mode, f))
            out.append(once + "\n" + user)
        elif k == 0:
            out.append(prompt_text("first", tools, task, mode=mode, form=f))
        else:
            out.append(prompt_text("turn" if backend.resumes else "turn_noresume", tools, task, res, ask, mode, f))
    return out


def backend_report(conv: dict[str, Any]) -> dict[str, Any]:
    """S5: per backend — the host's fixed overhead per turn and its source, the pspec prompt tokens ga sends there
    (estimate), and the provider's usage when the conversation carries a recording for that backend
    (``usage: {<backend>: {format, turns: [usage | null]}}``, read by Telemetry). Numbers and labels only."""
    from rlo import pspec
    from . import backends
    reg, rec = backends.registry(), conv.get("usage") or {}
    out: dict[str, Any] = {}
    probe_model = {"agv": "gemini-3.8-flash-high"}
    for name, plug in reg.plugins.items():
        try:
            runner = plug.create(probe_model.get(name, "model"), {}, {"cwd": None})
        except Exception as e:  # a plugin that cannot make a runner is listed, not counted
            out[name] = {"error": type(e).__name__}
            continue
        texts = backend_prompts(runner, conv)
        u = rec.get(name) if isinstance(rec.get(name), dict) else {}
        usages = u.get("turns") if isinstance(u.get("turns"), list) and len(u["turns"]) == len(texts) else None
        rep = pspec.token_report(texts)
        prov = [provider_prompt_tokens(x, u.get("format")) for x in usages] if usages else None
        ov = dict(plug.overhead)
        out[name] = {"bare": bool(runner.bare), "resumes": bool(runner.resumes), "fixed_overhead": ov.get("tokens"),
                     "fixed_overhead_source": ov.get("source"), "closest": ov.get("closest"),
                     "pspec_prompt_tokens": rep["estimate"],
                     "per_turn": [r["estimate"] for r in rep["turns"]],
                     "provider_prompt_tokens": sum(prov) if prov and None not in prov else None,
                     "usage_format": u.get("format") if usages else None}
    return out


# ---- the supervisor -------------------------------------------------------------------------------------------------

def _atomic_write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _hhmmss(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def tool_error_label(e: BaseException) -> str:
    """A tool error as a label for the model (GA41 S2): the exception type and its first line, secret-shaped spans
    withheld, capped — never a stack."""
    from .act.card import redact
    reason = getattr(e, "reason", None)
    first = (str(reason) if isinstance(reason, str) and reason else str(e)).strip().splitlines()
    text = f"{type(e).__name__}: {first[0][:160]}" if first and first[0] else type(e).__name__
    return redact(text)[0][:200]


def _add_usage(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Two turns' usage objects as one (a plan and its repair): numbers summed, the rest from the later turn."""
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and isinstance(out.get(k), (int, float)):
            out[k] = out[k] + v
        else:
            out[k] = v
    return out


def thinking_tokens(usage: Any) -> int | None:
    """Thinking (reasoning) tokens of a turn when the provider reports them apart (GA41 S6): OpenAI's
    completion_tokens_details.reasoning_tokens / output_tokens_details, Gemini's thoughts_token_count. None otherwise."""
    if not isinstance(usage, dict):
        return None
    for k in ("thoughts_token_count", "thoughtsTokenCount", "reasoning_tokens", "thinking_tokens"):
        if isinstance(usage.get(k), int):
            return usage[k]
    for d in ("completion_tokens_details", "output_tokens_details"):
        v = usage.get(d)
        if isinstance(v, dict) and isinstance(v.get("reasoning_tokens"), int):
            return v["reasoning_tokens"]
    return None


class Supervisor:
    def __init__(self, cfg: GeminiConfig, *, cli: Any = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, out: TextIO | None = None):
        """``cli`` is the backend runner (ga.backends; a test's fake); by default the config's backend makes one —
        ga-gemini/1's ``host`` maps to agv (agy) or gemini_cli with its old options."""
        self.cfg, self.clock, self._sleep, self.out = cfg, clock, sleep, out or sys.stdout
        self.dir = cfg.state_path
        (self.dir / "results").mkdir(parents=True, exist_ok=True)
        self.supervise = cfg.form == SUPERVISE_SCHEMA
        self.cmd = "ga supervise" if self.supervise else "ga gemini"  # the command named in status lines
        self.backend, self.model = cfg.backend_name, cfg.active_model
        self.host = self.backend if self.supervise else cfg.host  # ga gemini logs its host as before
        if cli is None:
            from . import backends
            if self.supervise:
                opts = cfg.options
            elif self.backend == "agv":  # CMD-GA23: Antigravity CLI, the user's Google account
                opts = {"cli": cfg.agy["cli"]}
            else:
                opts = {"cli": cfg.cli}
            try:
                cli = backends.create(self.backend, self.model, opts, {"cwd": str(cfg.root), "timeout_s": cfg.turn_timeout_s,
                                                                    "state_dir": self.dir})
            except (KeyError, backends.ConfigError) as e:
                raise FormError([Problem("$.backend" if isinstance(e, KeyError) else "$.model",
                                         str(e).strip("'\"")[:200])]) from None
        self.cli = cli
        quota_opts = {**AGY_DEFAULT, **({k: v for k, v in cfg.options.items() if k in AGY_DEFAULT}
                                         if self.supervise else cfg.agy)}
        has_probe = callable(getattr(self.cli, "usage", None))
        self.agy_quota = (AgyQuota(self.cli, self.model, quota_opts, clock, self.log)
                          if (self.host == "agy" if not self.supervise else has_probe) else None)
        self.day = DayCount(self.dir / "day.json", cfg.daily)
        self._quota: dict[str, str] = {}  # step id -> where its wait comes from: hint · window · daily reset
        self._lock = threading.RLock()  # tool steps may run on the Scheduler's threads (K12 rev 3): one writer at a time
        self.state_file, self.log_file = self.dir / "state.json", self.dir / "log.jsonl"
        self.st: dict[str, Any] = {}
        self.sched = None
        self._clients: dict[str, Any] = {}
        self._shown: tuple = ()
        self._reasons: dict[str, str] = {}  # step id -> a failure label the Scheduler's type name would hide (GA41)

    # -- state · log --
    def load(self) -> bool:
        if not self.state_file.exists():
            return False
        st = json.loads(self.state_file.read_text(encoding="utf-8"))
        if st.get("schema") != STATE_SCHEMA:
            raise FormError([Problem("$.schema", f"{self.state_file} is not {STATE_SCHEMA}")])
        if st.get("model") != self.model:  # the model is the config's; a saved task does not switch it
            raise FormError([Problem("$.model", "the saved task was run with another model than the config's")])
        self.st = st
        return True

    def save(self) -> None:
        with self._lock:
            self.st["updated"] = round(self.clock(), 3)
            _atomic_write(self.state_file, self.st)

    def log(self, event: str, **kw: Any) -> None:
        row = {"at": round(self.clock(), 3), "event": event, **kw}
        with self._lock, open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    def say(self, text: str) -> None:
        if self.cmd != "ga gemini":
            text = text.replace("ga gemini", self.cmd)
        self.out.write(text.rstrip("\n") + "\n")
        self.out.flush()

    # -- steps --
    def _rec(self, sid: str) -> dict[str, Any]:
        return next(s for s in self.st["steps"] if s["id"] == sid)

    def _result_text(self, sid: str) -> str:
        f = self.dir / "results" / f"{sid}.json"
        if not f.exists():
            return ""
        v = json.loads(f.read_text(encoding="utf-8"))
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        return text[: self.cfg.result_cap]

    def _prompt(self, rec: dict[str, Any]) -> str:
        """The turn's prompt, compiled from the spec. The first turn is verbatim; a follow-up is ``turn`` on a host that
        resumes (the protocol and the task are not sent again) or ``turn_noresume`` (agy, U: no --resume, so the
        protocol and the task go with every turn), in the config's prompt_mode."""
        f, mode = self.cfg.plan_schema, self.cfg.prompt_mode
        if rec.get("first"):  # BD-304 (a): compact too, in the config's prompt_mode
            return prompt_text("first", self.cfg.tools, rec["prompt"], mode=mode, form=f)
        first, results = self._task_results(rec)
        section = "turn" if getattr(self.cli, "resumes", True) else "turn_noresume"
        return prompt_text(section, self.cfg.tools, first, results, rec["prompt"], mode, f)

    def _task_results(self, rec: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
        first = next((s for s in self.st["steps"] if s.get("first")), {"prompt": ""})
        return first["prompt"], [{"id": t["plan_id"], "tool": t["tool"], "text": self._result_text(a)}
                                 for a, t in ((a, self._rec(a)) for a in rec.get("needs", []))]

    def _messages(self, rec: dict[str, Any]) -> tuple[str, str]:
        """A bare plan call (CMD-GA28 S5): (system, user) — the protocol as the system prompt, the task (and the
        results and the ask on a follow-up) as the user message. A bare host keeps no conversation."""
        f, mode, tools = self.cfg.plan_schema, self.cfg.prompt_mode, self.cfg.tools
        system = prompt_text("once", tools, mode=mode, form=f)
        if rec.get("first"):
            return system, prompt_text("task", tools, rec["prompt"], mode=mode, form=f)
        task, results = self._task_results(rec)
        return system, prompt_text("turn_bare", tools, task, results, rec["prompt"], mode, f)

    def _call_tool(self, rec: dict[str, Any]) -> Any:
        t = self.cfg.tools[rec["tool"]]
        args = rec.get("args") or {}
        if "python" in t:
            import importlib
            mod, fn = t["python"].split(":")
            return getattr(importlib.import_module(mod), fn)(**args)
        from .adapters.mcp_stdio import StdioClient
        name = t["mcp"]
        with self._lock:
            if name not in self._clients:
                s = self.cfg.mcp_servers[name]
                cwd = str((self.cfg.root / s["cwd"]).resolve()) if s.get("cwd") else str(self.cfg.root)
                self._clients[name] = StdioClient(s["command"], cwd=cwd, timeout_s=self.cfg.turn_timeout_s)
            client = self._clients[name]
        return client.call(t["tool"], args)

    def _tool_fn(self, sid: str) -> Callable[[dict], str]:
        def run(_results: dict) -> str:
            rec = self._rec(sid)
            t0 = self.clock()
            try:
                value = self._call_tool(rec)
            except Exception as e:  # GA41 S2: a failed tool step is a result the next model turn sees, not a failed task
                label = tool_error_label(e)
                value = f"{rec['tool']} failed: {label}"
                with self._lock:
                    self.st["tool_errors"] = int(self.st.get("tool_errors", 0)) + 1
                self.log("tool", step=sid, tool=rec["tool"], ok=False, error=label[:120])
            preview = (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))[: self.cfg.result_cap]
            with self._lock:
                _atomic_write(self.dir / "results" / f"{sid}.json", value)
                self.st["done"].append(sid)
            if not (isinstance(value, str) and value.startswith(f"{rec['tool']} failed: ")):
                self.log("tool", step=sid, tool=rec["tool"], ok=True, chars=len(preview),
                         seconds=round(self.clock() - t0, 3))
            self.save()
            return preview  # in memory: capped
        return run

    def _model_turn(self, sid: str) -> dict[str, Any]:
        rec = self._rec(sid)
        self._quota.pop(sid, None)
        self._shown = ()  # a park after this dispatch is a new park, shown again
        if sid in self.st["parked"]:
            _at, since, *src = self.st["parked"].pop(sid)
            waited = round(self.clock() - since, 1)
            if src and src[0] in ("daily reset", "quota reset"):  # the one probe at the reset: one call, whatever it shows
                what = "daily quota" if src[0] == "daily reset" else "agy quota"
                self.say(f"[ga gemini] {what} reset: one probe ({sid}) after {waited:g} s")
                self.log("probe", step=sid, waited_s=waited)
            else:
                self.say(f"[ga gemini] resumed {sid} after {waited:g} s")
                self.log("resume", step=sid, waited_s=waited)
        bare = bool(getattr(self.cli, "bare", False))
        if bare:  # S5: the host's tools off, the protocol as its system prompt
            system, prompt = self._messages(rec)
            extra: dict[str, Any] = {"system": system}
        else:
            system, prompt, extra = "", self._prompt(rec), {}
        turn, usage, fmt = self._one_turn(sid, prompt, system, extra, bare, "plan")
        plan, probs = self._checked(sid, turn.text)
        if probs:  # GA41 S1: one repair turn with the labels, the form's reminder and the answer itself; no task text
            from . import repair
            self.st["repairs"] = int(self.st.get("repairs", 0)) + 1
            self.log("repair", step=sid, problems=probs[:5])
            prompt = repair.plan_prompt(probs, self.cfg.plan_schema, turn.text or "")
            turn, usage2, fmt = self._one_turn(sid, prompt, system, extra, bare, "repair")
            usage = _add_usage(usage, usage2)
            plan, probs = self._checked(sid, turn.text)
            if probs:
                self.log("plan", step=sid, ok=False, problems=",".join(probs[:5])[:200], repaired=False)
                raise PlanError(",".join(probs[:5]))
        try:
            self._extend(sid, plan)
        except PlanError as e:
            self.log("plan", step=sid, ok=False, problems=str(e)[:200])
            raise
        self.st["done"].append(sid)
        self.save()
        if fmt == "otel":
            return {"usage": {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens") if k in usage}}
        return {"usage": dict(usage)}  # the provider's own shape; the Scheduler's ledger reads it as usage_format

    def _checked(self, sid: str, text: str) -> tuple[Any, list[str]]:
        """The answer's plan and its problems (labels): not_json, or check_plan's (the authority)."""
        try:
            plan = extract_plan(text if isinstance(text, str) else "")
        except PlanError as e:
            return None, [str(e)]
        probs = check_plan(plan, self.cfg.tools)
        spec_probs = spec_check(plan, self.cfg.tools)  # CMD-GA26: the same-spec check, beside it; labels only
        if bool(probs) != bool(spec_probs):
            self.log("check_disagree", step=sid, check_plan_ok=not probs, spec_ok=not spec_probs)
        return plan, probs

    def _one_turn(self, sid: str, prompt: str, system: str, extra: dict[str, Any], bare: bool,
                  kind: str) -> tuple[Any, dict[str, Any], str | None]:
        """One host turn (a plan or its repair), a transient server error retried once (GA41 S3), logged."""
        from .adapters.gemini_cli import GeminiRateLimited, quota_body
        from .backends.base import Transient, retry_transient

        def retry_line(e: Transient) -> None:
            b = self.cfg.transient_backoff_s
            self.say(f"[ga gemini] {sid}: the server failed the turn ({e.reason}); one retry in {b:g} s")
            self.log("transient", step=sid, reason=e.reason, backoff_s=b, kind=kind)
        try:
            turn = retry_transient(lambda: self.cli.run_turn(prompt, self.st.get("session_id"),
                                                             on_wait=self._turn_wait(sid),
                                                             wait_every_s=self.cfg.turn_status_s, **extra),
                                   backoff_s=self.cfg.transient_backoff_s, sleep=self._sleep, on_retry=retry_line)
        except GeminiRateLimited as e:
            now = self.clock()
            if e.kind == "day":  # not a minute window: wait for the reset, then probe once
                self.day.exhaust(now)
                e.body = quota_body("day", self.day.reset_at(now) - now)
                src = "daily reset"
            elif e.kind in ("quota", "credits") and self.agy_quota is not None:  # agy: the share is spent
                if e.kind == "credits":
                    self.say("[ga gemini] agy offered or mentioned paid AI credits; ga never accepts them — "
                             "the plan quota is spent, waiting for its reset")
                e.body = quota_body("day", self.agy_quota.spent(now))
                src = "quota reset"
            else:
                src = "hint" if e.hint_s is not None else "window"
            self._quota[sid] = src
            self.log("turn", step=sid, ok=False, reason=e.reason, source=src, via=e.via, model=self.model)
            raise
        except GeminiError as e:
            self.log("turn", step=sid, ok=False, reason=e.reason, model=self.model, kind=kind)
            if isinstance(e, Transient):
                self._reasons[sid] = e.reason  # the step fails as transient:<code>
            raise
        self.st["session_id"] = turn.session_id or self.st.get("session_id")
        from rlo.pspec import tokens as est
        usage = turn.usage if isinstance(turn.usage, dict) else {}
        fmt = getattr(turn, "usage_format", None) or ("otel" if usage else None)
        self.log("turn", step=sid, ok=True, served=turn.served, model=self.model, seconds=turn.seconds,
                 tokens=usage.get("total_tokens"), input_tokens=usage.get("input_tokens"),
                 output_tokens=usage.get("output_tokens", usage.get("completion_tokens")),  # GA36: ga ask --solve lines
                 provider_prompt_tokens=provider_prompt_tokens(usage, fmt), usage_format=fmt, bare=bare,
                 prompt_est=est(prompt) + (est(system) if system else 0), prompt_mode=self.cfg.prompt_mode,
                 kind=kind, thinking_tokens=thinking_tokens(usage))
        denied = list(getattr(turn, "denied", []) or [])
        if denied:  # agy refused tool calls inside the turn (V: denied_actions); reported, labels only
            self.say(f"[ga gemini] agy refused {len(denied)} action(s) in {sid}: {', '.join(denied[:8])}")
            self.log("denied", step=sid, count=len(denied), labels=sorted(set(denied))[:8])
        return turn, usage, fmt

    def _turn_wait(self, sid: str) -> Callable[[float], None]:
        """S7: a turn that runs long may be the CLI retrying a quota error inside the process (the preview model's
        policy allows 10 attempts, whatever general.maxAttempts says) — say so, with the state, once per interval."""
        def line(seconds: float) -> None:
            st = self.status()
            self.say(f"[ga gemini] turn {sid} running {seconds:g} s; the CLI may be retrying a quota error inside the turn"
                     f" · done {len(st['done'])} · running {len(st['running'])} · parked {len(st['parked'] or [])}")
            self.log("turn_wait", step=sid, seconds=seconds)
        return line

    def _extend(self, sid: str, plan: dict[str, Any]) -> None:
        """Put a plan's tool steps and its next model step into the state and the running scheduler."""
        from rlo.scheduler import Step
        task, n = self.st["task"], self.st["model_steps"]
        ids = {s["id"]: f"{task}.r{n}.{s['id']}" for s in plan.get("steps", [])}
        new = [{"id": ids[s["id"]], "kind": "tool", "plan_id": s["id"], "tool": s["tool"], "args": s.get("args") or {},
                "after": [ids[a] for a in s.get("after", [])]} for s in plan.get("steps", [])]
        nxt = plan.get("next")
        if nxt is not None:
            if n >= self.cfg.max_model_steps:
                raise PlanError("max_model_steps")
            needs = [ids[a] for a in nxt.get("after", [])]
            new.append({"id": f"{task}.m{n + 1}", "kind": "model", "prompt": nxt["prompt"], "after": needs, "needs": needs})
            self.st["model_steps"] = n + 1
        if isinstance(plan.get("say"), str) and plan["say"].strip():
            self.st["say"] = plan["say"][:2000]
        self.st["steps"].extend(new)
        self.log("plan", step=sid, ok=True, tool_steps=len(plan.get("steps", [])), next=nxt is not None)
        for r in new:
            self.sched.add(self._step(r, Step))

    def _step(self, rec: dict[str, Any], Step: Any) -> Any:
        after = tuple(rec["after"])
        if rec["kind"] == "model":
            return Step(rec["id"], MODEL_STEP, payload=rec["id"], after=after, est_tokens=self.cfg.est_tokens,
                        model=self.model)
        return Step(rec["id"], rec["tool"], fn=self._tool_fn(rec["id"]), after=after)

    # -- status: K12 rev 2's status object, plus where the wait comes from --
    def status(self) -> dict[str, Any]:
        """The scheduler's status() {resumes_in_s, done, running, parked, next}, with ``running`` as a list,
        ``resumes_in_s`` inf for a wait that never opens, ``eta_source`` (hint: a "retry in N s" in the error;
        window: the Governor's minute window; daily reset: today's requests are spent) and ``left_today``."""
        s = self.sched
        st = dict(s.status())
        st["running"] = [st["running"]] if isinstance(st.get("running"), str) else list(st.get("running") or [])
        parked = list(st["parked"] or [])
        if parked and st.get("resumes_in_s") is None:
            st["resumes_in_s"] = math.inf  # parked with no window that opens (the daily quota)
        st["resumes_in_s"] = st.get("resumes_in_s") or 0.0
        now = self.clock()
        src = next((self._quota[k] for k in parked if k in self._quota), None) or next(
            (self.st["parked"][k][2] for k in parked if len(self.st.get("parked", {}).get(k, [])) > 2), None)
        if self.agy_quota is not None:
            st["left_today"] = None
            st["share_pct"] = (self.agy_quota.info or {}).get("remaining_pct")
            st["eta_source"] = ("quota reset" if parked and self.agy_quota.wait(now) > 0 else (src or "window"))
            return st
        st["left_today"] = self.day.left(now)
        st["eta_source"] = "daily reset" if st["left_today"] == 0 and parked else (src or "window")
        return st

    def _sleep_hook(self, d: float) -> None:
        """The scheduler sleeps only when nothing can run: a model step is parked. Show it once, save, then sleep."""
        st = self.status()
        parked = tuple(st["parked"] or ())
        key = parked + (st["eta_source"],)
        if parked and key != self._shown:
            now = self.clock()
            eta = st["resumes_in_s"]
            for sid in parked:
                since = self.st["parked"].get(sid, [None, round(now, 3)])[1]
                self.st["parked"][sid] = [None if not math.isfinite(eta) else round(now + eta, 3), since, st["eta_source"]]
            self.save()
            left, limit = st.get("left_today"), self.day.limit
            self.log("park", steps=list(parked), resumes_in_s=None if not math.isfinite(eta) else round(eta, 1),
                     source=st["eta_source"], left_today=left, done=len(st["done"]), running=len(st["running"] or []))
            ids = ", ".join(parked)
            if not math.isfinite(eta):
                head = f"[ga gemini] quota: {ids} parked — no window opens"
            elif st["eta_source"] == "quota reset":  # agy: the weekly (or 5-hour) share of the model's family
                q = self.agy_quota
                h, m = divmod(math.ceil(eta / 60), 60)
                days, h = divmod(h, 24)  # not `d`: that is the sleep the scheduler asked for
                share = "unknown" if st.get("share_pct") is None else f"{st['share_pct']:g}%"
                head = (f"[ga gemini] agy quota ({q.family}, {q.window_kind}): {ids} parked — {share} left "
                        f"(floor {q.floor:g}%); resets at {time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(now + eta))}, "
                        f"in {days} d {h} h {m} min; one probe then")
            elif st["eta_source"] == "daily reset":
                h, m = divmod(math.ceil(eta / 60), 60)
                head = (f"[ga gemini] daily quota: {ids} parked — the quota resets at {self.day.at} {self.day.tz}, in "
                        f"{h} h {m} min (at {_hhmmss(now + eta)} here); one probe then")
            else:
                head = (f"[ga gemini] quota: {ids} parked — resumes in {math.ceil(eta)} s, at {_hhmmss(now + eta)} "
                        f"({st['eta_source']})")
            nxt = ", ".join(f"{x['id']} ({x['kind']})" for x in (st["next"] or [])[:6]) or "-"
            self.say("\n".join([
                head,
                f"  now:  done {len(st['done'])} · running {len(st['running'] or [])} · parked {len(parked)} · "
                + (f"agy share left {'unknown' if st.get('share_pct') is None else format(st['share_pct'], 'g') + '%'}"
                   if self.agy_quota is not None else
                   f"requests today {self.day.used} (no daily cap)" if left is None else
                   f"requests left today {left}/{limit}" if limit is not None else
                   "requests left today 0 (the server's daily quota)"),
                f"  next: {nxt}",
                f"  saved: {self.state_file} — after a crash or a closed terminal: ga gemini --resume",
            ]))
            self._shown = key
        self._sleep(d)

    # -- running a task --
    def start(self, prompt: str) -> bool:
        prev = self.st if self.st else ({} if not self.load() else self.st)
        if prev.get("status") in ("running", "parked"):
            self.say(f"[ga gemini] {prev['task']} is not finished — ga gemini --resume continues it first")
            return False
        task = f"T{int(prev.get('tasks', 0)) + 1}"
        self.st = {"schema": STATE_SCHEMA, "model": self.model, "session_id": prev.get("session_id"),
                   "tasks": int(prev.get("tasks", 0)) + 1, "task": task, "status": "running", "model_steps": 1,
                   "steps": [{"id": f"{task}.m1", "kind": "model", "first": True, "prompt": prompt, "after": []}],
                   "done": [], "failed": {}, "parked": {}, "say": ""}
        self.save()
        self.log("task", task=task, model=self.model, host=self.host, prompt_mode=self.cfg.prompt_mode)
        return self._execute()

    def resume(self) -> bool:
        if not self.load():
            self.say(f"[ga gemini] nothing to resume: no {self.state_file}")
            return False
        if self.st.get("status") not in ("running", "parked"):
            self.say(f"[ga gemini] nothing to resume: {self.st.get('task')} is {self.st.get('status')}")
            return self.st.get("status") == "done"
        self.log("resume_task", task=self.st["task"])
        return self._execute()

    def _execute(self) -> bool:
        from rlo.scheduler import Scheduler, Step
        gov = (agy_governor(self.cfg, self.clock, self.agy_quota, self.model) if self.agy_quota is not None
               else daily_governor(self.cfg, self.clock, self.day, self.model))
        kinds = {MODEL_STEP: "model", **{t: "tool" for t in self.cfg.tools}}
        # K12 rev 2 (S6): the scheduler saves its queue, results and Governor windows to this file on every row and,
        # when it exists, loads it — given the same steps again, so all of them are added, done ones too
        extra = {}
        if self.cfg.max_parallel is not None:  # S8: K12 rev 3 takes it; an earlier Scheduler runs tool steps one by one
            if "max_parallel" in inspect.signature(Scheduler).parameters:
                extra["max_parallel"] = self.cfg.max_parallel
            else:
                self.log("max_parallel_pending", max_parallel=self.cfg.max_parallel)
        self.sched = Scheduler([self._step(r, Step) for r in self.st["steps"]], gov, self._model_turn, kinds=kinds,
                               clock=self.clock, sleep=self._sleep_hook, ledger=str(self.dir / "ledger.jsonl"),
                               run_id=self.st["task"], provider_name=self.backend if self.supervise else "gemini",
                               usage_format=getattr(self.cli, "usage_format", None) or "otel",
                               state=str(self.dir / f"scheduler-{self.st['task']}.json"), **extra)
        self._shown = ()
        try:
            report = self.sched.run()
        finally:
            for c in self._clients.values():
                c.close()
            self._clients = {}
        self.st["failed"] = {k: self._reasons.get(k, v) for k, v in report.failed.items()}
        self.st["status"] = "done" if report.ok else ("parked" if report.parked and not report.failed else "failed")
        self.save()
        models = sum(1 for r in self.st["steps"] if r["kind"] == "model" and r["id"] in self.st["done"])
        tools = sum(1 for r in self.st["steps"] if r["kind"] == "tool" and r["id"] in self.st["done"])
        self.log("end", task=self.st["task"], status=self.st["status"], model_turns=models, tool_steps=tools,
                 failed=len(report.failed), skipped=len(report.skipped), slept_s=report.slept_s, sleeps=report.sleeps,
                 repairs=int(self.st.get("repairs", 0)), tool_errors=int(self.st.get("tool_errors", 0)))
        head = (f"[ga gemini] {self.st['task']} {self.st['status']}: {models} model turn(s), {tools} tool step(s), "
                f"{len(report.failed)} failed, waited {report.slept_s:g} s")
        if report.failed:
            head += " — failed: " + ", ".join(f"{k} ({v})" for k, v in self.st["failed"].items())
        self.say(head + (("\n" + self.st["say"]) if self.st.get("say") else ""))
        return report.ok


# ---- `ga gemini` ----------------------------------------------------------------------------------------------------

def main(args: Any) -> int:
    if getattr(args, "token_report", None):  # offline, no config and no model call
        try:
            conv = json.loads(Path(args.token_report).read_text(encoding="utf-8"))
            print(json.dumps(token_report(conv), indent=1, sort_keys=True))
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"token report: cannot read {args.token_report}: {type(e).__name__}", file=sys.stderr)
            return 2
        except FormError as e:
            for p in e.problems:
                print(f"token report: {p}", file=sys.stderr)
            return 2
        return 0
    try:
        cfg = load_config(args.gemini_config)
    except FormError as e:
        for p in e.problems:
            print(f"config: {p}", file=sys.stderr)
        return 2
    if getattr(args, "host", None):
        cfg.host = args.host
    return _run(cfg, args, "ga gemini")


def supervise_main(args: Any) -> int:
    """``ga supervise --backend <name> --config <file>`` (CMD-GA28 S4): a ga-supervise/1 config, or a ga-gemini/1 one
    read as ``ga gemini`` reads it (its host is the backend: agy -> agv, gemini_cli)."""
    if getattr(args, "token_report", None):
        return main(args)
    if getattr(args, "list_backends", False):
        from . import backends
        print(json.dumps(backends.registry().listing(), ensure_ascii=False, indent=1))
        return 0
    try:
        cfg = load_config(args.config, getattr(args, "backend", None))
        if cfg.form == CONFIG_SCHEMA and args.backend:
            from . import backends
            if backends.ALIASES.get(args.backend, args.backend) != cfg.backend_name:
                raise FormError([Problem("$.host", f"the {CONFIG_SCHEMA} config runs {cfg.backend_name}, not "
                                                   f"{args.backend}; use a {SUPERVISE_SCHEMA} config for another backend")])
    except FormError as e:
        for p in e.problems:
            print(f"config: {p}", file=sys.stderr)
        return 2
    return _run(cfg, args, "ga supervise")


def _run(cfg: GeminiConfig, args: Any, cmd: str) -> int:
    if getattr(args, "prompt_mode", None):
        cfg.prompt_mode = args.prompt_mode
    try:
        sup = Supervisor(cfg)
        sup.cmd = cmd
    except FormError as e:
        for p in e.problems:
            print(f"config: {p}", file=sys.stderr)
        return 2
    try:
        if args.resume:
            return 0 if sup.resume() else 1
        if args.prompt:
            return 0 if sup.start(" ".join(args.prompt)) else 1
        ok = True
        while True:  # the terminal front: one prompt in, the status and the outcome out
            try:
                line = input(f"{cmd}> ")
            except EOFError:
                return 0 if ok else 1
            if line.strip():
                ok = sup.start(line.strip()) and ok
    except FormError as e:
        for p in e.problems:
            print(f"state: {p}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"\n[{cmd}] stopped; the state is saved — {cmd} --resume continues", file=sys.stderr)
        return 130
