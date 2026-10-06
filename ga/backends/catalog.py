"""The built-in backends' model catalogs (CMD-GA31 S3, NETWORK.md 7): what each one can serve, for ga's router.

An entry: {model, family, capabilities, context_window, effort_option (null = no effort knob), tiers {R0-R3: effort
value or null}, price {in, out} USD per Mtok (null = not declared here: the router treats it as the most expensive),
quota_family}.

CMD-GA32 S4, from documented values only. Claude prices are the first-party API rates (Haiku 4.5 $1/$5, Sonnet 5.5
$2/$10, Opus 5.5 $4/$20 per Mtok). The one documented effort knob is the Messages API ``output_config.effort`` (low ..
max on Sonnet 5.5 and Opus 5.5; Haiku 4.5 rejects it), which ``anthropic_http`` takes as ``options.effort``. The CLI
plugins take no effort option, and agv carries effort in the slug (``-high``), so those stay null. Prices and knobs for
the gpt and gemini entries are not declared here: null, until a documented value is added. A node may narrow these or
add its own entries (ga/net/router.py ``catalog_for``).
"""
from __future__ import annotations

ALL = ("R0", "R1", "R2", "R3")


def _e(model, family, caps, window, tiers, price=None, quota=None, effort=None):
    """``tiers``: tier names (no knob) or, with ``effort`` (the option name), a {tier: effort value} map."""
    tmap = dict(tiers) if effort else {t: None for t in tiers}
    return {"model": model, "family": family, "capabilities": list(caps), "context_window": window,
            "effort_option": effort, "tiers": tmap, "price": price, "quota_family": quota or family}


def _claude(effort: bool) -> list:
    caps = ("text", "vision", "tools")
    return [
        _e("claude-haiku-4-5-20251001", "claude", caps, 200_000, ("R0", "R1"), {"in": 1.0, "out": 5.0}),  # no effort knob
        _e("claude-sonnet-5-5", "claude", caps, 1_000_000,
           {"R1": "medium", "R2": "high", "R3": "xhigh"} if effort else ("R1", "R2", "R3"),
           {"in": 2.0, "out": 10.0}, effort="effort" if effort else None),
        _e("claude-opus-5-5", "claude", caps, 1_000_000,
           {"R2": "high", "R3": "xhigh"} if effort else ("R2", "R3"),
           {"in": 4.0, "out": 20.0}, effort="effort" if effort else None),
    ]


CATALOG = {
    "claude_cli": _claude(False),
    "anthropic_http": _claude(True),
    "gemini_cli": [_e("gemini-3-flash-preview", "gemini", ("text", "vision", "tools"), 1_000_000, ("R0", "R1"))],
    "codex_cli": [_e("gpt-5.1-codex", "gpt", ("text", "tools"), 400_000, ("R1", "R2"))],
    "openai_http": [_e("gpt-5.1", "gpt", ("text", "vision"), 400_000, ("R1", "R2"))],
    "agv": [_e("gpt-5.1", "gpt", ("text", "tools"), 400_000, ("R1", "R2"), quota="agv"),
            _e("claude-sonnet-4-5", "claude", ("text", "vision", "tools"), 200_000, ("R1", "R2"), quota="agv"),
            _e("gemini-3.8-flash", "gemini", ("text", "vision", "tools"), 1_000_000, ("R0", "R1"), quota="agv"),
            _e("gemini-3.8-flash-high", "gemini", ("text", "vision", "tools"), 1_000_000, ("R2",), quota="agv")],
}


def price_for(model: str) -> dict[str, float]:
    """USD per Mtok {input, output, cache_read, cache_write} for a model (R1 gateway). Not declared (or an unknown
    model) = the most expensive declared price. cache_read / cache_write default to the first-party ratios
    (0.1x and 1.25x the input price) when an entry does not declare them."""
    declared = [e["price"] for es in CATALOG.values() for e in es if e["price"]]
    hit = next((e["price"] for es in CATALOG.values() for e in es if e["model"] == model and e["price"]), None)
    p = hit or {"in": max(d["in"] for d in declared), "out": max(d["out"] for d in declared)}
    return {"input": float(p["in"]), "output": float(p["out"]),
            "cache_read": float(p.get("cache_read", 0.1 * p["in"])),
            "cache_write": float(p.get("cache_write", 1.25 * p["in"]))}
