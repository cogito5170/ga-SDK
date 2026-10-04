"""File mailbox Channel (METHOD §4.1): ``<root>/<session>/<id>.md``.

Safe for several processes: each post is written to a temp file in the same directory and
renamed into place (atomic on POSIX and Windows), and ids are unique without locks.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from .base import Post

META_SEP = "\n---ga-post---\n"


class FileMailbox:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _dir(self, channel: str) -> Path:
        if not channel or "/" in channel or "\\" in channel or channel.startswith("."):
            raise ValueError(f"bad channel name {channel!r}")
        return self.root / channel

    def post(self, channel: str, author: str, text: str) -> Post:
        from ..net import is_peer_form
        if is_peer_form(text):  # CMD-GA31 S6: a peer message goes by ga mail between nodes, never on a hub channel
            raise ValueError("a peer message (```peer block) never goes on a hub channel")
        d = self._dir(channel)
        d.mkdir(parents=True, exist_ok=True)
        pid = f"{time.time_ns():020d}-{os.getpid():07d}-{secrets.token_hex(3)}"
        at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        meta = json.dumps({"author": author, "at": at}, ensure_ascii=False)
        tmp = d / f".{pid}.tmp"
        tmp.write_text(meta + META_SEP + text, encoding="utf-8")
        os.replace(tmp, d / f"{pid}.md")
        return Post(pid, channel, author, at, text)

    def read(self, channel: str, after: str | None = None) -> list[Post]:
        d = self._dir(channel)
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.glob("*.md")):
            pid = p.stem
            if after is not None and pid <= after:
                continue
            raw = p.read_text(encoding="utf-8")
            meta, _, text = raw.partition(META_SEP)
            m = json.loads(meta)
            out.append(Post(pid, channel, m["author"], m["at"], text))
        return out
