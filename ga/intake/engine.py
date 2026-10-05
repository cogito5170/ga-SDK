"""The intake engine of the GA Engine (CMD-GA37 S2-S4, rev 2 S6-S8): request -> repo summary (code) -> one model turn -> task/1 checked by code.

    summary  ga.intake.summary.summarize: no model, under 2000 tokens
    turn 1   INSTRUCTION + message(request, summary) on whichever backend the caller picked (same bytes for all)
    check    parse (fenced or bare JSON), code fills schema/id/request/repo, ga.forms.validate(task/1)
    repair   at most MAX_REPAIRS turns, each carrying only the problems and the answer they are about
    else     status blocked, with the problems

A runner with a system channel (``runner.bare``) gets INSTRUCTION as the system prompt; one without gets
``prompt.whole``. A claude_cli runner is always created bare here (tools off); agv cannot turn its tools off, so its
fixed overhead (the plugin's ``overhead``) is written on each of its rows. Every turn is one L0 ``run.end``
(``<ga_dir>/telemetry.jsonl``) and one ledger row (``<ga_dir>/ledger/<UTC day>.jsonl``): backend, model, input, output,
cache tokens, seconds. While a turn runs the engine calls ``on_progress`` every ``wait_every_s``: it never sits silent
on a model.
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .. import l0
from ..forms import hard, soft, validate
from ..forms.task import NEEDS, REQUEST_CAP
from . import prompt
from .summary import Summary, summarize

MAX_REPAIRS = 1
# fixed per-turn overhead measured for a backend that cannot run bare, when its plugin says "not measured" (S3):
# agv one 'OK' turn, 11,210 input - 86 prompt (baseline research/AGY_FACTS.md, 2026-10-05, gpt-oss-120b-medium)
MEASURED_OVERHEAD = {"agv": 11124}
LIST_FIELDS = ("constraints", "non_goals", "assumptions", "questions", "risks")  # absent = none


@dataclass
class Turn:
    n: int
    kind: str                      # intake | repair
    backend: str
    model: str
    bare: bool
    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_creation: int | None = None
    thinking: int | None = None    # thinking tokens when the provider reports them apart (GA41 S6)
    seconds: float = 0.0
    overhead_tokens: Any = None    # the backend's fixed per-turn overhead when it is not bare (S3), else None
    prompt_tokens: int = 0         # ga's estimate of what it sent (system + prompt)
    error: str | None = None
    problems: list[str] = field(default_factory=list)  # the hard problems found in this turn's answer: rule@path


@dataclass
class Result:
    status: str                    # ready | blocked
    task_id: str
    spec: dict[str, Any] | None
    problems: list[str]
    notes: list[str]
    turns: list[Turn]
    summary: Summary
    sent: list[dict[str, str | None]] = field(default_factory=list)  # [{system, prompt}] per turn, as sent
    resolution: Any = None         # ga.intake.fragment.Resolution for a fragment request (rev 2 S7)
    outcome: dict[str, Any] = field(default_factory=dict)  # what route() did (rev 2 S6); {} before routing

    def tokens(self) -> dict[str, int]:
        out = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        for t in self.turns:
            for k in out:
                out[k] += getattr(t, k) or 0
        return out

    def human_questions(self) -> list[dict[str, str]]:
        """What a person is asked (S4): only questions whose needs is one of the four kinds."""
        if self.spec is None:
            return []
        return [q for q in self.spec.get("questions", []) if q.get("needs") in NEEDS]

    def assumptions(self) -> list[dict[str, str]]:
        return list((self.spec or {}).get("assumptions", []))


def overhead_of(backend: str, plugin: Any) -> Any:
    """The fixed input tokens a non-bare backend adds to every turn: the plugin's figure, else the measured one."""
    tok = (getattr(plugin, "overhead", None) or {}).get("tokens")
    return tok if tok is not None else MEASURED_OVERHEAD.get(backend)


def _cite_found(cite: str, material: str, root: Path) -> bool:
    c = cite.strip().strip("`'\"")
    if not c:
        return False
    if c in material:
        return True
    p = c.split(":", 1)[0].split("#", 1)[0]
    if p and not p.startswith("/") and ".." not in Path(p).parts:
        try:
            return (root / p).exists()
        except OSError:
            return False
    return False


def context_problems(spec: dict[str, Any], material: str, root: Path,
                     open_work: list[str]) -> list[tuple[str, str]]:
    """Rev 2 S6 checks that need what code gathered: an answer's cites are found there (or are paths in the repo); a
    decision affects only open work ids. [(rule@path, message)]."""
    out = []
    if spec.get("kind") == "answer" and isinstance(spec.get("answer"), dict):
        for i, c in enumerate(spec["answer"].get("cites", [])):
            if not _cite_found(str(c), material, root):
                path = f"$.answer.cites[{i}]"
                out.append((f"task:cite@{path}", f"[hard] task:cite {path}: {str(c)[:80]!r} is not in the material "
                                                  "code gathered nor a path in the repo"))
    if spec.get("kind") == "decide" and isinstance(spec.get("decision"), dict):
        for i, w in enumerate(spec["decision"].get("affects", [])):
            if w not in open_work:
                path = f"$.decision.affects[{i}]"
                out.append((f"task:affects@{path}", f"[hard] task:affects {path}: {str(w)[:40]!r} is not an open "
                                                    "work id"))
    return out


def task_id(request: str) -> str:
    return "T-" + hashlib.sha256(request.encode("utf-8")).hexdigest()[:8]


def build_spec(request: str, answer: dict[str, Any], summary: Summary) -> dict[str, Any]:
    """The task/1 document: code's fields (schema, id, request, repo) around the model's. A key the model set for one
    of code's fields is overwritten; any other key it added stays, for the validator to refuse."""
    spec: dict[str, Any] = {"schema": "task/1", "id": task_id(request), "request": request[:REQUEST_CAP]}
    for k, v in answer.items():
        if k not in ("schema", "id", "request", "repo"):
            spec[k] = v
    for k in LIST_FIELDS:
        spec.setdefault(k, [])
    spec["repo"] = summary.repo()
    return spec


def usage_counts(usage: Any, fmt: str | None) -> dict[str, int]:
    """A provider usage object -> {input, output, cache_read, cache_creation}, only what it reported."""
    if not isinstance(usage, dict):
        return {}
    names = {
        "anthropic": ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"),
        "openai": ("prompt_tokens", "completion_tokens", None, None) if "prompt_tokens" in usage
        else ("input_tokens", "output_tokens", "cached_tokens", None),
        "otel": ("input_tokens", "output_tokens", "cached_input_tokens", None),
        "gemini": ("prompt_token_count", "candidates_token_count", "cached_content_token_count", None)
        if "prompt_token_count" in usage else ("promptTokenCount", "candidatesTokenCount", "cachedContentTokenCount",
                                                None),
    }.get(fmt or "")
    if names is None:
        return {}
    out = {}
    for k, src in zip(("input", "output", "cache_read", "cache_creation"), names):
        v = usage.get(src) if src else None
        if isinstance(v, int) and not isinstance(v, bool):
            out[k] = v
    if fmt == "openai" and "cache_read" not in out:
        det = usage.get("prompt_tokens_details")
        if isinstance(det, dict) and isinstance(det.get("cached_tokens"), int):
            out["cache_read"] = det["cached_tokens"]
    return out


def _run_with_progress(fn: Callable[[], Any], on_progress: Callable[[dict], None] | None, info: dict,
                       wait_every_s: float) -> Any:
    """``fn()`` in a worker thread; ``on_progress`` every ``wait_every_s`` until it returns (S4)."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # re-raised in the caller's thread
            box["error"] = e

    th = threading.Thread(target=work, daemon=True)
    t0 = time.monotonic()
    th.start()
    while th.is_alive():
        th.join(wait_every_s)
        if th.is_alive() and on_progress is not None:
            on_progress(dict(info, event="waiting", seconds=round(time.monotonic() - t0, 1)))
    if "error" in box:
        raise box["error"]
    return box["value"]


class Intake:
    def __init__(self, runner: Any, *, backend: str, model: str, ga_dir: str | Path,
                 overhead: Any = None, on_progress: Callable[[dict], None] | None = None,
                 clock: Callable[[], float] = time.time, wait_every_s: float = 5.0,
                 sleep: Callable[[float], None] = time.sleep, transient_backoff_s: float | None = None):
        self.runner, self.backend, self.model = runner, backend, model
        self.ga_dir = Path(ga_dir)
        self.bare = bool(getattr(runner, "bare", False))
        self.overhead = None if self.bare else overhead
        self.on_progress, self.clock, self.wait_every_s = on_progress, clock, wait_every_s
        from ..backends.base import TRANSIENT_BACKOFF_S
        self.sleep = sleep
        self.transient_backoff_s = TRANSIENT_BACKOFF_S if transient_backoff_s is None else transient_backoff_s

    # ---- one turn: the same text for every backend, only the channel differs
    def send(self, text: str) -> tuple[dict[str, str | None], Callable[[], Any]]:
        if self.bare:
            sent = {"system": prompt.INSTRUCTION, "prompt": text}
            return sent, lambda: self.runner.run_turn(text, None, system=prompt.INSTRUCTION)
        whole = prompt.whole(text)
        return {"system": None, "prompt": whole}, lambda: self.runner.run_turn(whole, None)

    def _record(self, tid: str, turn: Turn) -> None:
        res = SimpleNamespace(raw={}, model=self.model, seconds=turn.seconds, cost=None,
                              usage={k: getattr(turn, k) for k in ("input", "output", "cache_read", "cache_creation")
                                     if getattr(turn, k) is not None})
        ev = l0.run_end(f"{tid}:{turn.n}", res, decision_ref=tid, source="ga_intake")
        l0.append(self.ga_dir / "telemetry.jsonl", ev)
        now = self.clock()
        row = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "task": tid, **asdict(turn)}
        l0.append(self.ga_dir / "ledger" / (time.strftime("%Y-%m-%d", time.gmtime(now)) + ".jsonl"), row)

    def turn(self, tid: str, n: int, kind: str, text: str, sent_log: list) -> tuple[Turn, str | None]:
        from ..ctxpack import tokens
        sent, call = self.send(text)
        sent_log.append(sent)
        t = Turn(n, kind, self.backend, self.model, self.bare, overhead_tokens=self.overhead,
                 prompt_tokens=tokens((sent["system"] or "") + sent["prompt"]))
        info = {"task": tid, "turn": n, "kind": kind, "backend": self.backend, "model": self.model}
        if self.on_progress:
            self.on_progress(dict(info, event="started"))
        t0 = time.monotonic()
        answer = None
        try:
            from ..backends.base import retry_transient
            bt = _run_with_progress(lambda: retry_transient(call, backoff_s=self.transient_backoff_s, sleep=self.sleep),
                                    self.on_progress, info, self.wait_every_s)  # GA41 S3: one retry of a transient
            answer = bt.answer
            for k, v in usage_counts(bt.usage, bt.usage_format).items():
                setattr(t, k, v)
            from ..gemini import thinking_tokens
            t.thinking = thinking_tokens(bt.usage)
            t.seconds = float(bt.seconds or round(time.monotonic() - t0, 3))
        except Exception as e:  # a failed turn is a row and a blocked intake, never a crash
            t.error = str(getattr(e, "reason", "") or type(e).__name__)[:80]
            t.seconds = round(time.monotonic() - t0, 3)
        if self.on_progress:
            self.on_progress(dict(info, event="done", error=t.error, input=t.input, output=t.output))
        return t, answer

    def run(self, request: str, root: str | Path, summary: Summary | None = None, state: Any = None,
            resolution: Any = None) -> Result:
        """``state``: a ga.intake.state.State whose summary goes into the material (rev 2 S7); ``resolution``: what code
        resolved a fragment to (ga.intake.fragment.resolve)."""
        root = Path(root)
        summary = summary or summarize(root, state=state.lines() if state is not None else None)
        open_work = [w["id"] for w in state.work() if w.get("status") != "done"] if state is not None else []
        tid = task_id(request)
        turns: list[Turn] = []
        sent: list[dict] = []
        text = prompt.message(request[:REQUEST_CAP], summary.text, resolution.line() if resolution else None)
        spec, problems, notes = None, [], []
        for attempt in range(1 + MAX_REPAIRS):
            t, answer = self.turn(tid, attempt + 1, "intake" if attempt == 0 else "repair", text, sent)
            turns.append(t)
            if t.error is not None:
                spec, problems = None, [f"backend: {t.error}"]
                self._record(tid, t)
                break
            obj = prompt.parse(answer or "")
            if obj is None:
                spec, problems, notes = None, ["$: the answer holds no JSON object"], []
                t.problems = ["json@$"]
            else:
                spec = build_spec(request, obj, summary)
                if resolution is not None:
                    spec["resolution"] = resolution.spec()
                    if resolution.assumption:  # an unresolved fragment is an assumption, never a question (S7)
                        spec["assumptions"].append(dict(resolution.assumption))
                found = validate(spec, "task/1")
                problems = [str(p) for p in hard(found)]
                notes = [str(p) for p in soft(found)]
                t.problems = [f"{p.rule or 'shape'}@{p.path}" for p in hard(found)]
                if not problems:
                    extra = context_problems(spec, summary.text, root, open_work)
                    problems += [msg for _, msg in extra]
                    t.problems += [label for label, _ in extra]
            self._record(tid, t)
            if not problems:
                break
            text = prompt.repair(problems, answer or "")
        status = "ready" if not problems and spec is not None else "blocked"
        return Result(status, tid, spec, problems, notes, turns, summary, sent, resolution)


def handle(intake: Intake, raw_request: str, root: str | Path, state: Any, planner: Any = None) -> Result:
    """One request end to end (rev 2 S6-S8): withhold secret-looking lines, resolve a fragment from the state, intake,
    route by kind, and one request row in ``<ga_dir>/ledger/requests-<UTC day>.jsonl`` (kind, tokens, seconds,
    asked-human count, outcome)."""
    from .fragment import resolve, withhold
    from .route import planner_stub, route
    t0 = time.monotonic()
    request, withheld = withhold(raw_request.strip())
    resolution = resolve(request, state)
    res = intake.run(request, root, state=state, resolution=resolution)
    if res.status == "ready":
        res.outcome = route(res.spec, state, planner or planner_stub)
    else:
        res.outcome = {"status": "blocked", "next": None, "outcome": "blocked"}
    now = intake.clock()
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "task": res.task_id,
           "request": request[:120], "kind": (res.spec or {}).get("kind"), "outcome": res.outcome["outcome"],
           "tokens": res.tokens(), "seconds": round(time.monotonic() - t0, 3),
           "asked_human": len(res.human_questions()) if res.status == "ready" else 0, "turns": len(res.turns),
           "withheld": withheld, "resolution": resolution.how if resolution else None,
           "backend": intake.backend, "model": intake.model}
    l0.append(intake.ga_dir / "ledger" / f"requests-{time.strftime('%Y-%m-%d', time.gmtime(now))}.jsonl", row)
    return res


__all__ = ["Intake", "Result", "handle", "context_problems", "Turn", "MAX_REPAIRS", "MEASURED_OVERHEAD", "overhead_of", "task_id", "build_spec", "usage_counts"]
