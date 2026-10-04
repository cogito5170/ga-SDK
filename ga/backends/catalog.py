"""The built-in backends' model catalogs (CMD-GA31 S3, NETWORK.md 7): what each one can serve, for ga's router.

An entry: {model, family, capabilities, context_window, effort_option (null = no effort knob), tiers {R0-R3: effort
value or null}, price {in, out} USD per Mtok (null = not declared here: sorts last), quota_family}. The built-in
plugins take no effort option yet, so every built-in knob is declared none; agv carries effort in the slug (``-high``),
so those are separate entries. A node may narrow these or add its own entries (ga/net/router.py ``catalog_for``).
"""
from __future__ import annotations

ALL = ("R0", "R1", "R2", "R3")


def _e(model, family, caps, window, tiers, price=None, quota=None):
    return {"model": model, "family": family, "capabilities": list(caps), "context_window": window,
            "effort_option": None, "tiers": {t: None for t in tiers}, "price": price, "quota_family": quota or family}


HAIKU = _e("claude-haiku-4-5-20251001", "claude", ("text", "vision", "tools"), 200_000, ("R0", "R1"),
           {"in": 1.0, "out": 5.0})
SONNET = _e("claude-sonnet-5-5", "claude", ("text", "vision", "tools"), 200_000, ("R1", "R2", "R3"))
OPUS = _e("claude-opus-5-5", "claude", ("text", "vision", "tools"), 200_000, ("R2", "R3"))

CATALOG = {
    "claude_cli": [HAIKU, SONNET, OPUS],
    "anthropic_http": [HAIKU, SONNET, OPUS],
    "gemini_cli": [_e("gemini-3-flash-preview", "gemini", ("text", "vision", "tools"), 1_000_000, ("R0", "R1"))],
    "codex_cli": [_e("gpt-5.1-codex", "gpt", ("text", "tools"), 400_000, ("R1", "R2"))],
    "openai_http": [_e("gpt-5.1", "gpt", ("text", "vision"), 400_000, ("R1", "R2"))],
    "agv": [_e("gpt-5.1", "gpt", ("text", "tools"), 400_000, ("R1", "R2"), quota="agv"),
            _e("claude-sonnet-4-5", "claude", ("text", "vision", "tools"), 200_000, ("R1", "R2"), quota="agv"),
            _e("gemini-3.8-flash", "gemini", ("text", "vision", "tools"), 1_000_000, ("R0", "R1"), quota="agv"),
            _e("gemini-3.8-flash-high", "gemini", ("text", "vision", "tools"), 1_000_000, ("R2",), quota="agv")],
}
