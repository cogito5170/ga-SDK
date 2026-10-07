"""The executor loop (CMD-GA38 S4): card -> model (action format) -> code applies and runs -> next card, until the
item's done_when passes, a cap is hit, or progress stops.

- Before turn 1 code runs done_when once: the first failing set and its stacks are in the first card.
- A turn's EDIT/NEW apply first, then RUN, then NEED (served into the next card), then DONE/BLOCKED.
- After a turn that changed a file, code runs done_when; green = done (no DONE needed).
- DONE is accepted only when done_when passes when code runs it.
- Progress = a change in the set of failing test ids. The same set after two consecutive edit turns stops the item
  as ``blocked`` (reason ``no progress``); so do the turn cap (default 10) and the token cap.
- Every turn: one ledger row (``<state>/ledger/<UTC day>.jsonl``) and one L0 ``run.end`` (``<state>/telemetry.jsonl``)
  with backend, model, input/output/cache tokens (null when the backend did not report them), seconds, edits
  applied/rejected and commands run.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import events as EV
from .. import l0
from ..adapters.base import TurnResult
from ..backends.base import TRANSIENT_BACKOFF_S, BackendError, ModelMismatch, RateLimited, Transient, retry_transient
from ..ctxpack import tokens
from ..net.pool import owned
from . import card as C
from . import commands as K
from . import retrieve as R
from . import route as RT
from .apply import apply, secret_path
from .fmt import SPEC as FORM, parse

REPAIR_SYSTEM = "ga act: answer only with the actions of the form below; nothing else."

SPEC = "act/1"
MAX_TURNS, MAX_TOKENS = 10, 400_000
STEPS = ["measure", "edit", "verify", "finish"]  # CMD-GA50: the known steps of an item, as the live screen shows them
NO_PROGRESS_TURNS = 2


@dataclass
class Item:
    id: str
    goal: str
    files: list[str]
    done_when: list[str]


@dataclass
class Result:
    id: str
    status: str                       # done · blocked
    reason: str
    turns: int
    failing: list[str]
    tokens: dict[str, Any]
    changed: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    rungs: list[dict] = field(default_factory=list)  # CMD-GA45 S3: one {model, status, reason, turns, tokens} per rung
    route: dict | None = None         # CMD-GA47 S1: {difficulty, start, max_turns, source} when --route chose the start

    def to_dict(self) -> dict[str, Any]:
        d = {"schema": SPEC, "id": self.id, "status": self.status, "reason": self.reason, "turns": self.turns,
             "failing": self.failing, "tokens": self.tokens, "changed": self.changed}
        trace = []
        for r in self.rows:
            if "actions" in r:
                trace.append({
                    "turn": r["turn"],
                    "card_tokens": r["card_tokens"],
                    "actions": r["actions"],
                    "applied": r.get("applied", 0),
                    "rejected": r.get("rejected", 0),
                    "dropped": r.get("dropped", [])
                })
        d["trace"] = trace
        if self.rungs:
            d["rungs"] = self.rungs
        if self.route:
            d["route"] = self.route
        return d


def _label(e: BaseException) -> str:
    from ..gemini import tool_error_label
    return tool_error_label(e)


def _broken(answer: str) -> bool:
    """An answer that breaks the action form: format problems, or no action at all."""
    p = parse(answer or "")
    return bool(p.problems) or not p.actions


def usage_counts(usage: Any, fmt: str | None) -> dict[str, int] | None:
    from ..net.node import usage_counts as uc
    out = uc(usage, fmt)
    if out is None and fmt == "otel" and isinstance(usage, dict):  # codex / gemini cli stats
        names = {"input": ("input_tokens", "prompt"), "output": ("output_tokens", "candidates"),
                 "cache_read": ("cached_input_tokens", "cached")}
        out = {k: usage[n] for k, ns in names.items() for n in ns if isinstance(usage.get(n), int)} or None
    return out


class Act:
    def __init__(self, root: Path, item: Item, runner: Any, *, backend: str, model: str,
                 commands: dict[str, list[str]], timeout_s: float = 300.0, max_turns: int = MAX_TURNS,
                 max_tokens: int = MAX_TOKENS, cap: int = C.DEFAULT_CAP, state_dir: Path | None = None,
                 clock: Callable[[], float] = time.time, on_turn: Callable[[dict], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, transient_backoff_s: float = TRANSIENT_BACKOFF_S):
        self.root, self.item, self.runner = Path(root), item, runner
        self.backend, self.model = backend, model
        self.commands, self.timeout_s = dict(commands), timeout_s
        self.max_turns, self.max_tokens, self.cap = max_turns, max_tokens, cap
        self.state = Path(state_dir) if state_dir else self.root / ".ga" / "act"
        self.clock, self.on_turn = clock, on_turn
        self.sleep, self.transient_backoff_s = sleep, transient_backoff_s
        self.owned_now = [f for f in R.files(self.root, exts=None) if owned(f, item.files)]
        from .. import actions as GA
        self.actions = GA.about()  # CMD-GA42: approved actions, by name + one-line about only
        self.prefix, _ = C.redact(C.prefix(item.id, item.goal, item.done_when, item.files, self.owned_now,
                                           self.commands, self.actions))
        self.last = ""                  # the last turn's summary only: never a history
        self.needs: list[str] = []      # NEED results for the next card
        self.kept_needs: list[Any] = []
        self.needs_actions: list[Any] = []
        self.measure: K.Ran | None = None
        self.answered: str | None = None  # DEV-R0a: the model that answered the last turn
        self.used = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "estimated": 0}
        self.cards: list[C.Card] = []
        self.changed: list[str] = []

    # ---------------------------------------------------------------- code side
    def done_when(self) -> K.Ran:
        self.measure = self._cmd("done_when", self.item.done_when, red_ok=True)
        return self.measure

    def _cmd(self, name: str, argv: list[str], red_ok: bool = False) -> K.Ran:
        """K.run with its CODE event (CMD-GA50): the exit code and the last 20 lines, secrets withheld. A timeout or a
        start failure is an ERROR; so is a non-zero exit, except a done_when measure (red is expected there)."""
        sp = EV.span("CODE", "act", name, argv=EV.short(" ".join(map(str, argv))), item=self.item.id).start()
        r = K.run(name, argv, self.root, self.timeout_s)
        meta = {"exit": r.code, "tail": EV.tail(r.out), "failing": len(r.failing), "note": r.note or None}
        if r.ok:
            sp.done(**meta)
        else:
            if r.code is None or not red_ok:
                sp.error(r.note or f"exit {r.code}", exit=r.code)
            sp.fail(**meta)
        return r

    def _step(self, step: str, **meta: Any) -> None:
        if getattr(self, "ev_task", None) is not None:
            self.ev_task.update("step", step=step, **meta)

    def _agent(self, state: str, **meta: Any) -> None:
        if getattr(self, "ev_agent", None) is not None:
            self.ev_agent.update(state, **meta)

    def failing_set(self) -> frozenset[str]:
        m = self.measure
        if m is None or m.ok:
            return frozenset()
        return frozenset(m.failing or [m.note or f"exit {m.code}"])

    def units(self, turn: int) -> list[C.Unit]:
        u: list[C.Unit] = []
        m = self.measure
        u.append(C.Unit("failing", K.summarize(m, self.root) if m else "(not run yet)", 10))
        for t in self.needs:
            u.append(C.Unit("code", t, 20))
        current_sigs = set()
        for a in self.needs_actions:
            current_sigs.add((a.need, a.arg, tuple(a.lines) if getattr(a, "lines", None) else None))
        for a in self.kept_needs:
            sig = (a.need, a.arg, tuple(a.lines) if getattr(a, "lines", None) else None)
            if sig in current_sigs:
                continue
            try:
                t = self.serve_need(a)
            except Exception as e:
                t = f"NEED {a.need} {a.arg} failed: {_label(e)}"
            u.append(C.Unit("code", t, 20))
        shown: set[str] = set()
        if m and not m.ok:
            for rel, line, _fn in K.frames(m.out, self.root)[-6:][::-1]:
                s = R.enclosing(self.root, rel, line)
                if s and s.splitlines()[0] not in shown:
                    shown.add(s.splitlines()[0])
                    u.append(C.Unit("code", s, 30))
        for f in self.owned_now:
            if any(x.startswith(f + ":") or x.startswith(f + " ") for x in shown):
                continue
            s = R.file_slice(self.root, f, None, R.SMALL_FILE_LINES)
            if s and "(cut)" not in s.splitlines()[0]:
                u.append(C.Unit("code", s, 40))
        u.append(C.Unit("last", self.last or "(first turn)", 20))
        left = self.max_tokens - self.total()
        u.append(C.Unit("budget", f"turn {turn} of {self.max_turns}; about {max(0, left)} tokens left", 0))
        return u

    def total(self) -> int:
        return sum(v for k, v in self.used.items() if k != "estimated")

    def _need_ev(self, a: Any) -> str:
        sp = self._tool("NEED", f"{a.need} {a.arg}")
        got = self._need(a)
        body = got.split("\n", 1)[1] if "\n" in got else got
        miss = "(not found)" in body[:20] or ": refused" in got.splitlines()[0] or " failed: " in got.splitlines()[0]
        (sp.fail if miss else sp.done)(result=EV.short(got.splitlines()[0]), lines=len(body.splitlines()))
        return got

    def _need(self, a: Any) -> str:
        try:
            return self.serve_need(a)
        except Exception as e:  # GA41 S2: a retriever that raises is a result the next card shows
            return f"NEED {a.need} {a.arg} failed: {_label(e)}"

    def serve_need(self, a: Any) -> str:
        if a.need == "file" and (secret_path(a.arg) or R.safe_rel(self.root, a.arg) is None):
            return f"NEED file {a.arg}: refused (a secret or internal file, or outside the repository)"
        if a.need == "symbol":
            got = R.symbol(self.root, a.arg)
        elif a.need == "file":
            got = R.file_slice(self.root, a.arg, a.lines)
        else:
            got = R.grep(self.root, a.arg)
        return f"NEED {a.need} {a.arg}:\n" + (got if got is not None else "(not found)")

    # ---------------------------------------------------------------- loop
    def run(self) -> Result:
        """CMD-GA50: the loop inside its TASK (the item) and AGENT spans; DONE when the item is done, FAILED else."""
        self.ev_task = EV.span("TASK", self.item.id, "item", goal=EV.short(self.item.goal), steps=list(STEPS),
                               backend=self.backend, model=self.model, max_turns=self.max_turns).start()
        self.ev_agent = EV.span("AGENT", "act", "agent", item=self.item.id).start()
        try:
            r = self._run()
        except BaseException as e:
            self.ev_agent.error(f"{type(e).__name__}: {e}")
            self.ev_agent.fail("COMPLETING")
            self.ev_task.fail(error=type(e).__name__)
            raise
        meta = {"status": r.status, "reason": EV.short(r.reason), "turns": r.turns, "changed": len(r.changed),
                "model": self.answered or self.model,
                "input": r.tokens.get("input"), "output": r.tokens.get("output")}
        self._agent("COMPLETING", status=r.status)
        if r.status == "done":
            self._step("finish")
            self.ev_agent.done("COMPLETING", **meta)
            self.ev_task.done(**meta)
        else:
            self.ev_agent.fail("COMPLETING", **meta)
            self.ev_task.fail(**meta)
        return r

    def _run(self) -> Result:
        self.state.mkdir(parents=True, exist_ok=True)
        rows: list[dict] = []
        self._step("measure")
        self._agent("ANALYZING", why="first measure")
        self.done_when()
        if self.measure.ok:
            return self._result("done", "done_when already passes", 0, rows)
        prev, stall = self.failing_set(), 0
        for turn in range(1, self.max_turns + 1):
            if self.total() >= self.max_tokens:
                return self._result("blocked", f"token cap ({self.total()} >= {self.max_tokens})", turn - 1, rows)
            self._step("edit", turn=turn)
            self._agent("PLANNING", turn=turn)
            units, withheld = self.units(turn), 0
            for u in units:  # defence in depth: nothing secret-shaped reaches the provider
                u.text, k = C.redact(u.text)
                withheld += k
            cd = C.build(self.prefix, units, self.cap)
            self.cards.append(cd)
            t0 = self.clock()
            row: dict[str, Any] = {"schema": "act-ledger/1", "item": self.item.id, "turn": turn,
                                   "backend": self.backend, "model": self.model, "card_tokens": cd.tokens,
                                   "prefix_tokens": tokens(cd.prefix), "dropped": cd.dropped,
                                   "withheld": withheld}
            llm = self._llm(turn)
            err, answer, usage, served, secs = self._call(cd.body, cd.prefix, cd.text, t0, llm)
            row["repairs"], row["thinking"] = 0, getattr(self, "thinking", None)
            if not err and _broken(answer):  # GA41 S1: one repair turn: the problems, the form, the answer; no card
                from .. import repair
                row["repairs"] = 1
                labels = parse(answer).problems or ["no action found: the answer holds none of the actions"]
                text = repair.prompt(labels, FORM, answer)
                self._llm_end(llm, "", usage, secs, repair_next=True)
                llm = self._llm(turn, repair=True)
                err, answer2, usage2, served, secs2 = self._call(text, REPAIR_SYSTEM, REPAIR_SYSTEM + "\n\n" + text,
                                                                  self.clock(), llm)
                answer = answer2 if not err else answer
                secs = float(secs) + float(secs2)
                if usage is not None or usage2 is not None:
                    usage = {k: int((usage or {}).get(k, 0)) + int((usage2 or {}).get(k, 0))
                             for k in ("input", "output", "cache_read", "cache_creation")}
                else:
                    self.used["input"] += tokens(text) + tokens(REPAIR_SYSTEM)
                    self.used["estimated"] += tokens(text) + tokens(REPAIR_SYSTEM)
            if usage:
                for k in ("input", "output", "cache_read", "cache_creation"):
                    self.used[k] += int(usage.get(k, 0))
            else:
                est = cd.tokens + tokens(answer)
                self.used["input"] += cd.tokens
                self.used["output"] += tokens(answer)
                self.used["estimated"] += est
            row.update({"input": (usage or {}).get("input"), "output": (usage or {}).get("output"),
                        "cache_read": (usage or {}).get("cache_read"),
                        "cache_creation": (usage or {}).get("cache_creation"),
                        "usage_reported": usage is not None, "seconds": round(float(secs), 3), "served": served})
            if l0.answering(served):  # DEV-R0a: the row's model is the one that answered, not the configured rung
                row["model"] = self.answered = l0.answering(served)
            self._llm_end(llm, err, usage, secs, answer=answer, model=row["model"])
            if err:
                row.update({"error": err, "applied": 0, "rejected": 0, "commands": []})
                self._log(row, rows, usage, served, secs)
                return self._result("blocked", err, turn, rows)
            status, reason = self._act(answer, row)
            now = self.failing_set()
            if status is None:
                if now != prev:
                    stall = 0
                elif row["applied"] or row.pop("idle"):  # applied, or rejected / no action: nothing moved
                    stall += 1
                if stall >= NO_PROGRESS_TURNS:
                    status, reason = "blocked", f"no progress: the same failing set after {stall} turns"
            row.pop("idle", None)
            prev = now
            row["failing"] = sorted(now)[:50]
            self._log(row, rows, usage, served, secs)
            if status:
                return self._result(status, reason, turn, rows)
        return self._result("blocked", f"turn cap ({self.max_turns})", self.max_turns, rows)

    def _llm(self, turn: int, repair: bool = False) -> EV.Span:
        """CMD-GA50: one LLM span per model call: REQUEST, PROCESSING … RESPONSE_READY. Only states, the model,
        token counts and seconds: never the card, the answer or any reasoning."""
        sp = EV.span("LLM", "act", "REQUEST", turn=turn, model=self.model, backend=self.backend,
                     repair=repair or None).start()
        sp.update("PROCESSING", turn=turn)
        return sp

    def _llm_end(self, sp: EV.Span, err: str, usage: Any, secs: Any, answer: str = "",
                 repair_next: bool = False, model: str | None = None) -> None:
        model = model or self.model
        u = usage or {}
        meta = {"input": u.get("input"), "output": u.get("output"), "cache_read": u.get("cache_read"),
                "usage_reported": usage is not None, "seconds": round(float(secs or 0), 3), "model": model}
        if err:
            sp.error(err, model=model)
            sp.fail("RESPONSE_READY", error=EV.short(err), **meta)
            return
        sp.update("RECEIVING_RESULT")
        if repair_next:
            sp.done("RESPONSE_READY", repair="format broken: one repair turn", **meta)
            return
        kinds: dict[str, int] = {}
        for a in parse(answer).actions:
            kinds[a.kind] = kinds.get(a.kind, 0) + 1
        sp.update("SELECTING_TOOL", actions=kinds)
        sp.done("RESPONSE_READY", **meta)

    def _call(self, body: str, system: str, whole: str, t0: float,
              llm: EV.Span | None = None) -> tuple[str, str, Any, str | None, float]:
        """One model turn: (error label or "", answer, usage counts, served, seconds). A transient server error is
        retried once after the backoff (GA41 S3); a second one is the error ``transient:<code>``."""
        tries = [0]

        def turn() -> Any:
            tries[0] += 1
            if tries[0] > 1 and llm is not None:
                llm.retry("REQUEST", attempt=tries[0], why="transient server error")
            from .. import llm as L
            bare = getattr(self.runner, "bare", False)
            return L.run_turn(self.runner, body if bare else whole, None, system=system if bare else None,
                              purpose="build", item_id=self.item.id, model=self.model)
        try:
            out = retry_transient(turn, backoff_s=self.transient_backoff_s, sleep=self.sleep)
            usage = usage_counts(out.usage, getattr(out, "usage_format", None))
            from ..gemini import thinking_tokens
            self.thinking = thinking_tokens(out.usage)
            served = ",".join(getattr(out, "served", []) or []) or None
            return "", out.answer or "", usage, served, getattr(out, "seconds", None) or round(self.clock() - t0, 3)
        except ModelMismatch as e:
            return str(getattr(e, "reason", None) or e) or "served_model_mismatch", "", None, None, self.clock() - t0
        except RateLimited as e:
            return f"rate_limited:{getattr(e, 'kind', '?')}", "", None, None, self.clock() - t0
        except Transient as e:
            return e.reason, "", None, None, self.clock() - t0
        except BackendError as e:
            return f"backend:{getattr(e, 'reason', None) or type(e).__name__}"[:200], "", None, None, self.clock() - t0

    def _tool(self, kind: str, arg: str, **meta: Any) -> EV.Span:
        return EV.span("TOOL", "act", kind, input=EV.short(f"{kind} {arg}"), item=self.item.id, **meta).start()

    def _apply(self, p: Any) -> Any:
        """apply() inside one TOOL span with a FILE event per EDIT / NEW (path, line counts, applied or rejected)."""
        writes = [a for a in p.actions if a.kind in ("EDIT", "NEW")]
        if not writes:
            return apply(self.root, p.actions, self.item.files)
        self._agent("EXECUTING", edits=len(writes))
        sp = self._tool("EDIT" if all(a.kind == "EDIT" for a in writes) else "NEW" if all(a.kind == "NEW" for a in writes)
                        else "EDIT/NEW", ", ".join(a.arg for a in writes))
        out = apply(self.root, p.actions, self.item.files)
        for a in writes:
            if a.kind == "NEW":
                lines = {"lines": len(a.content.splitlines())}
            else:
                old = sum(len(b.search.splitlines()) for b in a.blocks)
                new = sum(len(b.replace.splitlines()) for b in a.blocks)
                lines = {"removed": old, "added": new, "blocks": len(a.blocks)}
            bad = [r for r in out.rejected if r.startswith(f"{a.kind} {a.arg}")]
            EV.emit("FILE", "act", a.kind, "FAILED" if bad and a.arg not in out.changed else "DONE",
                    path=a.arg, **lines, rejected=EV.short(bad[0]) if bad else None)
        res = {"result": EV.short(f"applied {len(out.applied)}, rejected {len(out.rejected)}"),
               "changed": out.changed[:20]}
        (sp.done if out.applied or not out.rejected else sp.fail)(**res)
        return out

    def _act(self, answer: str, row: dict) -> tuple[str | None, str]:
        p = parse(answer)
        out = self._apply(p)
        for f in out.changed:
            if f not in self.changed:
                self.changed.append(f)
        notes: list[str] = []
        ran: list[str] = []
        status, reason = None, ""
        if out.changed:
            self._step("verify")
            self._agent("ANALYZING", why="a file changed")
            m = self.done_when()
            if m.ok:
                status, reason = "done", "done_when passes"
        for a in p.actions:
            if a.kind == "PROPOSE":
                sp = self._tool("PROPOSE", a.arg)
                notes.append(self._propose(a))
                (sp.fail if "refused" in notes[-1] or "must be" in notes[-1] else sp.done)(result=EV.short(notes[-1]))
            if a.kind == "RUN" and a.arg not in self.commands and a.arg in self.actions:
                ran.append(a.arg)
                self._agent("WAITING_TOOL", tool=a.arg)
                sp = self._tool("RUN", a.arg, approved_action=True)
                notes.append(self._action(a))
                (sp.done if ": exit 0" in notes[-1].splitlines()[0] else sp.fail)(result=EV.short(notes[-1].splitlines()[0]))
                continue
            if a.kind == "RUN":
                if a.arg not in self.commands:
                    notes.append(f"RUN {a.arg}: rejected, not a listed command name ({', '.join(sorted(self.commands)) or 'none'})")
                    EV.emit("TOOL", "act", "RUN", "FAILED", input=EV.short(f"RUN {a.arg}"), result="not a listed command")
                    continue
                ran.append(a.arg)
                self._agent("WAITING_TOOL", tool=a.arg)
                sp = self._tool("RUN", a.arg)
                try:
                    r = self._cmd(a.arg, self.commands[a.arg])
                    notes.append(K.summarize(r, self.root))
                    (sp.done if r.ok else sp.fail)(result=EV.short(notes[-1].splitlines()[0] if notes[-1] else ""),
                                                   exit=r.code)
                except Exception as e:  # GA41 S2: a failed command is a note the next card shows, never a crash
                    notes.append(f"RUN {a.arg} failed: {_label(e)}")
                    sp.error(_label(e))
                    sp.fail(result=EV.short(notes[-1]))
        for a in self.needs_actions:
            sig = (a.need, a.arg, tuple(a.lines) if getattr(a, "lines", None) else None)
            self.kept_needs = [ka for ka in self.kept_needs 
                               if (ka.need, ka.arg, tuple(ka.lines) if getattr(ka, "lines", None) else None) != sig]
            self.kept_needs.insert(0, a)
        self.needs_actions = [a for a in p.actions if a.kind == "NEED"]
        self.kept_needs = self.kept_needs[:max(0, 8 - len(self.needs_actions))]
        self.needs = [self._need_ev(a) for a in self.needs_actions][:6]
        for a in p.actions:
            if status:
                break
            if a.kind == "DONE":
                m = self.done_when()
                if m.ok:
                    status, reason = "done", "DONE accepted: done_when passes"
                else:
                    notes.append("DONE not accepted: done_when is red (see failing)")
            elif a.kind == "BLOCKED":
                status, reason = "blocked", f"model: {a.arg}"[:300]
        self.last = self._last_text(out, p, notes)
        row.update({"applied": len(out.applied), "rejected": len(out.rejected), "commands": ran,
                    "needs": sum(1 for a in p.actions if a.kind == "NEED"), "format_problems": len(p.problems),
                    "noise_lines": p.noise, "changed": list(out.changed), "replaced": list(out.replaced),
                    "idle": not out.applied and not ran and not any(a.kind in ("NEED", "RUN", "PROPOSE") for a in p.actions)})
        acts = []
        for a in p.actions:
            if a.kind == "NEED":
                s = f"NEED {a.need} {a.arg}"
                if getattr(a, "lines", None):
                    s += f" lines {a.lines[0]}-{a.lines[1]}"
                acts.append(s[:120])
            else:
                acts.append(f"{a.kind} {a.arg}"[:120])
        row["actions"] = acts
        return status, reason

    def _propose(self, a: Any) -> str:
        """CMD-GA42 S2: record a proposal (check + trial); it never becomes callable here."""
        from .. import actions as GA
        try:
            p = json.loads(a.content)
        except ValueError:
            p = None
        if not isinstance(p, dict):
            return f"PROPOSE {a.arg}: the line after it must be one JSON object"
        rec = GA.propose({**p, "name": a.arg}, self.root, source=f"ga act {self.item.id}")
        if not rec["check"]["ok"]:
            return f"PROPOSE {a.arg}: refused: {'; '.join(rec['check']['reasons'])}"[:400]
        return f"PROPOSE {a.arg}: recorded; a person decides (ga actions approve {a.arg}); not callable until then"

    def _action(self, a: Any) -> str:
        from .. import actions as GA
        try:
            code, out = GA.run(a.arg, a.values, self.root)
        except GA.ActionError as e:
            return f"RUN {a.arg}: refused: {e}"[:400]
        return f"RUN {a.arg}: exit {code}\n{out[-1500:]}"

    def _last_text(self, out: Any, p: Any, notes: list[str]) -> str:
        lines = [f"applied: {', '.join(out.applied) or 'nothing'}"]
        lines += [f"REJECTED {r}" for r in out.rejected]
        lines += out.nearest
        lines += [f"format: {x}" for x in p.problems]
        if p.noise:
            lines.append(f"format: {p.noise} line(s) outside any action were ignored")
        if not p.actions and not p.problems:
            lines.append("format: no action found; answer only with the actions above")
        return "\n".join(lines + notes)

    def _log(self, row: dict, rows: list[dict], usage: Any, served: str | None, secs: float) -> None:
        rows.append(row)
        day = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        l0.append(self.state / "ledger" / f"{day}.jsonl", row)
        tr = TurnResult(ended=True, usage=usage, model=l0.answering(served), seconds=float(secs), error=row.get("error", ""))
        ev = l0.run_end(f"act:{self.item.id}:t{row['turn']}", tr, decision_ref=self.item.id, source="ga_act")
        ev["data"]["backend"] = self.backend
        ev["data"]["edits"] = {"applied": row.get("applied", 0), "rejected": row.get("rejected", 0)}
        ev["data"]["commands"] = list(row.get("commands", []))
        l0.append(self.state / "telemetry.jsonl", ev)
        if self.on_turn:
            self.on_turn(row)

    def _result(self, status: str, reason: str, turns: int, rows: list[dict]) -> Result:
        t = dict(self.used, total=self.total())
        return Result(self.item.id, status, reason, turns, sorted(self.failing_set()), t, list(self.changed), rows)


def make_item(raw: dict[str, Any], cfg: dict[str, Any]) -> Item:
    """A work/1-shaped item {id, goal, files, done_when?} -> Item; done_when falls back to the repo config's."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not isinstance(raw.get("goal"), str):
        raise K.ActConfigError("the item needs id and goal")
    files = raw.get("files")
    if not (isinstance(files, list) and files and all(isinstance(g, str) and g for g in files)):
        raise K.ActConfigError("the item needs files (globs it may edit)")
    dw = K.resolve_done(raw.get("done_when"), cfg["commands"]) if raw.get("done_when") is not None else cfg["done_when"]
    if not dw:
        raise K.ActConfigError("no done_when: the item or .ga-act.json must name one")
    return Item(raw["id"], raw["goal"], list(files), dw)


# CMD-GA45 S3: a blocked run climbs to the next rung only when it ran out of turns, tokens or progress — never on the
# model's own BLOCKED, a backend error or a refusal.
ESCALATE = ("turn cap", "token cap", "no progress")


def parse_ladder(v: Any) -> list[str]:
    """``m1,m2`` or ["m1", "m2"] -> the rung list (empty: no ladder)."""
    if v is None or v == "":
        return []
    items = v.split(",") if isinstance(v, str) else v
    if not isinstance(items, list) or not all(isinstance(m, str) and m.strip() for m in items):
        raise K.ActConfigError("ladder must be model names, comma-separated or a JSON list")
    return [m.strip() for m in items]


class Tree:
    """The worktree as a ladder found it: HEAD and the untracked files then. ``restore`` puts it back so the next rung
    starts from the same clean state (argv only, no shell). The state dir is never touched."""

    def __init__(self, root: Path, keep: list[Path]):
        import subprocess
        self.root, self.keep = Path(root), [Path(k).resolve() for k in keep]
        self._sp = subprocess
        self.head = self._git("rev-parse", "HEAD").stdout.strip()
        # the tree may start with changes to tracked files (the agy bridge removes an item's `rewrite` files before
        # ga act, BD-450): keep them as a binary patch and put them back on every restore
        self.start = self._git("diff", "HEAD", "--binary").stdout
        self.untracked = set(self._others())

    def _git(self, *a: str) -> Any:
        return self._sp.run(["git", *a], cwd=self.root, capture_output=True, text=True, timeout=120)

    def _others(self) -> list[str]:
        return [x for x in self._git("ls-files", "-z", "--others", "--exclude-standard").stdout.split("\0") if x]

    def restore(self) -> None:
        if self._git("reset", "-q", "--hard", self.head).returncode != 0:
            raise K.ActConfigError("the ladder could not reset the worktree")
        if self.start and self._sp.run(["git", "apply", "--binary", "-"], cwd=self.root, input=self.start,
                                       capture_output=True, text=True, timeout=120).returncode != 0:
            raise K.ActConfigError("the ladder could not put back the worktree's starting changes")
        for rel in self._others():
            f = (self.root / rel).resolve()
            if rel in self.untracked or any(f == k or k in f.parents for k in self.keep):
                continue
            f.unlink(missing_ok=True)


def run_item(root: Path, raw_item: dict[str, Any], *, backend: str, model: str, options: dict | None = None,
             runner: Any = None, config: str | None = None, state_dir: Path | None = None,
             ladder: Any = None, make_runner: Callable[[str], Any] | None = None,
             rung_turns: dict[str, int] | None = None, **kw: Any) -> Result:
    """Load the repo's commands, make the runner (bare, tools off) and run the item. With a ladder (CMD-GA45 S3) the
    item runs on each rung in turn, from the same clean tree, until one is done or ends blocked for a reason other
    than a cap or no progress. ``rung_turns`` (CMD-GA47) gives a rung its own turn cap."""
    options = dict(options or {})
    routed = options.pop("route", None)
    rkeys = {k: options.pop(k) for k in ("climb", "triage_model", "triage_compare") if k in options}
    if kw.pop("route", None) or routed:
        return _routed(root, raw_item, backend=backend, options=options, config=config, state_dir=state_dir,
                       make_runner=make_runner, **dict(rkeys, **kw))
    rungs = parse_ladder(ladder if ladder is not None else options.pop("ladder", None))
    options.pop("ladder", None)
    if not rungs:
        return _run_one(root, raw_item, backend=backend, model=model, options=options, runner=runner, config=config,
                        state_dir=state_dir, **kw)
    cfg = K.load(Path(root), config)
    bad = [m for m in rungs if m not in cfg["models"]]
    if bad:
        raise K.ActConfigError(f"ladder: unknown model(s) {', '.join(bad)[:200]} (not in the models list)")
    make_item(raw_item, cfg)  # a bad item stops before any run
    keep = [Path(state_dir or Path(root) / ".ga" / "act"), Path(config) if config else Path(root) / K.CONFIG]
    tree = Tree(Path(root), keep)
    res: Result | None = None
    rows: list[dict] = []
    for i, m in enumerate(rungs):
        if i:
            tree.restore()
        r = make_runner(m) if make_runner else None
        kr = dict(kw, max_turns=rung_turns[m]) if rung_turns and m in rung_turns else kw
        res = _run_one(root, raw_item, backend=backend, model=m, options=options, runner=r, config=config,
                       state_dir=state_dir, **kr)
        rows.append({"model": m, "status": res.status, "reason": res.reason, "turns": res.turns,
                     "tokens": dict(res.tokens)})
        if res.status == "done" or not res.reason.startswith(ESCALATE):
            break
    assert res is not None
    total: dict[str, Any] = {}
    for g in rows:
        for k, v in g["tokens"].items():
            total[k] = total.get(k, 0) + (v or 0)
    res.tokens, res.rungs, res.turns = total, rows, sum(g["turns"] for g in rows)
    return res


def _triage_runner(backend: str, options: dict, root: Path, state: Path) -> Callable[[str], Any]:
    """A tool-less triage runner: agent ga-plan where the backend takes an agent (agv), the same options otherwise."""
    def make(m: str) -> Any:
        from .. import backends
        from ..llm import create_runner
        opts = dict(options)
        if "agent" in getattr(backends.get(backend), "options", ()):
            opts["agent"] = RT.TRIAGE_AGENT
        return create_runner(backend, m, opts, {"cwd": str(root), "timeout_s": 600, "state_dir": str(state)})
    return make


def _routed(root: Path, raw_item: dict[str, Any], *, backend: str, options: dict, config: str | None,
            state_dir: Path | None, make_runner: Callable[[str], Any] | None, climb: Any = None,
            triage_model: str | None = None, triage_compare: Any = None,
            make_triage: Callable[[str], Any] | None = None, **kw: Any) -> Result:
    """CMD-GA47 S1: route once (item, ledger, triage, fallback), run start and at most ``climb`` rungs above it on
    GA45's ladder, add the triage tokens to the totals and record the outcome in <state>/routes.jsonl."""
    kw.pop("ladder", None)
    cfg = K.load(Path(root), config)
    make_item(raw_item, cfg)  # a bad item stops before any call
    state = Path(state_dir or Path(root) / ".ga" / "act")
    climb = RT.climb_of(climb)
    compare = parse_ladder(triage_compare) or None
    if compare and len(compare) != 2:
        raise K.ActConfigError("triage-compare takes two models: m1,m2")
    tm = triage_model or RT.TRIAGE_MODEL
    if make_triage is None:
        make_triage = _triage_runner(backend, options, Path(root), state)
    try:
        dec = RT.decide(Path(root), raw_item, cfg, state, make_triage=make_triage, triage_model=tm, compare=compare)
    except ValueError as e:
        raise K.ActConfigError(str(e)) from None
    route = dec["route"]
    run, caps = RT.plan_rungs(route, RT.rungs(cfg["models"]), climb)
    res = run_item(root, raw_item, backend=backend, model=run[0], options=options, config=config,
                   state_dir=state_dir, ladder=run, make_runner=make_runner, rung_turns=caps, **kw)
    tri = int(dec["triage_tokens"])
    res.tokens = dict(res.tokens, triage=tri, total=int(res.tokens.get("total", 0)) + tri)
    res.route = dict(route)
    used = [g["model"] for g in res.rungs] or [run[0]]
    row = {"id": res.id, "bucket": dec["bucket"], "route": dict(route), "rungs": used, "success": res.status == "done",
           "tokens": res.tokens["total"], "triage_tokens": tri, "turns": res.turns,
           "turns_last": res.rungs[-1]["turns"] if res.rungs else res.turns, "reason": res.reason[:200]}
    if "predictions" in dec:
        row["predictions"] = dec["predictions"]
    RT.append(state, row)
    return res


def _run_one(root: Path, raw_item: dict[str, Any], *, backend: str, model: str, options: dict | None = None,
             runner: Any = None, config: str | None = None, state_dir: Path | None = None, **kw: Any) -> Result:
    cfg = K.load(Path(root), config)
    item = make_item(raw_item, cfg)
    if runner is None:
        from ..llm import create_runner
        runner = create_runner(backend, model, dict(options or {}),
                                 {"cwd": str(root), "timeout_s": kw.pop("turn_timeout_s", 600), "state_dir": str(state_dir or Path(root) / ".ga" / "act")})
    if hasattr(runner, "cache_prefix"):
        runner.cache_prefix = True  # anthropic_http: the system text (the stable prefix) carries cache_control
    return Act(Path(root), item, runner, backend=backend, model=model, commands=cfg["commands"],
               timeout_s=cfg["timeout_s"], state_dir=state_dir, **kw).run()
