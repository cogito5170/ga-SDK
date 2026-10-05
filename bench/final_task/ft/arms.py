"""The three structures (FINAL_TASK section 2), 3 nodes each, every model call one bare ``claude -p`` through ``Run.llm``.

    A  a long-lived LLM hub judges and directs: hub call (directive) -> fresh worker -> hub call (judge); the hub's second
       call carries the first call's prompt and reply again (the hub keeps its conversation, claude -p resends it)
    B  the hub is code (ga tick + ga judge): routes by capability, merges node exports for missing refs (State rules F1-F3),
       then one fresh worker; a `need` the worker raises is served by the code hub
    C  ga/net peers, code runtime: ga.net.dc.decide picks consults (pi, edge budget 2), State.receive applies the answers,
       then the node's own worker turn

The same rules 1-5 text goes to every model call in every arm. Context (selective) is built with ga.ctxpack; bulk puts the
repository's files whole in front of every call.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ga.backends.base import BackendError
from ga.ctxpack import build, compact, tokens
from ga.net import dc
from ga.net.pi import Edges, NetConfig
from ga.net.state import State

from . import quota
from .backend import CallResult
from .budget import Budget
from .world import FIX, Task, build_state

MAX_CALLS = {"A": 4, "B": 3, "C": 4}   # worst case per run, checked against the cap before the run starts
EDGE_BUDGET = 2                        # C: peer consults per run
PACK_CAP = 8000

RULES = """Rules (every node and the hub, always):
1. Use only the context you are given.
2. A State item marked VALID is already known. Use it; do not re-derive it and do not ask a tool or a peer for it.
3. If the information needed is not in the context, answer UNKNOWN. Never guess and never fill a value in.
4. Ask a peer or a tool only when the information is missing and cannot be derived. Otherwise do not.
5. Reply with exactly one JSON object and nothing else."""

WORKER_FORMAT = ('Reply: {"answer": <string, or object when the task says so; "UNKNOWN" when the information is missing>, '
                 '"need": null | {"kind": "peer", "to": "<node>", "ref": "<ref>"} | {"kind": "tool", "ref": "<ref>"}}')
HUB_FORMAT = ('Reply to a directive request with {"worker": "<node>", "peer": null | "<node>" | ["<node>", ...] (the peers to consult before the worker starts), '
              '"instruction": "<one or two sentences>"}. Reply to a judge request with {"answer": <the final answer, in the '
              'same form the task asks for, or the string "WORKER" to accept the worker\'s answer exactly as it is>}.')


class RunFailed(RuntimeError):
    pass


def h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]


def parse_reply(text: str) -> dict | None:
    """The last JSON object in a reply (fences and prose around it tolerated). None when there is none."""
    dec, best, i = json.JSONDecoder(), None, 0
    while True:
        j = text.find("{", i)
        if j < 0:
            return best
        try:
            obj, end = dec.raw_decode(text[j:])
            if isinstance(obj, dict):
                best = obj
            i = j + end
        except ValueError:
            i = j + 1


@dataclass
class Run:
    task: Task
    model: str
    mode: str                      # selective | bulk
    backend: Any
    budget: Budget
    run_id: str
    bulk: dict | None = None
    nodes: dict = field(init=False)
    states: dict = field(init=False)
    claims: dict = field(default_factory=dict)       # ref -> [(value, evidence id, source)]
    recv_evidence: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    call_rows: list = field(default_factory=list)
    unit_log: list = field(default_factory=list)
    tool_calls: int = 0
    peer_messages: int = 0
    rederived: int = 0
    ctx_used: dict = field(default_factory=lambda: {"keep": 0, "summarize": 0, "retrieve": 0, "peer": 0})
    ctx_avail: int = 0
    queries: list = field(default_factory=list)
    events: list = field(default_factory=list)

    def __post_init__(self):
        self.nodes = self.task.nodes
        self.states = {n: build_state(node, self.task.required) for n, node in self.nodes.items()}

    # ---- the one place a model is called -----------------------------------------------------------------------
    def llm(self, role: str, system: str, prompt: str, units: list[str], used: dict, avail: int,
            image: str | None = None) -> CallResult:
        if self.mode == "bulk":
            prompt = self.bulk["text"] + prompt
        p_in, p_out = quota.prices(self.model)
        self.budget.before_call(tokens(prompt + system) * p_in + 1500 * p_out)
        try:
            res = self.backend.call(self.model, system, prompt, image)
        except BackendError as e:
            self.budget.record({"n": len(self.budget.rows) + 1, "run_id": self.run_id, "role": role, "model": self.model,
                                "usage": {}, "total_cost_usd": None, "quota_usd": 0.0, "error": str(e)[:200]})
            raise RunFailed(str(e)[:200]) from e
        q = quota.quota_usd(self.model, res.usage)
        self.budget.record({"n": len(self.budget.rows) + 1, "run_id": self.run_id, "role": role, "model": self.model,
                            "usage": res.usage, "total_cost_usd": res.total_cost_usd, "quota_usd": q,
                            "model_usage": res.model_usage})
        self.calls.append({"usage": res.usage, "total_cost_usd": res.total_cost_usd})
        self.call_rows.append({"role": role, "input": quota.call_input(res.usage), "output": quota.usage_parts(res.usage)["output"],
                               "quota_usd": q, "total_cost_usd": res.total_cost_usd})
        self.unit_log.append(units)
        for k, v in used.items():
            self.ctx_used[k] += v
        self.ctx_avail += avail
        return res

    # ---- context ---------------------------------------------------------------------------------------------
    def state_view(self, node: str) -> tuple[str, list[str], int]:
        """State lines for the task's required refs at ``node`` (selective: nothing else), their units, item count."""
        st, lines, units, evid = self.states[node], [], [], {}
        for ref in self.task.required:
            f = st.facts.get(ref)
            if f and not f.get("uncertain"):
                lines.append(f"- {ref} = {f['value']}  [VALID, evidence {f['evidence'][0]}]")
                units.append(f"state:{ref}={f['value']}@{f['evidence'][0]}")
                evid[f["evidence"][0]] = self._ev(node, f["evidence"][0])
            elif f:
                cl = self.claims.get(ref, [])
                lines.append(f"- {ref}: CONTRADICTED, no value is valid yet; claims: " +
                             "; ".join(f"{v} [{e}, from {s}]" for v, e, s in cl))
                for v, e, s in cl:
                    units.append(f"state:{ref}={v}@{e}")
                    evid[e] = self._ev(node, e)
            else:
                lines.append(f"- {ref}: UNKNOWN (no value in State; no peer or tool has given one)")
                units.append(f"state:{ref}=UNKNOWN")
        n = len(lines)
        for e, t in sorted(evid.items()):
            lines.append(f"  evidence {e}: {t}")
            units.append(f"ev:{e}:{h(t)}")
        return "\n".join(lines), units, n

    def _ev(self, node: str, evid: str) -> str:
        if evid in self.recv_evidence:
            return self.recv_evidence[evid]
        return self.nodes[node].evidence.get(evid, "(no text)")

    def avail_candidates(self, node: str) -> int:
        n = len(self.nodes[node].facts) + sum(len(o.facts) for k, o in self.nodes.items() if k != node)
        return n + (len(self.task.ref_files) if "repo" in self.nodes[node].caps else 0)

    def node_prompt(self, node: str, instruction: str = "", extra: list[str] = ()) -> tuple[str, list[str], dict, int, str | None]:
        t = self.task
        text, units, nkeep = self.state_view(node)
        files, used_files = [], 0
        if "repo" in self.nodes[node].caps:
            for f in t.ref_files:
                files.append((f, (FIX / "t2" / f).read_text(encoding="utf-8")))
                units.append(f"file:{f}#{h(files[-1][1])}")
            used_files = len(files)
        image = t.image if (t.image and "vision" in self.nodes[node].caps) else None
        pack = build({"directive": t.id, "node": node, "caps": self.nodes[node].caps}, cap=PACK_CAP,
                     state=f"## State of {node} (the refs this task needs)\n" + text, files=files, peer=list(extra))
        for e in extra:
            units.append("peer:" + h(e))
        body = (f"{pack.text}\n## task\n{t.question}\n" + (f"\n## instruction from the hub\n{instruction}\n" if instruction else "")
                + ("\n(An image is attached to this message.)\n" if image else "") + "\n" + WORKER_FORMAT + "\n")
        used = {"keep": nkeep, "retrieve": used_files, "peer": len(extra)}
        return body, units, used, self.avail_candidates(node), image

    def worker_system(self, node: str) -> str:
        return (f"You are node {node} of a three-node cluster (nodes N1, N2, N3). Capabilities of {node}: "
                f"{', '.join(self.nodes[node].caps)}.\n{RULES}")

    # ---- one fresh worker turn --------------------------------------------------------------------------------
    def worker(self, node: str, instruction: str = "", extra: list[str] = (), role: str = "worker") -> tuple[dict | None, str]:
        body, units, used, avail, image = self.node_prompt(node, instruction, extra)
        res = self.llm(role, self.worker_system(node), body, units, used, avail, image)
        return parse_reply(res.text), res.text

    # ---- peers and tools ---------------------------------------------------------------------------------------
    def serve_peer(self, asker: str, peer: str, ref: str) -> dict | None:
        """A request from ``asker`` to ``peer`` for ``ref`` and the reply: 2 peer messages. The peer answers from its State;
        when it only has the capability (vision), its answer is one model call. -> the observation item or None."""
        if peer not in self.nodes or peer == asker or ref not in self.task.required:
            return None
        if self.states[asker].valid(ref):
            self.rederived += 1
        self.peer_messages += 2
        pn, pst = self.nodes[peer], self.states[peer]
        item = None
        if pst.valid(ref):
            f = pst.facts[ref]
            item = {"kind": "observation", "ref": ref, "value": f["value"], "evidence": f["evidence"][0],
                    "text": self._ev(peer, f["evidence"][0])}
        elif ref in pn.local_obs and set(self.task.ref_needs.get(ref, {}).get("capabilities", [])) <= set(pn.caps):
            img = self.task.image if "vision" in pn.caps else None
            prompt = (f"## request from {asker}\nref {ref}\n{self.task.observe_prompt.get(ref, '')}\n"
                      + ("\n(An image is attached to this message.)\n" if img else "") + "\n" + WORKER_FORMAT + "\n")
            res = self.llm("peer", self.worker_system(peer), prompt, [f"req:{ref}"], {"peer": 1}, 1, img)
            rep = parse_reply(res.text) or {}
            ans = rep.get("answer")
            if isinstance(ans, (str, int)) and str(ans).strip() and str(ans).strip().upper() != "UNKNOWN":
                evid = f"img:{self.task.image.rsplit('/', 1)[-1]}@{peer}"
                item = {"kind": "observation", "ref": ref, "value": str(ans).strip().replace(" ", ""), "evidence": evid,
                        "text": f"{peer} read the attached image: {str(ans).strip()}"}
                pst.observe(ref, item["value"], evid, "self", [ref])
        self.events.append({"type": "peer.message", "from": asker, "to": peer, "ref": ref, "answered": item is not None})
        if item:
            self.receive(asker, peer, item)
        return item

    def receive(self, node: str, sender: str, item: dict) -> str:
        st = self.states[node]
        self.recv_evidence[item["evidence"]] = item["text"]
        before = st.facts.get(item["ref"])
        if before and not self.claims.get(item["ref"]):
            self.claims[item["ref"]] = [(before["value"], before["evidence"][0], "self")]
        out = st.receive(sender, f"{sender}>{node}:{item['ref']}", item, self.task.required)
        if out in ("contradicts", "same", "accepted"):
            self.claims.setdefault(item["ref"], [])
            if (item["value"], item["evidence"], f"session:{sender}") not in self.claims[item["ref"]]:
                self.claims[item["ref"]].append((item["value"], item["evidence"], f"session:{sender}"))
        return out

    def tool(self, node: str, ref: str) -> str:
        key = (node, ref)
        if key in self.queries or self.states[node].valid(ref):
            self.rederived += 1
        self.queries.append(key)
        self.tool_calls += 1
        self.events.append({"type": "tool.start", "node": node, "ref": ref})
        return self.nodes[node].local_obs.get(ref) or "no result"

    def serve_need(self, node: str, need: dict) -> list[str]:
        """The hub/runtime serves a worker's `need` (one round). -> peer-message lines for the next worker turn."""
        if not isinstance(need, dict):
            return []
        ref = str(need.get("ref") or "")
        if need.get("kind") == "tool":
            return [f"tool result for {ref}: {self.tool(node, ref)}"]
        if need.get("kind") == "peer":
            it = self.serve_peer(node, str(need.get("to") or ""), ref)
            return [f"{need.get('to')} replied: {it['ref']} = {it['value']} [evidence {it['evidence']}: {it['text']}]"] if it else \
                   [f"{need.get('to')} replied: no value for {ref}"]
        return []


def route(run: Run) -> str:
    t = run.task
    for n, node in run.nodes.items():
        if set(t.needs) <= set(node.caps):
            return n
    return t.target


# ---- arm B: code hub --------------------------------------------------------------------------------------------------

def arm_b(run: Run):
    t = run.task
    target = route(run)
    st = run.states[target]
    for ref in sorted(set(st.missing(t.required)) | set(st.uncertain())):
        for n, node in run.nodes.items():                      # the hub reads the nodes' exports (state files): no model, no peer message
            if n == target:
                continue
            for rec in dc.valid_records(run.states[n].export(node.caps)).values():
                if rec["name"] == ref:
                    run.receive(target, n, {"kind": "observation", "ref": ref, "value": rec["value"],
                                            "evidence": rec["evidence_refs"][0], "text": run._ev(n, rec["evidence_refs"][0])})
    rep, raw = run.worker(target)
    if rep and rep.get("need"):
        extra = run.serve_need(target, rep["need"])
        rep, raw = run.worker(target, extra=extra, role="worker2")
    return rep


# ---- arm C: ga/net peers ----------------------------------------------------------------------------------------------

def arm_c(run: Run):
    t = run.task
    me = t.target
    node, st = run.nodes[me], run.states[me]
    exports = {n: run.states[n].export(o.caps) for n, o in run.nodes.items() if n != me}
    can = lambda ref: ref in node.local_obs and set(t.ref_needs.get(ref, {}).get("capabilities", [])) <= set(node.caps)  # noqa: E731
    decisions = dc.decide(me, st, t.required, exports, Edges(), NetConfig(), 0.0, can_observe=can,
                          ref_needs=lambda r: t.ref_needs.get(r, {}))
    edges_used = 0
    for d in decisions:
        if d.action != "consult" or edges_used >= EDGE_BUDGET:
            continue
        run.serve_peer(me, d.peer, d.ref)
        edges_used += 1
        for k, e in exports.items():                           # verify: other peers that hold the same ref (State export), within the edge budget
            if k != d.peer and edges_used < EDGE_BUDGET and d.ref in dc.valid_records(e):
                run.serve_peer(me, k, d.ref)
                edges_used += 1
    rep, raw = run.worker(me)
    if rep and rep.get("need") and edges_used < EDGE_BUDGET:
        extra = run.serve_need(me, rep["need"])
        rep, raw = run.worker(me, extra=extra, role="worker2")
    return rep


# ---- arm A: long-lived LLM hub ----------------------------------------------------------------------------------------

def hub_system() -> str:
    return ("You are the hub: a long-lived coordinator of the three-node cluster (N1, N2, N3). You direct a fresh worker "
            "session for each task and then judge its result.\n" + RULES + "\n" + HUB_FORMAT)


def cluster_view(run: Run) -> tuple[str, list[str], int, int]:
    lines, units, n = [], [], 0
    for name, node in run.nodes.items():
        text, u, k = run.state_view(name)
        lines.append(f"### {name}  capabilities: {', '.join(node.caps)}\n{text}")
        units += u
        n += k
    return "\n".join(lines), units, n, sum(len(o.facts) for o in run.nodes.values())


def arm_a(run: Run):
    t = run.task
    view, units, n, avail = cluster_view(run)
    pack = build({"directive": t.id, "role": "hub"}, cap=PACK_CAP, state="## cluster view (the refs this task needs)\n" + view)
    p1 = f"{pack.text}\n## task\n{t.question}\n\nDirective request: choose the worker node and write its instruction.\n"
    r1 = run.llm("hub", hub_system(), p1, units, {"keep": n}, avail)
    d = parse_reply(r1.text) or {}
    worker = d.get("worker") if d.get("worker") in run.nodes else t.target
    extra: list[str] = []
    peers = d.get("peer") if isinstance(d.get("peer"), list) else [d.get("peer")]
    for peer in dict.fromkeys(x for x in peers if x in run.nodes and x != worker):
        for ref in t.required:
            it = run.serve_peer(worker, peer, ref)
            if it:
                extra.append(f"{peer} replied: {ref} = {it['value']} [evidence {it['evidence']}: {it['text']}]")
    rep, raw = run.worker(worker, str(d.get("instruction") or ""), extra)
    if rep and rep.get("need") and not extra:
        extra = run.serve_need(worker, rep["need"])
    p2 = (f"{p1}\n(your directive reply: {compact(d)})\n\n## worker {worker} replied\n{raw.strip()[:6000]}\n"
          + ("\n## peer replies\n" + "\n".join(extra) + "\n" if extra else "") + "\nJudge request: give the final answer.\n")
    r2 = run.llm("hub_judge", hub_system(), p2, units + [f"peer:{h(e)}" for e in extra], {"keep": n}, avail)
    j = parse_reply(r2.text) or {}
    if j.get("answer") == "WORKER":
        return rep
    return j if "answer" in j else None


ARMS = {"A": arm_a, "B": arm_b, "C": arm_c}


def execute(run: Run, arm: str) -> dict:
    """One run -> its metrics row (final-task-metrics/1 values). A failed call ends the run with ``error``."""
    err, rep = None, None
    try:
        rep = ARMS[arm](run)
    except RunFailed as e:
        err = str(e)
    answer = rep.get("answer") if isinstance(rep, dict) else None
    correct, unk = run.task.grade(answer, run.tool_calls, run.peer_messages) if err is None else (False, None)
    seen: dict[str, int] = {}
    for units in run.unit_log:
        for u in set(units):
            seen[u] = seen.get(u, 0) + 1
    repeated = sum(c - 1 for c in seen.values() if c > 1) + run.rederived
    m = quota.metrics(run.model, run.calls, correct=correct, tool_calls=run.tool_calls, peer_messages=run.peer_messages,
                      ctx={"context_items_used": dict(run.ctx_used), "context_items_available": run.ctx_avail},
                      repeated=repeated, unknown_correct=unk)
    m.update({"repeated_parts": {"units_resent": repeated - run.rederived, "rederived": run.rederived},
              "answer": (json.dumps(answer, ensure_ascii=False)[:300] if answer is not None else None), "error": err,
              "calls": run.call_rows})
    return m
