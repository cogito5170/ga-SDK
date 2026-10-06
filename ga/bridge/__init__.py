"""``ga bridge`` (CMD-GA36 S3): baseline's agy bridge inside ga — the hub's directives -> ga supervise (agv/agy) ->
report/2 back, over ga mail (BD-356). A Mac needs only ga-sdk: the tools come from ga (``ga.bridge.tools``).

    ga bridge --config ~/agy-bridge.json [--once]       (or: ga bridge once | loop)

Rules kept from baseline's bridge: only the hub's directive/2 runs, anything else is declined; TOOL_NEEDED lines and
refused tools become blockers; a secret-looking answer is withheld; each directive gets its own state dir. Added: a
503 MODEL_CAPACITY_EXHAUSTED (or agy's exit 3) is retried once after a short backoff, then reported as a dependency
blocker 'capacity' (not quota). Waiting is code: the supervise log is followed, one line per turn; no model is asked.
The config keys are the ones ~/agy-bridge.json already has (``pull`` is accepted and ignored: Mailbox fetches).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from ..forms import FormError, hard, parse_text, validate
from ..mailbox import Mailbox, MailError, secrets_in
from ..runlog import Tail, TurnMeter, follow
from .tools import TOOLS, table

DEFAULTS = {"name": "AGY", "hub": "baseline", "every_s": 300, "turn_timeout_s": 1800, "max_answer_chars": 4000,
            "supervise_config": "ga-supervise.json", "pull": True, "capacity_backoff_s": 30}
TOOL_NEEDED = re.compile(r"^\s*TOOL_NEEDED:\s*(.+?)\s*$", re.M)
REFUSED = re.compile(r"agy refused (\d+) action\(s\) in \S+: ([\w, ]+)")
STUCK = re.compile(r"\[ga supervise\] (\S+) is not finished")
CAPACITY = re.compile(r"MODEL_CAPACITY_EXHAUSTED")


def load_config(path: str) -> dict[str, Any]:
    cfg = {**DEFAULTS, **json.loads(Path(path).expanduser().read_text(encoding="utf-8"))}
    for k in ("mailbox_repo", "workdir"):
        if not cfg.get(k):
            raise SystemExit(f"agy-bridge config: {k} is required")
        cfg[k] = str(Path(cfg[k]).expanduser().resolve())
    return cfg


def task_text(head: dict[str, Any]) -> str:
    lines = [f"[{head['id']} rev {head.get('rev', 1)}] {head.get('goal', '')}"]
    for key, title in (("scope", "Scope"), ("done_when", "Done when")):
        items = head.get(key) or []
        if items:
            lines.append(f"{title}:")
            lines += [f"- {i.get('id', '')}: {i.get('text', '')}" for i in items]
    lines.append("Use only the tools ga lists. If a tool you need is missing, write one line "
                 "'TOOL_NEEDED: <name> - <what it must do>' and finish with what you could do.")
    return "\n".join(lines)


def effective_config(cfg: dict[str, Any], directive_id: str | None = None, max_steps: int | None = None) -> Path:
    """ga-supervise.json + ga's tool table; each directive gets its own state dir (<state_dir>/<id>). ``max_steps``
    (ga ask: the daily agy cap left) lowers the config's model-turn cap, never raises it."""
    base = json.loads((Path(cfg["workdir"]) / cfg["supervise_config"]).read_text(encoding="utf-8"))
    base["tools"] = {**table(), **(base.get("tools") or {})}
    if directive_id:
        base["state_dir"] = str(Path(base.get("state_dir") or ".ga-supervise") / directive_id)
    if max_steps is not None:
        base["max_model_steps"] = max(1, min(int(base.get("max_model_steps") or 20), int(max_steps)))
    out = Path(cfg["workdir"]) / "ga-supervise.bridge.json"
    out.write_text(json.dumps(base, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return out


def run_supervise(cfg: dict[str, Any], conf: Path, task: str, progress: Callable[[str], None] | None = None,
                  sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """One ``ga supervise`` process; while it runs, its log is followed (code, not a model) and each finished turn's
    line goes to ``progress``."""
    state = json.loads(conf.read_text(encoding="utf-8")).get("state_dir", ".ga-supervise")
    log = Path(cfg["workdir"]) / state / "log.jsonl"
    tail, meter = Tail(log), TurnMeter()
    start = tail.offset
    src = str(Path(__file__).resolve().parents[2])  # this ga, also when it runs from a checkout
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH", "")])),
               AGY_BRIDGE_COMMANDS=json.dumps(cfg.get("commands") or {}))
    t0 = time.monotonic()
    with subprocess.Popen([sys.executable, "-m", "ga", "supervise", "--config", conf.name, task], cwd=cfg["workdir"],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as p:
        import threading
        chunks: list[str] = []
        reader = threading.Thread(target=lambda: chunks.append(p.stdout.read() if p.stdout else ""), daemon=True)
        reader.start()
        timed_out = []

        def done() -> bool:
            if p.poll() is not None:
                return True
            if time.monotonic() - t0 > cfg["turn_timeout_s"]:
                p.kill()
                timed_out.append(True)
                return True
            return False
        follow(done, tail, meter, progress or (lambda line: None), sleep=sleep, poll_s=float(cfg.get("poll_s", 0.5)))
        p.wait()
        reader.join(5)
    out = "".join(chunks)
    code = p.returncode
    if timed_out:
        code, out = 124, f"bridge: ga supervise timed out after {cfg['turn_timeout_s']} s\n{out}"
    events = []
    if log.exists():
        with log.open(encoding="utf-8") as f:
            f.seek(start)
            for line in f:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
    return {"code": code, "out": out, "events": events}


def is_capacity(run: dict[str, Any]) -> bool:
    """agy's 503 MODEL_CAPACITY_EXHAUSTED (or its exit 3 straight through): the server's capacity, retryable."""
    return (run.get("code") == 3 or bool(CAPACITY.search(run.get("out") or ""))
            or any(e.get("event") == "turn" and e.get("reason") in ("capacity", "transient:503") for e in run.get("events") or []))


def _served_model(turns: list[dict[str, Any]]) -> str | None:
    """The model of the rung that actually answered (a turn's ``served``), never the configured default: None when no
    turn says (then results has no model entry) (CMD-GA49 S3)."""
    for t in turns:
        sv = t.get("served")
        sv = [sv] if isinstance(sv, str) else sv
        names = [str(x) for x in sv or [] if x]
        if names:
            return ",".join(names)
    return None


def build_report(cfg: dict[str, Any], head: dict[str, Any], run: dict[str, Any], capacity: bool = False) -> str:
    turns = [e for e in run["events"] if e.get("event") == "turn"]
    end = next((e for e in reversed(run["events"]) if e.get("event") == "end"), {})
    ok = run["code"] == 0 and end.get("status") == "done" and not capacity
    out = run["out"]
    needed = TOOL_NEEDED.findall(out)
    if needed:  # CMD-GA42 S2: a TOOL_NEEDED line becomes a proposal (refused until a person writes its argv)
        from .. import actions
        try:
            actions.from_text(out, Path(cfg["workdir"]), source="ga bridge")
        except (OSError, ValueError, KeyError):
            pass
    refused = REFUSED.findall(out)
    blockers = []
    if capacity:
        blockers.append({"kind": "dependency", "what": "capacity: agy's model capacity was exhausted (503 "
                         "MODEL_CAPACITY_EXHAUSTED) and stayed so after one retry; not the quota"})
    blockers += [{"kind": "dependency", "what": f"tool needed: {t}"[:300]} for t in needed]
    blockers += [{"kind": "permission", "what": f"agy refused {n} action(s): {what.strip()}"} for n, what in refused]
    blockers += [{"kind": "dependency", "what": f"ga supervise state: task {t} unfinished in the state dir"}
                 for t in STUCK.findall(out)]
    evidence = [f"ga supervise exit {run['code']}, status {end.get('status', '?')}, model turns "
                f"{end.get('model_turns', len(turns))}, tool steps {end.get('tool_steps', 0)}",
                "self-reported through the agy bridge; baseline verifies"]
    items = [{"id": d["id"], "state": "met" if ok else "unmet", "evidence": evidence}
             for d in head.get("done_when") or [] if re.match(r"^D\d+$", str(d.get("id", "")))]
    report = {"schema": "report/2", "from": cfg["name"],
              "handled": [{"id": head["id"], "rev_seen": int(head.get("rev", 1)), "status": "done"}],
              "items": items or [{"id": "D1", "state": "met" if ok else "unmet", "evidence": evidence}],
              "results": [{"name": "input_tokens", "value": sum(int(t.get("input_tokens") or 0) for t in turns)},
                          {"name": "tokens", "value": sum(int(t.get("tokens") or 0) for t in turns)},
                          {"name": "seconds", "value": round(sum(float(t.get("seconds") or 0) for t in turns), 3)}]}
    served = _served_model(turns)
    if served:  # report/2 results hold numbers and strings only: an unknown model is left out, never guessed
        report["results"].append({"name": "model", "value": served})
    if blockers:
        report["blockers"] = blockers[:10]
    answer = out.strip()[-cfg["max_answer_chars"]:]
    if secrets_in(answer):
        answer = "(the answer was withheld: it looked like it held a secret)"
    return "```ga\n" + json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n```\n\n## agy answer\n\n" + \
        "```text\n" + answer.replace("```", "'''") + "\n```\n"


def declined(cfg: dict[str, Any], form: str, why: str) -> str:
    did = form if re.match(r"^CMD-[A-Z]+\d+$", form) else "CMD-X0"
    report = {"schema": "report/2", "from": cfg["name"], "handled": [{"id": did, "rev_seen": 1, "status": "declined",
              "reason": why[:200]}], "items": [{"id": "D1", "state": "na"}]}
    return "```ga\n" + json.dumps(report, separators=(",", ":")) + "\n```\n"


def one_pass(cfg: dict[str, Any], box: Mailbox | None = None,
             runner: Callable[[dict, Path, str], dict] = run_supervise, log: Callable[[str], None] = print,
             sleep: Callable[[float], None] = time.sleep, max_steps: int | None = None, limit: int | None = None) -> int:
    """Answer the unread messages for ``cfg['name']`` (at most ``limit``): run the hub's directives, decline the rest."""
    box = box or Mailbox(cfg["mailbox_repo"])
    handled = 0
    for m in box.unread(cfg["name"]):
        if limit is not None and handled >= limit:
            break
        log(f"bridge: message from {m.sender}: {m.schema or '?'} {m.form}")
        try:
            if m.sender != cfg["hub"]:
                reply = declined(cfg, m.form, f"only {cfg['hub']} may send directives to {cfg['name']}")
            elif m.schema != "directive/2" or not m.valid:
                reply = declined(cfg, m.form, "not a valid directive/2: " + "; ".join(m.problems)[:150])
            else:
                head, _ = parse_text(m.text)
                conf = effective_config(cfg, head["id"], max_steps)
                task = task_text(head)
                run = runner(cfg, conf, task)
                capacity = is_capacity(run)
                retried = any(e.get("event") == "transient" for e in run.get("events") or [])
                if capacity and not retried:  # once, after a short backoff; a second capacity stop is reported, not retried
                    # again (GA41: ga supervise retries a transient turn itself; then the bridge does not retry the run)
                    log(f"bridge: agy capacity exhausted (503); one retry in {cfg['capacity_backoff_s']} s")
                    sleep(float(cfg["capacity_backoff_s"]))
                    run = runner(cfg, conf, task)
                    capacity = is_capacity(run)
                reply = build_report(cfg, head, run, capacity)
            problems = hard(validate(parse_text(reply)[0]))
            if problems:
                raise FormError(problems)
            box.send(cfg["hub"], reply, cfg["name"])
            log(f"bridge: report sent to {cfg['hub']} for {m.form}")
        except (MailError, FormError, OSError, ValueError) as e:
            log(f"bridge: {m.form} not answered: {type(e).__name__}: {str(e)[:200]}")
        box.mark_read(cfg["name"], m.path)  # once: a failing directive is not retried in a loop
        handled += 1
    return handled


def pid_file() -> Path:
    from ..ask.store import home
    return home() / "bridge.pid"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ga bridge", description="GA CLI: the agy bridge — the hub's directives through "
                                 "ga supervise, report/2 back over ga mail")
    ap.add_argument("mode", nargs="?", choices=["once", "loop"], default="loop")
    ap.add_argument("--config", default="agy-bridge.json")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    once = a.once or a.mode == "once"
    progress = (lambda line: print(f"bridge: {line}", flush=True))
    pf = pid_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(os.getpid()), encoding="utf-8")  # `ga ask "멈춰"` stops this process
    try:
        while True:
            n = one_pass(cfg, runner=lambda c, conf, task: run_supervise(c, conf, task, progress))
            if once:
                return 0
            if n == 0:
                print(f"bridge: nothing new; next check in {cfg['every_s']} s", flush=True)
            try:
                time.sleep(cfg["every_s"])
            except KeyboardInterrupt:
                return 130
    finally:
        try:
            if pf.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pf.unlink()
        except OSError:
            pass
