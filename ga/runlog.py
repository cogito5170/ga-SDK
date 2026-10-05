"""Waiting is code (CMD-GA36): follow a ga supervise log (``<state_dir>/log.jsonl``) while the loop runs and turn its
rows into one line per model turn. Nothing here calls a model: it reads files and sleeps.

- ``Tail``       the complete JSON lines appended to a file since the last read
- ``TurnMeter``  log rows -> one line per turn (turn n, input, output tokens, seconds, tool steps) and the totals
- ``follow``     poll a running process and its log until it exits; every new turn line goes to ``on_line``
- ``RunLog``     the run log a person (or ga ui's SSE) reads: plain text lines, secret-looking text withheld
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

WITHHELD = "(withheld: it looked like it held a secret)"


def redact(text: str) -> str:
    """The text, or a withheld note when it looks like it holds a secret (rule R6's patterns)."""
    from .mailbox import secrets_in
    return WITHHELD if secrets_in(text) else text


class Tail:
    """The complete JSON lines appended to ``path`` since the last read (a half-written line waits for the next)."""

    def __init__(self, path: Path, start: int | None = None):
        self.path = Path(path)
        self.offset = (self.path.stat().st_size if self.path.exists() else 0) if start is None else start

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open("rb") as f:
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self.offset += end + 1
        rows = []
        for line in data[:end].decode("utf-8", "replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class TurnMeter:
    """Supervise log rows -> turns. A turn's line is written when its plan row (with the tool steps it asked for)
    arrives, when the turn failed, or at the end of the task."""

    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []
        self.refused: list[str] = []      # plan problems: a tool not in the table, ...
        self.denied: list[str] = []       # tools the host refused inside a turn
        self.end: dict[str, Any] = {}
        self._open: dict[str, Any] | None = None

    def _close(self, tool_steps: int) -> str:
        t, self._open = self._open, None
        t["tool_steps"] = tool_steps
        self.turns.append(t)
        return self.line(t)

    @staticmethod
    def line(t: dict[str, Any]) -> str:
        def n(v: Any) -> str:
            return "?" if v is None else f"{v:,}"
        return (f"턴 {t['n']}: 입력 {n(t['input'])} · 출력 {n(t['output'])} 토큰 · {t['seconds']:g}초 · "
                f"도구 {t['tool_steps']}단계" + ("" if t["ok"] else f" · 실패 ({t.get('reason') or '?'})"))

    def feed(self, row: dict[str, Any]) -> list[str]:
        out: list[str] = []
        ev = row.get("event")
        if ev == "turn":
            if self._open is not None:
                out.append(self._close(0))
            inp, total, outp = _int(row.get("input_tokens")), _int(row.get("tokens")), _int(row.get("output_tokens"))
            if outp is None and inp is not None and total is not None:
                outp = max(total - inp, 0)
            self._open = {"n": len(self.turns) + 1, "step": row.get("step"), "ok": bool(row.get("ok")),
                          "input": inp, "output": outp, "seconds": float(row.get("seconds") or 0),
                          "reason": row.get("reason"), "model": row.get("model")}
            if not row.get("ok"):
                out.append(self._close(0))
        elif ev == "plan" and self._open is not None and row.get("step") == self._open["step"]:
            if not row.get("ok"):
                self.refused.append(str(row.get("problems") or "plan refused"))
            out.append(self._close(int(row.get("tool_steps") or 0)))
        elif ev == "denied":
            self.denied += [str(x) for x in row.get("labels") or []]
        elif ev == "end":
            if self._open is not None:
                out.append(self._close(0))
            self.end = row
        return out

    def totals(self) -> dict[str, Any]:
        """The sums of the turn lines: exactly what was shown, turn by turn."""
        return {"turns": len(self.turns),
                "input": sum(t["input"] or 0 for t in self.turns),
                "output": sum(t["output"] or 0 for t in self.turns),
                "seconds": round(sum(t["seconds"] for t in self.turns), 3),
                "tool_steps": sum(t["tool_steps"] for t in self.turns)}


def follow(done: Callable[[], bool], tail: Tail, meter: TurnMeter, on_line: Callable[[str], None], *,
           sleep: Callable[[float], None] = time.sleep, poll_s: float = 0.5) -> None:
    """Until ``done()``: read the new log rows, print each finished turn's line, sleep. A last read after the end."""
    while True:
        finished = done()
        for row in tail.read():
            for line in meter.feed(row):
                on_line(line)
        if finished:
            return
        sleep(poll_s)


class RunLog:
    """``<dir>/<id>.log``: the lines a person reads (the terminal and ga ui's SSE); ``<id>.done`` when it ended."""

    def __init__(self, dir: Path, run_id: str):
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.id = run_id
        self.path, self.done_path = self.dir / f"{run_id}.log", self.dir / f"{run_id}.done"

    def write(self, line: str) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            for part in str(line).splitlines() or [""]:
                f.write(redact(part) + "\n")

    def close(self, status: str = "done") -> None:
        self.done_path.write_text(status + "\n", encoding="utf-8")
