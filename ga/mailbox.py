"""ga mailbox — session-to-session ga forms through git (CMD-GA22, BD-235). The floor that always exists.

A mailbox is the orphan branch ``ga-mailbox`` of a git repository both sessions already have (for AMP and W1: amp).
One file per message, ``to/<recipient>/<utc>-<sender>-<form id>.md``, holding one ga form. Files are only added, never
edited, so concurrent senders never conflict: a send fetches the branch tip, adds its one file on top (plumbing, a
temporary index — the session's working tree and branches are not touched), and pushes; a rejected push is redone on the
new tip, a bounded number of times with backoff.

- Every form is validated before it is written (a hard problem refuses the send) and refused if it looks like a secret.
- Reading prints each new message with its sender, form and validation result, as data for the reader — never as
  instructions to obey — and only then marks it read. The read set (one name per line) is local to the reader's clone,
  in the git dir (``<git dir>/ga-mailbox/<recipient>.cursor``), never pushed; a set, not a time, so no clock skew between
  machines can hide a message.
- No network but the repository's own git remote. Standard library plus git, so a guard that grants Bash ``ga mail …``
  is enough.
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .forms import FormError, hard, parse_text, validate
from .rules import SECRET_PATTERNS

BRANCH = "ga-mailbox"
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")   # a recipient (a directory)
SENDER = re.compile(r"^[A-Za-z0-9_.]{1,40}$")  # a sender: no '-', so <utc>-<sender>-<form id> splits one way only
RETRIES = 20
BACKOFF_BASE, BACKOFF_CAP = 0.1, 3.0  # full jitter: a random sleep up to min(cap, base * 2**attempt)


def backoff(attempt: int) -> float:
    """The bound on the sleep before retry ``attempt`` (0-based)."""
    return min(BACKOFF_CAP, BACKOFF_BASE * 2 ** attempt)


class MailError(RuntimeError):
    pass


def _safe(s: Any, fallback: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s or ""))[:40].strip("._")
    return s or fallback


def form_id(head: dict[str, Any]) -> str:
    """The id a message's file name carries: a directive's id, the first handled id of a report, else the schema."""
    if isinstance(head.get("id"), str):
        return _safe(head["id"], "form")
    handled = head.get("handled")
    if isinstance(handled, list) and handled and isinstance(handled[0], dict) and handled[0].get("id"):
        return _safe(handled[0]["id"], "form")
    return _safe(str(head.get("schema", "form")).replace("/", "-"), "form")


def secrets_in(text: str) -> list[str]:
    """Shown prefixes of anything in ``text`` that looks like a secret (the built-in patterns of rule R6)."""
    return [m.group(0)[:6] + "…" for pat in SECRET_PATTERNS for m in re.finditer(pat, text)]


@dataclass
class Message:
    path: str
    recipient: str
    sender: str
    utc: str
    form: str
    text: str
    schema: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems


def parse_path(path: str) -> tuple[str, str, str, str] | None:
    """to/<recipient>/<utc>-<sender>-<form id>.md -> (recipient, utc, sender, form id)."""
    m = re.match(r"^to/([A-Za-z0-9_.-]+)/(\d{8}T\d{6}\.\d{6}Z)-([A-Za-z0-9_.]+)-([A-Za-z0-9_.-]+)\.md$", path)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


class Mailbox:
    def __init__(self, repo: str | Path, *, remote: str = "origin", retries: int = RETRIES,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 quiet: bool = False):
        self.repo, self.remote, self.retries, self.sleep, self.clock = Path(repo), remote, retries, sleep, clock
        self.quiet = quiet  # CMD-GA50: no NET events (GA Console's own read-only polling is not the VM's work)
        self.pushes = 0  # attempts made by the last send (for the bounded-retry test)

    # -- git --
    def _git(self, *args: str, env: dict[str, str] | None = None, input: str | None = None, check: bool = True) -> str:
        e = dict(os.environ, GIT_TERMINAL_PROMPT="0", **(env or {}))
        p = subprocess.run(["git", *args], cwd=self.repo, env=e, input=input, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise MailError(f"git {args[0]} failed: {p.stderr.strip()[:200]}")
        return p.stdout

    def host(self) -> str:
        """The remote's host only (no user, no token, no path), ``local`` for a path remote; for NET events."""
        if getattr(self, "_host", None) is None:
            try:
                url = self._git("remote", "get-url", self.remote, check=False).strip()
            except Exception:
                url = ""
            m = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)", url) or re.match(r"^(?:[^@/]+@)?([^/:]+):(?!/)", url)
            self._host = m.group(1) if m else ("local" if url else "?")
        return self._host

    def git_dir(self) -> Path:
        d = Path(self._git("rev-parse", "--git-common-dir").strip())
        return d if d.is_absolute() else (self.repo / d).resolve()

    def tip(self) -> str | None:
        """Fetch the mailbox branch into a ref of this call's own (concurrent sends in one clone do not share one), return
        its commit, or None when the remote has no mailbox yet."""
        ref = f"refs/ga-mailbox/fetch-{uuid.uuid4().hex}"
        from . import events as EV
        sp = EV.Quiet() if self.quiet else EV.span("NET", "mailbox", "git fetch", host=self.host()).start()
        # --refmap= : no opportunistic update of refs/remotes/<remote>/ga-mailbox, which concurrent sends would race on
        p = subprocess.run(["git", "fetch", "-q", "--no-write-fetch-head", "--refmap=", self.remote,
                            f"+refs/heads/{BRANCH}:{ref}"],
                           cwd=self.repo, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), capture_output=True, text=True)
        if p.returncode != 0:
            if "couldn't find remote ref" in p.stderr or "could not find remote ref" in p.stderr:
                sp.done(result="no mailbox yet")
                return None
            sp.error(f"git fetch failed: {p.stderr.strip()[:100]}")
            sp.fail(exit=p.returncode)
            raise MailError(f"git fetch failed: {p.stderr.strip()[:200]}")
        sp.done(exit=0)
        sha = self._git("rev-parse", ref).strip()
        self._git("update-ref", "-d", ref, check=False)
        return sha

    def _files(self, tip: str | None, prefix: str = "to/") -> list[str]:
        if tip is None:
            return []
        return sorted(x for x in self._git("ls-tree", "-r", "--name-only", tip, "--", prefix).splitlines() if x)

    # -- send --
    def send(self, to: str, text: str, sender: str | None = None) -> str:
        """Validate and add one form for ``to``; return its path. Raises MailError (refused or not delivered)."""
        if not NAME.match(to or ""):
            raise MailError(f"recipient {to!r} is not a name ([A-Za-z0-9_.-], up to 40)")
        try:
            head, _body = parse_text(text)
            problems = validate(head)
        except FormError as e:
            head, problems = {}, e.problems
        if hard(problems):
            raise MailError("not a valid ga form, not sent: " + "; ".join(str(p) for p in hard(problems))[:400])
        leaks = secrets_in(text)
        if leaks:
            raise MailError(f"looks like a secret ({', '.join(leaks[:3])}), not sent")
        sender = sender or head.get("from")
        if not isinstance(sender, str) or not sender:
            raise MailError("the sender is unknown: the form has no 'from'; give --from NAME")
        if not SENDER.match(sender):
            raise MailError(f"sender {sender!r} is not a name ([A-Za-z0-9_.], up to 40, no '-')")
        fid = form_id(head)
        blob = self._git("hash-object", "-w", "--stdin", input=text).strip()
        who = {"GIT_AUTHOR_NAME": f"ga mail ({sender})", "GIT_AUTHOR_EMAIL": "ga-mail@localhost",
               "GIT_COMMITTER_NAME": f"ga mail ({sender})", "GIT_COMMITTER_EMAIL": "ga-mail@localhost"}
        self.pushes = 0
        for attempt in range(self.retries + 1):
            parent = self.tip()
            now = self.clock()
            utc = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f".{int(now * 1e6) % 1_000_000:06d}Z"
            path = f"to/{to}/{utc}-{sender}-{fid}.md"
            with tempfile.TemporaryDirectory() as tmp:
                idx = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
                if parent:
                    self._git("read-tree", parent, env=idx)
                    if path in self._files(parent, path):
                        continue  # the same name on the tip already: a new time on the next round
                else:
                    self._git("read-tree", "--empty", env=idx)
                self._git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=idx)
                tree = self._git("write-tree", env=idx).strip()
            commit = self._git("commit-tree", tree, *(["-p", parent] if parent else []), "-m",
                               f"ga mail: {sender} -> {to} {fid}", env=who).strip()
            self.pushes += 1
            from . import events as EV
            sp = EV.span("NET", "mailbox", "git push", host=self.host(), to=to, bytes=len(text.encode("utf-8")),
                         attempt=attempt + 1).start()
            p = subprocess.run(["git", "push", "-q", self.remote, f"{commit}:refs/heads/{BRANCH}"], cwd=self.repo,
                               env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), capture_output=True, text=True)
            if p.returncode == 0:
                sp.done(exit=0)
                return path
            if attempt < self.retries:
                sp.retry(why="push rejected: someone added first")
            else:
                sp.error("push rejected", attempts=self.pushes)
            sp.fail(exit=p.returncode)
            if attempt < self.retries:  # someone else added first: redo on the new tip, after a bounded backoff
                self.sleep(random.uniform(0.2, 1.0) * backoff(attempt))
        raise MailError(f"not delivered: the push was rejected {self.pushes} time(s)")

    # -- read --
    def cursor_file(self, name: str) -> Path:
        return self.git_dir() / "ga-mailbox" / f"{name}.cursor"

    def read_set(self, name: str) -> set[str]:
        f = self.cursor_file(name)
        return set(f.read_text(encoding="utf-8").splitlines()) if f.exists() else set()

    def mark_read(self, name: str, path: str) -> None:
        f = self.cursor_file(name)
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as h:
            h.write(path + "\n")

    def message(self, tip: str, path: str) -> Message:
        recipient, utc, sender, fid = parse_path(path) or (path.split("/")[1] if path.count("/") >= 2 else "?",
                                                            "", "?", "?")
        text = self._git("show", f"{tip}:{path}")
        schema, problems = None, []
        try:
            head, _ = parse_text(text)
            schema = head.get("schema")
            problems = [str(p) for p in hard(validate(head))]
        except FormError as e:
            problems = [str(p) for p in e.problems]
        if secrets_in(text):
            problems.append("looks like a secret")
        if schema and isinstance(head.get("from"), str) and head["from"] != sender:
            problems.append(f"the form says from {head['from']!r}, the file name {sender!r}")
        return Message(path, recipient, sender, utc, fid, text, schema, problems)

    def unread(self, name: str) -> Iterator[Message]:
        """New messages for ``name``, oldest first. The caller marks each read after showing it (``mark_read``)."""
        tip = self.tip()
        seen = self.read_set(name)
        for path in self._files(tip, f"to/{name}/"):
            if path not in seen:
                yield self.message(tip, path)

    # -- scan --
    def scan(self) -> dict[str, Any]:
        """Per recipient: messages, unread (by this clone's read set for that name when it has one, else the messages
        after the recipient's own last send), and the oldest report to it that it has not answered (no message from it
        to that sender since)."""
        tip = self.tip()
        msgs = []
        for path in self._files(tip):
            parts = parse_path(path)
            if parts:
                m = self.message(tip, path)
                msgs.append(m)
        out: dict[str, Any] = {}
        for r in sorted({m.recipient for m in msgs} | {m.sender for m in msgs}):
            mine = [m for m in msgs if m.recipient == r]
            sent = [m.utc for m in msgs if m.sender == r]
            seen = self.read_set(r) if self.cursor_file(r).exists() else None
            if seen is not None:
                unread, basis = sum(m.path not in seen for m in mine), "cursor"
            else:
                last = max(sent) if sent else ""
                unread, basis = sum(m.utc > last for m in mine), "since_last_send"
            open_reports = [m for m in mine if str(m.schema or "").startswith("report/")
                            and not any(x.sender == r and x.recipient == m.sender and x.utc > m.utc for x in msgs)]
            oldest = min(open_reports, key=lambda m: m.utc) if open_reports else None
            out[r] = {"messages": len(mine), "unread": unread, "basis": basis,
                      "oldest_unanswered_report": ({"path": oldest.path, "from": oldest.sender, "utc": oldest.utc}
                                                   if oldest else None)}
        return out


# ---- S4: a guard event as a report/2 --------------------------------------------------------------------------------

def guard_report(lines: list[str], *, sender: str, directive: str, rev: int = 1, items: list[str] | None = None,
                 guard: str = "rlo") -> str:
    """A guard's record lines (rlo --record or ga's guard log) as a report/2 with a permission blocker: the deny that
    stayed inside a container reaches the channel at the next allowed Bash. Counts and labels only."""
    from .hub import guard_summary
    g = guard_summary(lines)
    what = f"guard {guard} denied {g['deny']} tool call(s)" + (f": {', '.join(g['labels'])}" if g["labels"] else "")
    head = {"schema": "report/2", "from": sender,
            "handled": [{"id": directive, "rev_seen": rev, "status": "paused",
                         "reason": f"blocked by guard {guard}"}],
            "items": [{"id": i, "state": "blocked", "evidence": [what]} for i in (items or [])],
            "blockers": [{"kind": "permission", "what": what}]}
    body = (f"[{sender}] blocked by guard {guard}: {g['deny']} deny, {g['allow']} allow, {g['errors']} error(s)"
            + (f"; labels {', '.join(g['labels'])}" if g["labels"] else "") + ".\n")
    from .forms import dump_text
    return dump_text(head, body)
