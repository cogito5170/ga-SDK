"""The ``peer_interaction`` purpose (CMD-GA31 S2; NET3 contract, thin adapter until its sha) and the peer context.

A node decides consult · send · skip from validated state, never from raw messages (NETWORK.md 1 #3):

    required   the node's missing refs and its uncertain refs
    optional   peer exports (``peer[<j>].<ref>``), valid only with evidence and status OBSERVED (Validate), and pi[<j>]
    consult    only when a peer export covers a missing ref and activation allows it (pi >= theta, or policy: the one
               peer that covers it, or an open contradicts verify flag); one open consult per ref
    send       a reply to an open request from j for a ref this node holds (policy: answering a consult)
    skip       the default

``peer_context`` is the ContextPolicy step for the pack's peer section (MS KEEP / DROP; thin adapter): only items about
the refs this turn works on are kept, newest first, within a byte budget; the rest are dropped and counted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .pi import Edges, NetConfig, activate

PURPOSE = "peer_interaction"
ACTIONS = ("consult", "send", "skip")
DEFAULT_ACTION = "skip"


@dataclass(frozen=True)
class Decision:
    action: str          # consult · send · skip · observe (the node's own turn will observe it)
    ref: str
    peer: str | None = None
    reason: str = ""
    msg_id: str | None = None  # the request a send answers


def valid_records(export: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Validate a peer's state export: a record counts only with evidence refs and status OBSERVED."""
    out = {}
    for r in (export or {}).get("records", []):
        if isinstance(r, dict) and r.get("status") == "OBSERVED" and r.get("evidence_refs") and isinstance(r.get("name"), str):
            out[r["name"]] = r
    return out


def covers(export: dict[str, Any] | None, ref: str, ref_needs: dict[str, Any]) -> bool:
    if ref in valid_records(export):
        return True
    caps = set((export or {}).get("capabilities", []))
    need = set((ref_needs or {}).get("capabilities", []))
    return bool(need) and need <= caps


def decide(me: str, state: Any, required: list[str], exports: dict[str, dict], edges: Edges, cfg: NetConfig, now: float,
           *, can_observe: Callable[[str], bool], ref_needs: Callable[[str], dict]) -> list[Decision]:
    needed = sorted(set(state.missing(required)) | set(r for r in state.uncertain() if r in required))
    cover = {j: sorted(r for r in required if covers(e, r, ref_needs(r))) for j, e in sorted(exports.items()) if j != me}
    out: list[Decision] = []
    for ref in needed:
        if ref in state.consults:
            out.append(Decision("skip", ref, state.consults[ref]["to"], "pending"))
            continue
        if ref not in state.facts and can_observe(ref):
            out.append(Decision("observe", ref, None, "local"))
            continue
        cands = [j for j, refs in cover.items() if ref in refs]
        if not cands:
            out.append(Decision("skip", ref, None, "no peer covers it"))
            continue
        ranked = sorted(cands, key=lambda j: (-edges.pi(j, required, cover[j], now, cfg), j))
        for j in ranked:
            ok, why = activate(me, j, edges.values[j], cfg, missing=needed, covers={k: cover[k] for k in cands},
                               verify_open=edges.flags.get(j) == "open")
            if ok:
                out.append(Decision("consult", ref, j, why))
                break
        else:
            out.append(Decision("skip", ref, ranked[0], "below theta"))
    for mid, rq in sorted(state.requests.items()):
        if rq.get("status") != "open":
            continue
        if state.valid(rq["ref"]):
            out.append(Decision("send", rq["ref"], rq["from"], "reply", mid))
        elif can_observe(rq["ref"]):
            out.append(Decision("observe", rq["ref"], rq["from"], "request", mid))
        else:
            out.append(Decision("skip", rq["ref"], rq["from"], "cannot observe", mid))
    return out


def peer_context(items: list[dict[str, Any]], refs: set[str], max_bytes: int) -> tuple[list[str], dict[str, int]]:
    """items: {"from", "msg_id", "line", "ref"} oldest first -> (kept lines oldest first, {"keep", "drop"})."""
    kept, used, drop = [], 0, 0
    for it in reversed(items):
        n = len(it["line"].encode("utf-8")) + 1
        if it.get("ref") in refs and used + n <= max_bytes:
            kept.append(it["line"])
            used += n
        else:
            drop += 1
    return list(reversed(kept)), {"keep": len(kept), "drop": drop}
