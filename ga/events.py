"""ga.events/1 (CMD-GA50 S1): one structured event stream that ga act, the bridge, the hub (shadow included) and
``ga vm update`` write, and GA Console's 실시간 screen reads.

One JSON line per event in ``~/.ga/events.jsonl`` (``GA_EVENTS=<path>`` moves it, ``GA_EVENTS=off`` stops it; with
``GA_HOME`` set it is ``$GA_HOME/events.jsonl``), rotated
at 20 MB with 3 old files kept (``.1`` … ``.3``)::

    {"schema": "ga.events/1", "event_id", "ts" (UTC ISO ms), "type", "component", "action", "status",
     "parent_id" (null or the enclosing activity), "span_id" (the STARTED event of this activity),
     "duration_ms" (on DONE / FAILED), "metadata" {small, <= 2 KB}}

An *activity* (a span) is a STARTED event and the RUNNING / RETRY / WAITING / DONE / FAILED events that follow it with
the same ``span_id`` (= the STARTED event's ``event_id``). Children name it as their ``parent_id``. The enclosing span
is carried in a context variable inside a process and in ``GA_EVENT_PARENT`` across processes (ga bridge -> ga act).

Rules: append-only, no fsync, and nothing here ever raises into the caller. Every metadata string goes through
runlog's secret rule (a secret-looking value is withheld whole). Prompts, model answers and reasoning are never
written: an LLM event carries only a state, the model / rung, token counts and seconds.
"""
from __future__ import annotations

import contextvars
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "ga.events/1"
TYPES = ("TASK", "AGENT", "LLM", "TOOL", "CODE", "NET", "FILE", "QUEUE", "MEMORY", "DB", "ERROR", "USER", "SYSTEM")
STATUSES = ("STARTED", "RUNNING", "DONE", "FAILED", "RETRY", "WAITING")
LLM_STATES = ("REQUEST", "PROCESSING", "SELECTING_TOOL", "RECEIVING_RESULT", "RESPONSE_READY")
AGENT_STATES = ("PLANNING", "EXECUTING", "WAITING_TOOL", "ANALYZING", "COMPLETING")
ROTATE_BYTES = 20 * 1024 * 1024
KEEP = 3
META_BYTES = 2048
SUMMARY_CHARS = 120
TAIL_LINES = 20
FIELDS = ("schema", "event_id", "ts", "type", "component", "action", "status", "parent_id", "span_id", "duration_ms",
          "metadata")
ENV_PATH, ENV_PARENT = "GA_EVENTS", "GA_EVENT_PARENT"

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("ga_event_parent", default=None)
_lock = threading.Lock()


def path() -> Path | None:
    """``$GA_EVENTS``, else ``$GA_HOME/events.jsonl``, else ``~/.ga/events.jsonl``. Under a test runner with neither
    variable set nothing is written (a suite never lands in a person's ~/.ga)."""
    v = os.environ.get(ENV_PATH, "")
    if v.lower() in ("off", "0", "none"):
        return None
    if v:
        return Path(v).expanduser()
    if os.environ.get("GA_HOME"):
        return Path(os.environ["GA_HOME"]).expanduser() / "events.jsonl"
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return None
    return Path.home() / ".ga" / "events.jsonl"


def current() -> str | None:
    """The enclosing activity: this context's open span, else the one a parent process handed down."""
    return _current.get() or (os.environ.get(ENV_PARENT) or None)


def child_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """``env`` (default: os.environ) plus ``GA_EVENT_PARENT``, so a child process's events hang under this span."""
    e = dict(os.environ if env is None else env)
    p = current()
    if p:
        e[ENV_PARENT] = p
    return e


def now_iso(t: float | None = None) -> str:
    t = time.time() if t is None else t
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


def new_id() -> str:
    """Time-ordered: 12 hex of ms since the epoch, then 20 random hex (sorts like a ULID)."""
    return f"{int(time.time() * 1000):012x}{uuid.uuid4().hex[:20]}"


def short(text: Any, n: int = SUMMARY_CHARS) -> str:
    """One line, at most ``n`` characters, secret-looking text withheld."""
    s = " ".join(str(text if text is not None else "").split())
    s = _redact(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def tail(text: str, n: int = TAIL_LINES, width: int = 160) -> list[str]:
    """The last ``n`` non-empty lines of a command's output, each cut to ``width`` and each secret-checked alone."""
    lines = [ln.rstrip() for ln in str(text or "").splitlines() if ln.strip()][-n:]
    return [_redact(ln)[:width] for ln in lines]


def _redact(s: str) -> str:
    try:
        from .runlog import redact
        return redact(s)
    except Exception:  # pragma: no cover - the rule itself failing withholds, never leaks
        return "(withheld)"


def _clean(v: Any, depth: int = 0) -> Any:
    if isinstance(v, str):
        return _redact(v)
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return v
    if depth > 4:
        return short(v)
    if isinstance(v, dict):
        return {str(k)[:60]: _clean(x, depth + 1) for k, x in list(v.items())[:40]}
    if isinstance(v, (list, tuple)):
        return [_clean(x, depth + 1) for x in list(v)[:40]]
    return _redact(str(v))


def _fit(meta: dict[str, Any]) -> dict[str, Any]:
    """The metadata within META_BYTES: long strings and lists are cut first, then the largest keys dropped."""
    def size(m: dict) -> int:
        return len(json.dumps(m, ensure_ascii=False).encode("utf-8"))
    if size(meta) <= META_BYTES:
        return meta
    m = dict(meta)
    for k, v in list(m.items()):
        if isinstance(v, str) and len(v) > 200:
            m[k] = v[:199] + "…"
        elif isinstance(v, list) and len(json.dumps(v, ensure_ascii=False)) > 600:
            m[k] = v[-8:]
    while size(m) > META_BYTES and m:
        big = max(m, key=lambda k: len(json.dumps(m[k], ensure_ascii=False)))
        m.pop(big)
        m["cut"] = True
    return m


def make(type_: str, component: str, action: str, status: str, *, parent_id: str | None = None,
         span_id: str | None = None, duration_ms: int | None = None, metadata: dict[str, Any] | None = None,
         event_id: str | None = None, ts: float | None = None) -> dict[str, Any]:
    eid = event_id or new_id()
    return {"schema": SCHEMA, "event_id": eid, "ts": now_iso(ts),
            "type": type_ if type_ in TYPES else "SYSTEM", "component": short(component, 80) or "?",
            "action": short(action, 80) or "?", "status": status if status in STATUSES else "RUNNING",
            "parent_id": parent_id, "span_id": span_id or (eid if status == "STARTED" else None),
            "duration_ms": None if duration_ms is None else max(0, int(duration_ms)),
            "metadata": _fit(_clean(metadata or {}))}


def validate(ev: Any) -> list[str]:
    """Problems with one event (empty: valid ga.events/1)."""
    if not isinstance(ev, dict):
        return ["not an object"]
    p = [f"missing {k}" for k in FIELDS if k not in ev]
    if p:
        return p
    if ev["schema"] != SCHEMA:
        p.append("schema")
    if ev["type"] not in TYPES:
        p.append(f"type {ev['type']!r}")
    if ev["status"] not in STATUSES:
        p.append(f"status {ev['status']!r}")
    for k in ("event_id", "component", "action", "ts"):
        if not isinstance(ev[k], str) or not ev[k]:
            p.append(k)
    if not (isinstance(ev["ts"], str) and len(ev["ts"]) == 24 and ev["ts"].endswith("Z")):
        p.append("ts format")
    for k in ("parent_id", "span_id"):
        if ev[k] is not None and not isinstance(ev[k], str):
            p.append(k)
    if ev["duration_ms"] is not None and not (isinstance(ev["duration_ms"], int) and ev["duration_ms"] >= 0):
        p.append("duration_ms")
    if ev["status"] in ("DONE", "FAILED") and ev["span_id"] and ev["duration_ms"] is None:
        p.append("duration_ms missing on DONE/FAILED")
    if not isinstance(ev["metadata"], dict) or len(json.dumps(ev["metadata"], ensure_ascii=False).encode()) > META_BYTES:
        p.append("metadata")
    return p


def _rotate(f: Path) -> None:
    for i in range(KEEP, 0, -1):
        src = f if i == 1 else f.with_name(f"{f.name}.{i - 1}")
        if src.exists():
            os.replace(src, f.with_name(f"{f.name}.{i}"))


def write(ev: dict[str, Any]) -> bool:
    f = path()
    if f is None:
        return False
    line = json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _lock:
        f.parent.mkdir(parents=True, exist_ok=True)
        try:
            if f.stat().st_size + len(line) > ROTATE_BYTES:
                _rotate(f)
        except FileNotFoundError:
            pass
        with f.open("a", encoding="utf-8") as h:
            h.write(line)
    return True


_UNSET: Any = object()


def emit(type_: str, component: str, action: str, status: str = "RUNNING", *, parent_id: Any = _UNSET,
         span_id: str | None = None, duration_ms: int | None = None, metadata: dict[str, Any] | None = None,
         **meta: Any) -> str | None:
    """Write one event (``parent_id`` defaults to the enclosing span); its id, or None when nothing was written."""
    try:
        ev = make(type_, component, action, status, parent_id=current() if parent_id is _UNSET else parent_id,
                  span_id=span_id, duration_ms=duration_ms, metadata={**(metadata or {}), **meta})
        return ev["event_id"] if write(ev) else None
    except Exception:
        return None


class Span:
    """One activity: STARTED on enter (or ``start()``), then ``update`` / ``retry`` / ``wait``, and DONE or FAILED once
    (an exception leaving the ``with`` block is FAILED + an ERROR child). While open it is the enclosing span."""

    def __init__(self, type_: str, component: str, action: str, *, parent_id: str | None = None,
                 clock: Any = time.monotonic, **meta: Any):
        self.type, self.component, self.action, self.meta = type_, component, action, meta
        self.parent = parent_id
        self.clock = clock
        self.id: str | None = None
        self.t0 = 0.0
        self.ended = False
        self._token: Any = None

    def start(self) -> "Span":
        if self.parent is None:
            self.parent = current()
        self.t0 = self.clock()
        try:
            ev = make(self.type, self.component, self.action, "STARTED", parent_id=self.parent, metadata=self.meta)
            self.id = ev["event_id"]
            write(ev)
        except Exception:
            self.id = self.id or new_id()
        try:
            self._token = _current.set(self.id)
        except Exception:
            self._token = None
        return self

    def __enter__(self) -> "Span":
        return self.start()

    def __exit__(self, et: Any, e: Any, tb: Any) -> bool:
        if not self.ended:
            if e is not None:
                self.error(f"{type(e).__name__}: {e}")
                self.fail(error=type(e).__name__)
            else:
                self.done()
        return False

    def ms(self) -> int:
        return int(round((self.clock() - self.t0) * 1000))

    def _ev(self, status: str, action: str | None, meta: dict[str, Any], end: bool = False) -> str | None:
        return emit(self.type, self.component, action or self.action, status, parent_id=self.parent,
                    span_id=self.id, duration_ms=self.ms() if end else None, metadata=meta)

    def update(self, action: str | None = None, **meta: Any) -> None:
        self._ev("RUNNING", action, meta)

    def retry(self, action: str | None = None, **meta: Any) -> None:
        self._ev("RETRY", action, meta)

    def wait(self, action: str | None = None, **meta: Any) -> None:
        self._ev("WAITING", action, meta)

    def error(self, label: str, **meta: Any) -> None:
        """An ERROR event under this span (the timeout, the exception, the backend error label)."""
        emit("ERROR", self.component, short(label, 80), "FAILED", parent_id=self.id, metadata={"label": short(label), **meta})

    def _close(self) -> None:
        self.ended = True
        try:
            if self._token is not None:
                _current.reset(self._token)
        except (ValueError, RuntimeError):  # closed in another context: the context ends with it anyway
            pass
        self._token = None

    def done(self, action: str | None = None, **meta: Any) -> None:
        if not self.ended:
            self._close()
            self._ev("DONE", action, meta, end=True)

    def fail(self, action: str | None = None, **meta: Any) -> None:
        if not self.ended:
            self._close()
            self._ev("FAILED", action, meta, end=True)


def span(type_: str, component: str, action: str, **meta: Any) -> Span:
    return Span(type_, component, action, **meta)


class Quiet(Span):
    """A span that writes nothing (a caller whose own polling is not news, e.g. GA Console's mailbox reader)."""

    def __init__(self) -> None:
        super().__init__("SYSTEM", "-", "-")

    def start(self) -> "Quiet":
        return self

    def _ev(self, status: str, action: str | None, meta: dict[str, Any], end: bool = False) -> str | None:
        return None

    def error(self, label: str, **meta: Any) -> None:
        return None


def _tail_lines(files: list[Path], max_bytes: int) -> list[str]:
    """The last ``max_bytes`` of the chain ``files`` (oldest first) as whole lines, oldest first."""
    out: list[str] = []
    left = max_bytes
    for x in reversed(files):
        if left <= 0:
            break
        try:
            with x.open("rb") as h:
                h.seek(0, 2)
                size = h.tell()
                start = max(0, size - left)
                h.seek(start)
                data = h.read()
        except OSError:
            continue
        left -= len(data)
        lines = data.decode("utf-8", "replace").split("\n")
        if start > 0:
            lines = lines[1:]  # a cut first line
        out = [ln for ln in lines if ln.strip()] + out
    return out


def read(f: Path | None = None, *, limit: int = 200, types: Iterable[str] | None = None,
         task: str | None = None, max_bytes: int = 8 * 1024 * 1024) -> list[dict[str, Any]]:
    """The newest ``limit`` events (newest first) from the last ``max_bytes`` of the file and its rotated copies,
    optionally only some types or only one task's tree (``task``: a component name or an event id; its descendants
    come with it)."""
    f = f or path()
    if f is None:
        return []
    files = [f.with_name(f"{f.name}.{i}") for i in range(KEEP, 0, -1)] + [f]
    rows: list[dict[str, Any]] = []
    for line in _tail_lines(files, max_bytes):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("event_id"):
            rows.append(ev)
    return select(rows, limit=limit, types=types, task=task)


def select(rows: list[dict[str, Any]], *, limit: int = 200, types: Iterable[str] | None = None,
           task: str | None = None) -> list[dict[str, Any]]:
    """``rows`` (oldest first) filtered as ``read`` does, newest first."""
    if task:
        keep: set[str] = set()
        for ev in rows:  # in file order: a parent is written before its children
            if (ev.get("component") == task or ev.get("event_id") == task or ev.get("span_id") in keep
                    or ev.get("parent_id") in keep):
                keep.add(ev["event_id"])
                if ev.get("span_id"):
                    keep.add(ev["span_id"])
        rows = [ev for ev in rows if ev["event_id"] in keep]
    want = {t.strip().upper() for t in types or [] if t and t.strip()}
    if want:
        rows = [ev for ev in rows if ev.get("type") in want]
    return rows[::-1][:max(1, int(limit))]


class Follow:
    """The complete event lines appended since the last read; a rotation (the file got shorter) starts it over."""

    def __init__(self, f: Path, start_at_end: bool = True):
        self.f = Path(f)
        try:
            self.offset = self.f.stat().st_size if start_at_end else 0
            self.ino = self.f.stat().st_ino
        except OSError:
            self.offset, self.ino = 0, None

    def read(self) -> list[dict[str, Any]]:
        try:
            st = self.f.stat()
        except OSError:
            return []
        if st.st_ino != self.ino or st.st_size < self.offset:
            self.offset, self.ino = 0, st.st_ino
        if st.st_size == self.offset:
            return []
        with self.f.open("rb") as h:
            h.seek(self.offset)
            data = h.read(st.st_size - self.offset)
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self.offset += end + 1
        out = []
        for line in data[:end].decode("utf-8", "replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("event_id"):
                out.append(ev)
        return out
