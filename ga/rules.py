"""Rule checks R1–R13 (METHOD §5, G4).

Every check returns a list of Problems carrying the rule id and its strength from the config
(hard rules block the action, soft rules only notify; config may raise soft to hard, never lower).
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .config import Config
from .forms import Problem, parse_sections, validate

GATED_ACTIONS = ("pr", "merge_default", "tag", "stage_close")

# Built-in secret patterns. Fake values in tests are built at runtime so this repo never holds one.
SECRET_PATTERNS = [
    r"sk-ant-[A-Za-z0-9_-]{20,}",
    r"sk-[A-Za-z0-9]{32,}",
    r"gh[pousr]_[A-Za-z0-9]{36,}",
    r"github_pat_[A-Za-z0-9_]{40,}",
    r"AKIA[0-9A-Z]{16}",
    r"xox[abposr]-[A-Za-z0-9-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_\-/+]{16,}['\"]",
    # Google (CMD-GA24, BD-254): the only secrets this project holds are GEMINI_API_KEY and the Google OAuth tokens that
    # agy / Gemini CLI sign-in stores
    r"AIza[0-9A-Za-z_-]{35}",                     # Google / Gemini API key
    r"ya29\.[0-9A-Za-z_-]{20,}",                   # OAuth access token
    r"1//[0-9A-Za-z_-]{30,}",                      # OAuth refresh token
    r"GOCSPX-[0-9A-Za-z_-]{20,}",                  # OAuth client secret
    r'"(access_token|refresh_token|client_secret)"\s*:\s*"[^"]{16,}"',  # the same, as JSON fields
]

REQUEST_RE = re.compile(r"요청\s*:\s*([^\s,·:]+)")


def _v(cfg: Config, rule: str, where: str, message: str) -> Problem:
    return Problem(where, message, cfg.strength(rule), rule)


# ----------------------------------------------------------------------------- R1


def r1_post(cfg: Config, channel: str, author: str) -> list[Problem]:
    """By default a session talks to the hub only: only the channel's session and the hub write in a channel.
    A session that had to talk to another one reports it as exchange/1 in its own channel (§3.5)."""
    if author in (channel, cfg.hub_name):
        return []
    return [_v(cfg, "R1", f"channel {channel}", f"{author} wrote in {channel}'s channel: an unreported exchange? "
               "send a Request to the hub, or report the exchange as exchange/1 in your own channel")]


# ----------------------------------------------------------------------------- R1b


def r1b_unclaimed(cfg: Config, repo: str, session: str, sha: str, why: str) -> list[Problem]:
    """An exchange is not an action: every integrated change belongs to a directive (BD-133)."""
    return [_v(cfg, "R1b", f"{repo}@{sha[:7]} ({session})", f"not integrated: {why}; changes need a directive (turn it into a Proposal)")]


def requests_of(body: str) -> list[tuple[str, str]]:
    """``요청: <session> …`` lines of a report body -> [(target, line)]."""
    out = []
    for line in body.splitlines():
        m = REQUEST_RE.search(line)
        if m:
            out.append((m.group(1), line.strip()))
    return out


# ----------------------------------------------------------------------------- R2


def r2_ownership(cfg: Config, repo: str, session: str, changed: Iterable[str]) -> list[Problem]:
    out = []
    for path in changed:
        owner = cfg.owner_of(repo, path)
        if owner is None:
            out.append(_v(cfg, "R2", f"{repo}:{path}", "no row in the ownership table (gate 4)"))
        elif owner != session:
            out.append(_v(cfg, "R2", f"{repo}:{path}", f"owned by {owner}, changed by {session}; send it as a request"))
    return out


# ----------------------------------------------------------------------------- R3


def r3_push(cfg: Config, actor: str, repo: str, branch: str, force: bool = False, delete: bool = False) -> list[Problem]:
    out = []
    if actor == cfg.hub_name:
        allowed = cfg.integration_branch
    else:
        s = cfg.sessions.get(actor)
        if s is None:
            return [_v(cfg, "R3", f"{repo}@{branch}", f"unknown actor {actor}")]
        if repo not in s.repos:
            return [_v(cfg, "R3", f"{repo}@{branch}", f"{actor} has no branch in {repo}")]
        allowed = s.branch_for(repo)
    if branch != allowed:
        out.append(_v(cfg, "R3", f"{repo}@{branch}", f"{actor} may push only to {allowed}"))
    if force:
        out.append(_v(cfg, "R3", f"{repo}@{branch}", "force push / history rewrite"))
    if delete:
        out.append(_v(cfg, "R3", f"{repo}@{branch}", "branch deletion"))
    return out


def r3_integration_moved(cfg: Config, repo: str, recorded: str | None, actual: str | None) -> list[Problem]:
    """Only the hub moves the integration branch. A head the hub did not record means someone else pushed."""
    if recorded and actual and recorded != actual:
        return [_v(cfg, "R3", f"{repo}@{cfg.integration_branch}", f"moved to {actual[:7]} outside the hub (recorded {recorded[:7]})")]
    return []


# ----------------------------------------------------------------------------- R4


def r4_ff(cfg: Config, repo: str, session: str, fast_forward: bool) -> list[Problem]:
    if fast_forward:
        return []
    return [_v(cfg, "R4", f"{repo}@{cfg.sessions[session].branch_for(repo) if session in cfg.sessions else session}",
               f"not a fast-forward of {cfg.integration_branch}; {session} must merge the integration branch first")]


# ----------------------------------------------------------------------------- R5


def r5_action(cfg: Config, action: str | None) -> list[Problem]:
    if action in GATED_ACTIONS:
        return [_v(cfg, "R5", f"action {action}", "PR · default-branch merge · tag · stage close is the user's (gate 1)")]
    return []


# ----------------------------------------------------------------------------- R6


def r6_secrets(cfg: Config, text: str, where: str) -> list[Problem]:
    out = []
    for pat in SECRET_PATTERNS + cfg.secret_patterns:
        for m in re.finditer(pat, text):
            shown = m.group(0)[:6] + "…"
            out.append(_v(cfg, "R6", where, f"looks like a secret ({shown}); integration stops"))
    return out


# ----------------------------------------------------------------------------- R7


def r7_done_when(cfg: Config, directive: dict[str, Any]) -> list[Problem]:
    if any(p.rule == "R7" for p in validate(directive, directive.get("schema", "directive/1"))):
        return [_v(cfg, "R7", directive.get("id", "directive"), "no done_when: what changes on success and how it is checked")]
    return []


# ----------------------------------------------------------------------------- R8


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def r8_duplicate(cfg: Config, directive: dict[str, Any], history: Iterable[dict[str, Any]]) -> list[Problem]:
    """history: [{"directive": directive/1, "status": open|done|failed, "round": n}]"""
    goal = _norm(directive.get("goal", ""))
    out = []
    for h in history:
        d = h["directive"]
        if d.get("id") == directive.get("id") or _norm(d.get("goal", "")) != goal:
            continue
        where = f"{directive.get('id')} vs {d.get('id')}"
        if h["status"] == "open" and d.get("to") != directive.get("to"):
            out.append(_v(cfg, "R8", where, f"same work is in progress at {d.get('to')} (round {h.get('round')})"))
        elif h["status"] in ("done", "failed"):
            out.append(_v(cfg, "R8", where, f"same work already {h['status']} (round {h.get('round')})"))
    return out


# ----------------------------------------------------------------------------- R9


def r9_quiet(cfg: Config, new_items: int, writes: int) -> list[Problem]:
    if new_items == 0 and writes > 0:
        return [_v(cfg, "R9", "tick", f"nothing new but {writes} write(s)")]
    return []


# ----------------------------------------------------------------------------- R10


def r10_wait(cfg: Config, open_work: int, next_choice: str) -> list[Problem]:
    if open_work == 0 and next_choice not in ("wait", "ask_user"):
        return [_v(cfg, "R10", "next", f"nothing left to do but next is {next_choice}; choose wait")]
    return []


# ----------------------------------------------------------------------------- R11


def r11_counts(cfg: Config, repo: str, claimed: dict[str, int] | None, reproduced: dict[str, int] | None) -> list[Problem]:
    if not claimed or not reproduced:
        return []
    diffs = [f"{k} {claimed.get(k, 0)}→{reproduced.get(k, 0)}" for k in ("passed", "failed", "skipped") if claimed.get(k, 0) != reproduced.get(k, 0)]
    if diffs:
        return [_v(cfg, "R11", repo, "report and reproduction differ (recorded the reproduced numbers): " + ", ".join(diffs))]
    return []


# ----------------------------------------------------------------------------- R12


def r12_budget(cfg: Config, directive: dict[str, Any] | None, spent: dict[str, float]) -> list[Problem]:
    """A directive whose budget, on top of what is spent, exceeds a configured limit goes to the user."""
    out = []
    want = (directive or {}).get("budget", {})
    for k, limit in cfg.budget.items():
        used = spent.get(k, 0)
        add = want.get(k, 0)
        if not isinstance(add, (int, float)) or isinstance(add, bool):
            continue  # e.g. a model name in a directive budget: not a limit
        # a limit already reached also stops a turn whose own cost is unknown in advance (e.g. "cost")
        if used + add > limit or (add == 0 and limit > 0 and used >= limit):
            out.append(_v(cfg, "R12", k, f"{used:g} spent + {add:g} asked > limit {limit:g} (gate 6)"))
    return out


# ----------------------------------------------------------------------------- R13


def _scan_shell(line: str) -> list[str]:
    issues = []
    quote = None
    prev = " "
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "\\":
            pass
        elif ch == "#" and prev.isspace() and line[:i].strip():
            issues.append("inline comment (zsh without interactivecomments fails)")
            break
        elif ch in "[?" and not prev.isspace() and prev not in "=":
            issues.append(f"unquoted '{ch}' (zsh glob: no matches found) — quote the argument")
        prev = ch
    if line.rstrip("\n").endswith("\\ "):
        issues.append("space after line continuation")
    return issues


SHELL_LANGS = ("", "sh", "bash", "zsh", "shell", "console")


def _shell_lines(text: str) -> list[str]:
    """Lines inside fenced shell blocks (unlabelled fences count; ```ga heads and other languages do not).
    A text without any fence is taken as commands as a whole."""
    lines, fence, saw_fence = [], None, False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            saw_fence = True
            fence = s[3:].strip().lower() if fence is None else None
            continue
        if fence is not None and fence in SHELL_LANGS:
            lines.append(line)
    return lines if saw_fence else text.splitlines()


def r13_commands(cfg: Config, text: str) -> list[Problem]:
    """Commands given to the user must paste and run. Checks fenced shell blocks (or the whole text if none)."""
    lines = _shell_lines(text)
    out = []
    for n, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        for issue in dict.fromkeys(_scan_shell(line)):
            out.append(_v(cfg, "R13", f"line {n}: {line.strip()[:60]}", issue))
    return out


ALL = {
    "R1": r1_post, "R1b": r1b_unclaimed, "R2": r2_ownership, "R3": r3_push, "R4": r4_ff, "R5": r5_action, "R6": r6_secrets,
    "R7": r7_done_when, "R8": r8_duplicate, "R9": r9_quiet, "R10": r10_wait, "R11": r11_counts,
    "R12": r12_budget, "R13": r13_commands,
}


def report_sections(body: str) -> dict[str, str]:
    return parse_sections(body)
