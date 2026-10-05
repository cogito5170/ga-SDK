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

Real repository work (CMD-GA34; every key is optional and a config without them behaves as before):

    "pool": {..., "repo": {"path": "<local git repo>", "branch": "main", "judge"?: "<judge config path>"},
             "roles": {"<role>": {..., "tools"?: {"allow": ["Read", "Edit", "Write"], "permission_mode"?: "dontAsk"},
                                  "progress"?: true}}}
    work/1 gains  "after": [item ids]  (start only when all are done; a failed one fails the item)
                  "files": [globs]     (ownership: overlapping items never live together; the hand-in may touch only these)

With ``repo`` a started node gets a worktree ``.ga/worktrees/<node>/`` on branch ``ga/<node>`` from the integration head
(a done dependency is already in it), with the R3 pre-push hook of ga/adapters/git.py; its turns run there. A done
item is handed in: the runtime commits what the node left uncommitted, checks ownership, merges a moved integration head
into the node branch (never a rebase), runs ``ga judge`` (judge_commit) on the branch head against the integration
branch and fast-forwards the integration branch only on pass (L0 work.integrated / work.rejected). Retire removes the
worktree and keeps the branch.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .. import l0
from ..adapters import git as G
from ..forms import Problem, HARD
from .state import State

POOL_KEYS = ("max_live", "max_queue", "max_spawn_per_round", "max_depth", "idle_rounds", "roles", "repo")
DEFAULTS = {"max_live": 3, "max_queue": 20, "max_spawn_per_round": 1, "max_depth": 2, "idle_rounds": 3}
ROLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.]{0,29}$")
ITEM_ID = re.compile(r"^(?:CMD-[A-Z]+\d+|[A-Z][A-Z0-9]*-[A-Z]+-\d+)$")  # CMD-X1, or <PREFIX>-<LETTERS>-<n> (W-FE-01)
ITEM_KEYS = {"schema", "id", "role", "goal", "uses", "needs", "answer", "check", "parent", "depth", "origin",
             "after", "files", "done_when"}  # done_when: CMD-GA38 (executor: act), a command name or argv
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
        if "tools" in n:
            from ..backends.builtin import tools_problems
            for m in tools_problems(n["tools"]):
                bad(p + ".tools", m)
            no = [b for b in n.get("backends") or [] if not _takes_tools(b)]
            if no:
                bad(p + ".tools", f"backend(s) {', '.join(map(str, no))} cannot run a turn with tools (claude_cli can)")
        if "progress" in n and not isinstance(n["progress"], bool):
            bad(p + ".progress", "must be true or false")
        if "executor" in n:  # CMD-GA38 S5: executor act = the node runs ga act in its worktree, tools off
            if n["executor"] not in ("turn", "act"):
                bad(p + ".executor", "must be turn or act")
            elif n["executor"] == "act":
                if "tools" in n:
                    bad(p + ".executor", "act runs the model with its tools off; drop tools")
                if "repo" not in pool:
                    bad(p + ".executor", "act needs network.pool.repo (it works in the node's worktree)")
        a = n.get("act", {})
        if not isinstance(a, dict) or set(a) - {"max_turns", "max_tokens", "cap"} or any(
                isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in a.values()):
            bad(p + ".act", "must be {max_turns?, max_tokens?, cap?} (positive integers)")
    if "repo" in pool:
        r = pool["repo"]
        if not isinstance(r, dict) or set(r) - {"path", "branch", "judge"} or not isinstance(r.get("path"), str) \
                or not isinstance(r.get("branch", "main"), str) or not isinstance(r.get("judge", ""), str):
            bad("$.network.pool.repo", "must be {path, branch?, judge?} (a local git repository)")
        elif not G.SAFE_NAME.match(r.get("branch", "main")):
            bad("$.network.pool.repo.branch", "is not a safe branch name")
    return out


def _takes_tools(name: Any) -> bool:
    try:
        from .. import backends
        return bool(getattr(backends.get(name), "tools", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------------------------------- ownership globs
def _glob_re(g: str) -> re.Pattern:
    """A path glob: ``**`` any depth (``**/`` also none), ``*`` and ``?`` within one path segment."""
    out, i = "", 0
    while i < len(g):
        if g.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif g.startswith("**", i):
            out, i = out + ".*", i + 2
        elif g[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif g[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(g[i]), i + 1
    return re.compile(out + r"\Z")


def owned(path: str, globs: list[str]) -> bool:
    return any(_glob_re(g).match(path) for g in globs)


def _literal(g: str) -> str:
    return re.split(r"[*?\[]", g, 1)[0]


def overlap(a: list[str], b: list[str]) -> bool:
    """Whether two ownerships may share a path (conservative: two globs overlap when one's literal prefix is a prefix
    of the other's, or one glob matches the other written out)."""
    for x in a:
        for y in b:
            px, py = _literal(x), _literal(y)
            if px.startswith(py) or py.startswith(px) or owned(x, [y]) or owned(y, [x]):
                return True
    return False


def item_problems(item: Any, roles: dict) -> list[str]:
    """A work/1 item: {id, role, goal, uses?, needs?, answer?, check?, parent?, depth}."""
    from . import _needs_problems
    if not isinstance(item, dict):
        return ["a work item is an object"]
    out = [f"unknown key {k!r}" for k in sorted(set(item) - ITEM_KEYS)]
    if item.get("schema", "work/1") != "work/1":
        out.append("schema is work/1")
    if not isinstance(item.get("id"), str) or not ITEM_ID.match(item["id"]):
        out.append("id is CMD-<PREFIX><number> or <PREFIX>-<LETTERS>-<number>")
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
    a = item.get("after", [])
    if not isinstance(a, list) or not all(isinstance(x, str) and ITEM_ID.match(x) for x in a):
        out.append("after lists item ids")
    elif item.get("id") in a:
        out.append("after names the item itself")
    f = item.get("files", ["x"])
    if not isinstance(f, list) or not f or not all(
            isinstance(x, str) and x and not x.startswith("/") and ".." not in x.split("/") for x in f):
        out.append("files lists relative path globs")
    dw = item.get("done_when", "x")  # CMD-GA38
    if not (isinstance(dw, str) and dw) and not (isinstance(dw, list) and dw and all(isinstance(x, str) and x for x in dw)):
        out.append("done_when is a command name or an argv list")
    return out


def _cycle(graph: dict[str, list[str]], start: str) -> bool:
    """Whether ``start`` reaches itself through ``after`` edges."""
    seen, todo = set(), list(graph.get(start, []))
    while todo:
        x = todo.pop()
        if x == start:
            return True
        if x not in seen:
            seen.add(x)
            todo += graph.get(x, [])
    return False


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
    for k, v in (("needs", needs), ("answer", it.get("answer")), ("check", it.get("check")),
                 ("files", it.get("files")), ("done_when", it.get("done_when"))):  # files, done_when: CMD-GA38
        if v is not None:
            task[k] = v
    spec = {**{k: v for k, v in role.items() if k != "needs"}, "task": task}
    if live.get("ws"):
        spec["workdir"] = live["ws"]["path"]  # S1: the turn's cwd; the node's own files stay in .ga/nodes/<id>/
    return spec


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
    def __init__(self, cfg: Any, ga_dir: Path, *, clock: Callable[[], float] | None = None,
                 judge: Callable[..., Any] | None = None, **node_kw: Any):
        self.cfg, self.ga = cfg, Path(ga_dir)
        r = cfg.network["pool"].get("repo")
        self.repo = {"path": Path(os.path.realpath(cfg.resolve(r["path"]))), "branch": r.get("branch", "main"),
                     "judge": str(cfg.resolve(r["judge"])) if r.get("judge") else None} if r else None
        self.judge = judge
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
                "live": r.get("live", {}), "attempts": r.get("attempts", {}), **({"blocked": r["blocked"]} if r.get("blocked") else {})}

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
        if item.get("after"):  # S3: a dependency is queued (live included) or done; never failed, unknown or a cycle
            known = {p.name.split("-", 1)[1][:-5]: p for s in ("", "done") for p in self._files(s)}
            unknown = [a for a in item["after"] if a not in known]
            if unknown:
                return False, f"after: unknown item(s) {', '.join(unknown)} (neither queued nor done)"
            graph = {i: list((_read(q) or {}).get("after") or []) for i, q in known.items()}
            graph[item["id"]] = list(item["after"])
            if _cycle(graph, item["id"]):
                return False, f"after: a dependency cycle through {item['id']}"
        if len(self._files()) >= int(self.conf["max_queue"]):
            return False, "queue full"
        seq = self._seq()
        _atomic(self.q / f"{seq:06d}-{item['id']}.json", {"schema": "work/1", **item})
        return True, f"{seq:06d}-{item['id']}"

    def _state_of(self, item_id: str) -> str | None:
        for sub in ("done", "failed", ""):
            if any(p.name.split("-", 1)[1][:-5] == item_id for p in self._files(sub)):
                return sub or "queued"
        return None

    def _fail(self, item_id: str, role: str, reason: str) -> None:
        self._move(item_id, "failed")
        self._l0("work.failed", item=item_id, role=role, reason=reason)

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
            if self.repo and not reg["live"][node].get("retiring"):
                self._workspace(reg, node)
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
        if self.repo and live.get("ws") and "handin" not in live:
            if live["retiring"] == "done":
                ok, why = self._hand_in(reg, node)
                live["handin"] = why
                if not ok:
                    live["retiring"] = "failed"
            else:
                live["handin"] = self._commit_left(live, "wip")  # nothing is lost: the branch is kept
            self.save(reg)
        if self.repo and live.get("ws"):
            self._drop_worktree(live["ws"])
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
            if live.get("handin") and live["retiring"] == "failed" and self.repo:
                self._l0("work.failed", item=iid, role=role, reason=str(live["handin"])[:500])
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
                self._fail(item["id"], role, "runs budget exhausted")
                continue
            deps = {a: self._state_of(a) for a in item.get("after") or []}  # S3
            bad = sorted(a for a, st in deps.items() if st in ("failed", None))
            if bad:
                reg.get("blocked", {}).pop(item["id"], None)
                self._fail(item["id"], role, f"dependency failed: {', '.join(bad)}")
                continue
            waiting = sorted(a for a, st in deps.items() if st != "done")
            files = item.get("files")
            if not waiting and files:  # S4: an overlapping owner is live; the later item waits
                waiting = sorted(v["item"]["id"] for v in reg["live"].values()
                                 if v["item"].get("files") and overlap(files, v["item"]["files"]))
            if waiting:
                blocked = reg.setdefault("blocked", {})
                if item["id"] not in blocked:  # once per item while it waits
                    self._l0("work.blocked", item=item["id"], waiting_on=waiting)
                if blocked.get(item["id"]) != waiting:
                    blocked[item["id"]] = waiting
                    self.save(reg)
                continue
            n = reg["n"][role] = reg["n"].get(role, 0) + 1
            node = f"{role}_{n}"
            reg["roles"][node] = role
            reg["live"][node] = {"role": role, "item": item, "since": reg["round"], "idle": 0, "children": 0}
            reg.get("blocked", {}).pop(item["id"], None)
            self.save(reg)  # the claim: from here the item is not pending for anyone else
            if self.repo:
                self._workspace(reg, node)
            self._seed(node, role, reg)
            self._l0("node.started", node=node, role=role, item=item["id"])
            started.append({"node": node, "item": item["id"]})
        return started

    # -- the node workspace and the integration step (S1, S4, S5)
    def _workspace(self, reg: dict, node: str) -> None:
        """The node's worktree on ga/<node> from the integration head, with the R3 pre-push hook (idempotent)."""
        live = reg["live"][node]
        rd, integ = self.repo["path"], self.repo["branch"]
        if not live.get("ws"):
            base = G.git(rd, "rev-parse", "--verify", f"refs/heads/{integ}^{{commit}}")
            live["ws"] = {"path": os.path.realpath(self.ga / "worktrees" / node), "branch": f"ga/{node}", "base": base}
            self.save(reg)  # decided before created: a restart makes the same worktree
        ws = live["ws"]
        G.add_worktree(rd, Path(ws["path"]), ws["branch"], ws["base"])
        G.install_pre_push_hook(rd, Path(ws["path"]), self.ga / "hooks" / node, node, rd.name, ws["branch"])

    def _drop_worktree(self, ws: dict) -> None:
        if Path(ws["path"]).exists():
            G.git(self.repo["path"], "worktree", "remove", "--force", ws["path"])
        G.git(self.repo["path"], "worktree", "prune", check=False)

    def _commit_left(self, live: dict, what: str) -> str:
        """Commit what the node left uncommitted in its worktree (the runtime's hand-in commit); the branch head."""
        wt = live["ws"]["path"]
        if G.git(wt, "status", "--porcelain"):
            G.git(wt, "add", "-A")
            G.git(wt, "-c", "user.name=ga-node", "-c", "user.email=ga-node@localhost", "commit", "--quiet", "--no-verify",
                  "-m", f"{live['item']['id']}: {what}: {live['item']['goal'].splitlines()[0][:72]}")
        return G.git(wt, "rev-parse", "HEAD")

    def _outside(self, live: dict, base: str, head: str) -> list[str]:
        files = live["item"].get("files")
        return [f for f in G.changed_files(self.repo["path"], base, head)
                if f == ".ga" or f.startswith(".ga/") or (files and not owned(f, files))]

    def _hand_in(self, reg: dict, node: str) -> tuple[bool, str]:
        """(integrated?, why). Own-files check, merge of a moved integration head, judge, fast-forward only."""
        live = reg["live"][node]
        rd, integ, ws = self.repo["path"], self.repo["branch"], live["ws"]
        iid = live["item"]["id"]
        from .. import judge as J
        try:
            head = self._commit_left(live, "hand-in")
            bad = self._outside(live, ws["base"], head)
            if bad:
                return self._rejected(node, iid, head, "outside files: " + ", ".join(bad[:50]))
            ih = G.git(rd, "rev-parse", "--verify", f"refs/heads/{integ}^{{commit}}")
            if head == ih or G.is_ancestor(rd, head, ih):
                self._l0("work.integrated", node=node, item=iid, sha=ih, branch=ws["branch"], nothing=True)
                return True, "nothing to integrate"
            if not G.is_ancestor(rd, ih, head):  # the integration head moved: merge it in (never a rebase), judge again
                m = subprocess.run(["git", "-c", "user.name=ga-node", "-c", "user.email=ga-node@localhost", "merge",
                                    "--no-edit", "--quiet", "--no-verify", ih], cwd=ws["path"], capture_output=True)
                if m.returncode:
                    G.git(ws["path"], "merge", "--abort", check=False)
                    return self._rejected(node, iid, head, "merge conflict with " + integ)
                head = G.git(ws["path"], "rev-parse", "HEAD")
                bad = self._outside(live, ih, head)
                if bad:
                    return self._rejected(node, iid, head, "outside files: " + ", ".join(bad[:50]))
            jf = self.judge or J.judge_commit
            j = jf(rd, head, integ, config=self.repo["judge"])
            if j.cls != "success" or not j.ff:
                red = list(getattr(j, "failing", []) or [])
                return self._rejected(node, iid, head, f"judge {j.cls}" + (": " + ", ".join(red[:20]) if red else "")
                                      + ("" if red else "; " + "; ".join(j.notes[-2:])), failing=red)
            G.fast_forward_local(rd, integ, head)  # refuses a non-fast-forward; never forces
        except (G.GitError, J.JudgeError, OSError, subprocess.SubprocessError) as e:
            return self._rejected(node, iid, None, f"integration error: {e}"[:300])
        self._l0("work.integrated", node=node, item=iid, sha=head, branch=ws["branch"], tests=j.tests.get(j.repo))
        return True, f"integrated {head[:12]}"

    def _rejected(self, node: str, iid: str, head: str | None, why: str, failing: list | None = None) -> tuple[bool, str]:
        self._l0("work.rejected", node=node, item=iid, sha=head, reason=why[:500], failing=(failing or [])[:50])
        return False, why

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
                "queue": [it["id"] for _, it in self.pending(reg)], **({"blocked": dict(reg["blocked"])} if reg.get("blocked") else {}),
                "done": [p.name.split("-", 1)[1][:-5] for p in self._files("done")],
                "failed": [p.name.split("-", 1)[1][:-5] for p in self._files("failed")], "roles": roles}
