"""The model and effort router (CMD-GA31 S3, NETWORK.md 7). Deterministic code plus what it learns from results.

    demand    ``needs`` on a directive or a node task: {capabilities, tier R0-R3, context (tokens)}
    supply    each GA28 backend plugin's ``catalog``: entries {model, family, capabilities, context_window,
              effort_option (or null: no knob), tiers {tier: effort value or null}, price {in, out} per Mtok or null,
              quota_family}; a node may add or narrow entries (``catalog`` in its config)
    choice    the cheapest entry that meets the needs at the class's current tier (unknown price sorts last, then
              catalog order); a verifiable failure goes up one tier, ``down_after`` successes in a row go down one (never
              below the needed tier). Tokens per accepted answer are learnt per (task class, backend, model, tier) as
              node derived state (``router.json``).

A served model other than the chosen one fails the turn (``check``); an unknown model or family is a ConfigError.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..backends.base import ConfigError, ModelMismatch, check_served
from . import TIERS

FAMILIES = ("claude", "gpt", "gemini")  # the families ga knows (GA28 slug_family); anything else is refused
DOWN_AFTER = 3


class NoRoute(RuntimeError):
    """No catalog entry meets the needs at any tier up to R3 (e.g. a capability the node's backends lack)."""


@dataclass(frozen=True)
class Choice:
    backend: str
    model: str
    tier: str
    options: dict = field(default_factory=dict)
    family: str = ""


def _entry_problems(e: Any) -> str | None:
    if not isinstance(e, dict):
        return "a catalog entry must be an object"
    for k in ("backend", "model", "family"):
        if not isinstance(e.get(k), str) or not e[k]:
            return f"catalog entry needs {k}"
    if e["family"] not in FAMILIES:
        return f"unknown model family {e['family']!r} for {e['model']!r} (known: {', '.join(FAMILIES)})"
    tiers = e.get("tiers")
    if not isinstance(tiers, dict) or not tiers or set(tiers) - set(TIERS):
        return f"{e['model']}: tiers must map some of {', '.join(TIERS)} to an effort value (or null)"
    if e.get("effort_option") is None and any(v is not None for v in tiers.values()):
        return f"{e['model']}: no effort knob (effort_option null) but tiers carry effort values"
    if not isinstance(e.get("capabilities", []), list):
        return f"{e['model']}: capabilities must be a list"
    return None


def catalog_for(backends: list[str], overrides: list[dict] | None, get_backend) -> list[dict]:
    """The node's entries: each named backend plugin's catalog, then the node's own entries (same backend + model
    replaces, a new one is added). ConfigError on an entry that names an unknown model or family."""
    out: list[dict] = []
    for b in backends:
        try:
            plugin = get_backend(b)
        except KeyError as e:
            raise ConfigError(str(e)) from None
        for e in getattr(plugin, "catalog", []) or []:
            out.append(dict(e, backend=b))
    for o in overrides or []:
        if not isinstance(o, dict) or o.get("backend") not in backends:
            raise ConfigError(f"a node catalog entry must name one of its backends: {o!r}"[:200])
        base = next((e for e in out if e["backend"] == o["backend"] and e["model"] == o.get("model")), None)
        if base is None and set(o) <= {"backend", "model", "capabilities", "price", "tiers"}:
            raise ConfigError(f"unknown model {o.get('model')!r} for backend {o['backend']!r} (not in its catalog)")
        if base is not None:
            base.update(o)
        else:
            out.append(dict(o))
    for e in out:
        why = _entry_problems(e)
        if why:
            raise ConfigError(why)
    return out


def _cost(e: dict) -> float:
    p = e.get("price")
    if not isinstance(p, dict):
        return float("inf")
    return float(p.get("in", 0) or 0) + float(p.get("out", 0) or 0)


class Router:
    def __init__(self, entries: list[dict], state: dict | None = None, *, down_after: int = DOWN_AFTER):
        self.entries, self.down_after = entries, down_after
        s = state or {}
        self.tier: dict[str, str] = s.get("tier", {})
        self.streak: dict[str, int] = s.get("streak", {})
        self.learn: dict[str, dict] = s.get("learn", {})

    def meets(self, e: dict, needs: dict, tier: str) -> bool:
        return (set(needs.get("capabilities", [])) <= set(e.get("capabilities", []))
                and int(needs.get("context", 0) or 0) <= int(e.get("context_window", 0) or 0)
                and tier in e["tiers"])

    def can(self, needs: dict) -> bool:
        return any(self.meets(e, needs, t) for e in self.entries for t in TIERS[TIERS.index(needs.get("tier", "R0")):])

    def pick(self, task_class: str, needs: dict) -> Choice:
        floor = needs.get("tier", "R0")
        cur = self.tier.get(task_class, floor)
        start = max(TIERS.index(floor), TIERS.index(cur))
        for t in TIERS[start:]:
            cands = [e for e in self.entries if self.meets(e, needs, t)]
            if cands:
                e = min(cands, key=lambda x: (_cost(x), self.entries.index(x)))
                eff = e["tiers"][t]
                return Choice(e["backend"], e["model"], t, {e["effort_option"]: eff} if eff is not None else {},
                              e["family"])
        raise NoRoute(f"no backend meets {sorted(needs.get('capabilities', []))} at {TIERS[start]} or above")

    @staticmethod
    def check(choice: Choice, served: list[str]) -> None:
        """The served model must be exactly the chosen one (ModelMismatch otherwise, the turn fails)."""
        check_served(list(served or []), choice.model)

    def result(self, task_class: str, needs: dict, choice: Choice, accepted: bool, tokens: int | None) -> None:
        key = f"{task_class}|{choice.backend}|{choice.model}|{choice.tier}"
        L = self.learn.setdefault(key, {"runs": 0, "accepted": 0, "tokens": 0, "tokens_per_accepted": None})
        L["runs"] += 1
        L["tokens"] += int(tokens or 0)
        floor = TIERS.index(needs.get("tier", "R0"))
        i = TIERS.index(choice.tier)
        if accepted:
            L["accepted"] += 1
            self.streak[task_class] = self.streak.get(task_class, 0) + 1
            if self.streak[task_class] >= self.down_after and i > floor:
                self.tier[task_class] = TIERS[i - 1]
                self.streak[task_class] = 0
            else:
                self.tier[task_class] = TIERS[i]
        else:
            self.streak[task_class] = 0
            self.tier[task_class] = TIERS[min(i + 1, len(TIERS) - 1)]
        L["tokens_per_accepted"] = round(L["tokens"] / L["accepted"], 1) if L["accepted"] else None

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "ga-node-router/1", "tier": self.tier, "streak": self.streak, "learn": self.learn}


__all__ = ["FAMILIES", "NoRoute", "Choice", "Router", "catalog_for", "ModelMismatch", "ConfigError"]
