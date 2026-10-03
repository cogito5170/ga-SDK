"""The hub loop (METHOD §2): receive → integrate → reproduce → judge → record → choose next.

One ``tick()`` is one pass of stage 1 (G5: the safety net is just ``ga tick`` run again). When
nothing is new a tick writes nothing at all (R9). Rules (§5) block hard and notify soft; gates (§6)
stop and ask. The judge proposes; the machine fixes the classes it can prove (blocked / failure /
crossed / skipped).
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from . import rules
from .adapters.base import Channel, JudgeContext, Post, Runner, TurnRequest
from .adapters.git import GitVcs
from .adapters.human import NeedJudgement
from .adapters.venv import VenvBundle
from .config import Config
from .forms import FormError, Problem, apply_changes, canonical_json, deprecated, dump_text, hard, load, parse_post, parse_sections
from .adapters.runner import ManualRunner
from .forms.kinds import RUNNER_KINDS
from .gates import Gate, detect, question_for

# §4c (METHOD rev 13): the choices of a not-sent directive's question. The closing one is last (the default
# recommendation); the hub itself never picks the manual Runner.
DOWNGRADE = "수동으로 강등한다"
PERMIT_OPTIONS = [
    ("허락한다", "`ga permit` 로 범위를 적은 결정을 남기고, 설정 runner.permission 에 그 id 를 넣은 뒤 다시 보낸다"),
    (DOWNGRADE, "같은 지시를 수동 Runner 로 낸다(사람이 세션에 붙여 넣는다) · 기록에 runner: manual 과 까닭"),
    ("멈춘다", "보내지 않은 채로 둔다"),
]
GUARD_OPTIONS = [
    ("가드를 고친다", "설정 runner.guards 의 명령을 고친 뒤 다시 보낸다"),
    (DOWNGRADE, "같은 지시를 수동 Runner 로 낸다(모형 턴이 아니다) · 기록에 runner: manual 과 까닭"),
    ("멈춘다", "보내지 않은 채로 둔다"),
]
JUDGE_PERMIT_OPTIONS = [
    ("허락한다", "`ga permit --judge-only`(또는 Runner 허락에 `--measurement-calls`)로 남기고 runner.permission 에 넣는다 — 다음 회차부터 Judge 를 부른다"),
    ("멈춘다", "Judge 없이 기계의 클래스로만 판정한다"),
]
REFUSED_OPTIONS = [
    (DOWNGRADE, "같은 지시를 수동 Runner 로 낸다(사람이 세션에 붙여 넣는다) · 기록에 runner: manual 과 까닭"),
    ("멈춘다", "보내지 않은 채로 둔다 — 권한을 고친 뒤 다시 보낼 수 있다"),
]
from .prompts import post_allow, turn_prompt
from .records import RecordStore

CLASS_RANK = {"blocked": 4, "failure": 3, "insufficient": 2, "partial": 1}


def _line_count(path: Path) -> int:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _new_lines(path: Path, skip: int) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[skip:]
    except OSError:
        return []


def guard_summary(lines: list[str]) -> dict[str, Any]:
    """Allow · deny counts and the deny labels of a PreToolUse guard's JSONL record: ga's own guard log
    ({"decision", "rule"}) or rlo.hooks --record ({"kind": "guard", "result": {"verdict", "rule"}}). No line is kept."""
    allow = deny = errors = 0
    labels: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            errors += 1
            continue
        if not isinstance(d, dict):
            errors += 1
            continue
        if "error" in str(d.get("kind", "")):
            errors += 1
            continue
        res = d.get("result") if isinstance(d.get("result"), dict) else {}
        verdict = str(d.get("decision") or res.get("verdict") or "").lower()
        if not verdict:
            continue
        if verdict == "allow":
            allow += 1
        else:
            deny += 1
            labels.add(str(d.get("rule") or res.get("rule") or verdict)[:40])
    return {"allow": allow, "deny": deny, "errors": errors, "labels": sorted(labels)}


@dataclass
class TickResult:
    quiet: bool = False
    writes: int = 0
    posts: int = 0
    turns: int = 0
    round: int | None = None
    verdict: dict[str, Any] | None = None
    integrated: dict[str, str] = field(default_factory=dict)
    blocked: list[str] = field(default_factory=list)
    gates: list[dict[str, Any]] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    waiting_for: str = ""  # e.g. a judgement request path
    plan: list[str] = field(default_factory=list)  # dry-run: what would happen
    findings: list[Problem] = field(default_factory=list)


class Hub:
    def __init__(
        self,
        cfg: Config,
        *,
        ga_dir: str | Path,
        channel: Channel,
        vcs: GitVcs,
        judge: Any,
        runner: Runner,
        bundle: VenvBundle | None = None,
        records: RecordStore | None = None,
        modes: tuple[str, ...] = ("path", "install"),
        today: Callable[[], str] = lambda: date.today().isoformat(),
    ):
        self.cfg = cfg
        self.ga = Path(ga_dir).resolve()  # a relative ga_dir broke the session clone (GA10 F4)
        self.channel = channel
        self.vcs = vcs
        self.judge = judge
        self.runner = runner
        self.bundle = bundle or VenvBundle(cfg, vcs, self.ga / "bundle")
        self.records = records or RecordStore(self.ga / "records")
        self.modes = modes
        self.today = today
        self.state_path = self.ga / "state.json"
        self._writes = 0

    # ================================================================== state

    def load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"seen": {}, "branches": {}, "integration": {}, "directives": {}, "spent": {}, "questions": {}, "resume": {}, "pending": None}

    def _write(self, path: Path, text: str) -> bool:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        self._writes += 1
        return True

    def save_state(self, st: dict[str, Any]) -> None:
        self._write(self.state_path, canonical_json(st))

    def setup(self) -> list[Path]:
        """Install the receiving-side R3 hook on every local bare remote. Returns the hooks written."""
        return [h for h in (self.vcs.install_pre_receive(r) for r in self.cfg.repos) if h is not None]

    # ================================================================== sending (stage 6)

    def send(self, directive: dict[str, Any], body: str = "", st: dict[str, Any] | None = None, round_n: int | None = None) -> tuple[Post | None, list[Problem], list[Gate]]:
        """Post a directive to its session's channel and run that session's turn. Returns (post, findings, gates).

        Gated or hard-blocked directives are not sent. METHOD rev 13 §4c: a Runner that opens model turns needs the
        person's permission (config ``runner.permission`` → their decision/1 with a scope that covers it); without it,
        or when the Runner is refused, the directive is recorded as not sent and gate 6 asks — nothing is retried."""
        own = st is None
        st = st or self.load_state()
        if directive.get("schema") not in ("directive/1", "directive/2"):
            raise FormError([Problem("$.schema", f"a hub sends directive/1 or directive/2, not {directive.get('schema')!r}")])
        load(directive)
        sent_doc = directive
        prev = st["directives"].get(directive["id"])
        # METHOD rev 16 §3.6: a rev > 1 carries only its changes; the full revision is the previous one with them applied
        base = (prev.get("base") if prev.get("status") == "not_sent" and prev["rev"] == directive["rev"] else prev["doc"]) if prev else None
        directive = apply_changes(base, directive) if directive["schema"] == "directive/2" else directive
        findings = [Problem(f"directive {directive['id']}", p.message, p.strength) for p in deprecated(directive["schema"])]
        if directive["schema"] == "directive/1" and directive["rev"] > 1:
            findings.append(Problem(f"directive {directive['id']}", "rev > 1 without changes (directive/1); in directive/2 changes are required", "soft"))
        findings += rules.r7_done_when(self.cfg, directive)
        history = [{"directive": d["doc"], "status": d["status"], "round": d.get("round")} for d in st["directives"].values()]
        findings += rules.r8_duplicate(self.cfg, directive, history)
        findings += rules.r12_budget(self.cfg, dict(directive, budget={"runs": 1, **directive.get("budget", {})}), st["spent"])
        gates = detect(self.cfg, proposal={"directive": directive}, findings=findings, user_decision=self._is_user_decision)
        if gates or hard(findings):
            return None, findings, gates
        to = directive["to"]
        if prev and prev["rev"] >= directive["rev"] and prev.get("status") != "not_sent":
            raise FormError([Problem("$.rev", f"{directive['id']} rev {directive['rev']} already sent (rev {prev['rev']})")])
        text = dump_text(sent_doc, body)
        findings += rules.r6_secrets(self.cfg, text, f"directive {directive['id']}")
        if hard(findings):
            return None, findings, detect(self.cfg, findings=findings)
        gap = self._permission_gap(st)
        options = PERMIT_OPTIONS
        if not gap:
            gap, options = self._guard_gap(to), GUARD_OPTIONS  # rev 15 §4c 7: never quietly without the operator's guard
        if gap:  # §4c: nothing is written to the channel; the directive is kept as not sent, for the person to decide
            st["directives"][directive["id"]] = {"rev": directive["rev"], "to": to, "status": "not_sent", "round": round_n,
                                                 "doc": directive, "sent": sent_doc, "base": base, "body": body, "not_sent": gap, "post": None}
            gate = Gate(6, f"{directive['id']} not sent: {gap}", [directive["id"]], to, options, directive["id"])
            return None, findings, self._gates_out(st, [gate], own)
        post = self.channel.post(to, self.cfg.hub_name, text)
        self._writes += 1
        st["directives"][directive["id"]] = {"rev": directive["rev"], "to": to, "status": "open", "round": round_n, "doc": directive,
                                             "sent": sent_doc, "base": base, "body": body, "post": post.id}
        try:
            result = self._run_turn(st, directive, text, post, self.runner)
        except Exception as e:  # never leave a posted directive without its state (GA10 intervention 1)
            st["directives"][directive["id"]].update(status="not_sent", not_sent=f"error:{type(e).__name__}")
            if own:
                self.save_state(st)
            raise
        if result.error.startswith("refused"):
            # §4c: refused — not sent, not counted, not retried any other way; the person decides (gate 6)
            st["directives"][directive["id"]].update(status="not_sent", not_sent=result.error)
            gate = Gate(6, f"{directive['id']}: the {getattr(self.runner, 'kind', '?')} Runner was refused ({result.error}); not sent",
                        [directive["id"]], to, REFUSED_OPTIONS, directive["id"])
            return None, findings, self._gates_out(st, [gate], own)
        sup = directive.get("supersedes")
        if sup and sup["id"] != directive["id"] and sup["id"] in st["directives"]:
            st["directives"][sup["id"]].update(status="superseded", by=directive["id"])
        if result.error:
            findings.append(Problem(f"turn {to} {directive['id']}", f"runner {getattr(self.runner, 'kind', '?')}: {result.error}", "soft", None))
        if own:
            self.save_state(st)
        return post, findings, []

    def _run_turn(self, st: dict[str, Any], directive: dict[str, Any], text: str, post: Post, runner: Any, **record: Any) -> Any:
        """One turn of ``runner`` for an already posted directive; its numbers go into the state (never its text)."""
        to = directive["to"]
        st["seen"].setdefault(to, None)
        workdir = self._prepare_worktrees(to)
        start = self._session_heads(to)
        resume = st["resume"].get(to) if runner is self.runner else None
        records = runner.guard_records(to) if hasattr(runner, "guard_records") else []
        before = {name: _line_count(path) for name, path in records}
        result = runner.run_turn(TurnRequest(to, turn_prompt(self.cfg, to, text, directive), workdir, resume,
                                             permissions=self.sandbox_paths(to), budget=dict(directive.get("budget", {}))))
        self._writes += 1
        refused = result.error.startswith("refused")
        # numbers only: no prompt, transcript or answer text is kept
        st.setdefault("turns", []).append({
            "session": to, "directive": directive["id"], "rev": directive["rev"], "runner": getattr(runner, "kind", "?"),
            "ended": result.ended, "error": result.error, "cost": result.cost, "seconds": result.seconds,
            "resumed": resume, "session_id": result.session_id, "sandboxed": result.sandboxed,
            "start": start, "post": post.id, "labels": result.note[:200], "sent": not refused, **record,
        })
        if records:  # rev 15 §4c 7: the operator guards' verdicts in this turn — counts and labels, never the lines
            st["turns"][-1]["guards"] = [dict(guard_summary(_new_lines(path, before[name])), guard=name) for name, path in records]
        if refused:
            return result
        if result.ended:  # the runner knows the turn is over: label it now (GA5 rev 2 lost a cause for want of this)
            st["turns"][-1]["diag"] = self._turn_diag(st["turns"][-1])
            if records:
                st["turns"][-1]["diag"]["guards"] = st["turns"][-1]["guards"]
        spent = st["spent"]
        spent["runs"] = spent.get("runs", 0) + 1
        spent[f"runs:{to}"] = spent.get(f"runs:{to}", 0) + 1
        if result.cost is None:
            spent["cost_unknown_runs"] = spent.get("cost_unknown_runs", 0) + 1
        else:
            spent["cost"] = round(spent.get("cost", 0) + result.cost, 6)
        if result.session_id and runner is self.runner:
            st["resume"][to] = result.session_id
        return result

    # ================================================================== §4c permission (METHOD rev 13)

    def _permission_gap(self, st: dict[str, Any]) -> str:
        """Why this hub's Runner may not open a model turn, or "" when the person's permission covers it."""
        kind = getattr(self.runner, "kind", "manual")
        if kind not in RUNNER_KINDS:
            return ""  # the manual Runner calls no model
        bd = self.cfg.runner.get("permission")
        if not bd:
            return f"no permission for the {kind} Runner (config runner.permission names no decision/1)"
        d = self.records.decision(bd)
        if d is None or d["by"] != "user":
            return f"{bd} is not a decision of the user"
        sc = d.get("scope")
        if not sc:
            return f"{bd} has no scope"
        if sc["runner"] != kind:
            return f"{bd} permits the {sc['runner']} Runner, not {kind}"
        if hasattr(self.runner, "model") and sc.get("model") != self.runner.model:
            return f"{bd} permits model {sc.get('model')!r}, not {self.runner.model!r}"
        if hasattr(self.runner, "sandbox") and sc.get("sandbox") != self.runner.sandbox:
            return f"{bd} permits sandbox {sc.get('sandbox')!r}, not {self.runner.sandbox!r}"
        for k, lim in (sc.get("budget") or {}).items():
            if st["spent"].get(k, 0) >= lim:
                return f"{bd} permits {k} up to {lim:g}; {st['spent'].get(k, 0):g} spent"
        return ""

    def _judge_permission_gap(self, st: dict[str, Any]) -> str:
        """Why this hub's Judge may not call a model, or "" (METHOD rev 14 §4c 6). A Judge that calls no model (a person,
        code) needs nothing."""
        if not getattr(self.judge, "calls_model", False):
            return ""
        bd = self.cfg.runner.get("permission")
        if not bd:
            return "config runner.permission names no decision/1"
        d = self.records.decision(bd)
        if d is None or d["by"] != "user":
            return f"{bd} is not a decision of the user"
        sc = d.get("scope") or {}
        if sc.get("measurement_calls") is not True:
            return f"{bd} does not include measurement calls"
        if sc.get("runner") == "manual" and "model" in sc and sc["model"] != getattr(self.judge, "model", None):
            return f"{bd} permits Judge model {sc['model']!r}, not {getattr(self.judge, 'model', None)!r}"
        lim = (sc.get("budget") or {}).get("judge_runs")
        if isinstance(lim, (int, float)) and st["spent"].get("judge_runs", 0) >= lim:
            return f"{bd} permits judge_runs up to {lim:g}"
        return ""

    @staticmethod
    def _machine_proposal(pending: dict[str, Any], reason: str, summary: str) -> dict[str, Any]:
        """A proposal that is the machine's class itself (no model was asked)."""
        m = pending["machine"] or {}
        v = {"schema": "verdict/1", "class": m.get("class", "insufficient"), "evidence": pending["evidence"],
             "claims_vs_evidence": [], "next": {"choice": "wait", "reason": reason}}
        if v["class"] != "success":
            v["cause"] = m.get("cause", "measurement")
        if m.get("subclass"):
            v["subclass"] = m["subclass"]
        return {"verdict": v, "summary": summary, "directive": None}

    def _guard_gap(self, session: str) -> str:
        """Why an operator guard cannot run (its program is not there), or "" — a turn never goes on without it."""
        for g in getattr(self.runner, "guards", []):
            cmd = self.runner.fill(g["command"], session) if hasattr(self.runner, "fill") else g["command"]
            try:
                prog = shlex.split(cmd)[0]
            except (ValueError, IndexError):
                return f"guard command cannot be read: {g['command']!r}"
            if "/" in prog:
                if not (Path(prog).is_file() and os.access(prog, os.X_OK)):
                    return f"guard command not found or not executable: {prog}"
            elif shutil.which(prog) is None:
                return f"guard command not found on PATH: {prog}"
        return ""

    def permit(self, runner: str, *, model: str | None = None, sandbox: str | None = None, budget: dict[str, float] | None = None,
               measurement_calls: bool = False, note: str = "") -> dict[str, Any]:
        """The person's permission for a Runner that opens model turns (decision/1 by user, with its scope).
        Put its id in the config as ``runner.permission``."""
        scope: dict[str, Any] = {"runner": runner, "measurement_calls": bool(measurement_calls or runner == "manual")}
        if model is not None:
            scope["model"] = model
        if sandbox is not None:
            scope["sandbox"] = sandbox
        if budget:
            scope["budget"] = dict(budget)
        bd = self.records.next_decision()
        what = " · ".join(f"{k} {v}" for k, v in scope.items())
        doc = {"schema": "decision/1", "id": bd, "date": self.today(), "by": "user", "supersedes": [], "scope": scope,
               "decision": f"[게이트 6 · 자격] 모형 턴을 여는 Runner 를 허락한다: {what}",
               "basis": f"사용자 결정 {self.today()} (§4c 허락){' ' + note if note else ''}"}
        self.records.put(doc)
        self._writes += 1
        return doc

    def _gates_out(self, st: dict[str, Any], gates: list[Gate], own: bool) -> list[Gate]:
        """Standalone sends write their §4c questions themselves (a tick writes the questions of its round)."""
        if own:
            n = self.records.next_round()
            for i, g in enumerate(gates, 1):
                k = i
                while f"Q-{n}-p{k}" in st["questions"]:
                    k += 1
                self._ask(st, g, f"Q-{n}-p{k}", n, why=f"보내기 전: {g.reason}", changed="지시는 보내지 않았다(상태에 not_sent 로 남음)",
                          next_="사람이 고른 대로: 허락을 고쳐 다시 보내거나, 수동 Runner 로 내거나, 멈춘다")
            self.save_state(st)
        return gates

    def _ask(self, st: dict[str, Any], g: Gate, qid: str, n: int, *, why: str, changed: str, next_: str,
             recommendation: str | None = None) -> dict[str, Any]:
        q = question_for(g, why=why, changed=changed, next_=next_, recommendation=recommendation, options=g.options)
        self._write(self.ga / "questions" / f"{qid}.md", dump_text(q, f"# {qid}\n\n{q['about']}\n"))
        st["questions"][qid] = {"status": "open", "gate": g.number, "session": g.session, "round": n, "doc": q,
                                **({"directive": g.directive} if g.directive else {}),
                                **({"judge_permission": True} if g.options is JUDGE_PERMIT_OPTIONS else {})}
        return dict(q, id=qid)

    def _downgrade(self, st: dict[str, Any], did: str, bd: str) -> None:
        """The person chose the manual Runner for a directive that was not sent (§4c 4). Same directive, same rules;
        the record says runner: manual and why."""
        d = st["directives"].get(did)
        if d is None or d["status"] != "not_sent":
            return
        directive, body = d["doc"], d.get("body", "")
        text = dump_text(d.get("sent") or directive, body)
        post = self.channel.post(directive["to"], self.cfg.hub_name, text)
        self._writes += 1
        d.update(status="open", post=post.id, downgraded={"from": getattr(self.runner, "kind", "?"), "why": d.get("not_sent"), "decision": bd})
        self._run_turn(st, directive, text, post, ManualRunner(self.ga / "outbox"),
                       downgraded_from=getattr(self.runner, "kind", "?"), why=d.get("not_sent"), decision=bd)
        sup = directive.get("supersedes")
        if sup and sup["id"] != directive["id"] and sup["id"] in st["directives"]:
            st["directives"][sup["id"]].update(status="superseded", by=directive["id"])

    # ================================================================== turn diagnostics (labels and counts only)

    def _session_heads(self, session: str) -> dict[str, str | None]:
        heads = {}
        for r in self.cfg.sessions[session].repos:
            self.vcs.fetch(r)
            heads[r] = self.vcs.session_head(r, session)
        return heads

    def _turn_diag(self, turn: dict[str, Any]) -> dict[str, Any]:
        """Did the turn commit, did its report come, does it parse, does the hub know what it claims."""
        s = turn["session"]
        now = self._session_heads(s)
        posts = [p for p in self.channel.read(s, turn.get("post")) if p.author == s]
        reports_ok, claims_known = 0, None
        for p in posts:
            try:
                head, _, _ = parse_post(p.text)
            except FormError:
                continue
            if head["schema"] not in ("report/1", "report/2"):
                continue
            reports_ok += 1
            for c in head.get("commits", []):
                known = c["repo"] in self.cfg.repos and self.vcs.resolve(c["repo"], c["sha"]) is not None
                claims_known = known if claims_known is None else (claims_known and known)
        return {
            "committed": any(now[r] and now[r] != (turn.get("start") or {}).get(r) for r in now),
            "posts": len(posts),
            "reports_ok": reports_ok,
            "claims_known": claims_known,
        }

    def sandbox_paths(self, session: str) -> dict[str, list[str]]:
        """What a session's turn may not write (protect) and the only places inside them it may (writable).

        Empty in worktree isolation: a worktree keeps its git data inside the hub's repository, so it cannot work
        with that repository read-only — that mode has no structural R3 (see README). Empty in remote isolation
        too: the session runs on another machine, where only the platform's branch rules apply."""
        if self.cfg.isolation != "clone":
            return {"allow": post_allow(self.cfg, session)}
        protect = [self.cfg.base_dir, self.ga]
        for r in self.cfg.repos:
            protect.append(self.vcs.repo_dir(r))
            remote = self.vcs.local_remote_dir(r)
            if remote is not None:
                protect.append(remote)
        writable = [self.ga / "worktrees" / session]
        root = getattr(self.channel, "root", None)
        if root is not None:
            (Path(root) / session).mkdir(parents=True, exist_ok=True)
            writable.append(Path(root) / session)
        (self.ga / "worktrees" / session).mkdir(parents=True, exist_ok=True)
        return {"protect": [str(p) for p in protect], "writable": [str(p) for p in writable], "allow": post_allow(self.cfg, session)}

    def _fill_drafts(self, st: dict[str, Any], verdict: dict[str, Any], reports: list[dict[str, Any]], reviews: list[dict[str, Any]],
                     not_ff: list[tuple[str, str, str]]) -> list[tuple[dict[str, Any], str]]:
        """rev+1 of each directive the round's grounds point at, one per session: an outside verdict's commit first,
        then each report's directive (or, for a report that names none, its session's open directive). The grounds
        (floor, notes, claims vs evidence, reviews) become the body. Each is checked as directive/1; one that does not
        check is dropped."""
        sent_r4 = {did for _, _, did in not_ff}
        targets: dict[str, str] = {}  # session -> directive id
        for rv in reviews:
            who = st.get("integrated_by", {}).get(rv["sha"])
            if who and who.get("directive") and who.get("session"):
                targets.setdefault(who["session"], who["directive"])
        for r in reports:
            session = r["post"].channel
            ids = [h["id"] for h in r["head"].get("handled", []) if st["directives"].get(h["id"], {}).get("to") == session]
            if not ids:
                ids = [i for i, d in st["directives"].items() if d.get("to") == session and d["status"] == "open"]
            if ids:
                targets.setdefault(session, ids[-1])
        grounds = [f"판정: {verdict['class']}" + (f" · {verdict['cause']}" if verdict.get("cause") else "")
                   + (f" · {verdict['subclass']}" if verdict.get("subclass") else "") + f" — 다음 {verdict['next']['choice']}: {verdict['next']['reason']}"]
        grounds += [n for n in verdict["evidence"].get("notes", []) if "— known:" not in n and not n.startswith("judge proposed")]
        grounds += [f"보고의 주장과 증거가 다름: {c}" for c in verdict.get("claims_vs_evidence", [])]
        out = []
        for session, target in targets.items():
            prev = st["directives"].get(target)
            if prev is None or target in sent_r4:
                continue
            try:
                doc = self._next_rev(prev, target, "아래 '고칠 것' 을 푼다(범위는 처음 지시와 같다)",
                                     "아래 '고칠 것' 이 모두 풀린다", replace_done=False)
                load(doc)
                apply_changes(prev["doc"], doc)  # a directive/2 draft must apply to the revision it follows
            except (FormError, KeyError, TypeError, ValueError):
                continue
            mine = [g for g in grounds if not g.startswith("R1b: ") or g.startswith(f"R1b: {session} ")]
            body = ("## 고칠 것 (허브가 판정 근거로 만든 초안, METHOD rev 10 — Judge 가 초안을 비웠다)\n"
                    + "".join(f"- {g}\n" for g in mine)
                    + "\n## 할 일\n1. 위 '고칠 것' 가운데 네 몫을 푼다. 범위는 처음 지시와 같다.\n2. 시험을 다시 돌린다.\n"
                      "3. 커밋하고, 머리를 `commits` 로 주장해 보고한다(머리 틀은 아래 '일하는 방법').\n")
            out.append((doc, body))
        return out

    @staticmethod
    def _next_rev(prev: dict[str, Any], did: str, scope_more: str, done_when: str, *, replace_done: bool) -> dict[str, Any]:
        """rev+1 of a directive the hub makes by itself. directive/1: the text fields are rewritten. directive/2
        (METHOD rev 16 §3.6): only what changed — one scope item added, and done_when items added (or all replaced)."""
        doc = {k: v for k, v in prev["doc"].items() if k not in ("budget", "changes")}
        doc.update(rev=prev["rev"] + 1, supersedes={"id": did, "rev": prev["rev"]})
        if doc.get("schema") != "directive/2":
            doc["scope"] = f"{doc['scope']} — {scope_more}"
            doc["done_when"] = done_when if replace_done else f"{doc.get('done_when', '')} — {done_when}".lstrip(" —")
            return doc
        nxt = lambda items, p: f"{p}{max([int(x['id'][1:]) for x in items] or [0]) + 1}"
        changes = [{"item": nxt(doc.get("scope", []), "S"), "op": "add", "text": scope_more}]
        if replace_done:
            changes += [{"item": x["id"], "op": "drop"} for x in doc.get("done_when", [])]
        changes.append({"item": nxt(doc.get("done_when", []), "D"), "op": "add", "text": done_when})
        for k in ("scope", "done_when"):
            doc.pop(k, None)
        doc["changes"] = changes
        return doc

    def _merge_again(self, st: dict[str, Any], repo: str, session: str, did: str) -> tuple[dict[str, Any] | None, str]:
        """rev+1 of ``did`` after an R4 non-fast-forward: merge the integration branch, report the merged head."""
        prev = st["directives"].get(did)
        if prev is None:
            return None, ""
        rev = prev["rev"] + 1
        branch = self.cfg.sessions[session].branch_for(repo)
        integ = self.cfg.integration_branch
        doc = self._next_rev(prev, did, "이번 판은 통합 브랜치를 합치고 다시 보고만 한다(새 기능 없음)",
                             f"{branch} 가 통합 브랜치 {integ} 를 포함하고, 합친 머리를 commits 로 주장한 보고가 올라온다", replace_done=True)
        if self.cfg.isolation == "worktree":
            how = f"`git -C {repo} merge --no-edit {integ}`"
        else:
            how = f"`git -C {repo} fetch origin {integ}` 뒤 `git -C {repo} merge --no-edit FETCH_HEAD`"
        body = (f"## 고친 까닭 (허브가 기계적으로 만든 지시, METHOD rev 9)\n"
                f"보고한 `{repo}` 커밋이 통합 브랜치 `{integ}` 의 ff 가 아니라서(R4) 통합하지 않았다. 다른 세션의 일이 먼저 들어갔다.\n\n"
                f"## 할 일\n1. 통합 브랜치를 네 브랜치 `{branch}` 에 합친다: {how}.\n"
                f"2. 시험을 다시 돌린다.\n3. 합친 머리를 `commits` 로 주장해 다시 보고한다(머리 틀은 아래 '일하는 방법').\n")
        return doc, body

    def _prepare_worktrees(self, session: str) -> Path:
        if self.cfg.isolation == "remote":  # the session's checkout is wherever the session runs, not here
            return self.ga
        s = self.cfg.sessions[session]
        paths = [self.vcs.ensure_session_worktree(session, r) for r in s.repos]
        return paths[0].parent if paths else self.ga

    # ================================================================== answers to gates

    def review(self, by: str, repo: str, sha: str, cls: str, why: str, cause: str | None = None) -> dict[str, Any]:
        """METHOD rev 10 §3.3b: record an outside verdict (review/1) on an integrated result. The round that integrated
        the sha is only ever made stricter (an appended review, never a rewritten round); the next round's Judge and
        draft see it."""
        if repo not in self.cfg.repos:
            raise FormError([Problem("$.repo", f"unknown repo {repo!r}")])
        self.vcs.fetch(repo)
        full = self.vcs.resolve(repo, sha)
        if full is None:
            raise FormError([Problem("$.sha", f"{repo} has no commit {sha}")])
        st = self.load_state()
        doc: dict[str, Any] = {"schema": "review/1", "id": self.records.next_review(), "date": self.today(), "by": by,
                               "repo": repo, "sha": full, "class": cls, "why": why}
        if cause:
            doc["cause"] = cause
        for r in reversed(self.records.all("round/1")):
            if any(x["repo"] == repo and full.startswith(x["sha"]) for x in r["repos"]):
                doc["round"] = r["n"]
                if CLASS_RANK.get(cls, 0) > CLASS_RANK.get(r["verdict"], 0):
                    doc["amends"] = {"round": r["n"], "from": r["verdict"], "to": cls}
                break
        load(doc, "review/1")
        self.records.put(doc)
        self._writes += 1
        st.setdefault("reviews", {})[doc["id"]] = {"status": "open", "doc": doc}
        for name, text in self.records.render({r: s.slug for r, s in self.cfg.repos.items() if s.slug}).items():
            self._write(self.records.root / name, text)
        self.save_state(st)
        return doc

    def answer(self, qid: str, label: str, note: str = "") -> dict[str, Any]:
        """Record the user's answer to a gate question as a decision/1 (by user)."""
        st = self.load_state()
        q = st["questions"].get(qid)
        if q is None or q["status"] != "open":
            raise KeyError(f"no open question {qid}")
        doc = q["doc"]
        option = next((o for o in doc["options"] if o["label"] == label), None)
        if option is None:
            raise ValueError(f"{label!r} is not an option of {qid}: {[o['label'] for o in doc['options']]}")
        bd = self.records.next_decision()
        decision = {
            "schema": "decision/1",
            "id": bd,
            "date": self.today(),
            "decision": f"{doc['about']} → {label}: {option['effect']}",
            "basis": f"사용자 결정 {self.today()} ({qid}){' ' + note if note else ''}",
            "by": "user",
            "supersedes": [],
        }
        self.records.put(decision)
        self._writes += 1
        q.update(status="answered", decision=bd, label=label, processed=False)
        if q.get("directive"):  # §4c: the person's answer is acted on as given; the hub never downgrades by itself
            if label == DOWNGRADE:
                self._downgrade(st, q["directive"], bd)
            q["processed"] = True
        if q.get("judge_permission"):  # rev 14: the permission itself is a config change; no Judge round for the answer
            q["processed"] = True
        self.save_state(st)
        return decision

    def _is_user_decision(self, bd: str) -> bool:
        d = self.records.decision(bd)
        return bool(d and d["by"] == "user")

    # ================================================================== the tick

    def tick(self, dry_run: bool = False) -> TickResult:
        self._writes = 0
        st = self.load_state()
        res = TickResult()

        # ---------------------------------------------------------- 1 receive
        new_posts: list[Post] = []
        for name in self.cfg.sessions:
            for p in self.channel.read(name, st["seen"].get(name)):
                if p.author != self.cfg.hub_name:
                    new_posts.append(p)
                    res.findings += rules.r1_post(self.cfg, name, p.author)
        for repo in self.cfg.repos:
            self.vcs.fetch(repo)
        reports, exchanges, notices, rejected = self._parse_posts(new_posts, st)
        # R1b: a change is integrated only as far as a report claims it under a directive sent to that session.
        # Commits nobody claims are not new work for the hub (the report is still to come) and are never integrated.
        claims: dict[tuple[str, str], str] = {}
        claimed_under: dict[tuple[str, str], list[str]] = {}
        for r in reports:
            session = r["post"].channel
            under = [h["id"] for h in r["head"].get("handled", []) if st["directives"].get(h["id"], {}).get("to") == session]
            for c in r["head"].get("commits", []):
                if c["repo"] not in self.cfg.repos:
                    notices.append(f"post {r['post'].id}: commit in unknown repo {c['repo']}")
                    continue
                if not under:
                    res.findings += rules.r1b_unclaimed(self.cfg, c["repo"], session, c["sha"], "the report handles no directive sent to this session")
                    continue
                full = self.vcs.resolve(c["repo"], c["sha"])
                if full is None:
                    res.findings += rules.r1b_unclaimed(self.cfg, c["repo"], session, c["sha"], "the claimed commit is not in the repository")
                    continue
                claims[(c["repo"], session)] = full  # a later report overrides an earlier one
                claimed_under[(c["repo"], session)] = under
        heads: dict[str, str] = {}
        candidates: list[tuple[str, str, str]] = []
        for repo in self.cfg.repos:
            ih = self.vcs.integration_head(repo)
            if ih is None:
                continue
            heads[repo] = ih
            moved_now = rules.r3_integration_moved(self.cfg, repo, st["integration"].get(repo), ih)
            alert = f"R3:{repo}:{ih}"
            if moved_now and alert not in st.setdefault("alerts", []):
                st["alerts"].append(alert)  # report a foreign move once, not on every tick (R9)
                res.findings += moved_now
            for s in self.cfg.sessions_of_repo(repo):
                claim = claims.get((repo, s.name))
                if claim is None or self.vcs.is_ancestor(repo, claim, ih):
                    continue  # nothing claimed, or already integrated
                sh = self.vcs.session_head(repo, s.name)
                if sh is None or not self.vcs.is_ancestor(repo, claim, sh):
                    res.findings += rules.r1b_unclaimed(self.cfg, repo, s.name, claim, f"the claimed commit is not on {s.branch_for(repo)}")
                    continue
                if sh != claim:
                    extra = len(self.vcs.commit_subjects(repo, claim, sh))
                    notices.append(f"R1b: {repo} {s.name}: {extra} commit(s) after the claimed {claim[:7]} are not integrated (no directive claims them)")
                candidates.append((repo, s.name, claim))
        if new_posts:  # turns whose end the runner could not see get their labels when their session posts
            for turn in st.get("turns", []):
                if "post" in turn and (turn.get("diag") or {}).get("reports_ok", 0) == 0 and any(p.channel == turn["session"] for p in new_posts):
                    turn["diag"] = self._turn_diag(turn)
        answered = [qid for qid, q in st["questions"].items() if q["status"] == "answered" and not q.get("processed")]
        pending = st.get("pending")
        reviews = [v["doc"] for v in st.get("reviews", {}).values() if v["status"] == "open"]
        retry = st.get("judge_retry")
        if not new_posts and not candidates and not answered and not pending and not reviews and not retry and not hard(res.findings):
            res.quiet = True
            return res  # R9: nothing new, nothing written

        # ---------------------------------------------------------- 2 integrate
        moved = {a.split(":")[1] for a in st.get("alerts", []) if a.startswith("R3:") and heads.get(a.split(":")[1]) == a.split(":", 2)[2]}
        integrated: dict[str, str] = {}
        not_ff: list[tuple[str, str, str]] = []  # (repo, session, directive id) blocked only because not a fast-forward
        for repo, session, head in candidates:
            base = heads[repo]
            f: list[Problem] = []
            if repo in moved:
                f.append(Problem(f"{repo}", "integration branch moved outside the hub; not integrating", "hard", "R3"))
            ff = self.vcs.is_ancestor(repo, base, head)
            f += rules.r4_ff(self.cfg, repo, session, ff)
            if ff:
                f += rules.r2_ownership(self.cfg, repo, session, self.vcs.changed_files(repo, base, head))
                f += rules.r6_secrets(self.cfg, self.vcs.added_lines(repo, base, head), f"{repo} {base[:7]}..{head[:7]}")
            res.findings += f
            st["branches"].setdefault(repo, {})[session] = head
            if hard(f):
                res.blocked.append(f"{repo}@{head[:7]} ({session})")
                if not ff and all(p.rule == "R4" for p in hard(f)) and claimed_under.get((repo, session)):
                    not_ff.append((repo, session, claimed_under[(repo, session)][-1]))
                continue
            if dry_run:
                res.plan.append(f"ff {repo} {self.cfg.integration_branch} {base[:7]} → {head[:7]} ({session})")
            else:
                self.vcs.fast_forward(repo, head)
                st.setdefault("integrated_by", {})[head] = {"session": session, "directive": (claimed_under.get((repo, session)) or [None])[-1]}
            heads[repo] = head
            integrated[repo] = head
        res.integrated = integrated
        for repo, h in heads.items():
            if repo not in moved:
                st["integration"][repo] = h

        # ---------------------------------------------------------- 3 reproduce
        notices += [self._exchange_note(x) for x in exchanges]
        if pending is None and retry and not new_posts and not candidates and not answered and not reviews:
            # METHOD rev 12 (BD-151): the Judge failed last round — call it once more on that round's evidence
            pending = dict(retry["pending"], retry_of=retry["round"])
            pending["notices"] = list(pending.get("notices", [])) + [f"retry of round {retry['round']}: the Judge failed there"]
        if pending is None:
            evidence, mclass = self._reproduce(heads, integrated, reports, res.findings, st, rejected=rejected, reviews=reviews)
            for t in st.get("turns", []):  # rev 15 §4c 7: what the operator guards refused in the turns now reported on
                if t.get("guards_noted") or t["session"] not in {p.channel for p in new_posts}:
                    continue
                for g in t.get("guards", []):
                    if g["deny"]:
                        evidence["notes"].append(f"guard {g['guard']} refused {g['deny']} tool call(s) in {t['session']}'s "
                                                 f"{t['directive']} rev {t['rev']} turn: {', '.join(g['labels']) or '-'}")
                if t.get("guards"):
                    t["guards_noted"] = True
            for r in reports:  # a commit the session made but its report did not claim (GA10 round 1)
                if r["head"].get("commits"):
                    continue
                s_ = self.cfg.sessions.get(r["post"].channel)
                for repo in (s_.repos if s_ else []):
                    sh = self.vcs.session_head(repo, s_.name)
                    if sh and heads.get(repo) and not self.vcs.is_ancestor(repo, sh, heads[repo]):
                        evidence["notes"].append(f"R1b: {s_.name} committed {sh[:7]} on {s_.branch_for(repo)} but its report claims "
                                                 f"no commit — not integrated")
            for repo, session, did in not_ff:
                evidence["notes"].append(f"R4: {repo} {session} is not a fast-forward of {self.cfg.integration_branch}; "
                                         f"the hub sends {did} rev+1 (merge it, report again) by itself — not a question (METHOD rev 9)")
            pending = {
                "posts": [p.id for p in new_posts],
                "reports": [{"post": r["post"].__dict__, "head": r["head"], "body": r["body"]} for r in reports],
                "evidence": evidence,
                "machine": mclass,
                "findings": [p.__dict__ for p in res.findings],
                "notices": notices,
                "integrated": integrated,
                "exchanges": [{"post": x["post"].__dict__, "head": x["head"]} for x in exchanges],
            }
        else:
            # a judgement was pending: fold in anything that arrived meanwhile
            pending["posts"] += [p.id for p in new_posts]
            pending["reports"] += [{"post": r["post"].__dict__, "head": r["head"], "body": r["body"]} for r in reports]
            pending["notices"] += notices
            pending["integrated"].update(integrated)
            pending["findings"] += [p.__dict__ for p in res.findings]
            pending.setdefault("exchanges", []).extend({"post": x["post"].__dict__, "head": x["head"]} for x in exchanges)
        for p in new_posts:
            st["seen"][p.channel] = max(st["seen"].get(p.channel) or "", p.id)
        if dry_run:
            res.plan.append(f"judge round {self.records.next_round()} on {len(pending['reports'])} report(s)")
            res.verdict = {"machine": pending["machine"], "evidence": pending["evidence"]}
            return res

        # ---------------------------------------------------------- 4 judge
        n = self.records.next_round()
        findings = [Problem(**d) for d in pending["findings"]]
        all_reports = [dict(r, post=Post(**r["post"])) for r in pending["reports"]]
        ctx = JudgeContext(
            round=n,
            reports=all_reports,
            evidence=pending["evidence"],
            machine_class=(pending["machine"] or {}).get("class"),
            findings=findings,
            open_directives=[d["doc"] for d in st["directives"].values() if d["status"] == "open"],
        )
        ctx.answers = [dict(st["questions"][q], id=q) for q in answered]
        ctx.exchanges = [x["head"] for x in pending.get("exchanges", [])]
        ctx.reviews = reviews
        jb = self.cfg.budget.get("judge_runs")
        budget_out = bool(pending.get("retry_of") and isinstance(jb, (int, float)) and st["spent"].get("judge_runs", 0) >= jb)
        judge_gap = self._judge_permission_gap(st)
        try:
            if judge_gap:  # METHOD rev 14 §4c 6: no permission for measurement calls — not called, not a failure
                pending["evidence"].setdefault("notes", []).append(f"Judge 허락 없음: {judge_gap}")
                proposal = self._machine_proposal(pending, "Judge 허락 없음", "판정 보조를 부르지 않았다(허락 없음)")
            elif budget_out:  # METHOD rev 12: a retry only within the budget — not called, the machine's class stands
                proposal = dict(self._machine_proposal(pending, "judge budget exhausted", "판정 보조 예산이 다 됐다"),
                                judge_failed="judge budget exhausted")
            else:
                proposal = self.judge.propose(ctx)
        except NeedJudgement as e:
            st["pending"] = pending
            self.save_state(st)
            res.waiting_for = str(e.request_path)
            res.writes = self._writes + (1 if e.wrote else 0)
            return res
        judge_failed = proposal.get("judge_failed")
        jrec = proposal.get("judge")
        if isinstance(jrec, dict) and "cost" in jrec:  # an LLM judge call: numbers only
            st.setdefault("judge_calls", []).append(dict(jrec, round=n))
            st["spent"]["judge_runs"] = st["spent"].get("judge_runs", 0) + 1
            if jrec.get("cost") is not None:
                st["spent"]["cost"] = round(st["spent"].get("cost", 0) + jrec["cost"], 6)
        verdict = self._settle_verdict(proposal["verdict"], pending["machine"], pending["evidence"], findings, n)
        res.verdict = verdict

        # ---------------------------------------------------------- 6a gates on what is proposed
        approved = self._approved_gates(proposal.get("approved_by", []), answered, st)
        directive = proposal.get("directive")
        directive_body = proposal.get("directive_body", "")
        drafts: list[tuple[dict[str, Any], str]] = []
        if directive is None and verdict["next"]["choice"] in ("refine", "verify"):
            # METHOD rev 10 (BD-147): the Judge left the draft empty — the hub makes rev+1 from the verdict's grounds
            drafts = self._fill_drafts(st, verdict, all_reports, reviews, not_ff)
        drafted = [f"hub drafted {d['id']} rev {d['rev']} (the Judge's {verdict['next']['choice']} had no draft)" for d, _ in drafts]
        gates: list[Gate] = []
        for r in all_reports:
            gates += detect(self.cfg, report=r["head"], body=r["body"], findings=[], user_decision=self._is_user_decision)
            self._mark_handled(st, r["head"])
        if pending.get("retry_of"):  # the reports were already gated in the round that failed
            gates = []
        retry_note = ""
        if judge_gap:
            # rev 14: a known state, not a failure — no retry, no count; one question until it is answered
            if pending.get("retry_of"):
                st["judge_retry"] = None  # a pending rev 12 retry is used up by this round (else every tick would re-run it)
            if not any(q.get("judge_permission") and q["status"] == "open" for q in st["questions"].values()):
                gates.append(Gate(6, f"the LLM Judge is not permitted: {judge_gap}", options=JUDGE_PERMIT_OPTIONS))
        elif judge_failed or budget_out:
            fails = st.get("judge_failures", 0) + 1
            st["judge_failures"] = fails
            why = judge_failed or "judge budget exhausted"
            if fails >= 2 or budget_out:
                # METHOD rev 12: the Judge failed two rounds in a row — a person has to know (gate 6, budget · credentials)
                gates.append(Gate(6, f"the Judge failed {fails} rounds in a row: {why}"))
                st["judge_retry"] = None
            else:
                st["judge_retry"] = {"round": n, "pending": {k: v for k, v in pending.items() if k != "retry_of"}}
                retry_note = f"the Judge failed ({why}): judged on the machine's class; the next tick calls it once more"
        else:
            st["judge_failures"] = 0
            st["judge_retry"] = None
        # the judge's own "ask_user" is a gate only when nothing else already stops this round (one question, not two)
        gates += detect(self.cfg, proposal={"action": proposal.get("action"), "next": None if gates else verdict["next"]["choice"],
                                            "reason": verdict["next"]["reason"], "gate": proposal.get("gate")},
                        findings=findings, user_decision=self._is_user_decision)
        gates = [g for g in gates if g.number not in approved]

        # ---------------------------------------------------------- 6b send (unless stopped)
        sent: list[str] = []
        send_findings: list[Problem] = []
        for repo, session, did in not_ff:  # METHOD rev 9 (BD-146): mechanical, never a gate of its own
            auto, auto_body = self._merge_again(st, repo, session, did)
            if auto is None or did in sent:
                continue
            post, f_auto, g_auto = self.send(auto, auto_body, st, n)
            send_findings += f_auto
            gates += [g for g in g_auto if g.number not in approved]
            if post:
                sent.append(did)
        if directive and directive.get("id") in sent:
            send_findings.append(Problem("directive", f"judge's {directive['id']} not sent: the hub already sent its rev+1 this round", "soft", None))
            directive = None
        for d, body in drafts:  # the hub's drafts go through the same gates as a Judge's directive
            if gates:
                break
            post, f_d, g_d = self.send(d, body, st, n)
            send_findings += f_d
            gates += [g for g in g_d if g.number not in approved]
            if post:
                sent.append(d["id"])
        if directive and not gates:
            post, send_findings, send_gates = self.send(directive, directive_body, st, n)
            gates += [g for g in send_gates if g.number not in approved]
            if post:
                sent.append(directive["id"])
        if not gates and proposal.get("action") == "stage_close" and proposal.get("stage"):
            self.records.put(proposal["stage"])
            self._writes += 1

        open_work = sum(1 for d in st["directives"].values() if d["status"] == "open")
        choice = "ask_user" if gates else verdict["next"]["choice"]
        if choice == "ask_user" and not gates:
            # METHOD rev 11 (BD-148): no §6 ground named — a note, not a question; nothing is waiting on a person
            send_findings.append(Problem("next", f"the Judge's ask_user names no §6 gate ({proposal.get('gate')!r}): "
                                                 f"noted, not asked — {verdict['next']['reason']}", "soft", None))
            choice = "wait"
        if choice == "wait":
            # METHOD rev 11 (BD-148): an open outside verdict followed by wait, with no directive taking it up
            for rv in reviews:
                who = st.get("integrated_by", {}).get(rv["sha"], {})
                # a directive sent this round is open too
                taken = any(d.get("to") == who.get("session") and d["status"] == "open" for d in st["directives"].values())
                if not taken:
                    send_findings.append(Problem(f"review {rv['id']}", f"outside verdict {rv['class']} on {rv['repo']}@{rv['sha'][:7]} "
                                                 "is followed by wait and no directive takes it up", "soft", None))
        send_findings += rules.r10_wait(self.cfg, open_work, choice)

        # ---------------------------------------------------------- questions
        qdocs = []
        for i, g in enumerate(gates, 1):
            qdocs.append(self._ask(st, g, f"Q-{n}-{i}", n, why=f"{n} 회차: {g.reason}",
                                   changed=proposal.get("summary", verdict["next"]["reason"]),
                                   next_=f"답에 따라 {('지시 ' + directive['id']) if directive else '다음 단계'} 를 보내거나 멈춘다",
                                   recommendation=None if g.options else proposal.get("recommendation")))
        for qid in answered:
            st["questions"][qid]["processed"] = True
        for rv in reviews:
            st["reviews"][rv["id"]].update(status="used", round=n)

        # ---------------------------------------------------------- 5 record
        decisions = []
        dec = proposal.get("decision")
        if dec:
            if dec.get("by") != "hub":
                send_findings.append(Problem("decision", "a judge may only propose hub decisions; user decisions come from answers", "hard", None))
            else:
                self.records.put(dec)
                self._writes += 1
                decisions.append(dec["id"])
        all_findings = findings + send_findings
        handled_ids = [h["id"] for r in all_reports for h in r["head"].get("handled", [])]
        round_doc = {
            "schema": "round/1",
            "n": n,
            "date": self.today(),
            "repos": [
                {"repo": repo, "sha": sha, **({"tests": pending["evidence"]["tests"][repo]} if repo in pending["evidence"]["tests"] else {})}
                for repo, sha in sorted(pending["integrated"].items())
            ],
            "directives": list(dict.fromkeys(handled_ids + sent)),
            "verdict": verdict["class"],
            "summary": proposal.get("summary") or verdict["next"]["reason"],
            "next": choice,
            "notices": [str(p) for p in all_findings] + pending["notices"] + list(pending["evidence"].get("notes", [])) + drafted + ([retry_note] if retry_note else []),
        }
        if decisions:
            round_doc["decisions"] = decisions
        self.records.put(round_doc)
        self._writes += 1
        self._write(self.ga / "verdicts" / f"round-{n:04d}.json", canonical_json(verdict))
        for name, text in self.records.render({r: s.slug for r, s in self.cfg.repos.items() if s.slug}).items():
            self._write(self.records.root / name, text)

        st["pending"] = None
        self.save_state(st)
        res.round = n
        res.gates = qdocs
        res.sent = sent
        res.findings = all_findings
        res.writes = self._writes
        res.posts = len(sent)
        res.turns = len(sent)
        return res

    # ================================================================== helpers

    def _parse_posts(self, posts: list[Post], st: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], int]:
        """Split new posts into reports (report/1) and exchanges (exchange/1, §3.5). Anything else is a notice.
        The last value counts the posts refused by the forms (no valid report/1 or exchange/1 in them)."""
        reports, exchanges, notices = [], [], []
        rejected = 0
        for p in posts:
            try:
                head, body, notes = parse_post(p.text)
            except FormError as e:
                notices.append(f"post {p.id} by {p.author} is not a report/1 or exchange/1: {e}")
                rejected += 1
                continue
            if head["schema"] == "exchange/1":
                if head["from"] != p.channel:
                    notices.append(f"post {p.id}: exchange from {head['from']} posted in {p.channel}'s channel")
                exchanges.append({"post": p, "head": head, "body": body})
                continue
            if head["schema"] not in ("report/1", "report/2"):
                notices.append(f"post {p.id} by {p.author}: a session may post report/2 (report/1) or exchange/1, not {head['schema']}")
                rejected += 1
                continue
            missing = self._items_missing(head, p.channel, st)
            if missing:  # METHOD rev 16 §3.6: R7 hard — the report must answer every done_when item it handles
                notices.append(f"post {p.id} by {p.author}: R7 [hard] report/2 items miss {', '.join(missing)}: refused")
                rejected += 1
                continue
            notices += [f"post {p.id}: {q.message}" for q in deprecated(head["schema"])]
            if head["schema"] == "report/2" and len(body) > 1500:
                notices.append(f"post {p.id}: the body is {len(body)} chars; the head carries the judgement, keep the body near 1,500 (soft)")
            if head["from"] != p.channel:
                notices.append(f"post {p.id}: report from {head['from']} in {p.channel}'s channel")
            reports.append({"post": p, "head": head, "body": body})
            notices += [f"post {p.id}: {n}" for n in notes]
        return reports, exchanges, notices, rejected

    @staticmethod
    def _items_missing(head: dict[str, Any], channel: str, st: dict[str, Any] | None) -> list[str]:
        """done_when items of the handled directive/2s that a report/2 does not answer (as "CMD-X:D2")."""
        if head.get("schema") != "report/2" or st is None:
            return []
        have = {i["id"] for i in head.get("items", [])}
        out = []
        for h in head.get("handled", []):
            d = st["directives"].get(h["id"])
            if d and d.get("to") == channel and d["doc"].get("schema") == "directive/2":
                out += [f"{h['id']}:{x['id']}" for x in d["doc"].get("done_when", []) if x["id"] not in have]
        return out

    @staticmethod
    def _exchange_note(x: dict[str, Any]) -> str:
        h = x["head"]
        note = f"exchange {h['from']}→{h['to']} (not an action; BD-133): why {h['why']} · asked {h['asked']} · got {h['got']}"
        if h.get("proposal"):
            note += f" · proposal {h['proposal']}"
        return note

    def _reproduce(self, heads, integrated, reports, findings, st, rejected: int = 0, reviews=()):
        tested = {r: heads[r] for r in heads if self.cfg.repos[r].test or self.cfg.repos[r].package}
        evidence: dict[str, Any] = {"heads": dict(sorted(heads.items())), "tests": {}, "notes": []}
        machine: dict[str, Any] | None = None

        def worse(cls: str, cause: str, sub: str | None = None):
            nonlocal machine
            if machine is None or CLASS_RANK[cls] > CLASS_RANK[machine["class"]]:
                machine = {"class": cls, "cause": cause, **({"subclass": sub} if sub else {})}

        for rv in reviews:  # METHOD rev 10: an outside verdict on the head still in place is a floor; else a note
            cause = f" · {rv['cause']}" if rv.get("cause") else ""
            evidence["notes"].append(f"review {rv['id']} ({rv['by']}): {rv['repo']}@{rv['sha'][:7]} {rv['class']}{cause} — {rv['why']}")
            if heads.get(rv["repo"]) == rv["sha"] and rv["class"] in CLASS_RANK:
                worse(rv["class"], rv.get("cause") or "requirement")
        if rejected and not reports and not integrated:
            # METHOD rev 8 §3.3 (BD-144): every report of the round was refused by the forms and nothing was
            # integrated — a fact the machine proves, so it is a floor, not a notice only (GA8 round 1)
            evidence["notes"].append(f"all {rejected} session post(s) of the round refused by the forms; nothing integrated")
            worse("insufficient", "requirement")
        for r in reports:  # METHOD rev 16 §3.6: the items of a report/2 are a floor
            head = r["head"]
            if head.get("schema") != "report/2":
                continue
            who = head.get("from")
            states = {i["id"]: i["state"] for i in head.get("items", [])}
            blocked = [k for k, v in states.items() if v == "blocked"]
            if blocked:
                kinds = [b["kind"] for b in head.get("blockers", [])]
                cause = {"env": "environment", "permission": "environment", "credential": "environment", "budget": "requirement",
                         "dependency": "dependency", "design": "requirement"}.get(kinds[0] if kinds else "", "requirement")
                evidence["notes"].append(f"report/2 {who}: blocked {', '.join(blocked)}")
                worse("blocked", cause)
            elif any(h["status"] == "done" for h in head.get("handled", [])):
                short = [f"{k} {v}" for k, v in states.items() if v != "met"]
                if short:
                    evidence["notes"].append(f"report/2 {who}: done, but not met: {', '.join(short)}")
                    worse("partial", "requirement")
        for p in hard(findings):
            if p.rule in ("R1b", "R2", "R3", "R6"):
                worse("blocked", {"R1b": "requirement", "R2": "requirement", "R3": "environment", "R6": "implementation"}[p.rule])
            elif p.rule == "R4":
                # METHOD rev 9 (BD-146): a non-fast-forward is routine, not a design fork — not integrated, that
                # session's share is partial (requirement), and the hub sends it rev+1 by itself (no gate)
                worse("partial", "requirement")
        if tested and (integrated or reports):
            results = {}
            if "path" in self.modes:
                results["path"] = self.bundle.run_path(tested)
            if "install" in self.modes and any(self.cfg.repos[r].package for r in tested):
                results["install"] = self.bundle.run_install(tested)
            for mode, br in results.items():
                if br.tools:
                    evidence["notes"].append(f"bundle ({mode}): built with " + " · ".join(f"{k} {v}" for k, v in br.tools.items()))
                if br.problem:
                    evidence["notes"].append(f"bundle ({mode}): {br.problem}")
                    worse("failure", "dependency" if br.problem == "pin_conflict" else "environment")
                for run in br.runs:
                    if run.counts:
                        c = run.counts.as_dict()
                        if mode == "path" or run.repo not in evidence["tests"]:
                            evidence["tests"][run.repo] = c
                        other = evidence["tests"].get(run.repo)
                        if mode == "install" and other and other != c:
                            evidence["notes"].append(f"{run.repo}: path {other} vs install {c}")
                        if c["skipped"]:
                            known = self.cfg.repos[run.repo].expected_skipped
                            if known.get("why") and c["skipped"] == known.get("count"):
                                # METHOD rev 9: exactly the known skips, with their reason, are not a floor
                                evidence["notes"].append(f"{run.repo} ({mode}): skipped {c['skipped']} — known: {known['why']}")
                            else:
                                evidence["notes"].append(f"{run.repo} ({mode}): skipped {c['skipped']}")
                                worse("partial", "measurement", "skipped")
                        if c["failed"] or c.get("errors"):
                            worse("failure", "implementation")
                    elif run.problem == "unparsed_output":
                        evidence["notes"].append(f"{run.repo} ({mode}): test output not understood")
                        worse("insufficient", "measurement")
                    elif run.problem in ("missing_in_install", "import_error", "import_check_failed"):
                        # the installed copy lacks (or cannot import) what the source has: the package is broken
                        evidence["notes"].append(f"{run.repo} ({mode}): {run.problem}: {run.output[:300]}")
                        if run.problem == "import_check_failed":
                            worse("insufficient", "measurement")
                        else:
                            worse("failure", "implementation")
                    if not run.ok and run.counts and not (run.counts.failed or run.counts.errors):
                        worse("failure", "implementation")
            # METHOD rev 9 (BD-146): a packaged repository reproduced without a clean install is not a success —
            # a sub-package can be green on PYTHONPATH and missing from the installed copy (GA10 W1, F4)
            installed = {run.repo for run in (results["install"].runs if "install" in results else [])}
            for repo in sorted(tested):
                if results and repo not in installed and self.vcs.packaged(repo, tested[repo]):
                    evidence["notes"].append(f"{repo}: packaged, reproduced without a clean install (not_install_checked)")
                    worse("partial", "measurement", "not_install_checked")
        # R11 and crossings
        for r in reports:
            head = r["head"]
            repos = sorted({c["repo"] for c in head.get("commits", [])})
            if head.get("tests") and len(repos) == 1 and repos[0] in evidence["tests"]:
                for p in rules.r11_counts(self.cfg, repos[0], head["tests"], evidence["tests"][repos[0]]):
                    findings.append(p)
                    evidence["notes"].append(f"claims vs evidence: {p.message}")
            for h in head.get("handled", []):
                d = st["directives"].get(h["id"])
                seen = h["rev_seen"][-1] if isinstance(h["rev_seen"], list) else h["rev_seen"]
                # a crossing is a fact the machine proves (rev_seen), not a class floor: whether the result is
                # still good is for the judge or the person (METHOD §3.3 rev 6, BD-138 — round 53's A5 was a success)
                if d and d["status"] == "superseded":
                    evidence["notes"].append(f"crossed: {h['id']} was superseded by {d.get('by')} before this report")
                elif d and seen < d["rev"]:
                    evidence["notes"].append(f"crossed: {h['id']} handled at rev {seen}, current rev {d['rev']}")
        return evidence, machine

    def _settle_verdict(self, proposed: dict[str, Any], machine: dict[str, Any] | None, evidence: dict[str, Any], findings, n: int) -> dict[str, Any]:
        v = dict(proposed)
        v.setdefault("schema", "verdict/1")
        v["evidence"] = evidence
        v.setdefault("claims_vs_evidence", [])
        v["claims_vs_evidence"] = list(v["claims_vs_evidence"]) + [p.message for p in findings if p.rule == "R11"]
        v["round"] = n
        if machine and CLASS_RANK.get(machine["class"], 0) > CLASS_RANK.get(v.get("class"), 0):
            if v.get("class") != machine["class"]:
                evidence.setdefault("notes", []).append(f"judge proposed {v.get('class')}, machine evidence makes it {machine['class']}")
            v["class"] = machine["class"]
            v["cause"] = machine["cause"]
            if "subclass" in machine:
                v["subclass"] = machine["subclass"]
            else:
                v.pop("subclass", None)
        if v["class"] == "success":
            v.pop("cause", None)
        if not v.get("subclass") and any(n.startswith("crossed:") for n in evidence.get("notes", [])):
            v["subclass"] = "crossed"
        return load(v, "verdict/1")

    def _mark_handled(self, st: dict[str, Any], head: dict[str, Any]) -> None:
        for h in head.get("handled", []):
            d = st["directives"].get(h["id"])
            if not d or d["status"] == "superseded":
                continue
            seen = h["rev_seen"][-1] if isinstance(h["rev_seen"], list) else h["rev_seen"]
            if h["status"] == "done" and seen >= d["rev"]:
                d["status"] = "done"
            elif h["status"] == "declined":
                d["status"] = "declined"
            # paused or crossed: stays open

    def _approved_gates(self, approved_by: list[str], answered: list[str], st: dict[str, Any]) -> set[int]:
        """Gates the user approved: a decision (by user) answering a question of that gate, cited by the proposal."""
        out = set()
        for q in st["questions"].values():
            # the first option of a question is the one that lets the gated thing go ahead
            if q.get("decision") in approved_by and self._is_user_decision(q["decision"]) and q.get("label") == q["doc"]["options"][0]["label"]:
                out.add(q["gate"])
        return out
