"""The pinned dependencies of ga-sdk (CMD-GA20, BD-206 "one ga"). **This file is the source**; ``pyproject.toml``
``[project] dependencies`` must say the same, by meaning (tests/test_pins.py checks it, and the installed metadata too).

ga core imports none of these: it stays standard library only. They come in for ``ga.rlo`` (owned by GR), which imports
them lazily, when ``ga rlo ...`` runs.

The URL text is part of the pin: pip treats two spellings of a URL as two sources and refuses to resolve them, so any
other package that pins rlo-sdk next to ga-sdk must use this exact text.
"""
from __future__ import annotations

# name -> (distribution, extras, repository URL, commit sha)
PINS = {
    "rlo": ("rlo-sdk", ("sensor",), "https://github.com/cogito5170/rlo-SDK", "250a88e56a74d2776688d34ec732cbd0f4244ba0"),
}
# the version the pinned commit carries (rlo-SDK pyproject at that sha; K12, branch claude/peaceful-maxwell-i9vqpx, for ga gemini)
VERSIONS = {"rlo-sdk": "0.7.0"}


def requirement(pin: tuple) -> str:
    dist, extras, url, sha = pin
    return f"{dist}{'[' + ','.join(extras) + ']' if extras else ''} @ git+{url}@{sha}"


def requirements() -> list[str]:
    return [requirement(p) for p in PINS.values()]
