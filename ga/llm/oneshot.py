"""A one-process agy turn as a runner (ga ask): the subprocess lives here, the budget gate is ``ga.llm.run_turn``."""
from __future__ import annotations

import subprocess
import time
from typing import Any

from ..backends.base import BackendError, BackendTurn


class AgyOneShot:
    bare = False
    resumes = False

    def __init__(self, argv_of: Any, env: dict[str, str], model: str, cwd: str | None, timeout_s: float):
        self.argv_of, self.env, self.model, self.cwd, self.timeout_s = argv_of, env, model, cwd, timeout_s
        self.proc: subprocess.CompletedProcess | None = None
        self.seconds = 0.0

    def run_turn(self, prompt: str, session: str | None = None, **kw: Any) -> BackendTurn:
        t0 = time.monotonic()
        try:
            self.proc = subprocess.run(self.argv_of(prompt), cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                                       capture_output=True, text=True, timeout=self.timeout_s)
        except FileNotFoundError:
            raise BackendError("agy_not_found") from None
        except subprocess.TimeoutExpired:
            raise BackendError("timeout") from None
        self.seconds = time.monotonic() - t0
        return BackendTurn(self.proc.stdout or "", [self.model], seconds=self.seconds)
