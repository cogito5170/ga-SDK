"""The backend protocol (CMD-GA28 S1): what ga's supervisor loop asks of a model host, and nothing more.

A backend plugin (entry point group ``ga.backends``, plugin API ``API_VERSION``) is an object — a class is made with no
arguments — with ``name`` (the entry point's name), ``version``, ``api`` and

    create(model, options, ctx) -> runner     ctx: {cwd, timeout_s, state_dir}; a bad model or option raises ConfigError
    overhead: dict                            {bare, tokens, source, closest} — the fixed per-turn cost of the host (S5)

and the runner it makes has

    resumes: bool                             the host keeps the conversation (a follow-up does not resend the protocol)
    bare: bool                                plan calls go with the host's own tools off and ``system`` as its system
                                              prompt (S5); a runner that is not bare gets the whole prompt as ``prompt``
    run_turn(prompt, session, *, system=None, on_wait=None, wait_every_s=None) -> BackendTurn
    usage() -> {family: {remaining_pct, reset_at, window}}   optional: a quota probe that spends nothing
    quota_family: str                         with usage(): the family whose share gates the model step

A turn that fails raises BackendError; a quota or rate limit raises RateLimited (``kind`` minute · day · quota ·
credits, the signal the loop waits on); a turn served by another model than the configured one raises ModelMismatch —
for every backend and model family (``check_served``). The loop, state file, wait-and-resume, plan check and prompts
are ga's and do not depend on the backend. Standard library only.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..adapters.gemini_cli import GeminiError, GeminiRateLimited, ModelMismatch, quota_body

API_VERSION = 1
USAGE_FORMATS = ("anthropic", "openai", "gemini", "otel")  # Telemetry's l0_usage names (rlo.pspec.usage_report)

BackendError = GeminiError        # a failed turn; ``reason`` is a label, never provider text
RateLimited = GeminiRateLimited   # kind minute · day (agv: quota · credits); status/body as rlo's Governor reads them

__all__ = ["API_VERSION", "USAGE_FORMATS", "BackendError", "RateLimited", "ModelMismatch", "ConfigError",
           "BackendTurn", "check_served", "rate_limited", "quota_body", "Transient", "transient_code",
           "TRANSIENT_BACKOFF_S", "retry_transient"]

# CMD-GA41 S3: the server's own passing trouble (500 INTERNAL, 502, 503 UNAVAILABLE / MODEL_CAPACITY_EXHAUSTED, 504,
# 529 overloaded) is ``transient``: the loop retries the turn once after a short backoff, then the step fails with the
# reason ``transient:<code>``. A quota or rate limit is not transient (RateLimited: parked as before).
TRANSIENT_BACKOFF_S = 20.0
TRANSIENT_STATUS = (500, 502, 503, 504, 529)
_TRANSIENT_TEXT = (
    (re.compile(r"MODEL_CAPACITY_EXHAUSTED|(?-i:\bUNAVAILABLE\b)|\b503\b.{0,40}\bcapacity|\bcapacity\b.{0,40}\b503\b"
                r"|\(code 503\)", re.I), 503),
    (re.compile(r"(?-i:\bINTERNAL\b)|\(code 500\)|\binternal server error\b", re.I), 500),
    (re.compile(r"\boverloaded(?:_error)?\b|\(code 529\)", re.I), 529),
    (re.compile(r"(?-i:\bBAD_GATEWAY\b)|\(code 502\)", re.I), 502),
    (re.compile(r"(?-i:\bDEADLINE_EXCEEDED\b|\bGATEWAY_TIMEOUT\b)|\(code 504\)", re.I), 504),
)


class ConfigError(ValueError):
    """A backend's model or options cannot run (e.g. an agv slug whose family ga does not know): refused, not guessed."""


@dataclass
class BackendTurn:
    answer: str
    served: list[str]
    usage: dict[str, Any] | None = None      # the provider's own usage object, as it gave it (None = not reported)
    usage_format: str | None = None          # one of USAGE_FORMATS: how ``usage`` is read
    resume: str | None = None                # a handle the host resumes from, or None
    seconds: float = 0.0
    events: int = 0
    denied: list[str] = field(default_factory=list)

    # the names ga's loop read before GA28 (GeminiTurn), so one loop reads both
    @property
    def text(self) -> str:
        return self.answer

    @property
    def session_id(self) -> str | None:
        return self.resume


def check_served(served: list[str], model: str) -> None:
    """The one served-model rule: the host must say which model served, and it must be exactly the configured one."""
    if not served:
        raise ModelMismatch("served_model_unknown")
    if any(m != model for m in served):
        raise ModelMismatch("served_model_mismatch")


def rate_limited(retry_after: Any = None) -> RateLimited:
    """A per-minute rate limit with the host's Retry-After seconds as the hint (None when it gave none)."""
    try:
        hint = float(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        hint = None
    return RateLimited("minute", hint if hint is not None and hint >= 0 else None, via="status")


class Transient(GeminiError):
    """A turn the server failed for a passing reason (S3): ``code`` 500 · 502 · 503 · 504 · 529; reason
    ``transient:<code>``. A label only, never provider text."""

    def __init__(self, code: int):
        super().__init__(f"transient:{int(code)}", int(code))
        self.code = int(code)


def transient_code(text: str = "", status: int | None = None) -> int | None:
    """The transient class of an error: an HTTP status in TRANSIENT_STATUS, or a server text (agy's 'API error
    (attempt 3): INTERNAL (code 500)', 503 MODEL_CAPACITY_EXHAUSTED, Anthropic's overloaded_error); None otherwise."""
    if isinstance(status, int) and status in TRANSIENT_STATUS:
        return status
    for rx, code in _TRANSIENT_TEXT:
        if text and rx.search(text):
            return code
    return None


def retry_transient(call: Callable[[], Any], *, backoff_s: float = TRANSIENT_BACKOFF_S,
                    sleep: Callable[[float], None] = time.sleep,
                    on_retry: Callable[[Transient], None] | None = None) -> Any:
    """``call()``; on Transient once more after ``backoff_s``; a second Transient is raised (the step fails as
    ``transient:<code>``). Exactly one retry: never zero, never more."""
    try:
        return call()
    except Transient as e:
        if on_retry is not None:
            on_retry(e)
        sleep(float(backoff_s))
    return call()
