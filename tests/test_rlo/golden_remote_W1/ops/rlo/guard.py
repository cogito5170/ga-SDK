#!/usr/bin/env python3
"""PreToolUse rlo guard for a remote worker session (enforce). Written by `ga rlo init --profile remote` (CMD-GR7).
Owned by the hub: the worker session must not edit it. guard.sh runs it with python3; standard library only.

- Model and rlo PIN come from the guard ref when CONF has one (fetched here, outside rlo's judgement, at most once per
  CONF["fetch_every_s"]); from this working tree only when no ref is configured. A state line in the record names the
  source and the sha.
- rlo is installed again when the venv lacks the marker of the current PIN, so a PIN move lands at the next call.
- Fail closed: any problem denies, except the declared channel calls (CONF["channel_tools"] and Bash
  `ga mail send|read|scan --repo <channel repo>` as declared), so the session is never cut off from its channel.
- Every deny is written to CONF["outbox"] as rlo --record lines, one file per deny, for `ga mail send --guard-event`.
- rlo allows by printing nothing; this file never prints "allow". No clock override.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

CONF = json.loads(r'{"channel_remote":"origin","channel_repo":".","channel_tools":["ToolSearch","ReadNotifications","mcp__github__issue_read","mcp__github__add_issue_comment","mcp__claude-code-remote__send_message"],"fetch_every_s":300,"grants":["Bash","mcp__github__add_issue_comment","mcp__claude-code-remote__send_message","ga.mail.send"],"guard_dir":"ops/rlo","guard_ref":null,"hub":"hub","name":"W1","outbox":".ga/mailbox/outbox","sender":"W1","venv_var":"GA_RLO_VENV"}')

HOME = os.path.expanduser("~")
DIR = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE = os.path.join(HOME, ".rlo")
REC = os.path.join(STATE, CONF["name"] + ".jsonl")
VENV = os.environ.get(CONF["venv_var"]) or os.path.join(HOME, ".cache", "ga-rlo-venv")
LABEL = re.compile(r"^[A-Za-z0-9_.:+-]{1,40}$")
TOKEN = re.compile(r"^[A-Za-z0-9_./:=,@+-]+$")
PIN_LINE = re.compile(r'^PIN="([0-9a-f]{7,40})"$', re.M)
# `ga mail` where a command starts (not inside an argument such as `grep "ga mail"`)
MAIL_WORD = re.compile(r"(^|[;&|(`]|\$\()\s*ga\s+mail(\s|$)")
FLAGS = {"send": ("--to", "--from", "--guard-event", "--re", "--rev", "--items"), "read": ("--as", "--json"),
         "scan": ("--json",)}
SWITCHES = ("--json",)
EVENT = re.compile(r"^[A-Za-z0-9_.-]{1,80}\.jsonl$")


class Closed(Exception):
    def __init__(self, cause, detail):
        Exception.__init__(self, detail)
        self.cause, self.detail = cause, detail


def real(base, path):
    return os.path.realpath(os.path.join(base, path))


def in_outbox(cwd, path):
    p = real(cwd, path)
    return os.path.dirname(p) == real(DIR, CONF["outbox"]) and bool(EVENT.match(os.path.basename(p)))


def mail(command, cwd):
    """None: no `ga mail` command. "pinned": a declared channel command. "unpinned": any other use of ga mail."""
    if not MAIL_WORD.search(command):
        return None
    parts = command.strip().split(" && ")
    if "\n" in command or len(parts) > 2 or any(not TOKEN.match(t) for p in parts for t in p.split()):
        return "unpinned"
    words = parts[0].split()
    if len(words) < 3 or words[:2] != ["ga", "mail"] or words[2] not in FLAGS:
        return "unpinned"
    sub, args, seen, files = words[2], words[3:], {}, []
    i = 0
    while i < len(args):
        a = args[i]
        if a in SWITCHES and a in FLAGS[sub] and a not in seen:
            seen[a], i = True, i + 1
        elif (a in ("--repo", "--remote") or a in FLAGS[sub]) and a not in seen and a not in SWITCHES:
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                return "unpinned"
            seen[a], i = args[i + 1], i + 2
        elif sub == "send" and not a.startswith("-") and not files:
            files, i = [a], i + 1
        else:
            return "unpinned"
    if "--repo" not in seen or real(cwd, seen["--repo"]) != real(DIR, CONF["channel_repo"]):
        return "unpinned"
    if seen.get("--remote", CONF["channel_remote"]) != CONF["channel_remote"]:
        return "unpinned"
    event = seen.get("--guard-event")
    if event is not None and not in_outbox(cwd, event):
        return "unpinned"
    if len(parts) == 2 and (event is None or parts[1].split() != ["rm", "-f", event]):
        return "unpinned"
    return "pinned"


def append(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("".join(x + "\n" for x in lines))


def read_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def outbox(lines, label):
    """One guard event file: rlo --record lines, as `ga mail send --guard-event FILE` reads them."""
    try:
        d = os.path.join(DIR, CONF["outbox"])
        os.makedirs(d, exist_ok=True)
        ignore = os.path.join(d, ".gitignore")
        if not os.path.exists(ignore):
            with open(ignore, "w", encoding="utf-8") as f:
                f.write("*\n")
        t = time.time()
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(t)) + ".%06dZ" % (int(t * 1e6) % 1000000)
        name = "%s-%d-%s.jsonl" % (stamp, os.getpid(), re.sub(r"[^A-Za-z0-9_.-]", "_", label)[:30])
        tmp = os.path.join(d, "." + name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("".join(x + "\n" for x in lines))
        os.replace(tmp, os.path.join(d, name))
    except OSError:
        pass  # the deny stands without its event


def git(*args):
    return subprocess.run(["git", "-C", DIR] + list(args), capture_output=True, timeout=120,
                          env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))


def fetch(ref):
    """Fetch the guard ref's branch, at most once per CONF["fetch_every_s"] (the stamp is written first)."""
    stamp = os.path.join(STATE, CONF["name"] + ".fetch")
    now = time.time()
    try:
        with open(stamp, encoding="utf-8") as f:
            last = float(f.read().strip() or 0)
    except (OSError, ValueError):
        last = 0.0
    if 0 <= now - last < CONF["fetch_every_s"]:
        return
    os.makedirs(STATE, exist_ok=True)
    with open(stamp, "w", encoding="utf-8") as f:
        f.write("%.3f\n" % now)
    try:
        git("fetch", "-q", "--no-tags", ref["remote"],
            "+refs/heads/%s:refs/remotes/%s/%s" % (ref["branch"], ref["remote"], ref["branch"]))
    except (OSError, subprocess.TimeoutExpired):
        pass  # reading the ref decides


def source():
    """(model path, install.sh path, PIN, state labels) from the guard ref, or from the working tree without one."""
    ref, gd = CONF["guard_ref"], CONF["guard_dir"]
    if not ref:
        model, install = os.path.join(DIR, gd, "model.json"), os.path.join(DIR, gd, "install.sh")
        try:
            with open(model, "rb") as f:
                data = f.read()
        except OSError:
            raise Closed("no_model", "model file missing")
        labels = {"guard_source": "worktree", "guard_sha": hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()}
    else:
        fetch(ref)
        name = "refs/remotes/%s/%s" % (ref["remote"], ref["branch"])
        unreadable = Closed("ref_unreadable", "guard ref %s/%s unreadable" % (ref["remote"], ref["branch"]))
        try:
            p = git("rev-parse", "-q", "--verify", name + "^{commit}")
            sha = p.stdout.decode("ascii", "replace").strip()
            if p.returncode or not re.match(r"^[0-9a-f]{40}$", sha):
                raise unreadable
            model = os.path.join(STATE, "%s.ref-%s.model.json" % (CONF["name"], sha))
            install = os.path.join(STATE, "%s.ref-%s.install.sh" % (CONF["name"], sha))
            for path, rel in ((model, "model.json"), (install, "install.sh")):
                if not os.path.isfile(path):
                    p = git("show", "%s:%s/%s" % (sha, gd, rel))
                    if p.returncode:
                        raise unreadable
                    os.makedirs(STATE, exist_ok=True)
                    with open(path + ".tmp", "wb") as f:
                        f.write(p.stdout)
                    os.replace(path + ".tmp", path)
        except (OSError, subprocess.TimeoutExpired):
            raise unreadable
        labels = {"guard_source": "ref", "guard_ref": re.sub(r"[^A-Za-z0-9_.:+-]", ".", "%s/%s" % (
            ref["remote"], ref["branch"]))[:40], "guard_sha": sha}
    try:
        with open(install, encoding="utf-8") as f:
            pin = PIN_LINE.search(f.read()).group(1)
    except (OSError, AttributeError):
        raise Closed("no_pin", "install.sh has no PIN line")
    return model, install, pin, labels


def ensure(install, pin):
    """Install rlo when the venv lacks the marker of this PIN (not only when the venv is missing)."""
    marker = os.path.join(VENV, ".pinned-" + pin)
    if os.path.isfile(marker) and os.access(os.path.join(VENV, "bin", "python"), os.X_OK):
        return
    try:
        rc = subprocess.run(["bash", install], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900,
                            env=dict(os.environ, **{CONF["venv_var"]: VENV})).returncode
    except (OSError, subprocess.TimeoutExpired):
        rc = -1
    if rc or not os.path.isfile(marker):
        raise Closed("install_failed", "rlo %s not installed and install failed" % pin[:7])


def tool_use_id(line):
    try:
        return json.loads(line).get("tool_use_id")
    except (ValueError, AttributeError):
        return None


def judge(raw, data, model, labels):
    """rlo's verdict on this call (its output, "" = allow); a deny also goes to the outbox."""
    argv = [os.path.join(VENV, "bin", "python"), "-m", "rlo.hooks", "--model", model, "--mode", "enforce"]
    for g in CONF["grants"]:
        argv += ["--grant", g]
    argv += ["--record", REC]
    os.makedirs(STATE, exist_ok=True)
    before = len(read_lines(REC))
    try:
        p = subprocess.run(argv, input=raw.encode("utf-8"), capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        raise Closed("rlo_error", "rlo did not run")
    if p.returncode:
        raise Closed("rlo_exit", "rlo exited %d" % p.returncode)
    new = read_lines(REC)[before:]
    if not new:  # rlo writes one record line per verdict: none means no verdict was made
        raise Closed("no_verdict", "rlo recorded no verdict")
    out = p.stdout.decode("utf-8", "replace").strip()
    obj = {}
    if out:
        try:
            obj = json.loads(out)
        except ValueError:
            raise Closed("not_json", "rlo output is not JSON")
    state = json.dumps({"kind": "state", "labels": labels}, sort_keys=True)
    append(REC, [state])
    hso = obj.get("hookSpecificOutput") if isinstance(obj, dict) else None
    if isinstance(hso, dict) and hso.get("permissionDecision") == "deny":
        mine = [x for x in new if tool_use_id(x) in (data.get("tool_use_id"), None)]  # parallel calls: their own lines
        rule = None
        for x in mine:
            try:
                rule = json.loads(x)["result"]["rule"]
            except (ValueError, KeyError, TypeError):
                pass
        outbox(mine + [state], str(rule or "deny"))
    return out


def closed(data, rule, detail, react, prefix):
    """A deny made here, not by rlo: printed, recorded in rlo's line shape, and put in the outbox."""
    tool, inp = data.get("tool_name"), data.get("tool_input")
    line = json.dumps({"kind": "guard", "tool_use_id": data.get("tool_use_id") if isinstance(
        data.get("tool_use_id"), str) else None, "tool_name": tool if isinstance(tool, str) and LABEL.match(tool) else None,
        "tool_input_keys": sorted(k for k in inp if isinstance(k, str) and LABEL.match(k)) if isinstance(inp, dict) else [],
        "result": {"verdict": "DENY", "rule": rule, "reasons": [detail]}, "react": react}, sort_keys=True)
    try:
        append(REC, [line])
    except OSError:
        pass
    outbox([line], rule)
    still = ", ".join(CONF["channel_tools"])
    why = "rlo guard (%s): %s. Still allowed: %s; Bash `ga mail send|read|scan --repo %s` as declared in %s/GUARD.md" % (
        prefix, detail, still, CONF["channel_repo"], CONF["guard_dir"])
    why += "\n-- react: " + json.dumps(react, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                        "permissionDecisionReason": why}}) + "\n")
    return 0


def fail_closed(data, cause, detail):
    react = {"kind": "report", "rule": "closed", "cause": cause, "attempt": 1, "of": 2, "escalate": True}
    return closed(data, "closed:" + cause, detail, react, "fail closed")


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return fail_closed({}, "bad_input", "empty hook input" if not raw.strip() else "hook input is not a JSON object")
    tool, inp = data.get("tool_name"), data.get("tool_input")
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else DIR
    chan = False
    try:
        cmd = inp.get("command") if tool == "Bash" and isinstance(inp, dict) else None
        m = mail(cmd, cwd) if isinstance(cmd, str) else None
        if m == "unpinned":
            react = {"kind": "none", "rule": "mail_pin", "cause": "unpinned", "attempt": 1, "of": 2, "escalate": False}
            return closed(data, "mail_pin", "a ga mail command must be written as declared (--repo %s, remote %s)" % (
                CONF["channel_repo"], CONF["channel_remote"]), react, "channel pin")
        chan = tool in CONF["channel_tools"] or m == "pinned"
        model, install, pin, labels = source()
        ensure(install, pin)
        out = judge(raw, data, model, labels)
    except Exception as e:  # noqa: BLE001 -- closed, but a declared channel call stays allowed
        cause, detail = (e.cause, e.detail) if isinstance(e, Closed) else ("guard_error", "guard error " + type(e).__name__)
        if chan:
            try:
                append(REC, [json.dumps({"kind": "channel", "tool_use_id": data.get("tool_use_id"), "cause": cause})])
            except Exception:  # noqa: BLE001
                pass
            return 0
        return fail_closed(data, cause, detail)
    if out:
        sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
