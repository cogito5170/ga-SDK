"""GA Console's cloud collector (CMD-GA52 S1, S2): the user's cloud Claude sessions, read from a snapshot.

The VM has no Claude credentials and gets none: the baseline hub publishes a sanitized snapshot (schema cloud-sessions/1,
written by baseline ops/hub/cloud_snapshot.py) on the baseline repo's integration branch, and the console reads it::

    {"schema": "cloud-sessions/1", "at": "2026-10-06T18:00:00+09:00",
     "sessions": [{"id", "title", "status", "bucket", "detail", "repo", "branch", "model", "ctx", "cost_usd", "tags",
                   "updated_at", "url"}]}

The read never touches the worktree's checkout: ``git fetch <remote> <branch>`` into the remote-tracking ref at most every
``cloud.fetch_every_s`` (default 300 s), then ``git show <remote>/<branch>:ops/hub/cloud_sessions.json``. When the ref
has no snapshot the worktree file is read instead; when neither is usable the answer is ``{available: false, reason}``.
Only the fields above are kept (every other field is dropped), each is type-checked, at most ``MAX_SESSIONS`` sessions;
the page shows every string as text.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .collectors import clean, git

SCHEMA = "cloud-sessions/1"
PATH = "ops/hub/cloud_sessions.json"
DEFAULT_BRANCH = "claude/gracious-meitner-vp49xe"
FETCH_EVERY_S = 300.0
STALE_S = 2 * 3600  # older than this, the snapshot is stale (the hub stopped publishing)
MAX_SESSIONS = 100
BUCKETS = ("working", "review_ready", "blocked", "failed", "completed")
ACTIVE = ("working", "review_ready", "blocked", "failed")
ROLES = ("hub", "worker", "integrator", "watcher")
ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
TEXT = {"title": 200, "status": 40, "bucket": 40, "detail": 300, "repo": 200, "branch": 200, "model": 80}
FIELDS = ("id", "title", "status", "bucket", "detail", "repo", "branch", "model", "ctx", "cost_usd", "tags",
          "updated_at", "url", "role")
URL = "https://claude.ai/code/"


def settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """The ``cloud`` config key with its defaults: the baseline checkout, its integration branch, origin, 300 s."""
    c = dict(cfg.get("cloud") or {})
    repo = str(c.get("repo") or cfg.get("baseline") or "")
    branch = c.get("integration_branch")
    if not branch:
        for r in cfg.get("repos") or []:
            if str(r.get("path")) == repo and r.get("integration_branch"):
                branch = r["integration_branch"]
                break
    try:
        every = float(c.get("fetch_every_s", FETCH_EVERY_S))
    except (TypeError, ValueError):
        every = FETCH_EVERY_S
    return {"repo": repo, "remote": str(c.get("remote") or "origin"), "integration_branch": str(branch or DEFAULT_BRANCH),
            "path": str(c.get("path") or PATH), "fetch_every_s": every}


def _when(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo is not None else None


def _text(v: Any, limit: int) -> str | None:
    if not isinstance(v, str):
        return None
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", v)[:limit]


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0:
        return None
    return v


def role(s: dict[str, Any]) -> str | None:
    """hub / worker / integrator / watcher, from the tags first, then the title."""
    words = " ".join([*(s.get("tags") or []), s.get("title") or ""]).lower()
    for name, pat in (("hub", r"\bhub\b|허브"), ("integrator", r"integrat|통합"), ("watcher", r"\bwatch|감시"),
                      ("worker", r"\bworker\b|\bga\b|작업|\bcmd-[a-z]*\d+")):
        if re.search(pat, words):
            return name
    return None


def session(raw: Any) -> dict[str, Any] | None:
    """One session with the known fields only, each of its type (a wrong type becomes None); None when the id is bad."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not ID.match(raw["id"]):
        return None
    out: dict[str, Any] = {"id": raw["id"]}
    for k, lim in TEXT.items():
        out[k] = _text(raw.get(k), lim)
    if out["bucket"] not in BUCKETS:
        out["bucket"] = ""
    ctx = _num(raw.get("ctx"))
    out["ctx"] = int(ctx) if ctx is not None else None
    out["cost_usd"] = _num(raw.get("cost_usd"))
    tags = raw.get("tags")
    out["tags"] = [t[:40] for t in tags if isinstance(t, str)][:20] if isinstance(tags, list) else []
    out["updated_at"] = raw["updated_at"] if _when(raw.get("updated_at")) else None
    out["url"] = URL + raw["id"]  # built here from the checked id: a link only ever goes to the session's own page
    out["role"] = role(out)
    return {k: out[k] for k in FIELDS}


def parse(text: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """(snapshot with checked sessions, None) or (None, reason)."""
    if text is None:
        return None, "no snapshot"
    try:
        obj = json.loads(text)
    except ValueError:
        return None, "not JSON"
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA:
        return None, f"not {SCHEMA}"
    if _when(obj.get("at")) is None:
        return None, "at: not an ISO time with a zone"
    if not isinstance(obj.get("sessions"), list):
        return None, "sessions: not a list"
    sessions = [s for s in (session(x) for x in obj["sessions"]) if s is not None][:MAX_SESSIONS]
    return {"at": obj["at"], "sessions": sessions}, None


class CloudReader:
    """The snapshot from the integration ref (fetched at most every ``fetch_every_s``), else the worktree file."""

    def __init__(self, cfg: dict[str, Any], *, clock: Callable[[], float] = time.time):
        self.s = settings(cfg)
        self.clock = clock
        self.fetches = 0
        self._fetched_at = -1e18
        self._cache: tuple[Any, dict[str, Any] | None, str | None, str | None] = (None, None, "no snapshot", None)
        self.lock = threading.Lock()

    @property
    def ref(self) -> str:
        return f"refs/remotes/{self.s['remote']}/{self.s['integration_branch']}"

    def _fetch(self) -> None:
        if self.clock() - self._fetched_at < self.s["fetch_every_s"]:
            return
        self._fetched_at = self.clock()
        self.fetches += 1
        b = self.s["integration_branch"]
        git(self.s["repo"], "fetch", "-q", "--no-tags", self.s["remote"], f"+refs/heads/{b}:{self.ref}", timeout=60)

    def read(self) -> dict[str, Any]:
        """{at, sessions, source: ref|worktree} or {reason}; the ref's snapshot is parsed once per commit."""
        with self.lock:
            repo = self.s["repo"]
            if not repo or not Path(repo).is_dir():
                return {"reason": "no baseline checkout"}
            self._fetch()
            sha = (git(repo, "rev-parse", "-q", "--verify", self.ref + "^{commit}") or "").strip()
            key: Any = ("ref", sha)
            text = git(repo, "show", f"{sha}:{self.s['path']}") if sha else None
            source = "ref"
            if text is None:
                f = Path(repo) / self.s["path"]
                try:
                    st = f.stat()
                    key, source = ("worktree", st.st_mtime_ns, st.st_size), "worktree"
                    text = None if key == self._cache[0] else f.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    key, text = ("none",), None
            if key != self._cache[0]:
                snap, why = parse(text)
                self._cache = (key, snap, why, source)
            _key, snap, why, source = self._cache
            if snap is None:
                return {"reason": why}
            return {**snap, "source": source}

    def key(self) -> Any:
        """What the watcher compares: the snapshot's ``at`` (and whether there is one)."""
        r = self.read()
        return ("at", r["at"]) if "at" in r else ("none", r.get("reason"))

    def view(self) -> dict[str, Any]:
        """GET /api/cloud: {available, at, age_s, stale, sessions, source} or {available: false, reason}."""
        r = self.read()
        if "at" not in r:
            return clean({"available": False, "reason": r.get("reason"), "at": None, "age_s": None, "stale": None,
                          "sessions": []})
        age = max(0, int(self.clock() - _when(r["at"]).timestamp()))
        return clean({"available": True, "at": r["at"], "age_s": age, "stale": age > STALE_S, "source": r["source"],
                      "sessions": r["sessions"]})
