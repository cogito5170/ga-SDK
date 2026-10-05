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
    "rlo": ("rlo-sdk", ("sensor",), "https://github.com/cogito5170/rlo-SDK", "0d92a3de3fed6c67eb6643859588179107977cbd"),
}
# the optional extra ga-sdk[net] (CMD-GA32 S1): the five NET packages ga/net uses when installed, by full sha. These are the
# shas rlo-sdk[sensor] resolves to at the rlo pin above (the URL text is the one rlo uses, so pip sees one source each).
NET_PINS = {
    "telemetry": ("l0-telemetry", (), "https://github.com/cogito5170/Telemetry", "f6c7ae26d336965d3558092517e97d37d222c4ea"),
    "sensor": ("llmsensor", (), "https://github.com/cogito5170/Sensor", "ff17bddfd24f6d8cf9c14c0c45107b53818dd26a"),
    "dc": ("dc", (), "https://github.com/cogito5170/DC", "7e0ac1494192e49ab03c687c79f7156cb38abcec"),
    "ms": ("ms", (), "https://github.com/cogito5170/MS", "1f1018e0348428c9b83af4f6679659f0057e3f4b"),
    "action": ("action-contract", (), "https://github.com/cogito5170/action", "9d6729fc6809d68d2b5d55b4ad2fd37e5281d998"),
}
NET_VERSIONS = {"l0-telemetry": "0.2.0", "llmsensor": "0.2.1", "dc": "0.2.0", "ms": "0.3.1", "action-contract": "0.2.0"}
# the version the pinned commit carries (rlo-SDK pyproject at that sha, CMD-GA32 S2)
VERSIONS = {"rlo-sdk": "0.11.1"}


def requirement(pin: tuple) -> str:
    dist, extras, url, sha = pin
    return f"{dist}{'[' + ','.join(extras) + ']' if extras else ''} @ git+{url}@{sha}"


def net_requirements() -> list[str]:
    return [requirement(p) for p in NET_PINS.values()]


def requirements() -> list[str]:
    return [requirement(p) for p in PINS.values()]
