"""Runners (METHOD §4c). 1st edition: the manual runner.

The manual runner cannot run a turn itself: it writes the prompt to paste into the session's
terminal under ``<outbox>/<session>/`` and prints where it is. It does not know when the turn ends;
the session's next report in the mailbox tells the hub.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import TextIO

from .base import TurnRequest, TurnResult


class ManualRunner:
    kind = "manual"

    def __init__(self, outbox: str | Path, out: TextIO | None = None):
        self.outbox = Path(outbox)
        self.out = out if out is not None else sys.stdout

    def run_turn(self, req: TurnRequest) -> TurnResult:
        d = self.outbox / req.session
        d.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns():020d}.md"
        tmp = d / f".{name}.tmp"
        tmp.write_text(req.prompt, encoding="utf-8")
        os.replace(tmp, d / name)
        self.out.write(
            f"[ga] 세션 {req.session} 의 터미널(작업 디렉터리 {req.workdir})에 이 프롬프트를 붙여 넣으세요: {d / name}\n"
        )
        return TurnResult(ended=None, note=str(d / name))

    def pending(self, session: str) -> list[Path]:
        """Prompts written for a session, oldest first (what the person has to paste)."""
        d = self.outbox / session
        return sorted(d.glob("*.md")) if d.is_dir() else []


class RemoteSessionRunner:
    """§4c remote sessions: one turn = a message to a cloud session (baseline's way).

    The library has no public API it can call to create or wake a remote session, so the waking is a
    callback supplied by whoever has that power — an agent with remote-session tools, or a person.
    ``send(request) -> {"session_id": ...}``. The runner cannot see the end of the turn (``ended=None``):
    the session's report on its channel, and the safety-net ``ga tick``, carry the loop on.
    ``OutboxCallback`` is the plain form: it writes the request where the agent relays it from.
    """

    kind = "remote"

    def __init__(self, send):
        self.send = send

    def run_turn(self, req: TurnRequest) -> TurnResult:
        try:
            out = self.send(req) or {}
        except Exception as e:  # the kind only
            return TurnResult(ended=None, error=f"send_failed:{type(e).__name__}")
        return TurnResult(ended=None, session_id=out.get("session_id"), note=str(out.get("note", ""))[:120])


class OutboxCallback:
    """Writes each turn request as JSON to ``<outbox>/<session>/<n>.json`` for an agent to relay; returns the
    remote session id the agent recorded for that session (``<outbox>/<session>/session_id``), if any."""

    def __init__(self, outbox: str | Path):
        self.outbox = Path(outbox)

    def __call__(self, req: TurnRequest) -> dict:
        import json

        d = self.outbox / req.session
        d.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns():020d}.json"
        tmp = d / f".{name}.tmp"
        tmp.write_text(json.dumps({"session": req.session, "prompt": req.prompt, "resume_id": req.resume_id},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, d / name)
        sid = d / "session_id"
        return {"session_id": sid.read_text(encoding="utf-8").strip() if sid.exists() else None, "note": name}
