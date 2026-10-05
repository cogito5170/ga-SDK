"""Peer mode's work queue and node pool (CMD-GA33): nodes are allocated per work item, bounded, and retired when done.

Static ``network.nodes`` keep working unchanged and may be mixed with ``network.pool``:

    "pool": {"max_live": 3, "max_queue": 20, "max_spawn_per_round": 1, "max_depth": 2, "idle_rounds": 3,
             "roles": {"<role>": {"backends": [...], "catalog"?, "budget"?: {"runs", "work"}, "facts"?, "needs"?}}}

Files (the runtime writes them; a node writes only ``.ga/nodes/<id>/``):

    .ga/queue/<seq>-<id>.json        a pending work/1 item (append-only: one file per item, never rewritten);
    .ga/queue/done|failed/           the same file, moved when the item is finished
    .ga/pool.json                    the registry: live nodes (item claimed), per-role counters, attempts (atomic writes)
    .ga/roles/<role>/state.json,pi.json   what the role learnt; a new node of the role starts from it
    .ga/nodes/_retired/<id>/         a retired node's dir, kept
    .ga/pool/telemetry.jsonl         L0 facts: node.started, node.retired, work.accepted, work.dropped, work.failed

One round (``Pool.round``): step every live node, take proposals under the limits, retire the finished (merge State into
the role through state.py's rules, fold edges into role-pair pi), then start nodes for queued items in queue order
while live < max_live and starts this round < max_spawn_per_round. A node id is ``<role>_<n>`` (ga mail senders have
no '-'). Every step is idempotent: a crash between two writes never starts two nodes for one item.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from .. import l0
from ..forms import Problem, HARD
from .state import State

POOL_KEYS = ("max_live", "max_queue", "max_spawn_per_round", "max_depth", "idle_rounds", "roles")
DEFAULTS = {"max_live": 3, "max_queue": 20, "max_spawn_per_round": 1, "max_depth": 2, "idle_rounds": 3}
ROLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.]{0,29}$")
ITEM_ID = re.compile(r"^CMD-[A-Z]+\d+$")  # the id a node's report/2 head says it handled
ITEM_KEYS = {"schema", "id", "role", "goal", "uses", "needs", "answer", "check", "parent", "depth", "origin"}
PROPOSAL_KEYS = {"role", "goal", "uses", "needs", "answer"}  # no check: a node never names a command to run
ATTEMPTS = 3
WORK_BLOCK = re.compile(r"^```work[ \t]*\n(.*?)\n```[ \t]*$", re.MULTILINE | re.DOTALL)


def configured(network: Any) -> bool:
    return isinstance(network, dict) and isinstance(network.get("pool"), dict) and bool(network["pool"].get("roles"))


def problems_of(network: dict) -> list[Problem]:
    """``network.pool`` of ga-config/1 (called by net.problems_of)."""
    from . import _needs_problems
    pool = network.get("pool")
    if pool is None:
        return []
    out: list[Problem] = []
    bad = lambda p, m: out.append(Problem(p, m, HARD))  # noqa: E731
    if not isinstance(pool, dict):
        return [Problem("$.network.pool", "must be an object", HARD)]
    if network.get("mode") != "peer":
        bad("$.network.pool", "needs network.mode peer")
    for k in set(pool) - set(POOL_KEYS):
        bad(f"$.network.pool.{k}", "unknown key")
    for k in DEFAULTS:
        v = pool.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < (0 if k == "max_depth" else 1)):
            bad(f"$.network.pool.{k}", "must be a positive integer" + (" (0 allowed)" if k == "max_depth" else ""))
    roles = pool.get("roles", {})
    if not isinstance(roles, dict) or not roles:
        bad("$.network.pool.roles", "must map roles to node configs")
        roles = {}
    for name, n in roles.items():
        p = f"$.network.pool.roles.{name}"
        if not ROLE.match(name):
            bad(p, "a role is [A-Za-z0-9][A-Za-z0-9_.], up to 30")
        if name in network.get("nodes", {}):
            bad(p, "collides with a static node name")
        if not isinstance(n, dict):
            bad(p, "must be an object")
            continue
        if not isinstance(n.get("backends"), list) or not all(isinstance(b, str) for b in n["backends"]):
            bad(p + ".backends", "must list the backend plugins the node may use")
        if "needs" in n:
            out += _needs_problems(n["needs"], p + ".needs")
        if not isinstance(n.get("facts", {}), dict):
            bad(p + ".facts", "must map refs to {value, evidence}")
        b = n.get("budget", {})
        if not isinstance(b, dict) or set(b) - {"runs", "work"} or any(
                isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in b.values()):
            bad(p + ".budget", "must be {runs?, work?} (non-negative integers)")
        if "task" in n:
            bad(p + ".task", "a pool role takes its task from the work item")
    return out


def item_problems(item: Any, roles: dict) -> list[str]:
    """A work/1 item: {id, role, goal, uses?, needs?, answer?, check?, parent?, depth}."""
    from . import _needs_problems
    if not isinstance(item, dict):
        return ["a work item is an object"]
    out = [f"unknown key {k!r}" for k in sorted(set(item) - ITEM_KEYS)]
    if item.get("schema", "work/1") != "work/1":
        out.append("schema is work/1")
    if not isinstance(item.get("id"), str) or not ITEM_ID.match(item["id"]):
        out.append("id is CMD-<PREFIX><number>")
    if item.get("role") not in roles:
        out.append(f"unknown role {item.get('role')!r}")
    if not isinstance(item.get("goal"), str) or not item["goal"].strip():
        out.append("goal is a non-empty string")
    u = item.get("uses", [])
    if not isinstance(u, list) or not all(isinstance(x, str) for x in u):
        out.append("uses lists refs")
    if "needs" in item:
        out += [str(p) for p in _needs_problems(item["needs"], "needs")]
    if "answer" in item and not isinstance(item["answer"], str):
        out.append("answer is a ref")
    if "check" in item and not (isinstance(item["check"], list) and all(isinstance(x, str) for x in item["check"])):
        out.append("check is an argv list")
    d = item.get("depth", 0)
    if isinstance(d, bool) or not isinstance(d, int) or d < 0:
        out.append("depth is a non-negative integer")
    if "parent" in item and not isinstance(item["parent"], str):
        out.append("parent is an item id")
    return out


def work_blocks_in(text: str) -> list[Any]:
    """The work proposals a turn's answer holds (each ```work block: one object or a list; None = not JSON)."""
    out: list[Any] = []
    for b in WORK_BLOCK.findall(text or ""):
        try:
            v = json.loads(b)
        except json.JSONDecodeError:
            out.append(None)
            continue
        out.extend(v if isinstance(v, list) else [v])
    return out


def strip_work(text: str) -> str:
    return WORK_BLOCK.sub("", text or "")


def _atomic(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name("." + p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    tmp.replace(p)


def _read(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def node_spec(cfg: Any, ga_dir: Path, me: str) -> dict | None:
    """The config of a live pool node: its role's keys plus the claimed item as its task (None = not a live pool node)."""
    reg = _read(Path(ga_dir) / "pool.json") or {}
    live = (reg.get("live") or {}).get(me)
    role = ((cfg.network.get("pool") or {}).get("roles") or {}).get(live["role"]) if live else None
    if not live or role is None:
        return None
    it = live["item"]
    task = {"id": it["id"], "goal": it["goal"], "class": live["role"], "uses": list(it.get("uses", []))}
    needs = it.get("needs", role.get("needs"))
    for k, v in (("needs", needs), ("answer", it.get("answer")), ("check", it.get("check"))):
        if v is not None:
            task[k] = v
    return {**{k: v for k, v in role.items() if k != "needs"}, "task": task}


def peer_names(cfg: Any, ga_dir: Path) -> list[str]:
    """Static nodes plus the live pool nodes (the registry): who a node's peers, exports and pi edges are."""
    names = set(cfg.network.get("nodes", {}))
    if configured(cfg.network):
        names |= set((_read(Path(ga_dir) / "pool.json") or {}).get("live", {}))
    return sorted(names)


# ---------------------------------------------------------------------------------------------------- role State
def merge_state(role: State, node: State, node_id: str) -> None:
    """The node's State into the role's through the State rules (F1 accept, F2 contradicts -> uncertain, F3 dedupe).
    Never a plain overwrite: a fact the role holds is only changed to uncertain."""
    for ref, f in sorted(node.facts.items()):
        src = node_id if f["source"] == "self" else f["source"]
        for ev in f["evidence"]:
            role.observe(ref, f["value"], ev, src, [ref])
        if f.get("uncertain") and ref in role.facts and not role.facts[ref].get("uncertain"):
            role.facts[ref]["uncertain"] = True
            role._log("F2", ref, src, f["evidence"][0])
    seen = {json.dumps(r) for r in role.relations}
    for r in node.relations:
        if r[0] == "contradicts" and json.dumps(r) not in seen:
            role.relations.append(r)
            seen.add(json.dumps(r))
    for ev in node.evidence_seen:
        if ev not in role.evidence_seen:
            role.evidence_seen.append(ev)


def fold_edges(role_pi: dict, node_pi: dict, role_of: Callable[[str], str]) -> dict:
    """The node's edges into the role-pair pi: peer ids become peer roles, histories are appended without duplicates."""
    out = {"schema": "ga-role-pi/1", "interactions": dict(role_pi.get("interactions", {})),
           "observations": dict(role_pi.get("observations", {})), "flags": dict(role_pi.get("flags", {}))}
    for key in ("interactions", "observations"):
        for peer, rows in (node_pi.get(key) or {}).items():
            have = out[key].setdefault(role_of(peer), [])
            for r in rows:
                if r not in have:
                    have.append(r)
    for peer, v in (node_pi.get("flags") or {}).items():
        out["flags"][role_of(peer)] = v
    return out


def expand_edges(role_pi: dict, peers: list[str], role_of: Callable[[str], str]) -> dict:
    """A new node's pi.json from the role-pair pi: every live peer starts with its role's history."""
    out: dict[str, Any] = {"schema": "ga-node-pi/1", "interactions": {}, "observations": {}, "flags": {}, "pi": {}}
    for p in peers:
        r = role_of(p)
        for key in ("interactions", "observations"):
            if role_pi.get(key, {}).get(r):
                out[key][p] = [dict(x) for x in role_pi[key][r]]
        if r in role_pi.get("flags", {}):
            out["flags"][p] = role_pi["flags"][r]
    return out


# ---------------------------------------------------------------------------------------------------- the pool
class Pool:
    def __init__(self, cfg: Any, ga_dir: Path, *, clock: Callable[[], float] | None = None, **node_kw: Any):
        self.cfg, self.ga = cfg, Path(ga_dir)
        self.conf = {**DEFAULTS, **{k: v for k, v in cfg.network["pool"].items() if k in DEFAULTS}}
        self.roles: dict[str, dict] = cfg.network["pool"]["roles"]
        self.node_kw = dict(node_kw)
        if clock is not None:
            self.node_kw["clock"] = clock
        self.q = self.ga / "queue"

    # -- registry
    def reg(self) -> dict:
        r = _read(self.ga / "pool.json") or {}
        return {"schema": "ga-pool/1", "round": r.get("round", 0), "n": r.get("n", {}), "roles": r.get("roles", {}),
                "live": r.get("live", {}), "attempts": r.get("attempts", {})}

    def save(self, reg: dict) -> None:
        _atomic(self.ga / "pool.json", reg)

    def role_of(self, reg: dict) -> Callable[[str], str]:
        return lambda x: reg["roles"].get(x, x)

    def limit(self, role: str) -> int:
        return int((self.roles[role].get("budget") or {}).get("runs") or ATTEMPTS)

    def _l0(self, typ: str, **data: Any) -> None:
        l0.append(self.ga / "pool" / "telemetry.jsonl", {"spec": l0.SPEC, "type": typ, "source": "ga_pool", "data": data})

    # -- queue (append-only files)
    def _files(self, sub: str = "") -> list[Path]:
        d = self.q / sub if sub else self.q
        return sorted(p for p in d.glob("*.json") if p.is_file()) if d.exists() else []

    def ids(self) -> set[str]:
        return {p.stem.split("-", 1)[1] for s in ("", "done", "failed") for p in self._files(s)}

    def origins(self) -> set[str]:
        return {(_read(p) or {}).get("origin") for s in ("", "done", "failed") for p in self._files(s)} - {None}

    def _seq(self) -> int:
        return 1 + max([int(p.name.split("-", 1)[0]) for s in ("", "done", "failed") for p in self._files(s)] or [0])

    def pending(self, reg: dict | None = None) -> list[tuple[Path, dict]]:
        claimed = {v["item"]["id"] for v in (reg or self.reg())["live"].values()}
        out = []
        for p in self._files():
            it = _read(p)
            if isinstance(it, dict) and it.get("id") not in claimed:
                out.append((p, it))
        return out

    def add(self, item: dict) -> tuple[bool, str]:
        """``ga work add``: check and enqueue one item (depth defaults to 0)."""
        item = {k: v for k, v in item.items() if k != "schema"}
        item.setdefault("depth", 0)
        probs = item_problems(item, self.roles)
        if probs:
            return False, "; ".join(probs)
        if item["id"] in self.ids():
            return False, f"item {item['id']!r} exists"
        if len(self._files()) >= int(self.conf["max_queue"]):
            return False, "queue full"
        seq = self._seq()
        _atomic(self.q / f"{seq:06d}-{item['id']}.json", {"schema": "work/1", **item})
        return True, f"{seq:06d}-{item['id']}"

    def _move(self, item_id: str, sub: str) -> None:
        for p in self._files():
            if p.name.split("-", 1)[1][:-5] == item_id:
                (self.q / sub).mkdir(parents=True, exist_ok=True)
                p.replace(self.q / sub / p.name)

    # -- role State
    def role_dir(self, role: str) -> Path:
        return self.ga / "roles" / role

    def _seed(self, node: str, role: str, reg: dict) -> None:
        d = self.ga / "nodes" / node
        d.mkdir(parents=True, exist_ok=True)
        rs = _read(self.role_dir(role) / "state.json")
        if not (d / "state.json").exists():  # always written at the claim: the node starts from the role State as of then
            s = State.from_dict(node, rs)
            s.requests, s.consults, s.proposals, s.done, s.transitions = {}, {}, [], {}, []
            _atomic(d / "state.json", s.to_dict())
        if not (d / "pi.json").exists():
            peers = [p for p in peer_names(self.cfg, self.ga) if p != node]
            _atomic(d / "pi.json", expand_edges(_read(self.role_dir(role) / "pi.json") or {}, peers, self.role_of(reg)))

    def _started_logged(self, node: str) -> bool:
        f = self.ga / "pool" / "telemetry.jsonl"
        return f.exists() and any(f'"node": "{node}"' in ln and "node.started" in ln for ln in f.read_text().splitlines())

    # -- one round
    def round(self, step: Callable[[str], dict] | None = None) -> list[dict]:
        from .node import Node
        step = step or (lambda me: Node(self.cfg, me, ga_dir=self.ga, **self.node_kw).step())
        reg = self.reg()
        reg["round"] += 1
        events: dict[str, list] = {"started": [], "retired": [], "work": []}
        for node in sorted(reg["live"]):  # recovery: a node claimed but not yet seeded/logged (crash between writes)
            self._seed(node, reg["live"][node]["role"], reg)
            if not self._started_logged(node):
                self._l0("node.started", node=node, role=reg["live"][node]["role"], item=reg["live"][node]["item"]["id"])
        self.save(reg)
        outs = []
        for node in sorted(reg["live"]):
            if not reg["live"][node].get("retiring"):
                o = step(node)
                outs.append(o)
                live = reg["live"][node]
                live["idle"] = live.get("idle", 0) + 1 if o.get("outcome") in ("idle", "waiting") else 0
        self.save(reg)
        for node in sorted(reg["live"]):
            events["work"] += self._proposals(reg, node)
        self.save(reg)
        for node in sorted(reg["live"]):
            how = self._finished(reg, node)
            if how:
                reg["live"][node]["retiring"] = how
                self.save(reg)  # decided: a restart finishes the retire, never restarts the node
            if reg["live"][node].get("retiring"):
                events["retired"].append(self._retire(reg, node))
        reg["live"] = {k: v for k, v in reg["live"].items() if not v.get("retiring_done")}
        self.save(reg)
        events["started"] = self._start(reg)
        outs.append({"pool": {"round": reg["round"], **events, "live": sorted(reg["live"])}})
        return outs

    def _finished(self, reg: dict, node: str) -> str | None:
        live = reg["live"][node]
        if live.get("retiring"):
            return live["retiring"]
        st = _read(self.ga / "nodes" / node / "state.json") or {}
        d = (st.get("done") or {}).get(live["item"]["id"])
        if d:
            return "done" if d.get("verified") else "failed"
        if live.get("idle", 0) >= int(self.conf["idle_rounds"]):
            return "idle"
        return None

    def _retire(self, reg: dict, node: str) -> dict:
        live = reg["live"][node]
        role, how, iid = live["role"], live["retiring"], live["item"]["id"]
        src = self.ga / "nodes" / node
        dst = self.ga / "nodes" / "_retired" / node
        if src.exists():
            ns = State.from_dict(node, _read(src / "state.json"))
            rs = State.from_dict(role, _read(self.role_dir(role) / "state.json"))
            merge_state(rs, ns, node)
            _atomic(self.role_dir(role) / "state.json", rs.to_dict())
            rp = fold_edges(_read(self.role_dir(role) / "pi.json") or {}, _read(src / "pi.json") or {}, self.role_of(reg))
            _atomic(self.role_dir(role) / "pi.json", rp)
        if how == "done":
            self._move(iid, "done")
        elif how == "failed":
            self._move(iid, "failed")
        else:
            reg["attempts"][iid] = reg["attempts"].get(iid, 0) + 1  # idle: the item waits for a new node
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(src), str(dst))
        live["retiring_done"] = True
        self._l0("node.retired", node=node, role=role, item=iid, outcome=how)
        return {"node": node, "item": iid, "outcome": how}

    def _proposals(self, reg: dict, node: str) -> list[dict]:
        run = _read(self.ga / "nodes" / node / "run.json")
        if not run or run.get("work_done", 0) >= run.get("work_n", 0) or reg["live"][node].get("retiring"):
            return []
        live, out = reg["live"][node], []
        for e in run.get("work_out", []):
            if e["n"] <= run.get("work_done", 0):
                continue
            ok, why, wid = self._accept(reg, node, e)
            out.append({"node": node, "id": wid, "accepted": ok, "reason": why})
            self._l0("work.accepted" if ok else "work.dropped", node=node, item=live["item"]["id"], id=wid, reason=why)
        run["work_done"] = run["work_n"]
        run["work_out"] = []
        _atomic(self.ga / "nodes" / node / "run.json", run)
        return out

    def _accept(self, reg: dict, node: str, e: dict) -> tuple[bool, str, str]:
        live, p = reg["live"][node], e["p"]
        wid = f"{live['item']['id']}.w{e['n']}"  # the proposal's origin: one item per proposal, even across a restart
        if not isinstance(p, dict) or set(p) - PROPOSAL_KEYS:
            return False, "not a proposal {role, goal, uses?, needs?, answer?}", wid
        if p.get("role") not in self.roles:
            return False, "unknown role", wid
        depth = int(live["item"].get("depth", 0)) + 1
        if depth >= int(self.conf["max_depth"]):
            return False, f"depth {depth} >= max_depth {self.conf['max_depth']}", wid
        if len(self._files()) >= int(self.conf["max_queue"]):
            return False, "queue full", wid
        if wid in self.origins():
            return True, "already accepted", wid
        room = int((self.roles[live["role"]].get("budget") or {}).get("work", 2))
        if live.get("children", 0) >= room:
            return False, "node work budget used", wid
        cid = self._seq()
        while f"CMD-W{cid}" in self.ids():
            cid += 1
        ok, why = self.add({**p, "id": f"CMD-W{cid}", "origin": wid, "parent": live["item"]["id"], "depth": depth})
        if ok:
            live["children"] = live.get("children", 0) + 1
        return ok, "accepted" if ok else why, wid

    def _start(self, reg: dict) -> list[dict]:
        started = []
        for path, item in self.pending(reg):
            if len(reg["live"]) >= int(self.conf["max_live"]) or len(started) >= int(self.conf["max_spawn_per_round"]):
                break
            role = item["role"]
            if reg["attempts"].get(item["id"], 0) >= self.limit(role):
                self._move(item["id"], "failed")
                self._l0("work.failed", item=item["id"], role=role, reason="runs budget exhausted")
                continue
            n = reg["n"][role] = reg["n"].get(role, 0) + 1
            node = f"{role}_{n}"
            reg["roles"][node] = role
            reg["live"][node] = {"role": role, "item": item, "since": reg["round"], "idle": 0, "children": 0}
            self.save(reg)  # the claim: from here the item is not pending for anyone else
            self._seed(node, role, reg)
            self._l0("node.started", node=node, role=role, item=item["id"])
            started.append({"node": node, "item": item["id"]})
        return started

    # -- status
    def status(self) -> dict:
        reg = self.reg()
        roles = {}
        for r in sorted(self.roles):
            s = _read(self.role_dir(r) / "state.json") or {}
            roles[r] = {"facts": len(s.get("facts", {})),
                        "uncertain": sorted(k for k, f in s.get("facts", {}).items() if f.get("uncertain"))}
        return {"caps": dict(self.conf), "round": reg["round"],
                "live": {n: {"role": v["role"], "item": v["item"]["id"], "idle": v.get("idle", 0)}
                         for n, v in sorted(reg["live"].items())},
                "queue": [it["id"] for _, it in self.pending(reg)],
                "done": [p.name.split("-", 1)[1][:-5] for p in self._files("done")],
                "failed": [p.name.split("-", 1)[1][:-5] for p in self._files("failed")], "roles": roles}
