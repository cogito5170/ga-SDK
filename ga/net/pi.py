"""pi_ij and activation (CMD-GA31 S2): the NET4 functions (MS ``ms.network``, 937115144c83).

ga core is standard library only and the pinned rlo-sdk carries an MS without ``ms.network``, so this module uses MS's
functions when they import and otherwise this port of the same formulas (tests check both give the same numbers when
MS is there). pi is derived State of the node (``pi.json``), never an L0 field.

    pi = r * (w_u*u + w_g*g + w_t*t)   u usefulness (prior 1/2) · g information gain · t trust (prior 1/2) · r relevance
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

try:  # pragma: no cover - depends on the installed MS
    from ms import network as _ms  # type: ignore
    SOURCE = "ms.network"
except Exception:  # noqa: BLE001 - MS absent or older than NET4
    _ms = None
    SOURCE = "ga.net.pi (port of ms.network 937115144c83)"


@dataclass(frozen=True)
class NetConfig:
    window: int = 20
    half_life: float = 3600.0
    weights: tuple = (1 / 3, 1 / 3, 1 / 3)
    theta: float = 0.3


@dataclass(frozen=True)
class Interaction:
    ts: float
    transition: bool = False
    invalidated: bool = False
    uncertain_before: int = 0
    uncertain_after: int = 0
    verify: bool = False

    @property
    def useful(self) -> bool:
        return self.transition and not self.invalidated


@dataclass(frozen=True)
class Obs:
    ts: float
    invalid: bool = False


def _decay(age: float, half_life: float) -> float:
    return 0.5 ** (max(0.0, age) / half_life)


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _pi_port(inter, obs, required, exported, now, cfg: NetConfig) -> float:
    xs = [x for x in inter if not x.verify][-cfg.window:]
    w = [_decay(now - x.ts, cfg.half_life) for x in xs]
    u = (sum(wi for wi, x in zip(w, xs) if x.useful) + 1.0) / (sum(w) + 2.0)
    if not xs or sum(w) == 0:
        g = 0.0
    else:
        gains = [_clip((x.uncertain_before - x.uncertain_after) / max(1, x.uncertain_before)) for x in xs]
        g = _clip(sum(wi * gi for wi, gi in zip(w, gains)) / sum(w))
    os_ = list(obs)[-cfg.window:]
    wo = [_decay(now - o.ts, cfg.half_life) for o in os_]
    t = 0.5 if not os_ or sum(wo) == 0 else _clip(1.0 - sum(wi for wi, o in zip(wo, os_) if o.invalid) / sum(wo))
    req = set(required)
    r = len(req & set(exported)) / max(1, len(req))
    wu, wg, wt = cfg.weights
    return _clip(r * (wu * u + wg * g + wt * t))


def pi(inter, obs, required, exported, now, cfg: NetConfig = NetConfig()) -> float:
    if _ms is not None:
        mc = _ms.NetConfig(window=cfg.window, half_life=cfg.half_life, weights=cfg.weights, theta=cfg.theta)
        return _ms.pi([_ms.Interaction(**asdict(x)) for x in inter], [_ms.Observation(o.ts, o.invalid) for o in obs],
                      required, exported, now, mc)
    return _pi_port(inter, obs, required, exported, now, cfg)


def activate(i: str, j: str, pi_ij: float, cfg: NetConfig, *, missing=(), covers=None, verify_open=False):
    """(send?, reason) -- reason pi · required · verify · none (ms.network.activate, the flag given as a bool)."""
    if pi_ij >= cfg.theta:
        return True, "pi"
    covers = covers or {}
    for ref in sorted(missing):
        if [p for p, refs in sorted(covers.items()) if ref in refs] == [j]:
            return True, "required"
    if verify_open:
        return True, "verify"
    return False, "none"


class Edges:
    """The node's per-peer history (``pi.json``): interactions, observations, verify flags, and the last pi values."""

    def __init__(self, d: dict[str, Any] | None = None):
        d = d or {}
        self.inter: dict[str, list[dict]] = d.get("interactions", {})
        self.obs: dict[str, list[dict]] = d.get("observations", {})
        self.flags: dict[str, str] = d.get("flags", {})
        self.values: dict[str, float] = d.get("pi", {})

    def add_interaction(self, j: str, **kw: Any) -> None:
        self.inter.setdefault(j, []).append(asdict(Interaction(**kw)))

    def add_obs(self, j: str, ts: float, invalid: bool = False) -> None:
        self.obs.setdefault(j, []).append({"ts": ts, "invalid": invalid})

    def pi(self, j: str, required, exported, now: float, cfg: NetConfig) -> float:
        v = pi([Interaction(**x) for x in self.inter.get(j, [])], [Obs(**o) for o in self.obs.get(j, [])],
               required, exported, now, cfg)
        self.values[j] = round(v, 6)
        return v

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "ga-node-pi/1", "source": SOURCE, "interactions": self.inter, "observations": self.obs,
                "flags": self.flags, "pi": self.values}
