"""The real NET packages behind ga/net (CMD-GA32 S1): ``pip install ga-sdk[net]``.

When the five are importable, ``ga/net`` and ``ga/l0.py`` use them: l0-telemetry builds the ``peer.message.*`` events,
ms ``ms.network`` computes pi and activation, dc supplies the ``peer_interaction`` purpose and the state-export contract,
llmsensor's ``state.peer`` rules are the reference the node's State is checked against, action-contract is the shape
check of a peer action. When any is missing, ga's thin adapters (the same behaviour, standard library only) run instead.
The same behaviour tests run against both (tests/test_ga32_net.py). ``GA_NET=fallback`` forces the adapters.
"""
from __future__ import annotations

import contextlib
import importlib
import os

MODULES = ("telemetry.peer", "ms.network", "dc.peer", "llmsensor.state.peer", "action")
_forced: bool | None = None


def importable() -> bool:
    try:
        for m in MODULES:
            importlib.import_module(m)
    except Exception:  # noqa: BLE001 - absent, or an older sha without the NET module
        return False
    return True


def enabled() -> bool:
    """True when the NET path runs: the packages import and nothing forces the fallback."""
    if _forced is not None:
        return _forced
    return os.environ.get("GA_NET", "") != "fallback" and importable()


@contextlib.contextmanager
def forced(on: bool):
    """Tests: run a block on the NET path (on=True; the packages must import) or the fallback (on=False)."""
    global _forced
    if on and not importable():
        raise RuntimeError("ga-sdk[net] is not installed")
    prev, _forced = _forced, on
    try:
        yield
    finally:
        _forced = prev


def source() -> str:
    return "net" if enabled() else "fallback"
