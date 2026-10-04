"""``ga inbox <channel>`` (CMD-GA27 S4, BD-298): the one read path of a session. It prints only what is new since a
local cursor, one minified JSON line per message — ``{"id", "at", "head"}`` — the ga head (minified) or ``null`` for a
message without one. The API wrapper and every line of prose are dropped. A notify/1 carries only kind and ref: the
receiver reads the message here, never both the notify body and the issue.

Channels:
- ``github:<owner>/<repo>#<issue>`` (or the issue URL): the REST comments endpoint with ``since`` = the cursor's time.
  GitHub's ``since`` is inclusive (``updated_at >=``), so the cursor also keeps the ids already seen at that time.
  The token comes from an environment variable (``GITHUB_TOKEN`` by default) at each request, never stored or printed.
- ``mail:<NAME>``: ga mail's mailbox for that recipient, with its existing read set as the cursor.

The cursor lives in the git dir (``<git dir>/ga-inbox/<channel>.json``), so it is never pushed. It advances only
after the output is written and flushed: a failed write leaves it where it was, and the next read shows the same lines.
Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, TextIO

from .forms import FormError, minify, parse_text

GH_RE = re.compile(r"^(?:github:|https://github\.com/)([\w.-]+)/([\w.-]+)(?:#|/issues/)(\d+)(?:[#?].*)?$")
CURSOR_DIR = "ga-inbox"


def git_dir(repo: str | Path = ".") -> Path:
    out = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=str(repo), capture_output=True, text=True)
    if out.returncode != 0:
        raise InboxError(f"{repo}: not a git repository (the inbox cursor lives in the git dir)")
    return Path(out.stdout.strip())


class InboxError(RuntimeError):
    pass


def head_of(text: str) -> dict[str, Any] | None:
    """The message's ga head, or None. A head anywhere in the text counts (older posts put a line before it)."""
    try:
        return parse_text(text)[0]
    except FormError:
        m = re.search(r"```ga[ \t]*\n(.*?)\n```", text, re.S)
        if not m:
            return None
        try:
            v = json.loads(m.group(1))
        except ValueError:
            return None
        return v if isinstance(v, dict) else None


def line(mid: Any, at: str, head: dict[str, Any] | None) -> str:
    return '{"id":' + json.dumps(mid) + ',"at":' + json.dumps(at) + ',"head":' + (minify(head) if head else "null") + "}"


class Cursor:
    """{since, seen}: the newest ``updated_at`` read and the comment ids read at exactly that time."""

    def __init__(self, path: Path):
        self.path = path
        d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.since: str | None = d.get("since")
        self.seen: list[Any] = list(d.get("seen", []))

    def save(self, since: str | None, seen: list[Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"since": since, "seen": seen}, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
        self.since, self.seen = since, seen


def _key(channel: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", channel).strip("_")[:120]


def github_read(owner: str, repo: str, issue: int, cursor: Cursor, out: TextIO, *, token_env: str = "GITHUB_TOKEN",
                api: str | None = None, opener: Callable[..., Any] | None = None) -> int:
    """New comments since the cursor, as lines on ``out``; the cursor advances after the flush. Returns the count."""
    from .adapters.github import NEXT_RE, GitHubIssueChannel
    ch = GitHubIssueChannel(f"{owner}/{repo}", {}, token_env=token_env,
                            api=api or os.environ.get("GA_GITHUB_API", "https://api.github.com"), opener=opener)
    url = f"{ch.api}/repos/{owner}/{repo}/issues/{issue}/comments?per_page=100"
    if cursor.since:
        url += "&since=" + cursor.since
    got = []
    while url:
        data, headers = ch._request("GET", url)
        got += list(data or [])
        m = NEXT_RE.search(headers.get("link", ""))
        url = m.group(1) if m else ""
    seen = set(cursor.seen)

    def is_new(c: dict[str, Any]) -> bool:
        at = c.get("updated_at") or ""
        if cursor.since is None or at > cursor.since:
            return True
        return at == cursor.since and c["id"] not in seen  # since is inclusive

    new = sorted((c for c in got if is_new(c)), key=lambda c: (c.get("updated_at") or "", c["id"]))
    if not new:
        return 0
    out.write("".join(line(c["id"], c.get("updated_at") or "", head_of(c.get("body") or "")) + "\n" for c in new))
    out.flush()  # the cursor moves only after this
    last = new[-1]["updated_at"]
    keep = {c["id"] for c in new if c["updated_at"] == last} | (seen if last == cursor.since else set())
    cursor.save(last, sorted(keep))
    return len(new)


def mail_read(name: str, repo: str | Path, out: TextIO, *, remote: str = "origin") -> int:
    """ga mail's unread messages for ``name`` as lines; each is marked read after its line is flushed."""
    from .mailbox import Mailbox
    box = Mailbox(repo, remote=remote)
    n = 0
    for m in box.unread(name):
        out.write(line(m.path, m.utc, head_of(m.text)) + "\n")
        out.flush()
        box.mark_read(name, m.path)
        n += 1
    return n


def read(channel: str, out: TextIO, *, repo: str | Path = ".", remote: str = "origin", token_env: str = "GITHUB_TOKEN",
         api: str | None = None, opener: Callable[..., Any] | None = None) -> int:
    if channel.startswith("mail:"):
        return mail_read(channel[5:], repo, out, remote=remote)
    m = GH_RE.match(channel)
    if not m:
        raise InboxError(f"unknown channel {channel!r}: use github:<owner>/<repo>#<issue> or mail:<NAME>")
    owner, name, issue = m.group(1), m.group(2), int(m.group(3))
    cur = Cursor(git_dir(repo) / CURSOR_DIR / f"{_key(f'github_{owner}_{name}_{issue}')}.json")
    return github_read(owner, name, issue, cur, out, token_env=token_env, api=api, opener=opener)
