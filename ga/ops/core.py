"""The ops tick (CMD-GA57). One ``Ops.tick`` = VERIFY the pending actions, then one decision per new anomaly.

- Observations are read by code from the ga dir (``hub/shadow.jsonl``, ``hub/ledger``, ``ops/ledger``) and git: no model.
- ``table.json`` holds the rule/1 rows (first match wins) and the action-spec/1 rows (risk low|medium|high).
- The guard (rlo's action-spec/1 shape from ga.rlo.remote): low runs, medium runs and alerts, high never runs (alert).
- VERIFY: an executed action leaves {postcondition, deadline}; a later tick checks it. Unmet at the deadline is a
  failure: it re-enters one rung higher (the spec's ``escalate``); the same failure twice is one ``blocked`` alert and
  the subject is left alone. Nothing retries without bound.
- Only an anomaly no rule matches goes to a model: a fixed-size evidence card, one turn on the cheapest rung the
  ledgers show working, the answer is one action name from the table. Every turn is one ops ledger row with tokens.

State lives in ``<ga dir>/ops/``: state.json, decisions.jsonl (what the console shows), ledger/<UTC day>.jsonl.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

TABLE = Path(__file__).with_name("table.json")
OPS_TO = "baseline-ops"
OPS_FROM = "ga-ops"
CARD_TOKENS = 1500           # S4: the evidence card's cap (ga.ctxpack.tokens: ceil(bytes / 4))
# S6: the token loop's knobs (st["tune"]) and their bounds; a call over THRESHOLD tokens per item tries one change
TUNE = {"item_bytes": 600, "batch": 8, "rung": None, "threshold": 800, "promote_n": 3}
ITEM_BYTES_MIN, BATCH_MAX = 150, 32
STEPS = ("smaller_card", "cheaper_rung", "larger_batch")
ANSWER = re.compile(r"^\s*(\d+)\s*[:.)-]?\s*([A-Za-z_]+)")
SURVIVED = re.compile(r"^mutation (?P<label>.+?) survived: tests (?P<tests>.*?) do not cover it")
RISKS = ("low", "medium", "high")


def utc_now() -> float:
    return time.time()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    from ..hub import read_jsonl as rj
    return rj(path)


# ---------------------------------------------------------------------------------------------------------- table
def load_table(path: str | Path | None = None) -> dict[str, Any]:
    """The rule/1 + action-spec/1 table; every action in rlo's action-spec/1 shape (ga.rlo.remote) plus window_s."""
    from ..rlo.remote import _spec
    t = json.loads(Path(path or TABLE).read_text(encoding="utf-8"))
    actions = {}
    for a in t["actions"]:
        if a.get("risk") not in RISKS:
            raise ValueError(f"ops table: action {a.get('name')!r} risk must be one of {RISKS}")
        spec = _spec(a["name"], a["risk"], {}, a.get("description", ""))
        spec.update(preconditions=list(a.get("preconditions", [])), postcondition=list(a.get("postcondition", [])),
                    window_ms=int(a.get("window_s", 0)) * 1000)
        spec["window_s"], spec["escalate"] = int(a.get("window_s", 0)), a.get("escalate")
        actions[a["name"]] = spec
    for r in t["rules"]:
        if r.get("action") not in actions:
            raise ValueError(f"ops table: rule {r.get('id')!r} names unknown action {r.get('action')!r}")
    return {"rules": list(t["rules"]), "actions": actions}


def _cond(c: dict[str, Any], o: dict[str, Any]) -> bool:
    if "all" in c:
        return all(_cond(x, o) for x in c["all"])
    if "any" in c:
        return any(_cond(x, o) for x in c["any"])
    v = o.get(c["field"])
    if "eq" in c:
        return v == c["eq"] and type(v) is type(c["eq"])
    if "null" in c:
        return (v is None) == bool(c["null"])
    if "prefix" in c:
        return isinstance(v, str) and v.startswith(c["prefix"])
    if "nonempty" in c:
        return bool(v) == bool(c["nonempty"])
    if "contains" in c:
        return any(c["contains"] in str(x) for x in (v if isinstance(v, list) else [v] if v is not None else []))
    raise ValueError(f"ops table: unknown condition {sorted(c)}")


def match(table: dict[str, Any], o: dict[str, Any]) -> dict[str, Any] | None:
    """The first rule/1 whose ``when`` holds for observation ``o`` (None: no rule)."""
    return next((r for r in table["rules"] if _cond(r["when"], o)), None)


def guard(spec: dict[str, Any]) -> str:
    """S2: 'run' (low), 'run+alert' (medium), 'blocked' (high, and anything not low|medium)."""
    return {"low": "run", "medium": "run+alert"}.get(spec.get("risk"), "blocked")


# ---------------------------------------------------------------------------------------------------------- observe
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)


def descends(conf: dict[str, Any], sha: str | None) -> tuple[bool | None, str | None]:
    """(True | False | None, base ref): does ``sha`` descend from the integration branch of the configured repo that
    has it? None when no configured repo has the commit (nothing to say)."""
    if not sha or not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        return None, None
    for rc in (conf.get("repos") or {}).values():
        rp = Path(rc["path"]).expanduser()
        if not rp.is_dir() or _git(rp, "cat-file", "-e", f"{sha}^{{commit}}").returncode:
            continue
        ref = f"{rc.get('remote', 'origin')}/{rc['base']}"
        p = _git(rp, "merge-base", "--is-ancestor", ref, sha)
        return ({0: True, 1: False}.get(p.returncode), ref)
    return None, None


def survivors(needs: list[Any]) -> list[dict[str, Any]]:
    out = []
    for n in needs or []:
        m = SURVIVED.match(str(n))
        if m:
            out.append({"label": m["label"], "tests": m["tests"].split()})
    return out


def observe(ga_dir: Path, conf: dict[str, Any], cache: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The observations of this tick, by code: the last shadow row per mail (one per subject), with base ancestry
    (git, cached by sha in ``cache``) and parsed surviving mutants."""
    last: dict[str, dict[str, Any]] = {}
    for r in read_jsonl(ga_dir / "hub" / "shadow.jsonl"):
        if r.get("mail"):
            last[r["mail"]] = r
    out, cache = [], cache if cache is not None else {}
    for mail, r in last.items():
        tok = r.get("tokens") or {}
        sha = r.get("sha")
        if sha and sha not in cache:
            d, ref = descends(conf, sha)
            cache[sha] = [d, ref]
        d, ref = cache.get(sha) or [None, None]
        out.append({"src": "shadow", "subject": mail, "mail": mail, "id": r.get("id"), "rev": r.get("rev"), "sha": sha,
                    "at": r.get("at"), "judge_class": r.get("judge_class"), "decision": r.get("decision"),
                    "error": r.get("error") or None, "asks": list(r.get("asks") or []),
                    "tokens_in": tok.get("input") if isinstance(tok.get("input"), int) else None,
                    "served": r.get("served"), "model": r.get("served") or r.get("model_resolved"),
                    "needs": list(r.get("needs") or []), "survivors": survivors(r.get("needs") or []),
                    "descends": d, "base_ref": ref})
        o = out[-1]  # the shape fields learned rules match on (S6)
        o.update(error_kind=str(o["error"]).split(":", 1)[0] if o["error"] else None, no_tokens=o["tokens_in"] is None,
                 has_survivors=bool(o["survivors"]))
    return out


def anomalous(o: dict[str, Any]) -> bool:
    """An observation ops must act on (a rule or, failing one, the model): a shadow decision with no usable model
    judgement, or a report the judge faulted on base or mutants. A model's own ASK_HUMAN with tokens is not one."""
    if o.get("descends") is False or o.get("survivors"):
        return True
    return o.get("decision") == "ASK_HUMAN" and (o.get("tokens_in") is None or o.get("error") is not None)


# ---------------------------------------------------------------------------------------------------------- effects
class Effects:
    """What actions do in the world; tests replace it. Every method returns a short result string or raises."""

    def __init__(self, conf: dict[str, Any], ga_dir: Path, mailbox: Any = None):
        self.conf, self.ga, self._mailbox = conf, ga_dir, mailbox

    @property
    def mailbox(self) -> Any:
        if self._mailbox is None:
            from ..mailbox import Mailbox
            self._mailbox = Mailbox(self.conf["mailbox_repo"], remote=self.conf.get("mailbox_remote", "origin"))
        return self._mailbox

    def _hub(self, **over: Any) -> Any:
        from ..hub import MailHub
        return MailHub({**self.conf, **over}, ga_dir=self.ga, mailbox=self.mailbox)

    def _message(self, mail: str) -> Any:
        return self.mailbox.message(self.mailbox.tip(), mail)

    def mail(self, to: str, text: str) -> str:
        return self.mailbox.send(to, text, sender=OPS_FROM)

    def retry(self, mail: str, model: str) -> str:
        """The hub decides ``mail`` again with ``model`` (shadow stays shadow: one more shadow.jsonl row)."""
        from ..hub import TickResult
        hub = self._hub(model=model)
        hub._resolve_model()
        st = hub.load_state()
        res = TickResult()
        hub._one_ev(json.loads(json.dumps(st)) if hub.shadow else st, self._message(mail), res)
        if not hub.shadow:
            hub.save_state(st)
        return "; ".join(res.sent + res.plan)[:200] or "no outcome"

    def send_back(self, o: dict[str, Any], asks: list[str]) -> str:
        """SEND_BACK as the directive's next revision. A shadow hub sends it to its shadow recipient, not the worker."""
        hub = self._hub()
        st = hub.load_state()
        did = o.get("id")
        if hub._directive(st, did) is None:
            raise RuntimeError(f"no directive {did} on file")
        work = json.loads(json.dumps(st)) if hub.shadow else st
        nxt = hub._next_directive(work, did, asks)
        to = hub._shadow_to() if hub.shadow else self._message(o["mail"]).sender
        path = hub._mail(to, nxt["wire"])
        if not hub.shadow:
            hub.save_state(st)
        return f"{did} rev {nxt['rev']} -> {to} ({path})"

    def model_turn(self, card: str, model: str) -> dict[str, Any]:
        """One turn: {answer, usage, served}."""
        from .. import backends
        from ..act.loop import usage_counts
        r = backends.create(self.conf.get("backend", "agv"), model, dict(self.conf.get("options") or {}),
                            {"cwd": str(self.ga), "state_dir": str(self.ga / "ops")})
        out = r.run_turn(card, None)
        return {"answer": out.answer or "", "usage": usage_counts(out.usage, getattr(out, "usage_format", None)),
                "served": ",".join(getattr(out, "served", []) or []) or None}


# ---------------------------------------------------------------------------------------------------------- the tick
class Ops:
    def __init__(self, conf: dict[str, Any], *, ga_dir: str | Path, effects: Effects | None = None,
                 table: dict[str, Any] | None = None, clock: Callable[[], float] = utc_now):
        from ..paths import real_path
        self.conf = conf
        self.ga = real_path(ga_dir)
        self.dir = self.ga / "ops"
        self.table = table or load_table(conf.get("ops_table"))
        self.fx = effects or Effects(conf, self.ga)
        self.clock = clock
        self.out: list[dict[str, Any]] = []
        self.dry = False
        self.model_turns = 0

    # ------------------------------------------------------------ state
    def day(self) -> str:
        return iso(self.clock())[:10]

    def load_state(self) -> dict[str, Any]:
        f = self.dir / "state.json"
        st = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        for k in ("pending", "failures", "alerts", "blocked", "done", "descends", "learned"):
            st.setdefault(k, {})
        st.setdefault("rules", [])
        st["tune"] = {**TUNE, **(st.get("tune") or {})}
        return st

    def save_state(self, st: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(st, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.dir / "state.json")

    def _log(self, row: dict[str, Any]) -> None:
        row = {"at": iso(self.clock()), **row}
        self.out.append(row)
        if not self.dry:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / "decisions.jsonl", "a", encoding="utf-8") as h:
                h.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    # ------------------------------------------------------------ tick
    def tick(self, dry_run: bool = False) -> list[dict[str, Any]]:
        """VERIFY the pending actions, then decide each new anomaly. Returns this tick's decision rows."""
        self.out, self.dry, self.model_turns = [], dry_run, 0
        ask: list[dict[str, Any]] = []
        st = self.load_state()
        obs = observe(self.ga, self.conf, st["descends"])
        by = {o["subject"]: o for o in obs}
        for subject in list(st["pending"]):
            self._verify(st, subject, by, obs)
        for o in obs:
            s = o["subject"]
            if s in st["pending"] or s in st["blocked"] or st["done"].get(s) == self._fingerprint(o):
                continue
            if not anomalous(o):
                continue
            rule = match(self.table, o) or match({"rules": st["rules"]}, o)
            if rule is None:
                ask.append(o)  # S6: every item that needs a model goes into this tick's one call
                continue
            self._act(st, o, rule["action"], rule=rule["id"], kind=rule["kind"],
                      evidence={k: o.get(k) for k in rule.get("evidence", [])})
            if not dry_run and s not in st["pending"]:
                st["done"][s] = self._fingerprint(o)
        if ask:
            for o in self._model_batch(st, ask):
                if not dry_run and o["subject"] not in st["pending"]:
                    st["done"][o["subject"]] = self._fingerprint(o)
        if not dry_run:
            self.save_state(st)
        return self.out

    @staticmethod
    def _fingerprint(o: dict[str, Any]) -> str:
        return f"{o.get('at')}|{o.get('sha')}|{o.get('decision')}|{o.get('error')}"

    # ------------------------------------------------------------ act
    def _act(self, st: dict[str, Any], o: dict[str, Any], name: str, *, rule: str, kind: str,
             evidence: dict[str, Any], rung: int = 0, model_turn: bool = False) -> None:
        spec = self.table["actions"][name]
        g = guard(spec)
        row = {"subject": o["subject"], "id": o.get("id"), "rule": rule, "kind": kind, "action": name,
               "risk": spec["risk"], "guard": g, "rung": rung, "model_turn": model_turn, "evidence": evidence}
        if g == "blocked":
            row["result"] = "not run (high risk): alert only"
            self._log(row)
            self._alert(st, f"high:{name}", f"{rule} wants {name} on {o.get('id') or o['subject']}; high risk, not run")
            return
        if self.dry:
            row["result"] = "dry run"
            self._log(row)
            return
        try:
            res, extra = self._execute(st, o, name, kind, rung)
            ok = True
        except Exception as e:  # noqa: BLE001 -- a failed action is a failure, handled like an unmet postcondition
            res, extra, ok = f"failed: {type(e).__name__}: {str(e)[:160]}", {}, False
        row["result"] = res
        self._log(row)
        if g == "run+alert":
            self._alert(st, f"medium:{name}", f"{rule}: {name} on {o.get('id') or o['subject']}: {res}"[:260])
        if not ok:
            self._failed(st, o["subject"], rule, kind, name, rung, res, o)
            return
        if spec["postcondition"] and spec["window_s"] > 0:
            st["pending"][o["subject"]] = {"rule": rule, "kind": kind, "action": name, "rung": rung,
                                           "post": spec["postcondition"], "deadline": self.clock() + spec["window_s"],
                                           "since": iso(self.clock()), "obs": {k: o.get(k) for k in (
                                               "id", "sha", "mail", "at", "model", "subject")}, **extra}

    def _execute(self, st: dict[str, Any], o: dict[str, Any], name: str, kind: str, rung: int) -> tuple[str, dict]:
        if name == "retry_next_rung":
            model = self._next_rung(o.get("model"))
            if model is None:
                raise RuntimeError(f"no ladder rung above {o.get('model')}")
            return f"retry on {model}: " + self.fx.retry(o["mail"], model), {"model": model}
        if name == "alert_ops":
            return self._alert(st, kind, f"{kind} on {o.get('id') or o['subject']}: " + "; ".join(
                str(x) for x in (o.get("asks") or [o.get("error")]) if x)), {}
        if name == "send_back_base_drift":
            return self.fx.send_back(o, [f"re-merge {o.get('base_ref') or 'the integration branch'} into your branch "
                                         f"(head {str(o.get('sha'))[:12]} does not descend from it) and report again"]), {}
        if name == "send_back_survivors":
            return self.fx.send_back(o, self.survivor_asks(o)), {}
        if name == "wait":
            return "nothing to do", {}
        raise RuntimeError(f"no executor for {name}")

    @staticmethod
    def _next_rung(model: str | None) -> str | None:
        """The rung above ``model`` (the last one tried: the row's own) on ga.act.route.LADDER, cheapest first."""
        from ..act.route import LADDER
        last = str(model or "").split(",")[-1]
        j = LADDER.index(last) + 1 if last in LADDER else 0
        return LADDER[j] if j < len(LADDER) else None

    def survivor_asks(self, o: dict[str, Any]) -> list[str]:
        """S2: one template ask per surviving mutant: file, line, the replaced text, the expected behaviour."""
        specs = {}
        for rc in (self.conf.get("repos") or {}).values():
            if rc.get("mutations") and rc["mutations"] != "auto":
                try:
                    from ..judge import load_mutations
                    for mu in load_mutations(rc["mutations"]):
                        specs[mu.get("id") or f"{mu['file']}:{mu['find'][:30]!r}"] = (mu, Path(rc["path"]).expanduser())
                except Exception:  # noqa: BLE001 -- the ask names what it can
                    pass
        asks = []
        for s in o.get("survivors") or []:
            mu, root = specs.get(s["label"], ({}, None))
            f = mu.get("file") or s["label"].split(":", 1)[0]
            line = "?"
            if mu.get("find") and root is not None:
                try:
                    text = (root / f).read_text(encoding="utf-8")
                    if mu["find"] in text:
                        line = str(text[:text.index(mu["find"])].count("\n") + 1)
                except OSError:
                    pass
            expect = mu.get("expect") or (f"the behaviour of `{mu['find'][:60]}` as written" if mu.get("find")
                                          else "the behaviour the mutant changes")
            tests = " ".join(s["tests"]) or "the directive's tests"
            asks.append(f"mutant {s['label'][:60]} survived at {f}:{line}: add an assertion in {tests} that fails when "
                        f"it is applied; expected: {expect}"[:300])
        return asks or ["mutants survived: make each one fail a test"]

    def _alert(self, st: dict[str, Any], kind: str, note: str) -> str:
        """notify/1 kind alert to baseline-ops, once per kind per UTC day."""
        if st["alerts"].get(kind) == self.day():
            return f"alert {kind}: already sent today"
        if self.dry:
            return f"alert {kind}: dry run"
        from ..forms import dump_text
        from ..hub import MAILBOX_URL
        head = {"schema": "notify/1", "to": OPS_TO, "kind": "alert", "id": f"ops:{kind}"[:80],
                "ref": self.conf.get("mailbox_url", MAILBOX_URL), "note": note[:280] or kind}
        path = self.fx.mail(OPS_TO, dump_text(head))
        st["alerts"][kind] = self.day()
        self._log({"subject": f"alert:{kind}", "action": "alert_ops", "kind": kind, "risk": "low", "guard": "run",
                   "result": f"sent {path}", "note": note[:280]})
        return f"alert {kind}: sent"

    # ------------------------------------------------------------ verify
    def _verify(self, st: dict[str, Any], subject: str, by: dict[str, dict[str, Any]], obs: list[dict[str, Any]]) -> None:
        p = st["pending"][subject]
        met, why = self._post(p, by.get(subject), obs)
        if met:
            row = {"subject": subject, "id": p["obs"].get("id"), "rule": p["rule"], "action": p["action"],
                   "verify": "met", "result": why}
            self._log(row)
            if not self.dry:
                del st["pending"][subject]
                cur = by.get(subject)
                if cur is not None:
                    st["done"][subject] = self._fingerprint(cur)
            return
        if why is None and self.clock() < p["deadline"]:
            return  # still inside the window
        why = why or f"{p['post'][0]} not met within {int(p['deadline'] - self._since(p))} s"
        self._log({"subject": subject, "id": p["obs"].get("id"), "rule": p["rule"], "action": p["action"],
                   "verify": "failed", "result": why})
        if self.dry:
            return
        del st["pending"][subject]
        cur = by.get(subject) or {**p["obs"], "src": "shadow"}
        self._failed(st, subject, p["rule"], p["kind"], p["action"], p["rung"], why, cur)
        if subject not in st["pending"]:
            st["done"][subject] = self._fingerprint(cur)

    @staticmethod
    def _since(p: dict[str, Any]) -> float:
        return datetime.strptime(p["since"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()

    def _post(self, p: dict[str, Any], cur: dict[str, Any] | None, obs: list[dict[str, Any]]) -> tuple[bool, str | None]:
        """(met, why): why None = no verdict yet (wait for the window); a string on a definite failure or success."""
        post = p["post"][0]
        if post == "shadow_row_has_model_call":
            if cur is None or cur.get("at") == p["obs"].get("at"):
                return False, None
            if cur.get("tokens_in") is not None and cur.get("error") is None:
                return True, f"decided on {cur.get('model')}: {cur.get('decision')}"
            return False, f"no model call on retry ({cur.get('error') or 'no tokens'})"
        if post in ("new_report_descends", "new_report_no_survivors"):
            newer = [o for o in obs if o.get("id") == p["obs"].get("id") and o.get("sha") and o.get("sha") != p["obs"].get("sha")]
            if not newer:
                return False, None
            n = newer[-1]
            if post == "new_report_descends":
                return (True, f"{n['sha'][:12]} descends") if n.get("descends") is not False else (False, "new head still does not descend")
            return (True, f"{n['sha'][:12]}: no survivors") if not n.get("survivors") else (False, "mutants still survive")
        return True, "no postcondition"

    def _failed(self, st: dict[str, Any], subject: str, rule: str, kind: str, action: str, rung: int, why: str,
                o: dict[str, Any]) -> None:
        """S3: the same failure twice is one blocked alert; else one rung higher."""
        sig = f"{rule}|{subject}|{action}"
        st["failures"][sig] = st["failures"].get(sig, 0) + 1
        if st["failures"][sig] >= 2:
            st["blocked"][subject] = {"rule": rule, "action": action, "why": why[:200], "at": iso(self.clock())}
            self._alert(st, "blocked", f"blocked: {rule} {action} failed twice on {o.get('id') or subject}: {why}"[:280])
            return
        nxt = self.table["actions"][action].get("escalate")
        if nxt is None:
            st["blocked"][subject] = {"rule": rule, "action": action, "why": why[:200], "at": iso(self.clock())}
            self._alert(st, "blocked", f"blocked: {rule} {action} failed on {o.get('id') or subject}; no higher rung: {why}"[:280])
            return
        self._act(st, o, nxt, rule=rule, kind=kind, evidence={"after": why[:200]},
                  rung=rung + 1 if nxt == action else 0)

    # ------------------------------------------------------------ model (S4, S6)
    PREFIX = ("You are ga ops. Each ITEM below is an operational anomaly that matched no rule. For every item answer one "
              "line '<item number> <action name>' with an action name from the list, nothing else.\n\n## actions\n")

    def _item(self, n: int, o: dict[str, Any], cap: int) -> str:
        fields = {k: o.get(k) for k in ("id", "rev", "sha", "judge_class", "decision", "error", "tokens_in", "served",
                                         "model", "descends", "base_ref")}
        text = (f"ITEM {n}: " + json.dumps(fields, ensure_ascii=False, sort_keys=True)
                + " asks: " + "; ".join(str(x)[:200] for x in o.get("asks", [])[:3])
                + " needs: " + "; ".join(str(x)[:160] for x in o.get("needs", [])[:6]))
        raw = text.encode("utf-8")
        return text if len(raw) <= cap else raw[:cap - 3].decode("utf-8", "ignore") + "..."

    def batch_card(self, items: list[dict[str, Any]], item_bytes: int) -> str:
        """S6: one fixed prompt prefix (the table's actions; byte-identical every call, so it caches) + one capped
        evidence line per item, no history. The prefix itself is cut to CARD_TOKENS."""
        acts = "\n".join(f"- {n} ({a['risk']}): {a['description']}" for n, a in self.table["actions"].items())
        head = self.PREFIX + acts + "\n\n## items\n"
        cap = CARD_TOKENS * 4 - TUNE["item_bytes"] - 16  # room for one item: a one-item card stays <= CARD_TOKENS
        raw = head.encode("utf-8")
        if len(raw) > cap:
            head = raw[:cap - 5].decode("utf-8", "ignore") + "\n...\n"
        return head + "\n".join(self._item(n, o, item_bytes) for n, o in enumerate(items, 1)) + "\n"

    def evidence_card(self, o: dict[str, Any]) -> str:
        """The one-item card (S4: at most CARD_TOKENS)."""
        return self.batch_card([o], TUNE["item_bytes"])

    def cheapest_working(self) -> str:
        """The cheapest LADDER rung that some ledger row shows answering (no error, tokens counted); else hub.json's."""
        from ..act.route import LADDER
        from ..hub import resolve_model
        rows = read_jsonl(self.ga / "hub" / "shadow.jsonl")
        for d in ("hub", "ops"):
            for f in sorted((self.ga / d / "ledger").glob("*.jsonl")):
                rows += read_jsonl(f)
        worked = set()
        for r in rows:
            tin = r.get("input") if "input" in r else (r.get("tokens") or {}).get("input")
            if isinstance(tin, int) and not r.get("error"):
                for m in str(r.get("served") or r.get("model") or "").split(","):
                    worked.add(m)
        best = [m for m in LADDER if m in worked]
        return best[0] if best else resolve_model(self.conf)["resolved"]

    def _model_batch(self, st: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """S6: at most ONE model call per tick for every item no rule matched (up to the tuned batch size; the rest
        wait for the next tick). Returns the items handled."""
        tune = st["tune"]
        items = items[:max(1, int(tune["batch"]))]
        model = tune.get("rung") or self.cheapest_working()
        card = self.batch_card(items, int(tune["item_bytes"]))
        if self.dry:
            for o in items:
                self._log({"subject": o["subject"], "id": o.get("id"), "rule": None, "action": None, "model_turn": True,
                           "result": f"dry run: would ask {model} ({len(items)} items, one call)"})
            return items
        self.model_turns += 1
        t0, err, out = time.time(), None, {"answer": "", "usage": None, "served": None}
        try:
            out = self.fx.model_turn(card, model)
        except Exception as e:  # noqa: BLE001 -- a label, and each item is alerted
            err = f"backend:{getattr(e, 'reason', None) or type(e).__name__}"[:200]
        names = set(self.table["actions"])
        picks: dict[int, str] = {}
        for line in str(out.get("answer") or "").splitlines():
            m = ANSWER.match(line)
            if m and m[2] in names and 1 <= int(m[1]) <= len(items):
                picks.setdefault(int(m[1]), m[2])
        u = out.get("usage") or {}
        tin, tout = u.get("input"), u.get("output")
        tpi = round(((tin or 0) + (tout or 0)) / len(items), 1) if isinstance(tin, int) else None
        from .. import l0
        l0.append(self.dir / "ledger" / f"{self.day()}.jsonl",
                  {"at": iso(self.clock()), "kind": "ops", "subjects": [o["subject"] for o in items], "rung": model,
                   "model": model, "served": out.get("served"), "input": tin, "output": tout,
                   "cache_read": u.get("cache_read"), "cache_write": u.get("cache_write"), "n_items": len(items),
                   "tokens_per_item": tpi, "item_bytes": int(tune["item_bytes"]), "card_bytes": len(card.encode("utf-8")),
                   "seconds": round(time.time() - t0, 3), "error": err, "answers": len(picks)})
        for n, o in enumerate(items, 1):
            pick = picks.get(n)
            if pick is None:
                self._act(st, {**o, "asks": [f"no rule and the model gave no action ({err or 'no action name'})"]},
                          "alert_ops", rule="model", kind="unclassified", evidence={"model": model}, model_turn=True)
                continue
            self._act(st, o, pick, rule="model", kind="model", evidence={"model": model}, model_turn=True)
            self._learn(st, o, pick)
        if tpi is not None:
            self._optimize(st, tpi, model)
        return items

    # ------------------------------------------------------------ the token loop (S6)
    @staticmethod
    def signature(o: dict[str, Any]) -> dict[str, Any]:
        """The shape of an observation a learned rule matches on (no ids, no shas)."""
        return {k: o.get(k) for k in ("src", "decision", "judge_class", "error_kind", "no_tokens", "descends",
                                      "has_survivors")}

    def _learn(self, st: dict[str, Any], o: dict[str, Any], action: str) -> None:
        """A decision the model made promote_n times in a row for the same signature becomes a rule (no model)."""
        sig = self.signature(o)
        key = json.dumps(sig, sort_keys=True)
        rec = st["learned"].get(key)
        rec = rec if rec and rec.get("action") == action else {"action": action, "n": 0}
        rec["n"] += 1
        st["learned"][key] = rec
        if rec["n"] < int(st["tune"]["promote_n"]) or any(r.get("sig") == key for r in st["rules"]):
            return
        when = {"all": [{"field": k, "null": True} if v is None else {"field": k, "eq": v} for k, v in sig.items()]}
        rid = f"learned_{len(st['rules']) + 1}"
        st["rules"].append({"schema": "rule/1", "id": rid, "kind": "learned", "action": action, "when": when,
                            "evidence": ["id", "mail"], "sig": key})
        self._step({"step": "promote", "rule": rid, "action": action, "after": rec["n"], "sig": sig})

    def _step(self, row: dict[str, Any]) -> None:
        row = {"at": iso(self.clock()), **row}
        self.out.append({"subject": "optimize", **row})
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "optimize.jsonl", "a", encoding="utf-8") as h:
            h.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def _optimize(self, st: dict[str, Any], tpi: float, model: str) -> None:
        """After a call: a running trial is kept only if tokens per item fell, else reverted. With no trial and
        tokens per item over the threshold, try the next step: smaller card, cheaper rung, larger batch."""
        tune = st["tune"]
        trial = tune.pop("trial", None)
        if trial is not None:
            kept = tpi < trial["before"]
            if not kept:
                tune[trial["knob"]] = trial["old"]
            self._step({"step": trial["step"], "result": "kept" if kept else "reverted", "knob": trial["knob"],
                        "old": trial["old"], "new": trial["new"], "before": trial["before"], "after": tpi})
            return
        if tpi <= float(tune["threshold"]):
            return
        from ..act.route import LADDER
        for k in range(len(STEPS)):
            step = STEPS[(int(tune.get("next", 0)) + k) % len(STEPS)]
            knob, old = {"smaller_card": "item_bytes", "cheaper_rung": "rung", "larger_batch": "batch"}[step], None
            old = tune[knob]
            if step == "smaller_card":
                new = max(ITEM_BYTES_MIN, int(old) // 2)
            elif step == "cheaper_rung":
                i = LADDER.index(model) if model in LADDER else 0
                new = LADDER[i - 1] if i > 0 else None
            else:
                new = min(BATCH_MAX, int(old) * 2)
            if new is None or new == old or (step == "cheaper_rung" and new == model):
                continue
            tune[knob] = new
            tune["trial"] = {"step": step, "knob": knob, "old": old, "new": new, "before": tpi}
            tune["next"] = (STEPS.index(step) + 1) % len(STEPS)
            self._step({"step": step, "result": "trying", "knob": knob, "old": old, "new": new, "before": tpi})
            return


def last_decisions(ga_dir: str | Path, n: int = 20) -> list[dict[str, Any]]:
    """The newest ``n`` ops decision rows, newest first (for ga console)."""
    rows = read_jsonl(Path(ga_dir).expanduser() / "ops" / "decisions.jsonl")
    return list(reversed(rows[-n:]))
