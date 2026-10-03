"""The hub loop (METHOD §2): receive → integrate → reproduce → judge → record → choose next.

One ``tick()`` is one pass of stage 1 (G5: the safety net is just ``ga tick`` run again). When
nothing is new a tick writes nothing at all (R9). Rules (§5) block hard and notify soft; gates (§6)
stop and ask. The judge proposes; the machine fixes the classes it can prove (blocked / failure /
crossed / skipped).
"""
from __future__ import annotations

import json
import os
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
from .forms import FormError, Problem, canonical_json, dump_text, hard, load, parse_post, parse_sections
from .gates import Gate, detect, question_for
from .prompts import post_allow, turn_prompt
from .records import RecordStore

CLASS_RANK = {"blocked": 4, "failure": 3, "insufficient": 2, "partial": 1}


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

        Gated or hard-blocked directives are not sent."""
        own = st is None
        st = st or self.load_state()
        load(directive, "directive/1")
        findings = rules.r7_done_when(self.cfg, directive)
        history = [{"directive": d["doc"], "status": d["status"], "round": d.get("round")} for d in st["directives"].values()]
        findings += rules.r8_duplicate(self.cfg, directive, history)
        findings += rules.r12_budget(self.cfg, dict(directive, budget={"runs": 1, **directive.get("budget", {})}), st["spent"])
        gates = detect(self.cfg, proposal={"directive": directive}, findings=findings, user_decision=self._is_user_decision)
        if gates or hard(findings):
            return None, findings, gates
        to = directive["to"]
        prev = st["directives"].get(directive["id"])
        if prev and prev["rev"] >= directive["rev"]:
            raise FormError([Problem("$.rev", f"{directive['id']} rev {directive['rev']} already sent (rev {prev['rev']})")])
        text = dump_text(directive, body)
        findings += rules.r6_secrets(self.cfg, text, f"directive {directive['id']}")
        if hard(findings):
            return None, findings, detect(self.cfg, findings=findings)
        post = self.channel.post(to, self.cfg.hub_name, text)
        self._writes += 1
        st["directives"][directive["id"]] = {"rev": directive["rev"], "to": to, "status": "open", "round": round_n, "doc": directive}
        sup = directive.get("supersedes")
        if sup and sup["id"] != directive["id"] and sup["id"] in st["directives"]:
            st["directives"][sup["id"]].update(status="superseded", by=directive["id"])
        st["seen"].setdefault(to, None)
        workdir = self._prepare_worktrees(to)
        start = self._session_heads(to)
        resume = st["resume"].get(to)
        result = self.runner.run_turn(TurnRequest(to, turn_prompt(self.cfg, to, text, directive), workdir, resume,
                                                  permissions=self.sandbox_paths(to), budget=dict(directive.get("budget", {}))))
        self._writes += 1
        # numbers only: no prompt, transcript or answer text is kept
        st.setdefault("turns", []).append({
            "session": to, "directive": directive["id"], "rev": directive["rev"], "runner": getattr(self.runner, "kind", "?"),
            "ended": result.ended, "error": result.error, "cost": result.cost, "seconds": result.seconds,
            "resumed": resume, "session_id": result.session_id, "sandboxed": result.sandboxed,
            "start": start, "post": post.id, "labels": result.note[:200],
        })
        if result.ended:  # the runner knows the turn is over: label it now (GA5 rev 2 lost a cause for want of this)
            st["turns"][-1]["diag"] = self._turn_diag(st["turns"][-1])
        if result.error:
            findings.append(Problem(f"turn {to} {directive['id']}", f"runner {getattr(self.runner, 'kind', '?')}: {result.error}", "soft", None))
        spent = st["spent"]
        spent["runs"] = spent.get("runs", 0) + 1
        spent[f"runs:{to}"] = spent.get(f"runs:{to}", 0) + 1
        if result.cost is None:
            spent["cost_unknown_runs"] = spent.get("cost_unknown_runs", 0) + 1
        else:
            spent["cost"] = round(spent.get("cost", 0) + result.cost, 6)
        if result.session_id:
            st["resume"][to] = result.session_id
        if own:
            self.save_state(st)
        return post, findings, []

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
            if head["schema"] != "report/1":
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

    def _merge_again(self, st: dict[str, Any], repo: str, session: str, did: str) -> tuple[dict[str, Any] | None, str]:
        """rev+1 of ``did`` after an R4 non-fast-forward: merge the integration branch, report the merged head."""
        prev = st["directives"].get(did)
        if prev is None:
            return None, ""
        rev = prev["rev"] + 1
        branch = self.cfg.sessions[session].branch_for(repo)
        integ = self.cfg.integration_branch
        doc = dict(prev["doc"], rev=rev, supersedes={"id": did, "rev": prev["rev"]},
                   scope=f"{prev['doc']['scope']} — 이번 판은 통합 브랜치를 합치고 다시 보고만 한다(새 기능 없음)",
                   done_when=f"{branch} 가 통합 브랜치 {integ} 를 포함하고, 합친 머리를 commits 로 주장한 보고가 올라온다")
        doc.pop("budget", None)
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
        reports, exchanges, notices, rejected = self._parse_posts(new_posts)
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
        if not new_posts and not candidates and not answered and not pending and not hard(res.findings):
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
            heads[repo] = head
            integrated[repo] = head
        res.integrated = integrated
        for repo, h in heads.items():
            if repo not in moved:
                st["integration"][repo] = h

        # ---------------------------------------------------------- 3 reproduce
        notices += [self._exchange_note(x) for x in exchanges]
        if pending is None:
            evidence, mclass = self._reproduce(heads, integrated, reports, res.findings, st, rejected=rejected)
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
        try:
            proposal = self.judge.propose(ctx)
        except NeedJudgement as e:
            st["pending"] = pending
            self.save_state(st)
            res.waiting_for = str(e.request_path)
            res.writes = self._writes + (1 if e.wrote else 0)
            return res
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
        gates: list[Gate] = []
        for r in all_reports:
            gates += detect(self.cfg, report=r["head"], body=r["body"], findings=[], user_decision=self._is_user_decision)
            self._mark_handled(st, r["head"])
        # the judge's own "ask_user" is a gate only when nothing else already stops this round (one question, not two)
        gates += detect(self.cfg, proposal={"action": proposal.get("action"), "next": None if gates else verdict["next"]["choice"],
                                            "reason": verdict["next"]["reason"]},
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
        if directive and not gates:
            post, send_findings, send_gates = self.send(directive, proposal.get("directive_body", ""), st, n)
            gates += [g for g in send_gates if g.number not in approved]
            if post:
                sent.append(directive["id"])
        if not gates and proposal.get("action") == "stage_close" and proposal.get("stage"):
            self.records.put(proposal["stage"])
            self._writes += 1

        open_work = sum(1 for d in st["directives"].values() if d["status"] == "open")
        choice = "ask_user" if gates else verdict["next"]["choice"]
        send_findings += rules.r10_wait(self.cfg, open_work, choice)

        # ---------------------------------------------------------- questions
        qdocs = []
        for i, g in enumerate(gates, 1):
            qid = f"Q-{n}-{i}"
            q = question_for(
                g,
                why=f"{n} 회차: {g.reason}",
                changed=proposal.get("summary", verdict["next"]["reason"]),
                next_=f"답에 따라 {('지시 ' + directive['id']) if directive else '다음 단계'} 를 보내거나 멈춘다",
                recommendation=proposal.get("recommendation"),
            )
            self._write(self.ga / "questions" / f"{qid}.md", dump_text(q, f"# {qid}\n\n{q['about']}\n"))
            st["questions"][qid] = {"status": "open", "gate": g.number, "session": g.session, "round": n, "doc": q}
            qdocs.append(dict(q, id=qid))
        for qid in answered:
            st["questions"][qid]["processed"] = True

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
            "notices": [str(p) for p in all_findings] + pending["notices"] + list(pending["evidence"].get("notes", [])),
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

    def _parse_posts(self, posts: list[Post]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], int]:
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
            if head["schema"] != "report/1":
                notices.append(f"post {p.id} by {p.author}: a session may post report/1 or exchange/1, not {head['schema']}")
                rejected += 1
                continue
            if head["from"] != p.channel:
                notices.append(f"post {p.id}: report from {head['from']} in {p.channel}'s channel")
            reports.append({"post": p, "head": head, "body": body})
            notices += [f"post {p.id}: {n}" for n in notes]
        return reports, exchanges, notices, rejected

    @staticmethod
    def _exchange_note(x: dict[str, Any]) -> str:
        h = x["head"]
        note = f"exchange {h['from']}→{h['to']} (not an action; BD-133): why {h['why']} · asked {h['asked']} · got {h['got']}"
        if h.get("proposal"):
            note += f" · proposal {h['proposal']}"
        return note

    def _reproduce(self, heads, integrated, reports, findings, st, rejected: int = 0):
        tested = {r: heads[r] for r in heads if self.cfg.repos[r].test or self.cfg.repos[r].package}
        evidence: dict[str, Any] = {"heads": dict(sorted(heads.items())), "tests": {}, "notes": []}
        machine: dict[str, Any] | None = None

        def worse(cls: str, cause: str, sub: str | None = None):
            nonlocal machine
            if machine is None or CLASS_RANK[cls] > CLASS_RANK[machine["class"]]:
                machine = {"class": cls, "cause": cause, **({"subclass": sub} if sub else {})}

        if rejected and not reports and not integrated:
            # METHOD rev 8 §3.3 (BD-144): every report of the round was refused by the forms and nothing was
            # integrated — a fact the machine proves, so it is a floor, not a notice only (GA8 round 1)
            evidence["notes"].append(f"all {rejected} session post(s) of the round refused by the forms; nothing integrated")
            worse("insufficient", "requirement")
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
