"""Model backends. Real: a bare ``claude -p --output-format json`` (BD-302: tools off, system prompt replaced). Fake: replays
recorded usage JSON for the offline tests (0 model calls). The prompt goes in on stdin (argv is limited to 128 KiB)."""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ga.backends.base import BackendError, check_served


@dataclass
class CallResult:
    text: str
    usage: dict[str, Any]
    total_cost_usd: float | None
    served: list[str]
    seconds: float = 0.0


class ClaudeBackend:
    real = True

    def __init__(self, command: list[str] | None = None, timeout_s: float = 600.0):
        self.command, self.timeout_s = command or ["claude"], timeout_s

    def argv(self, model: str, system: str) -> list[str]:
        return self.command + ["-p", "--output-format", "json", "--model", model, "--no-session-persistence",
                               "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--system-prompt", system]

    def call(self, model: str, system: str, prompt: str, image: str | None = None) -> CallResult:
        """``image``: path of a PNG; the prompt gets ``@path`` (claude -p attaches it; the node must have the vision capability)."""
        stdin = prompt + (f"\n\n@{image}" if image else "")
        t0 = time.monotonic()
        try:
            p = subprocess.run(self.argv(model, system), input=stdin, capture_output=True, text=True,
                               timeout=self.timeout_s)
        except subprocess.TimeoutExpired as e:
            raise BackendError("timeout") from e
        except OSError as e:
            raise BackendError(f"cannot_run:{e.__class__.__name__}") from e
        return parse(p.stdout, p.returncode, model, time.monotonic() - t0)


def parse(stdout: str, code: int, model: str, seconds: float = 0.0) -> CallResult:
    try:
        data = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise BackendError(f"exit_{code}" if code else "bad_json")
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        raise BackendError(f"is_error:{str(data.get('subtype', '?'))[:40]}:{str(data.get('result') or '')[:120]}")
    mu = data.get("modelUsage")
    served = sorted(str(k) for k in mu) if isinstance(mu, dict) else []
    usage = data.get("usage")
    if not isinstance(usage, dict):
        raise BackendError("no_usage")
    return CallResult(str(data.get("result") or ""), usage, data.get("total_cost_usd"), served, seconds)


@dataclass
class FakeBackend:
    """Replays ``script``: a list of CallResult-like dicts {text, usage, total_cost_usd} (or a callable
    (model, system, prompt, image) -> that dict). Every call is kept in ``calls`` (prompt included, for the tests)."""
    script: list[dict[str, Any]] | Callable[..., dict[str, Any]]
    real: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    def call(self, model: str, system: str, prompt: str, image: str | None = None) -> CallResult:
        n = len(self.calls)
        self.calls.append({"model": model, "system": system, "prompt": prompt, "image": image is not None})
        r = self.script(model, system, prompt, image) if callable(self.script) else self.script[n % len(self.script)]
        return CallResult(r["text"], r["usage"], r.get("total_cost_usd"), [model], 0.0)
