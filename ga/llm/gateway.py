"""The one model gateway (R1): ``call(card, purpose, item_id, model_hint)``.

A card is a role plus evidence — no history parameter. Before a backend is touched the gateway (1) answers from the rule
cache when the fingerprint was answered before, (2) checks the card size, (3) re-reads the policy and checks every
applicable cap against the ledger's actual spend plus this call's estimate. The tightest cap wins. A refusal invokes no
backend, writes a shadow/1 row to the local outbox and a ledger row, and returns ``refused``. Every call that ends
(ok · refused · cached · error) writes one ledger row.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..backends.catalog import price_for
from . import policy as pol
from .ledger import Ledger, iso, now_utc

OK, REFUSED, CACHED, ERROR, NEEDS_JUDGMENT = "ok", "refused", "cached", "error", "needs_judgment"
DEFAULT_CARD_MAX_BYTES = 24_000
DEFAULT_EST_OUTPUT_TOKENS = 2_000
BYTES_PER_TOKEN = 3.0  # a deliberately high-side estimate of tokens from bytes


@dataclass
class Card:
    role: str
    evidence: str

    @classmethod
    def of(cls, card: Any) -> "Card":
        if isinstance(card, Card):
            return card
        if isinstance(card, dict):
            return cls(str(card.get("role", "")), str(card.get("evidence", "")))
        return cls("", str(card))

    def size(self) -> int:
        return len((self.role + self.evidence).encode("utf-8"))


@dataclass
class Result:
    status: str                       # ok | refused | cached | error | needs_judgment
    answer: str = ""
    reason: dict[str, Any] = field(default_factory=dict)
    usd: float = 0.0
    served: list[str] = field(default_factory=list)
    turn: Any = None                  # the BackendTurn of an ok call (resume handle, usage, ...)

    @property
    def called(self) -> bool:
        return self.status in (OK, ERROR)


@dataclass
class GatewayConfig:
    policy_path: str = ""
    home: str = ""                    # default ~/.ga/llm: ledger/, cache/, shadow.jsonl
    card_max_bytes: int = DEFAULT_CARD_MAX_BYTES
    est_output_tokens: int = DEFAULT_EST_OUTPUT_TOKENS
    actor: str = "ga-llm"
    backend: str = "claude_cli"       # the default runner_factory's backend plugin
    sdk_sha: str = ""

    def __post_init__(self) -> None:
        self.policy_path = str(self.policy_path or os.environ.get("GA_LLM_POLICY") or pol.default_path())
        self.home = str(self.home or os.environ.get("GA_LLM_HOME") or Path.home() / ".ga" / "llm")
        self.sdk_sha = self.sdk_sha or sdk_sha()

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "GatewayConfig":
        raw = dict(raw or {})
        return cls(**{k: raw[k] for k in ("policy_path", "home", "card_max_bytes", "est_output_tokens", "actor",
                                          "backend", "sdk_sha") if k in raw})


def sdk_sha() -> str:
    if os.environ.get("GA_SDK_SHA"):
        return os.environ["GA_SDK_SHA"]
    try:
        out = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    from .. import __version__ as v  # not a git checkout: the version stands for the SHA
    return f"v{v}"


def normalize(evidence: str) -> str:
    return re.sub(r"\s+", " ", evidence).strip()


def fingerprint(purpose: str, role: str, evidence: str, sha: str, model: str, policy_sha: str) -> str:
    h = hashlib.sha256()
    for part in (purpose, role, normalize(evidence), sha, model, policy_sha):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def estimate_usd(card_bytes: int, model: str, out_tokens: int) -> float:
    p = price_for(model)
    return (card_bytes / BYTES_PER_TOKEN * p["input"] + out_tokens * p["output"]) / 1e6


def actual_usd(usage: dict[str, Any] | None, fmt: str | None, model: str) -> tuple[dict[str, int], float | None]:
    """(input, output, cache_read, cache_write tokens, usd) from the provider's own usage object; usd None when the
    host did not report usage (the caller then books the estimate)."""
    if not isinstance(usage, dict):
        return {}, None
    g = lambda *k: next((int(usage[x]) for x in k if isinstance(usage.get(x), (int, float))), 0)  # noqa: E731
    if fmt == "openai":
        toks = {"input": g("prompt_tokens", "input_tokens"), "output": g("completion_tokens", "output_tokens"),
                "cache_read": g("cached_tokens"), "cache_write": 0}
        toks["input"] = max(0, toks["input"] - toks["cache_read"])
    elif fmt == "gemini":
        u = usage.get("usageMetadata") if isinstance(usage.get("usageMetadata"), dict) else usage
        gg = lambda *k: next((int(u[x]) for x in k if isinstance(u.get(x), (int, float))), 0)  # noqa: E731
        toks = {"input": gg("promptTokenCount"), "output": gg("candidatesTokenCount"),
                "cache_read": gg("cachedContentTokenCount"), "cache_write": 0}
        toks["input"] = max(0, toks["input"] - toks["cache_read"])
    else:
        toks = {"input": g("input_tokens"), "output": g("output_tokens"),
                "cache_read": g("cache_read_input_tokens"), "cache_write": g("cache_creation_input_tokens")}
    p = price_for(model)
    return toks, sum(toks[k] * p[k] for k in toks) / 1e6


class Gateway:
    def __init__(self, config: GatewayConfig | None = None, runner_factory: Callable[[str], Any] | None = None):
        self.cfg = config or GatewayConfig()
        self.runner_factory = runner_factory or self._default_runner
        self.ledger = Ledger(Path(self.cfg.home) / "ledger")
        self.cache_dir = Path(self.cfg.home) / "cache"
        self.shadow_path = Path(self.cfg.home) / "shadow.jsonl"

    def _default_runner(self, model: str) -> Any:
        from .. import backends
        return backends.create(self.cfg.backend, model, {}, {"cwd": ".", "timeout_s": 600.0})

    # -- the one entry
    def call(self, card: Any, purpose: str, item_id: str, model_hint: str) -> Result:
        c = Card.of(card)
        size = c.size()
        policy = pol.load(self.cfg.policy_path)
        base = {"purpose": purpose, "item_id": item_id, "requested_model": model_hint, "served_model": None,
                "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "usd": 0.0,
                "card_bytes": size, "policy_sha256": policy.sha256}
        fp = fingerprint(purpose, c.role, c.evidence, self.cfg.sdk_sha, model_hint, policy.sha256)
        base["fingerprint"] = fp
        if policy.ok:  # a refused policy answers nothing, not even from cache
            hit = self._cache_get(fp)
            if hit is not None:
                self.ledger.append({**base, "served_model": hit.get("served", [None])[0], "outcome": CACHED})
                return Result(CACHED, hit["answer"], served=hit.get("served", []))
        if size > self.cfg.card_max_bytes:
            return self._refuse(base, c, "card_size", {"card_bytes": size, "max": self.cfg.card_max_bytes}, policy)
        est = estimate_usd(size, model_hint, self.cfg.est_output_tokens)
        verdict = self._check_caps(policy, purpose, item_id, est)
        if verdict is not None:
            status, reason = verdict
            if status == NEEDS_JUDGMENT:
                self.ledger.append({**base, "outcome": REFUSED, "label": NEEDS_JUDGMENT, "reason": reason})
                return Result(NEEDS_JUDGMENT, reason=reason)
            return self._refuse(base, c, "budget", reason, policy)
        return self._invoke(base, c, model_hint, est, fp, policy)

    # -- caps
    def _check_caps(self, policy: pol.Policy, purpose: str, item_id: str, est: float):
        if not policy.ok:
            return REFUSED, {"cap": "policy", "window_spend_usd": None, "estimate_usd": round(est, 6),
                             "why": policy.why}
        now = now_utc()
        hits: list[tuple[str, dict]] = []
        for cap in policy.caps_for(purpose):
            limit = policy.caps.get(cap)
            if cap == pol.BUILD_ITEM:
                r = self._check_build(policy, cap, limit, purpose, item_id, est, now)
                if r is not None:
                    hits.append(r)
                continue
            if not isinstance(limit, (int, float)) or isinstance(limit, bool):
                continue
            purposes = policy.purposes_of(cap, {purpose} | self.ledger.purposes(policy.window_s(cap), now))
            spend = self.ledger.spend(purposes, policy.window_s(cap), now)
            if spend + est > float(limit):
                hits.append((REFUSED, {"cap": cap, "limit_usd": float(limit), "window_s": policy.window_s(cap),
                                       "window_spend_usd": round(spend, 6), "estimate_usd": round(est, 6)}))
        if not hits:
            return None
        hard = [h for h in hits if h[0] == REFUSED]
        return min(hard or hits, key=lambda h: h[1].get("limit_usd", 0.0))  # the tightest cap wins

    def _check_build(self, policy, cap, limit, purpose, item_id, est, now):
        soft, hard = (limit.get("default"), limit.get("hard")) if isinstance(limit, dict) else (limit, None)
        if purpose != "build" and cap not in policy.applies.get(purpose, []):
            return None
        spend = self.ledger.spend({purpose}, None, now, item_id=item_id)
        r = {"cap": cap, "window_spend_usd": round(spend, 6), "estimate_usd": round(est, 6), "item_id": item_id}
        if isinstance(hard, (int, float)) and spend + est > float(hard):
            return REFUSED, {**r, "limit_usd": float(hard), "kind": "hard"}
        if isinstance(soft, (int, float)) and spend + est > float(soft):
            return NEEDS_JUDGMENT, {**r, "limit_usd": float(soft), "kind": "soft"}
        return None

    def _refuse(self, base, c: Card, rejected_by: str, reason: dict, policy) -> Result:
        self.ledger.append({**base, "outcome": REFUSED, "label": rejected_by, "reason": reason})
        row = {"form": "shadow/1", "at": iso(now_utc()), "actor": self.cfg.actor, "action": "llm_call",
               "rejected_by": rejected_by, "reason": reason,
               "would_do": {"purpose": base["purpose"], "item_id": base["item_id"], "model": base["requested_model"],
                            "card_bytes": base["card_bytes"]}}
        self.shadow_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.shadow_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return Result(REFUSED, reason=reason)

    # -- the backend call
    def _invoke(self, base, c: Card, model: str, est: float, fp: str, policy) -> Result:
        from ..backends.base import BackendError, ModelMismatch
        try:
            runner = self.runner_factory(model)
            if getattr(runner, "bare", False):
                turn = runner.run_turn(c.evidence, None, system=c.role)
            else:
                turn = runner.run_turn((c.role + "\n\n" + c.evidence) if c.role else c.evidence, None)
        except ModelMismatch as e:
            return self._error(base, est, "served-model mismatch", str(getattr(e, "reason", "") or "")[:80])
        except BackendError as e:
            reason = str(getattr(e, "reason", "") or "")
            label = "timeout" if "timeout" in reason.lower() else "refusal" if "refus" in reason.lower() else "backend"
            return self._error(base, est, label, reason[:80])
        served = list(getattr(turn, "served", []) or [])
        toks, usd = actual_usd(getattr(turn, "usage", None), getattr(turn, "usage_format", None), model)
        usd = est if usd is None else usd
        row = {**base, "served_model": served[0] if served else None, "input_tokens": toks.get("input", 0),
               "output_tokens": toks.get("output", 0), "cache_read_tokens": toks.get("cache_read", 0),
               "cache_write_tokens": toks.get("cache_write", 0), "usd": round(usd, 8), "estimate_usd": round(est, 8),
               "outcome": OK}
        self.ledger.append(row)
        self._cache_put(fp, {"answer": turn.answer, "served": served})
        return Result(OK, turn.answer, usd=usd, served=served, turn=turn)

    def _error(self, base, est: float, label: str, detail: str) -> Result:
        # a failed turn may still have been billed; book the estimate so the windows stay conservative
        self.ledger.append({**base, "usd": round(est, 8), "estimate_usd": round(est, 8), "outcome": ERROR,
                            "label": label})
        return Result(ERROR, reason={"label": label, "detail": detail}, usd=est)

    # -- rule cache
    def _cache_get(self, fp: str) -> dict | None:
        try:
            return json.loads((self.cache_dir / f"{fp}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _cache_put(self, fp: str, doc: dict) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_dir / f".{fp}.tmp"
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.cache_dir / f"{fp}.json")


_DEFAULT: Gateway | None = None


def default_gateway(config: GatewayConfig | None = None, runner_factory: Callable[[str], Any] | None = None) -> Gateway:
    """The process-wide gateway (config from env/defaults on first use); an explicit config replaces it."""
    global _DEFAULT
    if config is not None or runner_factory is not None or _DEFAULT is None:
        _DEFAULT = Gateway(config, runner_factory)
    return _DEFAULT


def call(card: Any, purpose: str, item_id: str, model_hint: str, *, gateway: Gateway | None = None) -> Result:
    return (gateway or default_gateway()).call(card, purpose, item_id, model_hint)
