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
FILES = ("install.sh", "guard.sh", "guard.py", "model.json", "GUARD.md", "PROMPT.md")
INSTALL_CMD = 'bash "$CLAUDE_PROJECT_DIR/ops/rlo/install.sh"'
GUARD_CMD = 'bash "$CLAUDE_PROJECT_DIR/ops/rlo/guard.sh"'
VENV_VAR = "GA_RLO_VENV"
# CMD-GR7 S1: ga.mail.send is the channel command `ga mail send --repo <channel repo>` (a Bash form guard.py pins), named
# as a granted label so rlo's react may offer it (rlo substitutes are labels in the model, not command lines)
GRANTS = ("Bash", "mcp__github__add_issue_comment", "mcp__claude-code-remote__send_message", "ga.mail.send")
# S3: the sentence the worker reads; doctor checks it is there
DENY_REPORT = "Post every guard deny verbatim on your channel"
# CMD-GR4 S2: the in-turn ReAct rule (rlo CMD-K11: a deny ends with '-- react: {json}'); doctor checks it is there
REACT_RULE = ("When a deny ends with '-- react:', do exactly that alternative once. When escalate is true, post the deny "
              "verbatim on your channel and continue other work.")
# CMD-GR7 S2: every deny lands in the outbox as rlo --record lines; the worker sends them out. doctor checks the rule
OUTBOX = ".ga/mailbox/outbox"
OUTBOX_RULE = ("At your next allowed Bash, run 'ga mail send' for each pending guard event in .ga/mailbox/outbox/, "
               "then continue other work.")
# CMD-GR4 S1 / GR7 S1: same-purpose substitutes for the session plumbing (operator-owned, rlo K11 'substitutes').
# Only labels in the model may be named, and an external one only if granted; never a substitute for these:
SUBSTITUTES = {"ReadNotifications": ["ga.mail.read", "mcp__github__issue_read"],
               "mcp__claude-code-remote__send_message": ["ga.mail.send"]}
NO_SUBSTITUTE = ("WebFetch", "Agent", "mcp__claude-code-remote__create_session")
CHANNEL_REMOTE = "origin"
FETCH_EVERY_MIN = 5


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
# CMD-GR7 S1: the ga mailbox commands as labels rlo can name (a react 'use_tool' tool); never tools Claude Code calls.
# guard.py lets the Bash form through only as declared (argument-pinned to the channel repo), even when it fails closed.
MAIL = {"ga.mail.read": "read", "ga.mail.scan": "read", "ga.mail.send": "external"}
MAIL_SPECS = [_spec(n, r, {}, f"Channel command, not a tool: Bash `ga mail {n.rsplit('.', 1)[1]} --repo <channel repo> ...` "
                    "as declared in ops/rlo/GUARD.md.") for n, r in MAIL.items()]
# guard.py keeps these allowed when it fails closed (with the pinned `ga mail` Bash form)
CHANNEL_TOOLS = tuple(s["name"] for s in PLUMBING)
# measured on W1 (amp 714c00b): the optional fields Read and Grep really get, and Glob
MEASURED_PARAMS = {
    "Read": {"offset": _opt("number"), "limit": _opt("number")},
    "Grep": {"glob": _opt("string"), "type": _opt("string"), "output_mode": _opt("string"), "-n": _opt("bool"),
             "-i": _opt("bool"), "-A": _opt("number"), "-B": _opt("number"), "-C": _opt("number"),
             "head_limit": _opt("number"), "multiline": _opt("bool")},
}
MEASURED_SPECS = [_spec("Glob", "read", {"pattern": {"type": "string"}, "path": _opt("string")}, "Find files (read only).")]


def model(name: str = "worker", substitutes: bool = True) -> dict[str, Any]:
    """rlo's start model + W1's measured fields + plumbing (always every PLUMBING tool, GR2 S2) + the ga mail labels
    (GR7 S1) + the SUBSTITUTES map (GR4 S1). rlo >= 0.6.0 (K11) reads 'substitutes' beside action-model/1 and splits it off; install.sh pins it."""
    m = json.loads(preset.packaged_model().read_text(encoding="utf-8"))
    for s in m["specs"]:
        s["params"].update(MEASURED_PARAMS.get(s["name"], {}))
    have = {s["name"] for s in m["specs"]}
    m["specs"] += [json.loads(json.dumps(s)) for s in MEASURED_SPECS + PLUMBING + MAIL_SPECS if s["name"] not in have]
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
# PreToolUse rlo guard for the {name} worker session (enforce). Written by ga rlo init --profile remote.
# The guard is ops/rlo/guard.py (standard library only); this wrapper runs it with python3 and fails closed around it.
# rlo allows by printing nothing; nothing here prints "allow".
set -u
DIR="${{CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}}"
deny() {{
  printf '{{"hookSpecificOutput":{{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"rlo guard (fail closed): %s"}}}}\\n' "$1"
  exit 0
}}
PY="$(command -v python3 || true)"
[ -n "$PY" ] || deny "no python3 to run ops/rlo/guard.py"
OUT="$("$PY" "$DIR/ops/rlo/guard.py")" || deny "ops/rlo/guard.py failed"
case "$OUT" in
  "") ;;
  "{{"*) printf '%s\\n' "$OUT" ;;
  *) deny "guard output is not JSON" ;;
esac
exit 0
"""

GUARD_MD = """# {name} guard (rlo, enforce) -- owned by the hub

The middle verification line (METHOD §4c 7) for the `{name}` remote worker session, written by
`ga rlo init --profile remote` (ga-SDK `ga.rlo`, CMD-GR2 · GR7). A human commits it; no AI session changes another
session's guard.

- `.claude/settings.json`: SessionStart runs `ops/rlo/install.sh`; every tool call (PreToolUse, matcher `*`) runs
  `ops/rlo/guard.sh`, which runs `ops/rlo/guard.py` with python3.
- `install.sh`: installs pinned `rlo-sdk[sensor] @ {sha7}` into `${venv_var}` (default `~/.cache/ga-rlo-venv`) and marks
  the venv with the PIN. `guard.py` runs it again whenever the marker of the current PIN is missing, so a PIN move
  lands at the next tool call, without a new session.
- Model and PIN: {source}
- `guard.py`: runs `python -m rlo.hooks --mode enforce` with the model. Records one line per verdict, and a state line
  naming the source and its sha, in `~/.rlo/{name}.jsonl`.
  - Fail closed. It denies when:
    - python3 is missing, or the hook input is empty or not JSON;
    - the guard ref is unreadable, the model is missing, or install.sh has no PIN;
    - rlo is not installed for the PIN and the install fails;
    - rlo exits nonzero, records no verdict, or prints non-JSON.
  - Even then the channel stays open: {channel_tools}, and the Bash channel commands below, are not denied
    (python3 missing is the one exception).
  - rlo allows by printing nothing. There is no clock override.
- Grants (external actions allowed): {grants}. `ga.mail.send` is the Bash command below, not a tool.
- Session plumbing always in the model: {plumbing}. The channel commands are in it as `ga.mail.read`, `ga.mail.scan`,
  `ga.mail.send`, so a deny's react line can name them.
- Not in the model, so denied with A1: e.g. `WebFetch`, `Agent`, `mcp__claude-code-remote__create_session`.
- rlo does not see Bash command content, file paths or argument values. guard.py reads one thing in a Bash command:
  a `ga mail` command must be written as declared (repo `{channel_repo}`, remote `{remote}`), or it is denied.
- Every deny is saved as rlo record lines in `{outbox}/` (git-ignored), one file per deny.

## For the worker session

{worker_rules}
"""

WORKER_RULES = """- **{react_rule}**
- Channel commands (Bash). A react `tool` of `ga.mail.read`, `ga.mail.send` or `ga.mail.scan` means:
  - `ga.mail.read`: `ga mail read --repo {channel_repo} --as {name}`
  - `ga.mail.send`: `ga mail send --repo {channel_repo} --to {hub} --from {sender} FILE` (FILE holds one ga form)
  - `ga.mail.scan`: `ga mail scan --repo {channel_repo}`
  Write them as shown: plain words, no quotes and no other shell syntax. Written so, they pass even when the guard
  fails closed.
- **{outbox_rule}** For each file F there:
  `ga mail send --repo {channel_repo} --to {hub} --from {sender} --guard-event F --re <the CMD id you work on> && rm -f F`
- If a deny has no `-- react:` line (an older rlo), the alternative is: {deny_report} (the issue your hub reads),
  then go on with other work. Do not ask only in your own chat: the hub does not see it.
- After an idle gap (over about 10 minutes) the first Bash can be denied with D: the guard no longer knows your run's
  health. Make one read-only call (for example `Read` of a file you need) and retry.
- Do not edit `.claude/` or `ops/rlo/`. Changes go through the hub.
"""

PROMPT_MD = """# Guard rules for the {name} session (paste into the session's start prompt)

Every tool call you make goes through the rlo guard (`ops/rlo/GUARD.md`). It can deny a call; it never allows more.

{worker_rules}"""

NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
REF = re.compile(r"^([A-Za-z0-9_.-]{1,40})/([A-Za-z0-9_./-]{1,200})$")
SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]{1,200}$")


def config(name: str = "worker", *, hub: str = "hub", channel_repo: str = ".", guard_ref: str | None = None,
           fetch_every_min: float = FETCH_EVERY_MIN) -> dict[str, Any]:
    """guard.py's CONF: everything that varies between work repos (CMD-GR7)."""
    if not NAME.match(name or ""):
        raise ValueError(f"name {name!r}: letters, digits, _ . - only")
    if not NAME.match(hub or ""):
        raise ValueError(f"hub {hub!r}: letters, digits, _ . - only (a ga mail recipient)")
    if not SAFE_PATH.match(channel_repo or "") or channel_repo.startswith("-"):
        raise ValueError(f"channel repo {channel_repo!r}: a path relative to the work repo (letters, digits, _ . / -)")
    ref = None
    if guard_ref:
        m = REF.match(guard_ref)
        if not m or ".." in guard_ref or guard_ref.endswith("/"):
            raise ValueError(f"guard ref {guard_ref!r}: REMOTE/BRANCH, e.g. origin/main")
        ref = {"remote": m.group(1), "branch": m.group(2)}
    if not 0 < fetch_every_min <= 24 * 60:
        raise ValueError("fetch interval: more than 0 and at most 1440 minutes")
    return {"name": name, "sender": name.replace("-", "_"), "hub": hub, "venv_var": VENV_VAR, "grants": list(GRANTS),
            "guard_dir": GUARD_DIR, "outbox": OUTBOX, "channel_repo": channel_repo, "channel_remote": CHANNEL_REMOTE,
            "channel_tools": list(CHANNEL_TOOLS), "guard_ref": ref, "fetch_every_s": int(fetch_every_min * 60)}


def guard_py(conf: dict[str, Any]) -> str:
    from .guard_py import CONF_LINE, TEMPLATE

    text = json.dumps(conf, sort_keys=True, separators=(",", ":"))
    if "'" in text or "\\" in text:
        raise ValueError("guard config: no quote or backslash")
    return TEMPLATE.replace(CONF_LINE, CONF_LINE.replace("@CONF@", text))


def conf_of(text: str) -> dict[str, Any] | None:
    """The CONF of a guard.py, or None."""
    m = re.search(r"^CONF = json\.loads\(r'(.*)'\)$", text, re.M)
    try:
        conf = json.loads(m.group(1)) if m else None
    except ValueError:
        return None
    return conf if isinstance(conf, dict) else None


def files(name: str = "worker", **kw: Any) -> dict[str, str]:
    """{path in the work repo: content} for the guard files (not the settings). kw: config()."""
    conf = config(name, **kw)
    _, _, url, sha = _pins.PINS["rlo"]
    ref = conf["guard_ref"]
    rules = WORKER_RULES.format(react_rule=REACT_RULE, deny_report=DENY_REPORT, outbox_rule=OUTBOX_RULE,
                                channel_repo=conf["channel_repo"], name=name, hub=conf["hub"], sender=conf["sender"])
    if ref:
        source = (f"the guard ref `{ref['remote']}/{ref['branch']}` (`git show <sha>:ops/rlo/model.json` and "
                  f"`install.sh`), fetched by guard.py itself at most once every {conf['fetch_every_s'] // 60 or 1} "
                  "min; never the working tree. If the ref is unreadable, guard.py fails closed.")
    else:
        source = "this working tree (`ops/rlo/model.json`, `ops/rlo/install.sh`): no guard ref is configured."
    return {
        f"{GUARD_DIR}/install.sh": INSTALL_SH.format(sha=sha, url=url, venv_var=VENV_VAR),
        f"{GUARD_DIR}/guard.sh": GUARD_SH.format(name=name),
        f"{GUARD_DIR}/guard.py": guard_py(conf),
        f"{GUARD_DIR}/model.json": json.dumps(model(name), ensure_ascii=False, indent=1) + "\n",
        f"{GUARD_DIR}/PROMPT.md": PROMPT_MD.format(name=name, worker_rules=rules),
        f"{GUARD_DIR}/GUARD.md": GUARD_MD.format(
            name=name, sha7=sha[:7], venv_var=VENV_VAR, worker_rules=rules, source=source,
            channel_tools=", ".join(f"`{t}`" for t in CHANNEL_TOOLS), channel_repo=conf["channel_repo"],
            remote=CHANNEL_REMOTE, outbox=OUTBOX,
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


def write(repo: str | Path, name: str = "worker", force: bool = False, **kw: Any) -> dict[str, Any]:
    """Write the guard files and the settings into ``repo``. Never runs git (S5). kw: config()."""
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"{repo} is not a directory")
    out = files(name, **kw)
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


def transcript(steps: list[tuple], path: Path, pending: list[tuple] | None = None) -> None:
    """A Claude Code transcript at real times. steps: (tool, input, ok, at unix ms[, result text]). pending: calls sent
    together in one last assistant message, at the last step's time + 1 s, with no result yet: (tool, input, id)."""
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
    if pending:
        add("assistant", (steps[-1][3] if steps else start) + 1000, message={
            "id": "mp", "model": "replay", "role": "assistant", "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "tool_use", "id": i, "name": t, "input": inp} for t, inp, i in pending]})
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
    want: str  # "pass" | "A1" | "A7" | "D" | "fail-closed" | "input-error" | "pin"
    stdin: str | None = None  # raw stdin instead of the hook input
    setup: str = ""  # "no-model" | "bad-model" | "w1-v1" | "no-send-message" | "no-venv" | "no-python"
    direct: bool = False  # call rlo.hooks itself, not guard.sh (rlo 0.5.1 closes on malformed input on its own)
    hint: bool = False  # the deny must carry rlo's stale-only hint (rlo 0.5.1)
    react: dict | None = None  # these fields of the deny's '-- react: {json}' line (rlo 0.6.0, K11; GR4 S3)
    grants: tuple | None = None  # direct calls only: grants other than the preset's
    still: bool = False  # a fail-closed deny must name the channel calls still allowed (GR7 S4)
    pending: tuple | None = None  # a call sent in the same message as this one, with no result yet (unknown health)


def _deny_text(rule: str, cause: str, kind: str, attempt: int, **extra: Any) -> str:
    """What an earlier deny left in the transcript: its reason ends with the react line (rlo counts these)."""
    obj = {"kind": kind, "rule": rule, "cause": cause, "attempt": attempt, "of": 2, "escalate": kind == "report", **extra}
    return f"guard DENY({rule}): earlier deny\n-- react: " + json.dumps(obj, sort_keys=True, separators=(",", ":"))


def mail_calls(conf: dict[str, Any] | None = None) -> dict[str, tuple]:
    """The Bash channel commands as the worker rules spell them, and ones that are not pinned (GR7 S1)."""
    c = conf or config()
    r, ev = c["channel_repo"], f"{OUTBOX}/20260101T000000.000000Z-1-A1.jsonl"
    send = f"ga mail send --repo {r} --to {c['hub']} --from {c['sender']}"
    return {"read": f"ga mail read --repo {r} --as {c['name']}", "scan": f"ga mail scan --repo {r}",
            "send": f"{send} .ga/report.md", "send event": f"{send} --guard-event {ev} --re CMD-X1 && rm -f {ev}",
            "read, another repo": f"ga mail read --repo / --as {c['name']}",
            "read, another remote": f"ga mail read --repo {r} --remote fork --as {c['name']}",
            "read, then more": f"ga mail read --repo {r} --as {c['name']}; ls",
            "send event, rm another file": f"{send} --guard-event {ev} --re CMD-X1 && rm -f README.md"}


PINNED = ("read", "scan", "send", "send event")


K13 = (0, 8)  # rlo-sdk 0.8.0 (CMD-K13, BD-246 option B): rule D denies only on unknown health, not on stale health


def k13(version: str | None) -> bool:
    """Does this rlo-sdk version judge D as K13 does? None: the version ga pins."""
    try:
        return tuple(int(x) for x in (version or _pins.VERSIONS["rlo-sdk"]).split(".")[:2]) >= K13
    except ValueError:
        return True


def rlo_version(venv: str | Path) -> str | None:
    """The rlo-sdk version the replay venv runs (what decides the expected D), or None if it cannot be read."""
    try:
        p = subprocess.run([str(Path(venv) / "bin" / "python"), "-c", "import rlo; print(rlo.__version__)"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    v = p.stdout.strip()
    return v if p.returncode == 0 and re.fullmatch(r"\d+\.\d+(\.\d+)?\S*", v) else None


def cases(conf: dict[str, Any] | None = None, rlo: str | None = None) -> list[Case]:
    """The replay cases. ``rlo``: the rlo-sdk version replayed (None: the pinned one) -- rule D's expectation depends on
    it (K13)."""
    ok = [(BASH[0], BASH[1], True, 5000)]
    report = {"kind": "report", "escalate": True}
    mail = mail_calls(conf)
    out = [Case("Bash first call", [], BASH, "pass"), Case("Bash after Bash", ok, BASH, "pass"),
           Case("Read", ok, READ, "pass")]
    out += [Case(f"plumbing {t}", ok, (t, i), "pass") for t, i in PLUMBING_CALLS]
    # GR7 S1: the channel commands pass rlo as Bash; any other ga mail is denied by the pin
    out += [Case(f"ga mail {k}", ok, ("Bash", {"command": mail[k]}), "pass") for k in PINNED]
    out += [Case(f"ga mail {k}", ok, ("Bash", {"command": v}), "pin",
                 react={"kind": "none", "rule": "mail_pin", "escalate": False})
            for k, v in mail.items() if k not in PINNED]
    # W1 case 4 (and create_session): no substitute -> report, escalate
    out += [Case(f"A1 {t}", ok, (t, i), "A1", react=dict(report, rule="A1", cause="no_substitute")) for t, i in DENY_A1]
    gap = [(BASH[0], BASH[1], True, IDLE_MS)]
    if k13(rlo):  # W1 case 2 under K13 (POL-1 T1): a known health value does not age into D, so idle is not a lockout
        out += [Case("Bash after 2h idle", gap, BASH, "pass")]
    else:  # W1 case 2 before K13: stale-only D -> refresh_read; after one read the call passes
        out += [Case("Bash after 2h idle", gap, BASH, "D", hint=True,
                     react={"kind": "refresh_read", "rule": "D", "cause": "stale", "attempt": 1, "escalate": False})]
    out += [Case("Bash after 2h idle, then Read", gap + [(READ[0], READ[1], True, 2000)], BASH, "pass"),
            # unknown health is D on every rlo: the session's first calls, sent together, have no result yet
            Case("Bash beside a call with no result", [], BASH, "D", pending=READ,
                 react={"kind": "wait_previous", "rule": "D", "cause": "unavailable", "attempt": 1, "escalate": False})]
    # W1 case 1: the W1 v1 model had no ReadNotifications -> A1, use_tool 'ga mail read' (GR7 S1; was issue_read)
    rn = ("ReadNotifications", {})
    use = {"kind": "use_tool", "rule": "A1", "cause": "has_substitute", "tool": "ga.mail.read"}
    out += [Case("W1 v1 model: ReadNotifications", ok, rn, "A1", setup="w1-v1",
                 react=dict(use, attempt=1, escalate=False))]
    # the retry cap: the same deny once before -> attempt 2; twice before -> the 3rd escalates (report)
    before = [(rn[0], rn[1], False, 4000 - 1000 * k, _deny_text("A1", "has_substitute", "use_tool", k + 1,
                                                               tool="ga.mail.read")) for k in range(2)]
    out += [Case("W1 v1 model: ReadNotifications, denied once before", ok + before[:1], rn, "A1", setup="w1-v1",
                 react=dict(use, attempt=2, escalate=False)),
            Case("W1 v1 model: ReadNotifications, denied twice before", ok + before, rn, "A1", setup="w1-v1",
                 react=dict(report, rule="A1", cause="has_substitute", attempt=3))]
    # W1 case 3: an external tool without its grant -> A7, report, escalate (rlo itself with one grant fewer)
    sm = ("mcp__claude-code-remote__send_message", {"session_id": "session_x", "message": "{}"})
    out += [Case("A7 send_message without its grant", ok, sm, "A7", direct=True,
                 grants=tuple(g for g in GRANTS if g != sm[0]), react=dict(report, rule="A7", cause="not_granted")),
            # GR7 S1: send_message missing from a model -> A1, use_tool 'ga mail send'
            Case("model without send_message: send_message", ok, sm, "A1", setup="no-send-message",
                 react={"kind": "use_tool", "rule": "A1", "cause": "has_substitute", "tool": "ga.mail.send",
                        "attempt": 1, "escalate": False})]
    closed = dict(report, rule="closed")
    out += [Case("empty stdin", [], BASH, "fail-closed", stdin=""),
            Case("garbage stdin", [], BASH, "fail-closed", stdin="not json"),
            Case("model missing", ok, BASH, "fail-closed", setup="no-model", still=True,
                 react=dict(closed, cause="no_model")),
            Case("model broken", ok, BASH, "fail-closed", setup="bad-model", still=True,
                 react=dict(closed, cause="no_verdict")),
            Case("no venv, install fails", ok, BASH, "fail-closed", setup="no-venv", still=True,
                 react=dict(closed, cause="install_failed")),
            Case("no python3", ok, BASH, "fail-closed", setup="no-python"),
            Case("rlo itself: empty stdin", [], BASH, "input-error", stdin="", direct=True,
                 react=dict(report, rule="input", cause="malformed_input")),
            Case("rlo itself: garbage stdin", [], BASH, "input-error", stdin="not json", direct=True,
                 react=dict(report, rule="input", cause="malformed_input"))]
    # GR7 S4/S5: failing closed keeps the channel open -- the plumbing and the pinned ga mail, nothing else
    for setup, why in (("no-venv", "install fails"), ("bad-model", "model broken")):
        out += [Case(f"{why}: channel {t}", ok, (t, i), "pass", setup=setup) for t, i in PLUMBING_CALLS]
        out += [Case(f"{why}: ga mail {k}", ok, ("Bash", {"command": mail[k]}), "pass", setup=setup) for k in PINNED]
        out += [Case(f"{why}: ga mail read, another repo", ok, ("Bash", {"command": mail["read, another repo"]}), "pin",
                     setup=setup),
                Case(f"{why}: WebFetch", ok, DENY_A1[0], "fail-closed", setup=setup, still=True)]
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


def event_problems(path: Path, sender: str = "worker") -> list[str]:
    """A guard event file as `ga mail send --guard-event` turns it into a form: it must hold a deny, and the form must
    validate as report/2 and pass the mailbox's secret check (CMD-GR7 D1)."""
    from ..forms import FormError, hard, parse_text, validate
    from ..hub import guard_summary
    from ..mailbox import guard_report, secrets_in

    lines = path.read_text(encoding="utf-8").splitlines()
    if guard_summary(lines)["deny"] < 1:
        return [f"{path.name}: no deny in the event"]
    text = guard_report(lines, sender=sender, directive="CMD-X1", rev=1, items=["D1"])
    try:
        head, _ = parse_text(text)
        probs = [str(p) for p in hard(validate(head))]
    except FormError as e:
        head, probs = {}, [str(p) for p in e.problems]
    if head.get("schema") != "report/2":
        probs.append(f"schema {head.get('schema')!r}, not report/2")
    return probs + [f"looks like a secret: {x}" for x in secrets_in(text)]


def _shim(tmp: Path, venv: str, pin: str | None) -> str:
    """A venv that stands for the one install.sh makes: its python is ``venv``'s, its marker is the PIN's."""
    v = tmp / "venv"
    (v / "bin").mkdir(parents=True)
    py = v / "bin" / "python"
    py.write_text(f"#!/bin/sh\nexec {shlex.quote(str(Path(venv) / 'bin' / 'python'))} \"$@\"\n", encoding="utf-8")
    py.chmod(0o755)
    if pin:
        (v / f".pinned-{pin}").write_text("", encoding="utf-8")
    return str(v)


def _project(repo: Path, dest: Path, guard_rel: str) -> Path:
    """A copy of the guard dir to replay in (the outbox and models are written there, not in the work repo). A guard
    ref is replayed from this copy's working tree (doctor runs no git; tests/test_rlo/test_gr7.py covers the ref)."""
    import shutil

    gdir = Path(guard_rel).parent
    shutil.copytree(repo / gdir, dest / gdir)
    gp = dest / gdir / "guard.py"
    if gp.is_file():
        text = gp.read_text(encoding="utf-8")
        conf = conf_of(text)
        if conf and conf.get("guard_ref"):
            gp.write_text(text.replace(f"r'{json.dumps(conf, sort_keys=True, separators=(',', ':'))}'",
                                       f"r'{json.dumps(dict(conf, guard_ref=None), sort_keys=True, separators=(',', ':'))}'"),
                          encoding="utf-8")
    return dest


def replay(repo: str | Path, venv: str | Path, *, guard_rel: str = f"{GUARD_DIR}/guard.sh",
           model_rel: str = f"{GUARD_DIR}/model.json", venv_vars: tuple[str, ...] = (VENV_VAR,),
           only: list[str] | None = None) -> list[tuple[str, bool, str]]:
    """Run the repo's guard.sh as Claude Code would (bash, hook JSON on stdin) on fresh-time transcripts, each in a
    copy of the guard dir. ``venv`` is a venv with rlo installed (a shim with the PIN's marker stands for the one
    install.sh makes). Every deny must leave an outbox event that `ga mail send` accepts. Returns (case, ok, detail)."""
    import shutil

    repo = Path(repo).resolve()
    gp = repo / Path(guard_rel).parent / "guard.py"
    conf = conf_of(gp.read_text(encoding="utf-8")) if gp.is_file() else None
    version = rlo_version(venv)
    bash = shutil.which("bash") or "/bin/bash"
    res = []
    with tempfile.TemporaryDirectory(prefix="ga-rlo-remote-") as tmp:
        tmp = Path(tmp)
        shim = _shim(tmp, str(venv), pin_of(repo / Path(guard_rel).parent / "install.sh"))
        for c in cases(conf, version):
            if only and c.name not in only:
                continue
            home = tmp / f"home{len(res)}"
            home.mkdir()
            project = _project(repo, tmp / f"proj{len(res)}", guard_rel)
            v = shim
            m = project / model_rel
            if c.setup == "no-model":
                m.unlink()
            elif c.setup == "bad-model":
                m.write_text("{}", encoding="utf-8")
            elif c.setup in ("w1-v1", "no-send-message"):
                # w1-v1: the model W1 ran with first (amp 714c00b): no ReadNotifications, the substitutes map kept
                gone = "ReadNotifications" if c.setup == "w1-v1" else "mcp__claude-code-remote__send_message"
                d = json.loads(m.read_text(encoding="utf-8"))
                d["specs"] = [x for x in d["specs"] if x["name"] != gone]
                m.write_text(json.dumps(d), encoding="utf-8")
            if c.setup == "no-venv":
                v = "/dev/null/ga-rlo-no-venv"
            now = time.time() * 1000
            tpath = tmp / f"t{len(res)}.jsonl"
            transcript([(t, i, ok, now - ago, *rest) for t, i, ok, ago, *rest in c.steps], tpath,
                       pending=[(*c.pending, "earlier"), (*c.call, "current")] if c.pending else None)
            data = {"session_id": "replay", "transcript_path": str(tpath), "cwd": str(project),
                    "permission_mode": "default", "hook_event_name": "PreToolUse", "tool_name": c.call[0],
                    "tool_input": c.call[1], "tool_use_id": "current"}
            env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}
            env.update(HOME=str(home), CLAUDE_PROJECT_DIR=str(project), **{var: v for var in venv_vars})
            if c.setup == "no-python":
                (tmp / "empty-bin").mkdir(exist_ok=True)
                env["PATH"] = str(tmp / "empty-bin")
            argv = [bash, str(project / guard_rel)]
            if c.direct:  # rlo.hooks as guard.py calls it, without the guard's own checks
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
                ok = dec == "deny" and any(mk in why for mk in marks)
                detail = first[:90] if dec else "not denied: a fail-closed path lets the call through"
                if ok and c.still:
                    ok = "Still allowed: " in why and all(t in why for t in CHANNEL_TOOLS) and "ga mail" in why
                    detail += "; names the channel" if ok else "; does not name the channel calls still allowed"
            elif c.want == "pin":
                ok = dec == "deny" and "rlo guard (channel pin)" in why
                detail = "denied: ga mail not as declared" if ok else f"not denied by the pin: {(first or 'no decision')[:80]}"
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
            if ok and dec == "deny" and not c.direct and c.setup != "no-python":  # GR7 S2: the deny is in the outbox
                events = sorted((project / OUTBOX).glob("*.jsonl"))
                probs = [x for e in events for x in event_problems(e, (conf or {}).get("sender", "worker"))]
                ok = len(events) == 1 and not probs
                detail += ("; outbox event is a valid report/2" if ok else
                           f"; outbox: {len(events)} event(s){', ' + probs[0][:80] if probs else ''}")
            res.append((c.name, ok, detail))
    return res


# ------------------------------------------------------------------------------------------------ static checks

GIT_OK = ("fetch", "rev-parse", "show")  # guard.py reads the guard ref (GR7 S5); it never changes the repo


def guard_problems(repo: Path) -> list[str]:
    """guard.sh is the fixed wrapper; guard.py is the fixed code with a CONF of the preset's grants (GR7)."""
    out: list[str] = []
    text = (repo / GUARD_DIR / "guard.py").read_text(encoding="utf-8")
    conf = conf_of(text)
    sh = (repo / GUARD_DIR / "guard.sh").read_text(encoding="utf-8")
    if sh != GUARD_SH.format(name=(conf or {}).get("name", "worker")):
        out.append("guard.sh differs from the generated wrapper (it only runs ops/rlo/guard.py and fails closed)")
    if conf is None:
        return out + ["guard.py: no readable CONF line"]
    if "--now-ms" in text:
        out.append("guard.py: --now-ms (a clock override) is not allowed")
    if "--health-ttl" in text:
        out.append("guard.py: --health-ttl asks rlo for the pre-K13 rule D (stale health denies); K13's D is intended")
    if '"--mode", "enforce"' not in text:
        out.append("guard.py: rlo is not in --mode enforce")
    for sub in re.findall(r"""\bgit\(\s*["']([A-Za-z-]+)""", text):
        if sub not in GIT_OK:
            out.append(f"guard.py: runs git {sub} (the guard only reads its ref: {', '.join(GIT_OK)})")
    if sorted(conf.get("grants") or []) != sorted(GRANTS):
        out.append(f"guard.py: grants {sorted(conf.get('grants') or [])} are not {sorted(GRANTS)}")
    if conf.get("outbox") != OUTBOX:
        out.append(f"guard.py: the outbox is {conf.get('outbox')!r}, not {OUTBOX!r} (GR7 S2)")
    if sorted(conf.get("channel_tools") or []) != sorted(CHANNEL_TOOLS):
        out.append(f"guard.py: channel tools {conf.get('channel_tools')} are not the plumbing {list(CHANNEL_TOOLS)}")
    try:
        ref = conf.get("guard_ref")
        want = config(conf["name"], hub=conf["hub"], channel_repo=conf["channel_repo"],
                      guard_ref=f"{ref['remote']}/{ref['branch']}" if ref else None,
                      fetch_every_min=conf["fetch_every_s"] / 60)
    except (KeyError, TypeError, ValueError) as e:
        return out + [f"guard.py: CONF is off the preset ({str(e)[:80]})"]
    if conf != want:
        off = sorted(k for k in set(conf) | set(want) if conf.get(k) != want.get(k))
        out.append(f"guard.py: CONF differs from the preset in {', '.join(off)}")
    elif text != guard_py(want):
        out.append("guard.py differs from the generated guard (only its CONF line may vary)")
    return out


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
    out += guard_problems(repo)
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
        subs = mdl.get("substitutes") if isinstance(mdl.get("substitutes"), dict) else {}
        for tool, alt in (("ReadNotifications", "ga.mail.read"), ("mcp__claude-code-remote__send_message", "ga.mail.send")):
            if (subs.get(tool) or [None])[0] != alt:
                out.append(f"model.json: the first substitute for {tool} is not {alt} (GR7 S1)")
        preset.load_model(repo / GUARD_DIR / "model.json")
        specs = {s["name"]: s for s in mdl["specs"]}
        for p in PLUMBING + MAIL_SPECS:
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
        if OUTBOX_RULE not in text:
            out.append(f"{f}: the outbox rule is missing (GR7 S2)")
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
    version = _pins.VERSIONS["rlo-sdk"]
    return old, [
        # -i.bak works on GNU and BSD (macOS) sed alike; a bare -i is GNU-only (BD-203)
        f"sed -i.bak 's/^PIN=\"{old}\"$/PIN=\"{new}\"/' {f} && rm {shlex.quote(str(repo / install_rel) + '.bak')}",
        f"grep -n '^PIN=' {f}",
        f"git -C {q} add {shlex.quote(install_rel)}",
        f"git -C {q} commit -m {shlex.quote(f'rlo guard: rlo-sdk {version} ({new[:7]}), was {old[:7]}')}",
        f"git -C {q} push origin HEAD",
    ]


REC_LINE = re.compile(r'^REC="\$HOME/\.rlo/([A-Za-z0-9_.-]{1,40})\.jsonl"$', re.M)


def mailbox_move(repo: str | Path, *, name: str | None = None, hub: str = "hub", channel_repo: str = ".",
                 guard_ref: str | None = None) -> tuple[str, list[str]]:
    """CMD-GR7 S3: (why, commands) that move an existing remote guard to the GR7 guard -- ga mailbox channel, outbox,
    PIN-marker reinstall, guard ref. A person runs them (HUMAN_QUEUE); ga rlo runs none (BD-196) and pushes nothing to
    a guarded repo. ("", []) when the guard already is a GR7 guard as generated. Raises ValueError on bad options."""
    repo = Path(repo).resolve()
    gdir = repo / GUARD_DIR
    gp, gs = gdir / "guard.py", gdir / "guard.sh"
    conf = conf_of(gp.read_text(encoding="utf-8")) if gp.is_file() else None
    if conf is not None and gs.is_file() and not guard_problems(repo):
        return "", []
    if name is None:
        m = REC_LINE.search(gs.read_text(encoding="utf-8")) if gs.is_file() else None
        name = (conf or {}).get("name") or (m.group(1) if m else "worker")
    files(name, hub=hub, channel_repo=channel_repo, guard_ref=guard_ref)  # the options are checked before printing
    why = ("no ops/rlo/guard.py: a guard from before CMD-GR7. Failing closed, it cuts the session off from its channel, "
           "and its denies stay in the session" if conf is None else "ops/rlo/guard.py differs from the generated one")
    q = shlex.quote(str(repo))
    init = (f"ga rlo init --profile remote --work-repo {q} --name {shlex.quote(name)} --hub {shlex.quote(hub)}"
            f" --channel-repo {shlex.quote(channel_repo)}" + (f" --guard-ref {shlex.quote(guard_ref)}" if guard_ref else "")
            + " --force")
    stale = sorted(p.relative_to(repo).as_posix() for p in gdir.iterdir() if p.is_file() and p.name not in FILES) \
        if gdir.is_dir() else []
    remote_, branch = (guard_ref.split("/", 1) if guard_ref else ("origin", None))
    msg = f"rlo guard: ga mailbox channel, outbox, PIN marker (ga rlo, CMD-GR7) for {name}"
    return why, ([init, f"ga rlo doctor --profile remote --work-repo {q}"]
                 + ([f"git -C {q} rm -q {' '.join(shlex.quote(x) for x in stale)}"] if stale else [])
                 + [f"git -C {q} add {SETTINGS} {GUARD_DIR}", f"git -C {q} commit -m {shlex.quote(msg)}",
                    f"git -C {q} push {shlex.quote(remote_)} " + (f"HEAD:refs/heads/{shlex.quote(branch)}" if branch else "HEAD")])
