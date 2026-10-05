"""Peer mode (CMD-GA31, POL-3 structure 4): sessions are peer nodes over a deterministic code runtime.

Legacy hub mode is the default and unchanged. Peer mode is one config switch, off by default (PROTOCOL 1a):

    "network": {"mode": "peer", "mailbox": "<git repo>", "theta": 0.3, "edge_budget": {"rpm": 6, "tpm": 20000},
                "refs": {"<ref>": {"needs": {...}, "input": "..."}}, "nodes": {"A": {...}, ...}}

A node writes only ``.ga/nodes/<me>/``; the hub's state.json and RecordStore are never touched. Peer messages travel by
ga mail (``exchange/1`` head plus one ```peer block) and never land on a hub channel. A peer message is data: never a
directive, never an action (NETWORK.md 3). Modules:

    msg        the peer message form (build, parse, check)          state     the node's State and its rules (NET2)
    pi         interaction weight pi and activation (NET4)          dc        peer_interaction purpose (NET3)
    router     model and effort router (NETWORK.md 7)               checkpoint budget stop as a checkpoint (8)
    node       ``ga node step`` and ``ga run``                      usage     ``ga usage``: tokmon alarms from L0
"""
from __future__ import annotations

import re
from typing import Any

from ..forms import HARD, Problem

MODES = ("hub", "peer")
PEER_BLOCK_RE = re.compile(r"^```peer[ \t]*$", re.MULTILINE)
NODE_NAME = re.compile(r"^[A-Za-z0-9_.]{1,40}$")  # a ga mail sender name (no '-')
TIERS = ("R0", "R1", "R2", "R3")


class PeerModeOff(RuntimeError):
    """A peer command on a config whose network mode is not peer (PROTOCOL 1a: off by default)."""


def is_peer_form(text: str) -> bool:
    """A text that carries a ```peer block: a peer message, which never goes on a hub channel (S6)."""
    return isinstance(text, str) and bool(PEER_BLOCK_RE.search(text))


def mode_of(network: Any) -> str:
    return network.get("mode", "hub") if isinstance(network, dict) else "hub"


def _needs_problems(v: Any, path: str) -> list[Problem]:
    out = []
    if not isinstance(v, dict) or set(v) - {"capabilities", "tier", "context"}:
        return [Problem(path, "must be {capabilities?, tier?, context?}", HARD)]
    caps = v.get("capabilities", [])
    if not isinstance(caps, list) or not all(isinstance(c, str) and c for c in caps):
        out.append(Problem(path + ".capabilities", "must be a list of names", HARD))
    if v.get("tier", "R0") not in TIERS:
        out.append(Problem(path + ".tier", "must be one of " + ", ".join(TIERS), HARD))
    ctx = v.get("context", 0)
    if isinstance(ctx, bool) or not isinstance(ctx, int) or ctx < 0:
        out.append(Problem(path + ".context", "must be a non-negative integer (tokens)", HARD))
    return out


def problems_of(network: Any) -> list[Problem]:
    """The ``network`` section of ga-config/1. Absent = hub mode. Every problem is hard."""
    if network is None:
        return []
    if not isinstance(network, dict):
        return [Problem("$.network", "must be an object", HARD)]
    out: list[Problem] = []
    bad = lambda p, m: out.append(Problem(p, m, HARD))  # noqa: E731
    if network.get("mode", "hub") not in MODES:
        bad("$.network.mode", "must be hub or peer")
    for k in ("theta", "half_life"):
        v = network.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            bad(f"$.network.{k}", "must be a non-negative number")
    eb = network.get("edge_budget", {})
    if not isinstance(eb, dict) or set(eb) - {"rpm", "tpm"} or any(
            isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in eb.values()):
        bad("$.network.edge_budget", "must be {rpm?, tpm?} (non-negative integers)")
    refs = network.get("refs", {})
    if not isinstance(refs, dict):
        bad("$.network.refs", "must map refs to {needs?, input?}")
        refs = {}
    for r, spec in refs.items():
        if not isinstance(spec, dict) or set(spec) - {"needs", "input"}:
            bad(f"$.network.refs.{r}", "must be {needs?, input?}")
        elif "needs" in spec:
            out += _needs_problems(spec["needs"], f"$.network.refs.{r}.needs")
    nodes = network.get("nodes", {})
    if not isinstance(nodes, dict):
        bad("$.network.nodes", "must be an object")
        nodes = {}
    if network.get("mode") == "peer" and not isinstance(network.get("mailbox"), str):
        bad("$.network.mailbox", "is required in peer mode (the git repository that carries ga mail)")
    for name, n in nodes.items():
        p = f"$.network.nodes.{name}"
        if not NODE_NAME.match(name):
            bad(p, "a node name is [A-Za-z0-9_.], up to 40, no '-' (a ga mail sender)")
        if not isinstance(n, dict):
            bad(p, "must be an object")
            continue
        if not isinstance(n.get("backends"), list) or not all(isinstance(b, str) for b in n["backends"]):
            bad(p + ".backends", "must list the backend plugins the node may use")
        t = n.get("task")
        if t is not None:
            if not isinstance(t, dict) or not isinstance(t.get("id"), str):
                bad(p + ".task", "must be an object with an id")
            else:
                if "needs" in t:
                    out += _needs_problems(t["needs"], p + ".task.needs")
                if not isinstance(t.get("uses", []), list):
                    bad(p + ".task.uses", "must list refs")
                if "check" in t and not (isinstance(t["check"], list) and all(isinstance(x, str) for x in t["check"])):
                    bad(p + ".task.check", "must be an argv list")
        if not isinstance(n.get("facts", {}), dict):
            bad(p + ".facts", "must map refs to {value, evidence}")
    if "pool" in network:
        from . import pool
        out += pool.problems_of(network)
    return out


__all__ = ["MODES", "TIERS", "PeerModeOff", "is_peer_form", "mode_of", "problems_of"]
