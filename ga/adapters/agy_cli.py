"""Antigravity CLI (agy), headless (CMD-GA23, BD-234/255): one ga model step is one short-lived ``agy`` process.

    agy -p <prompt> --output-format stream-json --model <slug>

Facts, as baseline gave them from GMG's bench/preview/AGY.md (baseline#12 5971559909). V verified, A assumption,
U unknown. Every shape below that is A or U is read in one place (``parse_output``) and settled by the user's Mac run.
- V: ``-p``, ``--output-format text|json|stream-json``, ``--model <slug>``; an unknown ``--model`` fails hard (no
  silent substitution). The slug ga pins is ``gemini-3.8-flash-high`` (user decision, BD-255).
- V: a model or agent failure prints an ``AGY_ERROR`` line and exits with code 3 (here the exit code is meaningful).
- A: a quota stop ends the turn the same way (``AGY_ERROR``, exit 3); U: a distinct error class. agy stops at once on a
  cap, it does not retry inside the turn (V).
- V: past the plan quota agy can offer paid **AI credits** ("Use AI Credits"), or says "Your AI credits balance is too
  low to continue". ga never accepts or enables credits: either text is the quota spent (``AgyQuota(kind='credits')``).
- V: ``denied_actions`` appears in the JSON output when a tool call is refused; U: the entry shape.
- A: the served model is a ``model`` field (a string, or an object with ``display_name`` / ``name`` / ``id``) in the
  json / stream-json output (V: the status line has ``.model.display_name``). Any other model is a failed turn.
- U: the session id and ``--resume``: ``resumes`` is False, so the supervisor makes every prompt self-contained.
- V: ``agy -p /usage`` (and ``/quota``) prints the quota without spending any: a weekly share per model family (user's
  Mac: Gemini 96% remaining, reset 2026-10-11 00:56 KST; Claude and GPT 100%, reset 2026-10-11 01:29 KST). U: the
  exact text, so ``parse_usage`` is lenient: a family, a percent, a date-time with a zone.
- GA28 (S3): agv is a multi-family host — the slug may be ``gpt-*``, ``claude-*`` or ``gemini-*``; ``slug_family`` reads
  the family (any other prefix is a config error, never a guess), which picks the usage format (openai · anthropic ·
  gemini, read by Telemetry) and the quota family (``family_of``: gemini, or the shared claude_gpt share). A: a
  ``usage`` (or ``usageMetadata``) object in the json / stream-json output, in the family's provider shape; the last
  one seen is the turn's. U: whether agy prints one at all — None (not reported) when it does not.
- GA28 (S3): the command is ``agy`` (this file's facts: GMG's bench/preview/AGY.md); the user calls the host agv. No
  Antigravity CLI is installed where GA28 was built, so ``agy --help`` could not be read offline; ``agv`` is accepted
  as the backend name and ``agy`` as its alias, and ``cli`` in the options sets the command.
- V: auth is the system keyring or a browser Google sign-in; ga passes nothing for auth and never reads the token store.
Standard library only.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from .gemini_cli import GeminiError, GeminiRateLimited, GeminiTurn, ModelMismatch, clean_env, quota_body

DEFAULT_MODEL = "gemini-3.8-flash-high"
CREDITS = re.compile(r"(?i)\bAI credits\b")  # V: "Use AI Credits" / "Your AI credits balance is too low to continue"
QUOTA_WORDS = re.compile(r"(?i)\b(quota|usage limit|rate limit|limit reached|reached (?:the|your) limit|exhausted)\b")


class AgyQuota(GeminiRateLimited):
    """The agy quota is spent (``kind`` 'quota'), or agy offered or mentioned paid AI credits (``kind`` 'credits',
    never accepted). Either parks the model step until the reset agy reports, then one probe."""

    def __init__(self, kind: str):
        GeminiError.__init__(self, f"agy_{kind}", 429, quota_body("day", None))
        self.kind, self.hint_s, self.via = kind, None, "agy"


@dataclass
class AgyOutput:
    deltas: list[str] = field(default_factory=list)  # streamed pieces
    full: str = ""                                   # the last whole answer, if any event carries one
    served: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)  # labels of refused actions (U: the entry shape)
    usage: dict | None = None                        # A: the provider usage object, if agy prints one
    agy_error: bool = False
    credits: bool = False
    quota: bool = False
    events: int = 0

    @property
    def text(self) -> str:
        return self.full or "".join(self.deltas)


def _model_name(v: Any) -> str | None:
    if isinstance(v, str) and v:
        return v
    if isinstance(v, dict):
        for k in ("display_name", "name", "id", "slug"):
            if isinstance(v.get(k), str) and v[k]:
                return v[k]
    return None


def _denied_label(d: Any) -> str:
    if isinstance(d, dict):
        for k in ("tool", "tool_name", "name", "action", "type"):
            if isinstance(d.get(k), str) and re.match(r"^[A-Za-z0-9_.:+-]{1,40}$", d[k]):
                return d[k]
    return "action"


def _walk(obj: Any, out: AgyOutput) -> None:
    """Collect text, model and denied_actions from one JSON value (A/U shapes: looked for at any depth)."""
    if isinstance(obj, dict):
        m = _model_name(obj.get("model"))
        if m and m not in out.served:
            out.served.append(m)
        for uk in ("usage", "usageMetadata"):
            if isinstance(obj.get(uk), dict):
                out.usage = dict(obj[uk])
        if isinstance(obj.get("denied_actions"), list):
            out.denied += [_denied_label(d) for d in obj["denied_actions"]]
        if obj.get("role") in (None, "assistant", "model"):
            for k in ("text", "content", "response", "result", "message"):
                v = obj.get(k)
                if isinstance(v, str):
                    if obj.get("delta"):
                        out.deltas.append(v)
                    else:
                        out.full = v
                    break
        for k, v in obj.items():
            if k not in ("model", "denied_actions", "usage", "usageMetadata") and isinstance(v, (dict, list)):
                _walk(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out)


def parse_output(stdout: str, stderr: str = "") -> AgyOutput:
    """agy's json / stream-json (one JSON value or JSONL) plus the plain lines it prints (AGY_ERROR, credits)."""
    out = AgyOutput()
    whole = stdout.strip()
    try:  # --output-format json: one value
        _walk(json.loads(whole), out)
        out.events = 1
    except ValueError:
        for line in stdout.splitlines():  # stream-json: one event per line
            line = line.strip()
            if line.startswith("{"):
                try:
                    _walk(json.loads(line), out)
                    out.events += 1
                except ValueError:
                    pass
    plain = "\n".join(x for x in (stdout + "\n" + stderr).splitlines() if not x.strip().startswith("{"))
    out.agy_error = "AGY_ERROR" in plain
    out.credits = bool(CREDITS.search(stdout + "\n" + stderr))
    out.quota = out.agy_error and bool(QUOTA_WORDS.search(plain))
    return out


# ---- /usage (V: no quota spent; U: the text) ------------------------------------------------------------------------

ZONES = {"UTC": 0, "GMT": 0, "Z": 0, "KST": 9, "JST": 9, "PST": -8, "PDT": -7, "MST": -7, "MDT": -6, "CST": -6,
         "CDT": -5, "EST": -5, "EDT": -4, "CET": 1, "CEST": 2, "BST": 1, "IST": 5.5}
_DT = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})(?::(\d{2}))?\s*(Z|[+-]\d{2}:?\d{2}|[A-Z]{1,4})?")
_PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def parse_reset(text: str) -> float | None:
    """The first 'YYYY-MM-DD HH:MM[:SS] ZONE' (or ISO with an offset) in ``text`` -> epoch seconds; None if absent or
    the zone is unknown."""
    m = _DT.search(text)
    if not m:
        return None
    day, hm, sec, zone = m.group(1), m.group(2), m.group(3) or "00", m.group(4)
    if zone is None:
        return None
    if zone in ZONES:
        off = timedelta(hours=ZONES[zone])
    elif re.match(r"^[+-]\d{2}:?\d{2}$", zone):
        sign = 1 if zone[0] == "+" else -1
        hh, mm = int(zone[1:3]), int(zone[-2:])
        off = sign * timedelta(hours=hh, minutes=mm)
    else:
        return None
    h, mi = hm.split(":")
    dt = datetime.strptime(f"{day} {int(h):02d}:{mi}:{sec}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(off))
    return dt.timestamp()


FAMILIES = {"gpt": "openai", "claude": "anthropic", "gemini": "gemini"}  # slug family -> its usage format


def slug_family(model: str) -> str:
    """gpt · claude · gemini from an agv slug's prefix; any other slug raises ValueError (a config error, S3)."""
    fam = model.lower().split("-", 1)[0] if isinstance(model, str) and "-" in model else ""
    if fam not in FAMILIES:
        raise ValueError(f"agv: unknown model family for slug {model!r} (known: gpt-*, claude-*, gemini-*)")
    return fam


def family_of(model: str) -> str:
    """The quota family of a slug: agy's /usage shows one share for Gemini and one shared by Claude and GPT (V)."""
    m = model.lower()
    return "gemini" if m.startswith("gemini") else ("claude_gpt" if m.startswith(("claude", "gpt")) else m.split("-")[0])


def parse_usage(text: str) -> dict[str, dict[str, Any]]:
    """family -> {remaining_pct, reset_at, window} from /usage text, one family per line (lenient: U)."""
    out: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        low = line.lower()
        fam = "gemini" if "gemini" in low else ("claude_gpt" if ("claude" in low or "gpt" in low) else None)
        pct = _PCT.search(line)
        if fam is None or pct is None:
            continue
        used = re.search(r"(?i)\bused\b", line) and not re.search(r"(?i)\b(left|remaining)\b", line)
        remaining = 100.0 - float(pct.group(1)) if used else float(pct.group(1))
        window = "5-hour" if re.search(r"(?i)5[- ]?h(our)?", line) else ("weekly" if re.search(r"(?i)week", line) else None)
        out.setdefault(fam, {"remaining_pct": remaining, "reset_at": parse_reset(line), "window": window})
    return out


class AgyCLI:
    resumes = False  # U: no known --resume; the supervisor sends self-contained prompts

    def __init__(self, command: list[str] | None = None, model: str = DEFAULT_MODEL, *, cwd: str | None = None,
                 env: dict[str, str] | None = None, timeout_s: float = 600.0, settings_dir: Any = None):
        self.command = list(command or ["agy"])
        self.model, self.cwd, self.timeout_s = model, cwd, timeout_s
        self.env = clean_env() if env is None else dict(env)

    def argv(self, prompt: str, session_id: str | None = None) -> list[str]:
        return self.command + ["-p", prompt, "--output-format", "stream-json", "--model", self.model]

    def _run(self, argv: list[str], on_wait: Callable[[float], None] | None = None,
             wait_every_s: float | None = None) -> tuple[str, str, int]:
        t0 = time.monotonic()
        try:
            p = subprocess.Popen(argv, cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        except FileNotFoundError:
            raise GeminiError("cli_not_found") from None
        step = wait_every_s if on_wait is not None and wait_every_s else None
        while True:
            left = self.timeout_s - (time.monotonic() - t0)
            try:
                out, err = p.communicate(timeout=max(0.0, min(left, step) if step else left))
                return out, err, p.returncode
            except subprocess.TimeoutExpired:
                if time.monotonic() - t0 >= self.timeout_s:
                    p.kill()
                    p.communicate()
                    raise GeminiError("timeout") from None
                if step:
                    on_wait(round(time.monotonic() - t0, 1))

    def usage(self) -> dict[str, dict[str, Any]]:
        """``agy -p /usage`` (V: spends no quota), parsed per family. Credits text in it is never acted on."""
        out, err, _code = self._run(self.command + ["-p", "/usage"])
        return parse_usage(out + "\n" + err)

    def run_turn(self, prompt: str, session_id: str | None = None, *, on_wait: Callable[[float], None] | None = None,
                 wait_every_s: float | None = None) -> GeminiTurn:
        t0 = time.monotonic()
        stdout, stderr, code = self._run(self.argv(prompt), on_wait, wait_every_s)
        o = parse_output(stdout, stderr)
        if o.credits:  # never accept paid credits: the plan quota is spent
            raise AgyQuota("credits")
        if o.quota:
            raise AgyQuota("quota")
        if code != 0 or o.agy_error:
            raise GeminiError("agy_error" if o.agy_error or code == 3 else f"exit_{code}")
        from ..backends.base import check_served
        check_served(o.served, self.model)
        turn = GeminiTurn(None, list(o.served), o.text, {}, round(time.monotonic() - t0, 3), o.events)
        turn.raw_usage = o.usage  # type: ignore[attr-defined]
        turn.denied = list(o.denied)  # type: ignore[attr-defined]
        return turn
