"""GitHub issue Channel (METHOD §4.1, 2nd edition fourth): one issue per session, one post = one comment.

Standard library only (urllib). The token is read from an environment variable at each request and never
stored, logged or written into a post. Because every comment may come from the same GitHub account (the
person runs hub and sessions), the ga author travels in the text as a trailing marker line
``<!-- ga-author: <name> -->`` on its own line (the last such line counts; a footer may follow it); without it,
the GitHub login is the author. The marker is a claim, like the login: in a one-account setup it is what tells
hub and sessions apart, not a proof of who wrote the comment.

Post ids are the comment ids zero-padded to 20 digits, so they sort in posting order like the mailbox's.
Reading follows ``Link: rel="next"`` pages. A rate limit (429, or 403 with ``X-RateLimit-Remaining: 0``)
raises ``RateLimited`` with the seconds to wait; other HTTP failures raise ``ChannelError``.

``CallbackChannel`` is the alternative where the library cannot reach GitHub itself: an agent or a person
with GitHub tools supplies ``post`` and ``read``.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from .base import Post

MARKER_RE = re.compile(r"^<!-- ga-author: ([A-Za-z0-9._-]+) -->[ \t]*$", re.M)
NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


class ChannelError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RateLimited(ChannelError):
    def __init__(self, retry_after: float, status: int):
        super().__init__(f"GitHub rate limit: retry after {retry_after:g}s", status)
        self.retry_after = retry_after


def with_author(text: str, author: str) -> str:
    return text.rstrip("\n") + f"\n\n<!-- ga-author: {author} -->\n"


def split_author(body: str, login: str) -> tuple[str, str]:
    """(author, text): the last marker line names the author and is taken out. It need not be the very last line —
    a platform may append its own footer after it."""
    marks = list(MARKER_RE.finditer(body))
    if not marks:
        return login, body
    m = marks[-1]
    before, after = body[: m.start()].rstrip("\n"), body[m.end():].strip("\n")
    return m.group(1), before + "\n" + (after + "\n" if after else "")


class GitHubIssueChannel:
    def __init__(self, repo: str, issues: dict[str, int], *, token_env: str = "GITHUB_TOKEN",
                 api: str = "https://api.github.com", timeout: float = 30, opener: Callable[..., Any] | None = None):
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
            raise ValueError(f"repo must be owner/name, got {repo!r}")
        self.repo = repo
        self.issues = dict(issues)
        self.token_env = token_env
        self.api = api.rstrip("/")
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

    # ------------------------------------------------------------------ http

    def _request(self, method: str, url: str, payload: dict | None = None) -> tuple[Any, dict[str, str]]:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "ga-sdk", "X-GitHub-Api-Version": "2022-11-28"}
        token = os.environ.get(self.token_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                body = resp.read()
                return (json.loads(body) if body else None), {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
            e.close()  # the error body is not read: nothing from it is kept
            if e.code == 429 or (e.code == 403 and hdrs.get("x-ratelimit-remaining") == "0"):
                retry = hdrs.get("retry-after")
                if retry is None and hdrs.get("x-ratelimit-reset"):
                    import time
                    retry = max(0.0, float(hdrs["x-ratelimit-reset"]) - time.time())
                raise RateLimited(float(retry or 60), e.code) from None
            raise ChannelError(f"GitHub {method} {url.split('?')[0]} failed: HTTP {e.code}", e.code) from None
        except urllib.error.URLError as e:
            raise ChannelError(f"GitHub unreachable: {e.reason}") from None

    def _issue(self, channel: str) -> int:
        if channel not in self.issues:
            raise ChannelError(f"no issue configured for channel {channel!r}")
        return self.issues[channel]

    # ------------------------------------------------------------------ Channel

    def post(self, channel: str, author: str, text: str) -> Post:
        n = self._issue(channel)
        data, _ = self._request("POST", f"{self.api}/repos/{self.repo}/issues/{n}/comments", {"body": with_author(text, author)})
        return Post(f"{int(data['id']):020d}", channel, author, data.get("created_at", ""), text)

    def read(self, channel: str, after: str | None = None) -> list[Post]:
        n = self._issue(channel)
        url = f"{self.api}/repos/{self.repo}/issues/{n}/comments?per_page=100"
        out: list[Post] = []
        while url:
            data, headers = self._request("GET", url)
            for c in data or []:
                pid = f"{int(c['id']):020d}"
                if after is not None and pid <= after:
                    continue
                author, text = split_author(c.get("body") or "", (c.get("user") or {}).get("login", "?"))
                out.append(Post(pid, channel, author, c.get("created_at", ""), text))
            m = NEXT_RE.search(headers.get("link", ""))
            url = m.group(1) if m else ""
        return sorted(out, key=lambda p: p.id)


class CallbackChannel:
    """A Channel whose post/read are supplied by someone who can reach the real channel (an agent's tools)."""

    def __init__(self, post: Callable[[str, str, str], Post], read: Callable[[str, str | None], list[Post]]):
        self._post, self._read = post, read

    def post(self, channel: str, author: str, text: str) -> Post:
        return self._post(channel, author, text)

    def read(self, channel: str, after: str | None = None) -> list[Post]:
        return self._read(channel, after)
