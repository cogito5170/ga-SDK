"""``ga gemini`` — a Gemini supervisor (CMD-GA21, BD-221). The user talks to ga; Gemini only directs.

- Every Gemini turn is one headless, short-lived CLI process (``ga.adapters.gemini_cli``) on the model fixed in the
  config (default gemini-3-flash-preview). No switch prompt, no fallback: a turn served by another model fails.
- A Gemini turn answers with a closed step list (``ga-gemini-plan/1``): tool steps from the configured tool table (an
  extension's MCP tools over stdio, or Python functions) and at most one next model step.
- rlo's Scheduler (CMD-K12) runs the list: tool steps whenever ready, model steps only when the Governor allows. When a
  model step is parked (the minute quota, or a 429 with ``retryDelay``) ga prints one status block (when it resumes,
  what is done / running / parked, what comes next), saves the state file and sleeps until the window opens. After a
  crash or a closed terminal, ``ga gemini --resume`` continues from the state file.
- ga's state stays on disk (state file, tool results, log); in memory only capped previews. The log holds labels and
  numbers only — no prompt, answer or result text.

rlo is imported only inside the functions that run a task, so ``import ga`` never loads it.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

from .adapters.gemini_cli import DEFAULT_MODEL, GeminiCLI, GeminiError
from .forms import FormError, Problem

CONFIG_SCHEMA = "ga-gemini/1"
PLAN_SCHEMA = "ga-gemini-plan/1"
STATE_SCHEMA = "ga-gemini-state/1"
MODEL_STEP = "gemini"  # the one model step name in the step table
STEP_ID = re.compile(r"^[A-Za-z0-9_-]{1,12}$")
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:+-]{1,40}$")  # a step-table name is a label (rlo LABEL)
MAX_PLAN_STEPS = 16


# ---- config ---------------------------------------------------------------------------------------------------------

@dataclass
class GeminiConfig:
    root: Path
    model: str = DEFAULT_MODEL
    cli: list[str] = field(default_factory=lambda: ["gemini"])
    budget: dict[str, Any] = field(default_factory=lambda: {"rpm": 10})
    tools: dict[str, dict[str, str]] = field(default_factory=dict)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    state_dir: str = ".ga-gemini"
    max_model_steps: int = 20
    result_cap: int = 2000
    turn_timeout_s: float = 600.0
    est_tokens: int = 2000

    @property
    def state_path(self) -> Path:
        return (self.root / self.state_dir).resolve()


def config_problems(raw: Any) -> list[Problem]:
    p: list[Problem] = []

    def bad(path: str, msg: str) -> None:
        p.append(Problem(path, msg))
    if not isinstance(raw, dict):
        return [Problem("$", "must be an object")]
    known = {"schema", "model", "cli", "budget", "tools", "mcp_servers", "state_dir", "max_model_steps", "result_cap",
             "turn_timeout_s", "est_tokens"}
    for k in sorted(set(raw) - known):
        bad(f"$.{k}", "unknown field")
    if raw.get("schema") != CONFIG_SCHEMA:
        bad("$.schema", f"must be {CONFIG_SCHEMA!r}")
    if "model" in raw and not (isinstance(raw["model"], str) and raw["model"]):
        bad("$.model", "must be a model name")
    if "cli" in raw and not (isinstance(raw["cli"], list) and raw["cli"] and all(isinstance(x, str) and x for x in raw["cli"])):
        bad("$.cli", "must be a non-empty list of strings (the gemini command)")
    b = raw.get("budget", {"rpm": 10})
    if not isinstance(b, dict) or set(b) - {"rpm", "tpm"} or not any(b.get(k) for k in ("rpm", "tpm")) or \
            any(b.get(k) is not None and not (isinstance(b[k], int) and not isinstance(b[k], bool) and b[k] > 0) for k in b):
        bad("$.budget", "must be {rpm?, tpm?} with at least one positive integer")
    servers = raw.get("mcp_servers", {})
    if not isinstance(servers, dict):
        bad("$.mcp_servers", "must be an object")
        servers = {}
    for name, s in servers.items():
        if not isinstance(s, dict) or set(s) - {"command", "cwd"} or not (isinstance(s.get("command"), list) and s["command"]
                                                                          and all(isinstance(x, str) for x in s["command"])):
            bad(f"$.mcp_servers.{name}", "must be {command: [..], cwd?}")
    tools = raw.get("tools", {})
    if not isinstance(tools, dict):
        bad("$.tools", "must be an object")
        tools = {}
    for name, t in tools.items():
        where = f"$.tools.{name}"
        if name == MODEL_STEP or not TOOL_NAME.match(name):
            bad(where, f"a tool name is a label ([A-Za-z0-9_.:+-], up to 40) and not {MODEL_STEP!r}")
        if not isinstance(t, dict) or set(t) - {"mcp", "tool", "python", "about"}:
            bad(where, "must be {mcp, tool, about?} or {python, about?}")
            continue
        if "python" in t:
            if not (isinstance(t["python"], str) and re.match(r"^[\w.]+:\w+$", t["python"])) or "mcp" in t:
                bad(where, "python must be 'module:function' (and alone)")
        elif not (isinstance(t.get("mcp"), str) and isinstance(t.get("tool"), str)) or t["mcp"] not in servers:
            bad(where, "mcp must name a server in mcp_servers, tool the MCP tool name")
    for k, lo in (("max_model_steps", 1), ("result_cap", 100), ("est_tokens", 0)):
        if k in raw and not (isinstance(raw[k], int) and not isinstance(raw[k], bool) and raw[k] >= lo):
            bad(f"$.{k}", f"must be an integer >= {lo}")
    if "turn_timeout_s" in raw and not (isinstance(raw["turn_timeout_s"], (int, float)) and raw["turn_timeout_s"] > 0):
        bad("$.turn_timeout_s", "must be a positive number")
    if "state_dir" in raw and not (isinstance(raw["state_dir"], str) and raw["state_dir"]):
        bad("$.state_dir", "must be a path")
    return p


def load_config(path: str | Path) -> GeminiConfig:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise FormError([Problem("$", f"cannot read {path}: {type(e).__name__}")]) from None
    probs = config_problems(raw)
    if probs:
        raise FormError(probs)
    kw = {k: raw[k] for k in ("model", "cli", "budget", "tools", "mcp_servers", "state_dir", "max_model_steps",
                              "result_cap", "turn_timeout_s", "est_tokens") if k in raw}
    return GeminiConfig(root=path.resolve().parent, **kw)


# ---- the closed step list -------------------------------------------------------------------------------------------

class PlanError(ValueError):
    """A Gemini turn whose answer is not a valid ga-gemini-plan/1 — the model step fails; nothing of it runs."""


def extract_plan(text: str) -> Any:
    """The JSON object of a turn: the last ```json fenced block, or the whole text."""
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    src = blocks[-1] if blocks else text.strip()
    try:
        return json.loads(src)
    except ValueError:
        raise PlanError("not_json") from None


def check_plan(plan: Any, tools: dict[str, Any]) -> list[str]:
    """Problems of a plan against the closed tool table (labels only — no plan text is repeated)."""
    if not isinstance(plan, dict):
        return ["plan: not an object"]
    out = []
    if plan.get("schema") != PLAN_SCHEMA:
        out.append("schema")
    if set(plan) - {"schema", "steps", "next", "say"}:
        out.append("unknown_field")
    steps = plan.get("steps", [])
    if not isinstance(steps, list) or len(steps) > MAX_PLAN_STEPS:
        return out + ["steps"]
    seen: list[str] = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or set(s) - {"id", "tool", "args", "after"}:
            out.append(f"steps[{i}]")
            continue
        sid = s.get("id")
        if not (isinstance(sid, str) and STEP_ID.match(sid)) or sid in seen:
            out.append(f"steps[{i}].id")
        if s.get("tool") not in tools:
            out.append(f"steps[{i}].tool_not_in_table")
        if not isinstance(s.get("args", {}), dict):
            out.append(f"steps[{i}].args")
        after = s.get("after", [])
        if not isinstance(after, list) or any(a not in seen for a in after):
            out.append(f"steps[{i}].after")
        seen.append(sid)
    nxt = plan.get("next")
    if nxt is not None:
        if not isinstance(nxt, dict) or set(nxt) - {"prompt", "after"} or not (isinstance(nxt.get("prompt"), str)
                                                                               and nxt["prompt"].strip()):
            out.append("next")
        elif not isinstance(nxt.get("after", []), list) or any(a not in seen for a in nxt.get("after", [])):
            out.append("next.after")
    if "say" in plan and not isinstance(plan["say"], str):
        out.append("say")
    return out


def protocol(cfg: GeminiConfig) -> str:
    rows = "\n".join(f"- `{n}`: {t.get('about', '')}".rstrip(": ") for n, t in sorted(cfg.tools.items())) or "- (none)"
    return (
        "You are directing a controller. You do not call tools yourself. Answer every turn with exactly one JSON object "
        "in a ```json fenced block, of this form:\n"
        f'{{"schema": "{PLAN_SCHEMA}", "steps": [{{"id": "a", "tool": "<name>", "args": {{}}, "after": []}}], '
        '"next": {"prompt": "<what you will do with the results>", "after": ["a"]} or null, "say": "<short note for the user>"}\n'
        f"Rules: at most {MAX_PLAN_STEPS} steps; ids are short labels; `after` names earlier ids of the same answer; "
        "steps without `after` between them run at once; `next` is your one next turn and gets the results of the steps "
        "it lists; set `next` to null when the task is done.\n"
        f"Tools (closed list):\n{rows}\n"
    )


# ---- the supervisor -------------------------------------------------------------------------------------------------

def _atomic_write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _hhmmss(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


class Supervisor:
    def __init__(self, cfg: GeminiConfig, *, cli: GeminiCLI | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, out: TextIO | None = None):
        self.cfg, self.clock, self._sleep, self.out = cfg, clock, sleep, out or sys.stdout
        self.cli = cli or GeminiCLI(cfg.cli, cfg.model, cwd=str(cfg.root), timeout_s=cfg.turn_timeout_s)
        self.dir = cfg.state_path
        (self.dir / "results").mkdir(parents=True, exist_ok=True)
        self.state_file, self.log_file = self.dir / "state.json", self.dir / "log.jsonl"
        self.st: dict[str, Any] = {}
        self.sched = None
        self._clients: dict[str, Any] = {}
        self._shown: tuple = ()

    # -- state · log --
    def load(self) -> bool:
        if not self.state_file.exists():
            return False
        st = json.loads(self.state_file.read_text(encoding="utf-8"))
        if st.get("schema") != STATE_SCHEMA:
            raise FormError([Problem("$.schema", f"{self.state_file} is not {STATE_SCHEMA}")])
        if st.get("model") != self.cfg.model:  # the model is the config's; a saved task does not switch it
            raise FormError([Problem("$.model", "the saved task was run with another model than the config's")])
        self.st = st
        return True

    def save(self) -> None:
        self.st["updated"] = round(self.clock(), 3)
        _atomic_write(self.state_file, self.st)

    def log(self, event: str, **kw: Any) -> None:
        row = {"at": round(self.clock(), 3), "event": event, **kw}
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    def say(self, text: str) -> None:
        self.out.write(text.rstrip("\n") + "\n")
        self.out.flush()

    # -- steps --
    def _rec(self, sid: str) -> dict[str, Any]:
        return next(s for s in self.st["steps"] if s["id"] == sid)

    def _result_text(self, sid: str) -> str:
        f = self.dir / "results" / f"{sid}.json"
        if not f.exists():
            return ""
        v = json.loads(f.read_text(encoding="utf-8"))
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        return text[: self.cfg.result_cap]

    def _prompt(self, rec: dict[str, Any]) -> str:
        if rec.get("first"):
            return protocol(self.cfg) + "\nTask:\n" + rec["prompt"]
        lines = [f"Reply with one {PLAN_SCHEMA} JSON object, as before.", "Results:"]
        for a in rec.get("needs", []):
            t = self._rec(a)
            lines.append(f"- {t['plan_id']} ({t['tool']}): {self._result_text(a)}")
        return "\n".join(lines + ["", "Your next step: " + rec["prompt"]])

    def _call_tool(self, rec: dict[str, Any]) -> Any:
        t = self.cfg.tools[rec["tool"]]
        args = rec.get("args") or {}
        if "python" in t:
            import importlib
            mod, fn = t["python"].split(":")
            return getattr(importlib.import_module(mod), fn)(**args)
        from .adapters.mcp_stdio import StdioClient
        name = t["mcp"]
        if name not in self._clients:
            s = self.cfg.mcp_servers[name]
            cwd = str((self.cfg.root / s["cwd"]).resolve()) if s.get("cwd") else str(self.cfg.root)
            self._clients[name] = StdioClient(s["command"], cwd=cwd, timeout_s=self.cfg.turn_timeout_s)
        return self._clients[name].call(t["tool"], args)

    def _tool_fn(self, sid: str) -> Callable[[dict], str]:
        def run(_results: dict) -> str:
            rec = self._rec(sid)
            t0 = self.clock()
            try:
                value = self._call_tool(rec)
            except Exception as e:
                self.log("tool", step=sid, tool=rec["tool"], ok=False, error=type(e).__name__)
                raise
            _atomic_write(self.dir / "results" / f"{sid}.json", value)
            preview = (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))[: self.cfg.result_cap]
            self.st["done"].append(sid)
            self.log("tool", step=sid, tool=rec["tool"], ok=True, chars=len(preview), seconds=round(self.clock() - t0, 3))
            self.save()
            return preview  # in memory: capped
        return run

    def _model_turn(self, sid: str) -> dict[str, Any]:
        rec = self._rec(sid)
        if sid in self.st["parked"]:
            waited = round(self.clock() - self.st["parked"].pop(sid)[1], 1)
            self.say(f"[ga gemini] resumed {sid} after {waited:g} s")
            self.log("resume", step=sid, waited_s=waited)
        from .adapters.gemini_cli import GeminiRateLimited
        try:
            turn = self.cli.run_turn(self._prompt(rec), self.st.get("session_id"))
        except GeminiRateLimited:
            self.log("turn", step=sid, ok=False, reason="rate_limited", model=self.cfg.model)
            raise
        except GeminiError as e:
            self.log("turn", step=sid, ok=False, reason=e.reason, model=self.cfg.model)
            raise
        self.st["session_id"] = turn.session_id or self.st.get("session_id")
        self.log("turn", step=sid, ok=True, served=turn.served, model=self.cfg.model, seconds=turn.seconds,
                 tokens=turn.usage.get("total_tokens"))
        try:
            plan = extract_plan(turn.text)
            probs = check_plan(plan, self.cfg.tools)
            if probs:
                raise PlanError(",".join(probs[:5]))
            self._extend(sid, plan)
        except PlanError as e:
            self.log("plan", step=sid, ok=False, problems=str(e)[:200])
            raise
        self.st["done"].append(sid)
        self.save()
        return {"usage": {k: turn.usage[k] for k in ("input_tokens", "output_tokens", "total_tokens") if k in turn.usage}}

    def _extend(self, sid: str, plan: dict[str, Any]) -> None:
        """Put a plan's tool steps and its next model step into the state and the running scheduler."""
        from rlo.scheduler import Step
        task, n = self.st["task"], self.st["model_steps"]
        ids = {s["id"]: f"{task}.r{n}.{s['id']}" for s in plan.get("steps", [])}
        new = [{"id": ids[s["id"]], "kind": "tool", "plan_id": s["id"], "tool": s["tool"], "args": s.get("args") or {},
                "after": [ids[a] for a in s.get("after", [])]} for s in plan.get("steps", [])]
        nxt = plan.get("next")
        if nxt is not None:
            if n >= self.cfg.max_model_steps:
                raise PlanError("max_model_steps")
            needs = [ids[a] for a in nxt.get("after", [])]
            new.append({"id": f"{task}.m{n + 1}", "kind": "model", "prompt": nxt["prompt"], "after": needs, "needs": needs})
            self.st["model_steps"] = n + 1
        if isinstance(plan.get("say"), str) and plan["say"].strip():
            self.st["say"] = plan["say"][:2000]
        self.st["steps"].extend(new)
        self.log("plan", step=sid, ok=True, tool_steps=len(plan.get("steps", [])), next=nxt is not None)
        for r in new:
            self.sched.add(self._step(r, Step))

    def _step(self, rec: dict[str, Any], Step: Any) -> Any:
        after = tuple(rec["after"])
        if rec["kind"] == "model":
            return Step(rec["id"], MODEL_STEP, payload=rec["id"], after=after, est_tokens=self.cfg.est_tokens,
                        model=self.cfg.model)
        return Step(rec["id"], rec["tool"], fn=self._tool_fn(rec["id"]), after=after)

    # -- status: K12 rev 2's status object, plus where the wait comes from --
    def status(self) -> dict[str, Any]:
        """The scheduler's status() {resumes_in_s, done, running, parked, next}, with ``running`` as a list,
        ``resumes_in_s`` inf for a wait that does not end today, and ``eta_source``: retryDelay or window."""
        s = self.sched
        st = dict(s.status())
        st["running"] = [st["running"]] if isinstance(st.get("running"), str) else list(st.get("running") or [])
        parked = list(st["parked"] or [])
        if parked and st.get("resumes_in_s") is None:
            st["resumes_in_s"] = math.inf  # parked with no window that opens (the daily quota)
        st["resumes_in_s"] = st.get("resumes_in_s") or 0.0
        hit = any(r.get("kind") == "rate_limit" and r.get("step") in parked for r in (s.rows if s is not None else []))
        saved = any(len(self.st.get("parked", {}).get(k, [])) > 2 and self.st["parked"][k][2] == "retryDelay" for k in parked)
        st["eta_source"] = "retryDelay" if hit or saved else "window"  # the server's wait, or the governor's window
        return st

    def _sleep_hook(self, d: float) -> None:
        """The scheduler sleeps only when nothing can run: a model step is parked. Show it once, save, then sleep."""
        st = self.status()
        key = tuple(st["parked"] or ())
        if key and key != self._shown:
            now = self.clock()
            eta = st["resumes_in_s"]
            for sid in key:
                since = self.st["parked"].get(sid, [None, round(now, 3)])[1]
                self.st["parked"][sid] = [None if not math.isfinite(eta) else round(now + eta, 3), since, st["eta_source"]]
            self.save()
            self.log("park", steps=list(key), resumes_in_s=None if not math.isfinite(eta) else round(eta, 1),
                     source=st["eta_source"], done=len(st["done"]), running=len(st["running"] or []))
            when = "does not resume today (daily quota)" if not math.isfinite(eta) else \
                f"resumes in {math.ceil(eta)} s, at {_hhmmss(now + eta)}"
            nxt = ", ".join(f"{x['id']} ({x['kind']})" for x in (st["next"] or [])[:6]) or "-"
            self.say("\n".join([
                f"[ga gemini] quota: {', '.join(key)} parked — {when} ({st['eta_source']})",
                f"  now:  done {len(st['done'])} · running {len(st['running'] or [])} · parked {len(key)}",
                f"  next: {nxt}",
                f"  saved: {self.state_file} — after a crash or a closed terminal: ga gemini --resume",
            ]))
            self._shown = key
        self._sleep(d)

    # -- running a task --
    def start(self, prompt: str) -> bool:
        prev = self.st if self.st else ({} if not self.load() else self.st)
        if prev.get("status") in ("running", "parked"):
            self.say(f"[ga gemini] {prev['task']} is not finished — ga gemini --resume continues it first")
            return False
        task = f"T{int(prev.get('tasks', 0)) + 1}"
        self.st = {"schema": STATE_SCHEMA, "model": self.cfg.model, "session_id": prev.get("session_id"),
                   "tasks": int(prev.get("tasks", 0)) + 1, "task": task, "status": "running", "model_steps": 1,
                   "steps": [{"id": f"{task}.m1", "kind": "model", "first": True, "prompt": prompt, "after": []}],
                   "done": [], "failed": {}, "parked": {}, "say": ""}
        self.save()
        self.log("task", task=task, model=self.cfg.model)
        return self._execute()

    def resume(self) -> bool:
        if not self.load():
            self.say(f"[ga gemini] nothing to resume: no {self.state_file}")
            return False
        if self.st.get("status") not in ("running", "parked"):
            self.say(f"[ga gemini] nothing to resume: {self.st.get('task')} is {self.st.get('status')}")
            return self.st.get("status") == "done"
        self.log("resume_task", task=self.st["task"])
        return self._execute()

    def _execute(self) -> bool:
        from rlo.governor import Governor
        from rlo.scheduler import Scheduler, Step
        gov = Governor({self.cfg.model: dict(self.cfg.budget)}, clock=self.clock, provider="gemini")
        kinds = {MODEL_STEP: "model", **{t: "tool" for t in self.cfg.tools}}
        # K12 rev 2 (S6): the scheduler saves its queue, results and Governor windows to this file on every row and,
        # when it exists, loads it — given the same steps again, so all of them are added, done ones too
        self.sched = Scheduler([self._step(r, Step) for r in self.st["steps"]], gov, self._model_turn, kinds=kinds,
                               clock=self.clock, sleep=self._sleep_hook, ledger=str(self.dir / "ledger.jsonl"),
                               run_id=self.st["task"], provider_name="gemini", usage_format="otel",
                               state=str(self.dir / f"scheduler-{self.st['task']}.json"))
        self._shown = ()
        try:
            report = self.sched.run()
        finally:
            for c in self._clients.values():
                c.close()
            self._clients = {}
        self.st["failed"] = dict(report.failed)
        self.st["status"] = "done" if report.ok else ("parked" if report.parked and not report.failed else "failed")
        self.save()
        models = sum(1 for r in self.st["steps"] if r["kind"] == "model" and r["id"] in self.st["done"])
        tools = sum(1 for r in self.st["steps"] if r["kind"] == "tool" and r["id"] in self.st["done"])
        self.log("end", task=self.st["task"], status=self.st["status"], model_turns=models, tool_steps=tools,
                 failed=len(report.failed), skipped=len(report.skipped), slept_s=report.slept_s, sleeps=report.sleeps)
        head = (f"[ga gemini] {self.st['task']} {self.st['status']}: {models} model turn(s), {tools} tool step(s), "
                f"{len(report.failed)} failed, waited {report.slept_s:g} s")
        if report.failed:
            head += " — failed: " + ", ".join(f"{k} ({v})" for k, v in report.failed.items())
        self.say(head + (("\n" + self.st["say"]) if self.st.get("say") else ""))
        return report.ok


# ---- `ga gemini` ----------------------------------------------------------------------------------------------------

def main(args: Any) -> int:
    try:
        cfg = load_config(args.gemini_config)
    except FormError as e:
        for p in e.problems:
            print(f"config: {p}", file=sys.stderr)
        return 2
    sup = Supervisor(cfg)
    try:
        if args.resume:
            return 0 if sup.resume() else 1
        if args.prompt:
            return 0 if sup.start(" ".join(args.prompt)) else 1
        ok = True
        while True:  # the terminal front: one prompt in, the status and the outcome out
            try:
                line = input("ga gemini> ")
            except EOFError:
                return 0 if ok else 1
            if line.strip():
                ok = sup.start(line.strip()) and ok
    except FormError as e:
        for p in e.problems:
            print(f"state: {p}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"\n[ga gemini] stopped; the state is saved — ga gemini --resume continues", file=sys.stderr)
        return 130
