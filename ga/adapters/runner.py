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
