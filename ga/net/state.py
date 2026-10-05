"""A node's State and the rules that alone change it (CMD-GA31 S2; NET2 contract, thin adapter until its sha).

State_i(t+1) = F_i(State_i(t), O_i(t), M_j->i(t)) is kept like this (NETWORK.md 3.1): a message M_j->i enters as
(a) the observed fact 'j sent X' -- an ``informs`` relation, evidence = the message id -- and (b) X itself: an
observation (evidence reference) or an opinion (a Proposal with author ``session:<j>``). Only the rules below change a
value, and a peer opinion never does. Rules (deterministic, no clock: ``now`` is an argument):

    F1 accept    an observation with evidence for a ref this node needs and has no value for: value set, basis
                 OBSERVED, source = the sender (or self), evidence kept
    F2 conflict  same ref, a different value, different evidence: a ``contradicts`` relation and the ref is marked
                 uncertain; the value is never overwritten
    F3 dedupe    the same evidence id through several peers counts once (NETWORK.md 3.3)
    F4 propose   an opinion is a Proposal; it is listed, never applied
    F5 request   a peer's request is a fact 'j asked for r' kept for the node's own policy; never a task by itself

The State file is ``.ga/nodes/<me>/state.json``; its export for peers (``export.json``) is a thin
llmsensor.state-export/2 view: one record per ref, the value, status, basis and evidence refs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CONTRACT = "llmsensor.state-export/2"  # dc.sources.SENSOR_CONTRACT (tests/test_ga32_net.py checks it)


@dataclass
class State:
    me: str
    facts: dict[str, dict[str, Any]] = field(default_factory=dict)      # ref -> {value, basis, source, evidence, uncertain}
    relations: list[list[str]] = field(default_factory=list)            # [predicate, a, b, evidence]
    proposals: list[dict[str, Any]] = field(default_factory=list)       # {ref, value, why, author, msg_id}
    requests: dict[str, dict[str, Any]] = field(default_factory=dict)   # msg_id -> {from, ref, needs, input, status}
    consults: dict[str, dict[str, Any]] = field(default_factory=dict)   # ref -> {to, msg_id}  (asked, no answer yet)
    evidence_seen: list[str] = field(default_factory=list)
    transitions: list[dict[str, Any]] = field(default_factory=list)     # the rule log: {rule, ref, source, evidence}
    done: dict[str, Any] = field(default_factory=dict)                  # task id -> {"verified": bool, "answer": ...}

    # -- views --
    def value(self, ref: str) -> str | None:
        f = self.facts.get(ref)
        return f["value"] if f and not f.get("uncertain") else None

    def valid(self, ref: str) -> bool:
        return self.value(ref) is not None

    def uncertain(self) -> list[str]:
        return sorted(r for r, f in self.facts.items() if f.get("uncertain"))

    def missing(self, required: list[str]) -> list[str]:
        return sorted(r for r in required if r not in self.facts)

    # -- rules --
    def _log(self, rule: str, ref: str, source: str, evidence: str) -> None:
        self.transitions.append({"rule": rule, "ref": ref, "source": source, "evidence": evidence})

    def observe(self, ref: str, value: str, evidence: str, source: str, required: list[str]) -> str:
        """F1/F2/F3 for one observation. -> 'accepted' | 'duplicate' | 'contradicts' | 'same' | 'not_needed'."""
        if evidence in self.evidence_seen:
            return "duplicate"
        cur = self.facts.get(ref)
        if cur is None:
            if source != "self" and ref not in required:
                return "not_needed"  # a peer observation of a ref this node does not use stays a received fact only
            self.evidence_seen.append(evidence)
            self.facts[ref] = {"value": value, "basis": "OBSERVED", "source": source, "evidence": [evidence],
                               "uncertain": False}
            self._log("F1", ref, source, evidence)
            return "accepted"
        self.evidence_seen.append(evidence)
        if cur["value"] == value:
            cur["evidence"] = sorted(set(cur["evidence"]) | {evidence})
            return "same"
        self.relations.append(["contradicts", f"{ref}@{cur['evidence'][0]}", f"{ref}@{evidence}", evidence])
        if not cur.get("uncertain"):
            cur["uncertain"] = True
            self._log("F2", ref, source, evidence)
        return "contradicts"

    def receive(self, sender: str, msg_id: str, item: dict[str, Any], required: list[str]) -> str:
        """A valid peer item from ``sender``. Always records 'sender sent X' (informs); then F1-F5 by kind."""
        self.relations.append(["informs", f"session:{sender}", f"session:{self.me}", msg_id])
        k = item["kind"]
        if k == "observation":
            out = self.observe(item["ref"], item["value"], item["evidence"], f"session:{sender}", required)
            if item.get("re"):
                for ref, c in list(self.consults.items()):
                    if c.get("msg_id") == item["re"]:
                        self.consults.pop(ref)
            return out
        if k == "opinion":
            self.proposals.append({"ref": item["ref"], "value": item["value"], "why": item.get("why", ""),
                                   "author": f"session:{sender}", "msg_id": msg_id})
            return "proposal"
        self.requests[msg_id] = {"from": sender, "ref": item["ref"], "needs": item.get("needs", {}),
                                 "input": item.get("input"), "status": "open"}
        return "request"

    # -- files --
    def to_dict(self) -> dict[str, Any]:
        return {"schema": "ga-node-state/1", "me": self.me, "facts": self.facts, "relations": self.relations,
                "proposals": self.proposals, "requests": self.requests, "consults": self.consults,
                "evidence_seen": self.evidence_seen, "transitions": self.transitions, "done": self.done}

    @classmethod
    def from_dict(cls, me: str, d: dict[str, Any] | None) -> "State":
        d = d or {}
        return cls(me, **{k: d[k] for k in ("facts", "relations", "proposals", "requests", "consults", "evidence_seen",
                                            "transitions", "done") if k in d})

    def export(self, capabilities: list[str]) -> dict[str, Any]:
        recs = [{"contract": CONTRACT, "entity": f"session:{self.me}", "name": ref, "value": f["value"],
                 "status": "UNKNOWN" if f.get("uncertain") else "OBSERVED", "basis": f["basis"],
                 "evidence_refs": list(f["evidence"])} for ref, f in sorted(self.facts.items())]
        return {"contract": CONTRACT, "node": self.me, "capabilities": sorted(capabilities), "records": recs}
