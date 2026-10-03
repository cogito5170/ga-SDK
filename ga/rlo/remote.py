"""Remote profile (CMD-GR2) -- the rlo guard for a *remote* worker session, as project settings in its work repo.

Generalised from the W1 guard that ran on a real remote session (amp 344a604 ``ops/rlo/``, BD-183/189/193/194):

    .claude/settings.json     SessionStart -> ops/rlo/install.sh ; PreToolUse "*" -> ops/rlo/guard.sh
    ops/rlo/install.sh        pinned rlo-sdk[sensor] into a private venv (idempotent)
    ops/rlo/guard.sh          python -m rlo.hooks --mode enforce, fail closed
    ops/rlo/model.json        action-model/1: rlo's start model + what W1 measured + session plumbing (S2)
    ops/rlo/GUARD.md          ownership, grants, fail-closed list, and "report every deny on your channel" (S3)

What W1 taught (BD-194/197) is built in: the plumbing tools a remote worker needs are always in the model; the guard
reads stale health after an idle gap as D, and one read-only call fixes it (GUARD.md says so); denials must reach the
channel, not stay in the worker's own chat.

ga rlo writes these files and never commits or pushes them (S5, BD-196: an AI may not change another session's guard).
It prints the git commands for a human.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import _pins
from . import preset

GUARD_DIR = "ops/rlo"
SETTINGS = ".claude/settings.json"
FILES = ("install.sh", "guard.sh", "model.json", "GUARD.md", "PROMPT.md")
INSTALL_CMD = 'bash "$CLAUDE_PROJECT_DIR/ops/rlo/install.sh"'
GUARD_CMD = 'bash "$CLAUDE_PROJECT_DIR/ops/rlo/guard.sh"'
VENV_VAR = "GA_RLO_VENV"
GRANTS = ("Bash", "mcp__github__add_issue_comment", "mcp__claude-code-remote__send_message")
# S3: the sentence the worker reads; doctor checks it is there
DENY_REPORT = "Post every guard deny verbatim on your channel"
# CMD-GR4 S2: the in-turn ReAct rule (rlo CMD-K11: a deny ends with '-- react: {json}'); doctor checks it is there
REACT_RULE = ("When a deny ends with '-- react:', do exactly that alternative once. When escalate is true, post the deny "
              "verbatim on your channel and continue other work.")
# CMD-GR4 S1: same-purpose substitutes for the session plumbing (operator-owned, rlo K11 action-model 'substitutes').
# Only tools in the model may be named, and an external one only if granted; never a substitute for these:
SUBSTITUTES = {"ReadNotifications": ["mcp__github__issue_read"]}
NO_SUBSTITUTE = ("WebFetch", "Agent", "mcp__claude-code-remote__create_session")


def _spec(name: str, risk: str, params: dict[str, Any], description: str) -> dict[str, Any]:
    return {"schema": "action-spec/1", "name": name, "version": "1", "target_model": None, "params": params,
            "preconditions": [], "risk": risk, "postcondition": [], "window_ms": None, "description": description}


def _opt(t: str) -> dict[str, Any]:
    return {"type": t, "required": False}


# S2: session plumbing a remote worker needs (W1: without ReadNotifications it could not read its hub, BD-194)
PLUMBING = [
    _spec("ToolSearch", "read", {"query": {"type": "string"}, "max_results": _opt("number")},
          "Load deferred tool schemas (read only)."),
    _spec("ReadNotifications", "read", {}, "Read queued cross-session notifications (how the hub's send_message arrives)."),
    _spec("mcp__github__issue_read", "read",
          {"method": {"type": "string"}, "owner": {"type": "string"}, "repo": {"type": "string"},
           "issue_number": {"type": "number"}, "page": _opt("number"), "perPage": _opt("number")},
          "Read the channel issue or its comments."),
    _spec("mcp__github__add_issue_comment", "external",
          {"owner": {"type": "string"}, "repo": {"type": "string"}, "issue_number": {"type": "number"},
           "body": _opt("string"), "comment_id": _opt("number"), "reaction": _opt("string")},
          "Post a report or a guard deny on the channel. External: granted."),
    _spec("mcp__claude-code-remote__send_message", "external",
          {"session_id": {"type": "string"}, "message": {"type": "string"}, "priority": _opt("string")},
          "Send a notify/1 line to the hub session. External: granted."),
]
# measured on W1 (amp 714c00b): the optional fields Read and Grep really get, and Glob
MEASURED_PARAMS = {
    "Read": {"offset": _opt("number"), "limit": _opt("number")},
    "Grep": {"glob": _opt("string"), "type": _opt("string"), "output_mode": _opt("string"), "-n": _opt("bool"),
             "-i": _opt("bool"), "-A": _opt("number"), "-B": _opt("number"), "-C": _opt("number"),
             "head_limit": _opt("number"), "multiline": _opt("bool")},
}
MEASURED_SPECS = [_spec("Glob", "read", {"pattern": {"type": "string"}, "path": _opt("string")}, "Find files (read only).")]


def model(name: str = "worker", substitutes: bool = True) -> dict[str, Any]:
    """rlo's start model + W1's measured fields + plumbing (always every PLUMBING tool, GR2 S2) + the SUBSTITUTES map
    (GR4 S1). rlo >= 0.6.0 (K11) reads 'substitutes' beside action-model/1 and splits it off; install.sh pins it."""
    m = json.loads(preset.packaged_model().read_text(encoding="utf-8"))
    for s in m["specs"]:
        s["params"].update(MEASURED_PARAMS.get(s["name"], {}))
    have = {s["name"] for s in m["specs"]}
    m["specs"] += [json.loads(json.dumps(s)) for s in MEASURED_SPECS + PLUMBING if s["name"] not in have]
    m["version"] = f"ga-rlo-remote-1:{name}"
    if substitutes:
        m["substitutes"] = json.loads(json.dumps(SUBSTITUTES))
    return m


def substitute_problems(m: dict[str, Any]) -> list[str]:
    """A substitutes map may only name tools in the model (external ones granted) and never serve NO_SUBSTITUTE."""
    out = []
    subs = m.get("substitutes", {})
    if not isinstance(subs, dict):
        return ["model.json: substitutes is not an object"]
    specs = {s["name"]: s for s in m.get("specs", [])}
    for tool, alts in subs.items():
        if tool in NO_SUBSTITUTE:
            out.append(f"model.json: a substitute for {tool} (none is allowed: it must be reported, not worked around)")
        for alt in alts if isinstance(alts, list) else [alts]:
            if alt not in specs:
                out.append(f"model.json: substitute {alt!r} for {tool} is not in the model")
            elif specs[alt].get("risk") in ("external", "irreversible") and alt not in GRANTS:
                out.append(f"model.json: substitute {alt!r} for {tool} is {specs[alt]['risk']} and not granted")
    return out


INSTALL_SH = """#!/usr/bin/env bash
# Install rlo-sdk[sensor] (pinned) into a private venv for this repo's rlo guard. Idempotent.
# Written by ga-rlo init --profile remote. Owned by the hub: the worker session must not edit it.
set -u
PIN="{sha}"
VENV="${{{venv_var}:-$HOME/.cache/ga-rlo-venv}}"
MARK="$VENV/.pinned-$PIN"
[ -f "$MARK" ] && exit 0
PY="$(command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3)"
"$PY" -m venv "$VENV" >/dev/null 2>&1 || exit 1
"$VENV/bin/pip" install -q "rlo-sdk[sensor] @ git+{url}@$PIN" >/dev/null 2>&1 || exit 1
"$VENV/bin/python" -c "import rlo.hooks" >/dev/null 2>&1 || exit 1
touch "$MARK"
"""

GUARD_SH = """#!/usr/bin/env bash
# PreToolUse rlo guard for the {name} worker session (enforce). Written by ga-rlo init --profile remote.
# Fail closed: any problem (no venv, no model, rlo error, no recorded verdict, non-JSON output) -> deny.
# rlo allows by printing nothing; it never prints "allow" (it does not widen permissions).
# No clock override on purpose: a worker must not be able to make the guard read stale state.
set -u
DIR="${{CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}}"
VENV="${{{venv_var}:-$HOME/.cache/ga-rlo-venv}}"
MODEL="$DIR/ops/rlo/model.json"
REC="$HOME/.rlo/{name}.jsonl"
deny() {{
  printf '{{"hookSpecificOutput":{{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"rlo guard (fail closed): %s"}}}}\\n' "$1"
  exit 0
}}
INPUT="$(cat)"
[ -n "$INPUT" ] || deny "empty hook input"
if [ ! -x "$VENV/bin/python" ]; then
  bash "$DIR/ops/rlo/install.sh" || deny "rlo not installed and install failed"
fi
[ -f "$MODEL" ] || deny "model file missing"
mkdir -p "$(dirname "$REC")"
BEFORE=$(wc -l < "$REC" 2>/dev/null || echo 0)
OUT="$(printf '%s' "$INPUT" | "$VENV/bin/python" -m rlo.hooks --model "$MODEL" --mode enforce \\
        {grants} --record "$REC" 2>/dev/null)"
RC=$?
[ "$RC" -eq 0 ] || deny "rlo exited $RC"
AFTER=$(wc -l < "$REC" 2>/dev/null || echo 0)
# rlo writes one record line per verdict; no new line means no verdict was made -> deny
[ "$AFTER" -gt "$BEFORE" ] || deny "rlo recorded no verdict"
# allow = empty output (rlo never prints "allow"); anything printed must be JSON
if [ -n "$OUT" ]; then
  printf '%s' "$OUT" | "$VENV/bin/python" -c 'import json,sys; json.loads(sys.stdin.read())' 2>/dev/null || deny "rlo output is not JSON"
  printf '%s\\n' "$OUT"
fi
exit 0
"""

GUARD_MD = """# {name} guard (rlo, enforce) -- owned by the hub

The middle verification line (METHOD §4c 7) for the `{name}` remote worker session, written by
`ga-rlo init --profile remote` (ga_rlo, CMD-GR2). A human commits it; no AI session changes another session's guard.

- `.claude/settings.json`: SessionStart runs `ops/rlo/install.sh`; every tool call (PreToolUse, matcher `*`) runs
  `ops/rlo/guard.sh`.
- `install.sh`: installs pinned `rlo-sdk[sensor] @ {sha7}` into `${venv_var}` (default `~/.cache/ga-rlo-venv`).
- `guard.sh`: runs `python -m rlo.hooks --mode enforce` with `model.json`. Records one line per verdict in
  `~/.rlo/{name}.jsonl`.
  - Fail closed. It denies when:
    - the venv is missing and the install fails;
    - the model is missing;
    - rlo exits nonzero;
    - rlo records no new verdict line;
    - rlo prints non-JSON;
    - the hook input is empty.
  - rlo allows by printing nothing. There is no clock override.
- Grants (external tools allowed): {grants}.
- Session plumbing always in the model: {plumbing}.
- Not in the model, so denied with A1: e.g. `WebFetch`, `Agent`, `mcp__claude-code-remote__create_session`.
- rlo does not see Bash command content, file paths or argument values.

## For the worker session

{worker_rules}
"""

WORKER_RULES = """- **{react_rule}**
- If a deny has no `-- react:` line (an older rlo), the alternative is: {deny_report} (the issue your hub reads),
  then go on with other work. Do not ask only in your own chat: the hub does not see it.
- After an idle gap (over about 10 minutes) the first Bash can be denied with D: the guard no longer knows your run's
  health. Make one read-only call (for example `Read` of a file you need) and retry.
- Do not edit `.claude/` or `ops/rlo/`. Changes go through the hub.
"""

PROMPT_MD = """# Guard rules for the {name} session (paste into the session's start prompt)

Every tool call you make goes through the rlo guard (`ops/rlo/GUARD.md`). It can deny a call; it never allows more.

{worker_rules}"""


def files(name: str = "worker") -> dict[str, str]:
    """{path in the work repo: content} for the guard files (not the settings)."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
        raise ValueError(f"name {name!r}: letters, digits, _ . - only")
    _, _, url, sha = _pins.PINS["rlo"]
    grants = " ".join(f"--grant {shlex.quote(g)}" for g in GRANTS)
    rules = WORKER_RULES.format(react_rule=REACT_RULE, deny_report=DENY_REPORT)
    return {
        f"{GUARD_DIR}/install.sh": INSTALL_SH.format(sha=sha, url=url, venv_var=VENV_VAR),
        f"{GUARD_DIR}/guard.sh": GUARD_SH.format(name=name, venv_var=VENV_VAR, grants=grants),
        f"{GUARD_DIR}/model.json": json.dumps(model(name), ensure_ascii=False, indent=1) + "\n",
        f"{GUARD_DIR}/PROMPT.md": PROMPT_MD.format(name=name, worker_rules=rules),
        f"{GUARD_DIR}/GUARD.md": GUARD_MD.format(
            name=name, sha7=sha[:7], venv_var=VENV_VAR, worker_rules=rules,
            grants=", ".join(f"`{g}`" for g in GRANTS), plumbing=", ".join(f"`{s['name']}`" for s in PLUMBING)),
    }


def _ours(cmd: str) -> bool:
    return "ops/rlo/install.sh" in cmd or "ops/rlo/guard.sh" in cmd


def settings(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Project settings with our two hooks. Other keys and other hooks are kept; ours are replaced in place, once."""
    d = json.loads(json.dumps(existing or {}))
    hooks = d.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{SETTINGS}: hooks is not an object")
    for ev, matcher, cmd in (("SessionStart", None, INSTALL_CMD), ("PreToolUse", "*", GUARD_CMD)):
        groups = hooks.setdefault(ev, [])
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", []) if not _ours(h.get("command", ""))]
        groups[:] = [g for g in groups if g.get("hooks")]
        groups.append({**({"matcher": matcher} if matcher else {}),
                       "hooks": [{"type": "command", "command": cmd, "timeout": 600}]})
    return d


def write(repo: str | Path, name: str = "worker", force: bool = False) -> dict[str, Any]:
    """Write the guard files and the settings into ``repo``. Never runs git (S5)."""
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"{repo} is not a directory")
    out = files(name)
    sp = repo / SETTINGS
    existing = None
    if sp.exists():
        try:
            existing = json.loads(sp.read_text(encoding="utf-8") or "{}")
        except ValueError as e:
            raise ValueError(f"{sp}: not JSON -- nothing written") from e
    for rel, text in out.items():
        p = repo / rel
        if p.exists() and p.read_text(encoding="utf-8") != text and not force:
            raise FileExistsError(f"{p} exists and differs: not overwritten (--force to replace it)")
    for rel, text in out.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        if rel.endswith(".sh"):
            p.chmod(0o755)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(settings(existing), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths = [SETTINGS, GUARD_DIR]
    return {"repo": str(repo), "written": [SETTINGS, *out], "commands": human_commands(repo, paths, name),
            "ownership": [{"repo": "<work repo name in ga.json>", "path": p, "session": "<hub>"}
                          for p in (".claude/*", f"{GUARD_DIR}/*")]}


def human_commands(repo: Path, paths: list[str], name: str) -> list[str]:
    """What a human runs to commit and push the guard (S5). ga rlo prints these; it never runs them."""
    q = shlex.quote(str(repo))
    return [f"git -C {q} add {' '.join(paths)}",
            f"git -C {q} commit -m {shlex.quote(f'rlo guard for {name} (ga rlo init --profile remote)')}",
            f"git -C {q} push origin HEAD"]


# ------------------------------------------------------------------------------------------------ S4 replay

def _ts(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def transcript(steps: list[tuple], path: Path) -> None:
    """A Claude Code transcript at real times. steps: (tool, input, ok, at unix ms[, result text])."""
    lines, n = [], 0

    def add(kind: str, at: float, **kw: Any) -> None:
        nonlocal n
        n += 1
        lines.append({"type": kind, "sessionId": "replay", "uuid": f"u{n:04d}", "timestamp": _ts(at), **kw})

    start = (steps[0][3] if steps else time.time() * 1000) - 1000
    add("user", start, message={"role": "user", "content": "go"})
    for i, (tool, inp, ok, at, *text) in enumerate(steps):
        add("assistant", at, message={"id": f"m{i}", "model": "replay", "role": "assistant", "stop_reason": "tool_use",
                                      "usage": {"input_tokens": 1, "output_tokens": 1},
                                      "content": [{"type": "tool_use", "id": f"t{i}", "name": tool, "input": inp}]})
        add("user", at + 500, toolUseResult={}, message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": [{"type": "text", "text": text[0] if text else "x"}],
             "is_error": not ok}]})
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


BASH = ("Bash", {"command": "ls"})
READ = ("Read", {"file_path": "README.md"})
PLUMBING_CALLS = [
    ("ToolSearch", {"query": "select:ReadNotifications"}),
    ("ReadNotifications", {}),
    ("mcp__github__issue_read", {"method": "get_comments", "owner": "o", "repo": "r", "issue_number": 1}),
    ("mcp__github__add_issue_comment", {"owner": "o", "repo": "r", "issue_number": 1, "body": "report"}),
    ("mcp__claude-code-remote__send_message", {"session_id": "session_x", "message": "{}"}),
]
DENY_A1 = [("WebFetch", {"url": "https://example.invalid/"}),
           ("mcp__claude-code-remote__create_session", {"prompt": "p"})]
IDLE_MS = 2 * 3600 * 1000
STALE_HINT_MARK = "hint: the decision state is stale"  # rlo 0.5.1 rlo.hooks.STALE_HINT


@dataclass
class Case:
    name: str
    steps: list  # [(tool, input, ok, ms before now)]
    call: tuple
    want: str  # "pass" | "A1" | "D" | "fail-closed"
    stdin: str | None = None  # raw stdin instead of the hook input
    setup: str = ""  # "no-model" | "bad-model" | "no-venv"
    direct: bool = False  # call rlo.hooks itself, not guard.sh (rlo 0.5.1 closes on malformed input on its own)
    hint: bool = False  # the deny must carry rlo's stale-only hint (rlo 0.5.1)
    react: dict | None = None  # these fields of the deny's '-- react: {json}' line (rlo 0.6.0, K11; GR4 S3)
    grants: tuple | None = None  # direct calls only: grants other than the preset's


def _deny_text(rule: str, cause: str, kind: str, attempt: int, **extra: Any) -> str:
    """What an earlier deny left in the transcript: its reason ends with the react line (rlo counts these)."""
    obj = {"kind": kind, "rule": rule, "cause": cause, "attempt": attempt, "of": 2, "escalate": kind == "report", **extra}
    return f"guard DENY({rule}): earlier deny\n-- react: " + json.dumps(obj, sort_keys=True, separators=(",", ":"))


def cases() -> list[Case]:
    ok = [(BASH[0], BASH[1], True, 5000)]
    report = {"kind": "report", "escalate": True}
    out = [Case("Bash first call", [], BASH, "pass"), Case("Bash after Bash", ok, BASH, "pass"),
           Case("Read", ok, READ, "pass")]
    out += [Case(f"plumbing {t}", ok, (t, i), "pass") for t, i in PLUMBING_CALLS]
    # W1 case 4 (and create_session): no substitute -> report, escalate
    out += [Case(f"A1 {t}", ok, (t, i), "A1", react=dict(report, rule="A1", cause="no_substitute")) for t, i in DENY_A1]
    gap = [(BASH[0], BASH[1], True, IDLE_MS)]
    # W1 case 2: stale-only D -> refresh_read; after one read the call passes
    out += [Case("Bash after 2h idle", gap, BASH, "D", hint=True,
                 react={"kind": "refresh_read", "rule": "D", "cause": "stale", "attempt": 1, "escalate": False}),
            Case("Bash after 2h idle, then Read", gap + [(READ[0], READ[1], True, 2000)], BASH, "pass")]
    # W1 case 1: the W1 v1 model had no ReadNotifications -> A1, use_tool issue_read from the substitutes map
    rn = ("ReadNotifications", {})
    use = {"kind": "use_tool", "rule": "A1", "cause": "has_substitute", "tool": "mcp__github__issue_read"}
    out += [Case("W1 v1 model: ReadNotifications", ok, rn, "A1", setup="w1-v1",
                 react=dict(use, attempt=1, escalate=False))]
    # the retry cap: the same deny once before -> attempt 2; twice before -> the 3rd escalates (report)
    before = [(rn[0], rn[1], False, 4000 - 1000 * k, _deny_text("A1", "has_substitute", "use_tool", k + 1,
                                                               tool="mcp__github__issue_read")) for k in range(2)]
    out += [Case("W1 v1 model: ReadNotifications, denied once before", ok + before[:1], rn, "A1", setup="w1-v1",
                 react=dict(use, attempt=2, escalate=False)),
            Case("W1 v1 model: ReadNotifications, denied twice before", ok + before, rn, "A1", setup="w1-v1",
                 react=dict(report, rule="A1", cause="has_substitute", attempt=3))]
    # W1 case 3: an external tool without its grant -> A7, report, escalate (rlo itself with one grant fewer)
    sm = ("mcp__claude-code-remote__send_message", {"session_id": "session_x", "message": "{}"})
    out += [Case("A7 send_message without its grant", ok, sm, "A7", direct=True,
                 grants=tuple(g for g in GRANTS if g != sm[0]), react=dict(report, rule="A7", cause="not_granted"))]
    out += [Case("empty stdin", [], BASH, "fail-closed", stdin=""),
            Case("garbage stdin", [], BASH, "fail-closed", stdin="not json"),
            Case("model missing", ok, BASH, "fail-closed", setup="no-model"),
            Case("model broken", ok, BASH, "fail-closed", setup="bad-model"),
            Case("no venv, install fails", ok, BASH, "fail-closed", setup="no-venv"),
            Case("rlo itself: empty stdin", [], BASH, "input-error", stdin="", direct=True,
                 react=dict(report, rule="input", cause="malformed_input")),
            Case("rlo itself: garbage stdin", [], BASH, "input-error", stdin="not json", direct=True,
                 react=dict(report, rule="input", cause="malformed_input"))]
    return out


def react_of(reason: str) -> dict | None:
    """The '-- react: {json}' line of a deny reason (rlo 0.6.0), or None."""
    for line in reason.splitlines():
        if line.strip().startswith("-- react: "):
            try:
                obj = json.loads(line.strip()[len("-- react: "):])
            except ValueError:
                return None
            return obj if isinstance(obj, dict) else None
    return None


def replay(repo: str | Path, venv: str | Path, *, guard_rel: str = f"{GUARD_DIR}/guard.sh",
           model_rel: str = f"{GUARD_DIR}/model.json", venv_vars: tuple[str, ...] = (VENV_VAR,),
           only: list[str] | None = None) -> list[tuple[str, bool, str]]:
    """Run the repo's guard.sh as Claude Code would (bash, hook JSON on stdin) on fresh-time transcripts.
    ``venv`` is a venv with rlo installed (it stands for the one install.sh makes). Returns (case, ok, detail)."""
    import shutil

    repo = Path(repo).resolve()
    res = []
    with tempfile.TemporaryDirectory(prefix="ga-rlo-remote-") as tmp:
        tmp = Path(tmp)
        for c in cases():
            if only and c.name not in only:
                continue
            home = tmp / f"home{len(res)}"
            home.mkdir()
            project = repo
            v = str(venv)
            if c.setup in ("no-model", "bad-model", "w1-v1"):
                project = tmp / f"proj{len(res)}"
                shutil.copytree(repo / Path(guard_rel).parent, project / Path(guard_rel).parent)
                m = project / model_rel
                if c.setup == "no-model":
                    m.unlink()
                elif c.setup == "bad-model":
                    m.write_text("{}", encoding="utf-8")
                else:  # the model W1 ran with first (amp 714c00b): no ReadNotifications, the substitutes map kept
                    d = json.loads(m.read_text(encoding="utf-8"))
                    d["specs"] = [x for x in d["specs"] if x["name"] != "ReadNotifications"]
                    m.write_text(json.dumps(d), encoding="utf-8")
            if c.setup == "no-venv":
                v = "/dev/null/ga-rlo-no-venv"
            now = time.time() * 1000
            tpath = tmp / f"t{len(res)}.jsonl"
            transcript([(t, i, ok, now - ago, *rest) for t, i, ok, ago, *rest in c.steps], tpath)
            data = {"session_id": "replay", "transcript_path": str(tpath), "cwd": str(project),
                    "permission_mode": "default", "hook_event_name": "PreToolUse", "tool_name": c.call[0],
                    "tool_input": c.call[1], "tool_use_id": "current"}
            env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}
            env.update(HOME=str(home), CLAUDE_PROJECT_DIR=str(project), **{var: v for var in venv_vars})
            argv = ["bash", str(project / guard_rel)]
            if c.direct:  # rlo.hooks as guard.sh calls it, without the wrapper's own checks
                argv = [str(Path(v) / "bin" / "python"), "-m", "rlo.hooks", "--model", str(project / model_rel),
                        "--mode", "enforce", *[x for g in (GRANTS if c.grants is None else c.grants) for x in ("--grant", g)],
                        "--record", str(home / "rec.jsonl")]
            try:
                p = subprocess.run(argv, input=json.dumps(data) if c.stdin is None else c.stdin,
                                   capture_output=True, text=True, env=env, cwd=str(project), timeout=300)
            except (OSError, subprocess.TimeoutExpired) as e:
                res.append((c.name, False, type(e).__name__))
                continue
            try:
                out = json.loads(p.stdout) if p.stdout.strip() else {}
            except ValueError:
                res.append((c.name, False, f"exit {p.returncode}, stdout is not JSON"))
                continue
            hso = out.get("hookSpecificOutput") or {}
            dec, why = hso.get("permissionDecision"), hso.get("permissionDecisionReason", "")
            first = why.splitlines()[0] if why else ""  # details show the reason's first line, not the react line
            if p.returncode != 0:
                ok, detail = False, f"exit {p.returncode}: Claude Code would let the call through"
            elif dec == "allow" or str(out.get("decision", "")).lower() in ("allow", "approve"):
                ok, detail = False, "the guard said allow (P3)"
            elif c.want == "pass":
                ok, detail = dec is None, "not blocked" if dec is None else f"blocked: {first[:90]}"
            elif c.want in ("fail-closed", "input-error"):
                marks = ("rlo hook input error",) if c.want == "input-error" else ("fail closed", "rlo hook input error")
                ok = dec == "deny" and any(m in why for m in marks)
                detail = first[:90] if dec else "not denied: a fail-closed path lets the call through"
            else:
                ok = dec == "deny" and f"({c.want})" in why
                detail = f"denied {c.want}" if ok else f"not denied by {c.want}: {(first or 'no decision')[:80]}"
                if ok and c.hint:
                    ok = STALE_HINT_MARK in why
                    detail += " with the stale hint" if ok else " but without the stale hint (rlo < 0.5.1?)"
            if ok and c.react is not None:
                got = react_of(why) or {}
                off = {k: got.get(k) for k, v in c.react.items() if got.get(k) != v}
                ok = not off
                shown = ",".join(f"{k}={got.get(k)}" for k in ("kind", "tool", "attempt", "escalate") if k in got)
                detail = f"{detail}; react {shown}" if ok else f"react off: want {c.react}, got {got or 'no react line'}"
            res.append((c.name, ok, detail))
    return res


# ------------------------------------------------------------------------------------------------ static checks

def problems(repo: str | Path) -> list[str]:
    """What in the repo's guard files is off the remote preset (empty = as generated)."""
    repo = Path(repo)
    out: list[str] = []
    for rel in [SETTINGS] + [f"{GUARD_DIR}/{f}" for f in FILES]:
        if not (repo / rel).is_file():
            out.append(f"{rel} is missing")
    if out:
        return out
    try:
        s = json.loads((repo / SETTINGS).read_text(encoding="utf-8"))
        hooks = s.get("hooks") or {}
        start = [h.get("command") for g in hooks.get("SessionStart", []) for h in g.get("hooks", [])]
        pre = [(g.get("matcher"), h.get("command")) for g in hooks.get("PreToolUse", []) for h in g.get("hooks", [])]
    except (ValueError, AttributeError, TypeError):
        return [f"{SETTINGS} cannot be read"]
    if INSTALL_CMD not in start:
        out.append(f"{SETTINGS}: SessionStart does not run ops/rlo/install.sh")
    if ("*", GUARD_CMD) not in pre:
        out.append(f"{SETTINGS}: PreToolUse '*' does not run ops/rlo/guard.sh")
    guard = (repo / GUARD_DIR / "guard.sh").read_text(encoding="utf-8")
    m = re.search(r"-m rlo\.hooks(.*?)--record", guard, re.S)
    args = shlex.split(m.group(1).replace("\\\n", " ")) if m else []
    grants = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--grant"]
    if "--mode" not in args or args[args.index("--mode") + 1] != "enforce":
        out.append("guard.sh: rlo is not in --mode enforce")
    if sorted(grants) != sorted(GRANTS):
        out.append(f"guard.sh: grants {sorted(grants)} are not {sorted(GRANTS)}")
    if "--now-ms" in guard:
        out.append("guard.sh: --now-ms (a clock override) is not allowed")
    pin = pin_of(repo / GUARD_DIR / "install.sh")
    if pin != _pins.PINS["rlo"][3]:
        out.append(f"install.sh: PIN {pin!r} is not the pinned rlo-sdk {_pins.PINS['rlo'][3][:7]} "
                   "(ga rlo upgrade-remote prints how to move it)")
    for f in ("install.sh", "guard.sh"):
        if re.search(r"(^|[;&|`(\s])git\s", (repo / GUARD_DIR / f).read_text(encoding="utf-8"), re.M):
            out.append(f"{f}: runs git (the guard must not change the repo)")
    try:
        mdl = json.loads((repo / GUARD_DIR / "model.json").read_text(encoding="utf-8"))
        out += substitute_problems(mdl)
        preset.load_model(repo / GUARD_DIR / "model.json")
        specs = {s["name"]: s for s in mdl["specs"]}
        for p in PLUMBING:
            got = specs.get(p["name"])
            if got is None:
                out.append(f"model.json: plumbing tool {p['name']} is missing (S2)")
            elif got.get("risk") != p["risk"]:
                out.append(f"model.json: {p['name']} risk {got.get('risk')!r}, not {p['risk']!r}")
    except Exception as e:  # noqa: BLE001
        out.append(f"model.json is not a readable action-model/1 ({type(e).__name__})")
    for f in ("GUARD.md", "PROMPT.md"):
        text = (repo / GUARD_DIR / f).read_text(encoding="utf-8")
        if DENY_REPORT not in text:
            out.append(f"{f}: the deny-report rule ({DENY_REPORT!r}) is missing (GR2 S3)")
        if REACT_RULE not in text:
            out.append(f"{f}: the ReAct rule is missing (GR4 S2)")
    return out


# ------------------------------------------------------------------------------------------------ S4 moving a pin

PIN_LINE = re.compile(r'^PIN="([0-9a-f]{7,40})"$', re.M)


def pin_of(install_sh: Path) -> str | None:
    m = PIN_LINE.search(install_sh.read_text(encoding="utf-8")) if install_sh.is_file() else None
    return m.group(1) if m else None


def upgrade_commands(repo: str | Path, install_rel: str = f"{GUARD_DIR}/install.sh") -> tuple[str | None, list[str]]:
    """(current PIN, commands a human runs to move the guard's install.sh to the pinned rlo-sdk). ga rlo runs none of
    them (BD-196). The venv marker carries the PIN, so the worker's next SessionStart reinstalls."""
    repo = Path(repo).resolve()
    old = pin_of(repo / install_rel)
    new = _pins.PINS["rlo"][3]
    if old is None:
        raise ValueError(f"{repo / install_rel}: no PIN=\"<sha>\" line")
    if old == new:
        return old, []
    q = shlex.quote(str(repo))
    f = shlex.quote(str(repo / install_rel))
    return old, [
        # -i.bak works on GNU and BSD (macOS) sed alike; a bare -i is GNU-only (BD-203)
        f"sed -i.bak 's/^PIN=\"{old}\"$/PIN=\"{new}\"/' {f} && rm {shlex.quote(str(repo / install_rel) + '.bak')}",
        f"grep -n '^PIN=' {f}",
        f"git -C {q} add {shlex.quote(install_rel)}",
        f"git -C {q} commit -m {shlex.quote(f'rlo guard: rlo-sdk 0.5.1 ({new[:7]}), was {old[:7]}')}",
        f"git -C {q} push origin HEAD",
    ]
