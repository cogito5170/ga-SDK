"""``ga console`` (CMD-CON2 S1, S4): GA API — the server behind GA Console.

Security is GA36's (``ga.ui``), reused, not copied: 127.0.0.1 only, a one-time token in the opened URL (query ``t`` for
GET and SSE, header ``X-GA-Token`` for POST), Host / Origin / Sec-Fetch-Site checks (403), actions only by POST + token,
the strict CSP and no-store on every answer, nothing remote. The static console is ga/console/static.

    GET  /api/state  /api/work  /api/branches  /api/tokens  /api/decisions?q=  /api/mail?limit=  /api/ops
    GET  /api/services/{name}/logs?after=n
    POST /api/services/{name}/start|stop   /api/bridge/start|stop
    POST /api/ask {q, mode: ask|do} -> {plan, cost_estimate, confirm_id};  POST /api/ask/confirm {confirm_id} -> {run_id}
    GET  /api/events  (SSE; Last-Event-ID resumes)  types: state work mail service log act_turn bridge ev
    GET  /api/events/recent?limit=&type=&task=   ga.events/1 history, newest first (CMD-GA50)

Changes reach /api/events by code (file mtimes, git heads, the mailbox tip, service events), never by a model.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from .. import events as EV
from ..runlog import redact
from ..ui import BIND, HEADERS, Handler as UiHandler
from . import collectors as C
from . import config as K
from .services import Services

STATIC = Path(__file__).resolve().parent / "static"
CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
          ".woff2": "font/woff2"}
CONFIRM_TTL_S = 600.0


# ---- the event bus (SSE) ----------------------------------------------------------------------------------------------

class Bus:
    """Numbered events in a ring; a reader resumes after the id it last saw (Last-Event-ID)."""

    def __init__(self, size: int = 5000):
        self.ring: deque[tuple[int, str, Any]] = deque(maxlen=size)
        self.n = 0
        self.cv = threading.Condition()

    def publish(self, type_: str, data: Any) -> int:
        with self.cv:
            self.n += 1
            self.ring.append((self.n, type_, C.clean(data)))
            self.cv.notify_all()
            return self.n

    def after(self, last: int) -> tuple[list[tuple[int, str, Any]], bool]:
        """(events after ``last``, whether some were already dropped from the ring)."""
        with self.cv:
            gap = bool(self.ring) and last < self.ring[0][0] - 1
            return [e for e in self.ring if e[0] > last], gap

    def wait(self, last: int, timeout: float) -> None:
        with self.cv:
            if self.n <= last:
                self.cv.wait(timeout)


# ---- ask / do through the GA36 and GA37 engines ------------------------------------------------------------------------

class Asker:
    """``POST /api/ask`` shows what would run and what it costs and hands out a one-use confirm id; only
    ``/api/ask/confirm`` with that id runs it (the engines' own guards still apply)."""

    def __init__(self, cfg: dict[str, Any], bus: Bus, *, engine_factory: Callable[[Callable[[str], None]], Any] | None,
                 do_argv: Callable[[str], list[str]] | None = None, clock: Callable[[], float] = time.time):
        self.cfg, self.bus, self.clock = cfg, bus, clock
        if engine_factory is None:
            from ..ask import Engine
            home = cfg.get("ask_home")
            engine_factory = lambda out: Engine(Path(home) if home else None, out=out)  # noqa: E731
        self.engine_factory = engine_factory
        self.do_argv = do_argv or (lambda q: [sys.executable, "-m", "ga", "do", "--repo", cfg["baseline"], "--", q])
        self.pending: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def _ask_json(self) -> Path:
        from ..ask.store import home
        h = self.cfg.get("ask_home")
        return (Path(h) if h else home()) / "ask.json"

    def confirm_model_turns(self) -> bool:
        try:
            return bool(json.loads(self._ask_json().read_text(encoding="utf-8")).get("confirm_model_turns", False))
        except (OSError, ValueError, AttributeError):
            return False

    def set_confirm_model_turns(self, on: bool) -> None:
        f = self._ask_json()
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            raw = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            raw = {}
        raw["confirm_model_turns"] = bool(on)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def plan(self, q: str, mode: str) -> tuple[int, dict[str, Any]]:
        from ..ask import intents as I
        q = q.strip()
        if not q:
            return 400, {"error": "empty question"}
        if mode not in ("ask", "do"):
            return 400, {"error": "mode must be ask or do"}
        if mode == "do":
            from ..intake.summary import summarize
            try:
                n = summarize(Path(self.cfg["baseline"])).tokens
            except Exception:
                n = None
            job = {"mode": "do", "q": q}
            plan = [f"실행할 것: ga do --repo {self.cfg['baseline']} — 요청을 task/1 명세로 (모델 사용)",
                    f"저장소 요약: ~{n:,} 토큰 (모델 입력의 일부)" if n is not None else "저장소 요약: 크기 모름"]
            cost = {"cost": "model", "input_tokens": n, "turns": None}
        else:
            eng = self.engine_factory(lambda s: None)
            m = I.route(q)
            if m.intent is not None and m.intent.name == "setup":  # a code answer: 0 tokens, nothing to confirm
                lines: list[str] = []
                eng = self.engine_factory(lines.append)
                eng.execute("setup", confirmed=True)
                return 200, {"answer": redact("\n".join(lines)), "code": True,
                             "cost_estimate": {"cost": "free", "input_tokens": 0, "turns": 0}}
            if m.intent is not None:
                p = eng.prepare(m.intent.name)
                if p["refuse"]:
                    return 409, {"refuse": redact(p["refuse"])}
                job = {"mode": "ask", "intent": m.intent.name}
                plan = [f"{m.intent.label} ({' '.join(m.intent.argv)})"] + list(p["lines"])
                cost = {"cost": p["cost"], "input_tokens": p.get("estimate", 0 if p["cost"] != "model" else None),
                        "turns": p.get("turns", 0 if p["cost"] != "model" else None)}
            else:
                p = eng.prepare_model(q)
                if p["refuse"]:
                    return 409, {"refuse": redact(p["refuse"])}
                job = {"mode": "ask", "model": q}
                plan = list(p["lines"])
                cost = {"cost": "model", "input_tokens": p.get("prompt_tokens"), "turns": 1}
        cid = secrets.token_urlsafe(12)
        with self.lock:
            now = self.clock()
            self.pending = {k: v for k, v in self.pending.items() if now - v["at"] < CONFIRM_TTL_S}
            self.pending[cid] = {**job, "at": now}
        out = {"plan": [redact(x) for x in plan], "cost_estimate": cost, "confirm_id": cid}
        if mode == "ask" and "model" in job and not self.confirm_model_turns():
            # a model turn runs at once (the daily cap was checked in prepare_model); ga do always confirms
            code, r = self.confirm(cid)
            if code == 200:
                return 200, {**out, "confirm_id": None, "run_id": r["run_id"], "started": True}
        return 200, out

    def confirm(self, cid: str) -> tuple[int, dict[str, Any]]:
        with self.lock:
            job = self.pending.pop(str(cid or ""), None)
        if job is None or self.clock() - job["at"] >= CONFIRM_TTL_S:
            return 404, {"error": "no such confirmation (expired or used)"}
        rid = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock())) + "-" + secrets.token_hex(3)
        say = lambda text: self.bus.publish("log", {"run_id": rid, "text": redact(str(text))})  # noqa: E731
        if job["mode"] == "do":
            target = lambda: self._child(self.do_argv(job["q"]), say)  # noqa: E731
        else:
            def target() -> None:
                eng = self.engine_factory(say)
                eng.log = eng.new_log(job.get("intent") or "model")
                try:
                    if "intent" in job:
                        eng.execute(job["intent"], confirmed=True)
                    else:
                        eng.run_model(eng.prepare_model(job["model"]), confirmed=True)
                finally:
                    if not eng.log.done_path.exists():
                        eng.log.close()

        def run() -> None:
            try:
                target()
            except Exception as e:
                say(f"오류: {type(e).__name__}: {str(e)[:200]}")
            self.bus.publish("log", {"run_id": rid, "end": True})
        threading.Thread(target=run, daemon=True).start()
        return 200, {"run_id": rid}

    def _child(self, argv: list[str], say: Callable[[str], None]) -> None:
        p = subprocess.Popen(argv, cwd=self.cfg["baseline"], shell=False, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for raw in iter(p.stdout.readline, b""):
            say(raw.decode("utf-8", "replace").rstrip("\n"))
        say(f"ga do: exit {p.wait()}")


# ---- the server -----------------------------------------------------------------------------------------------------

class Console(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cfg: dict[str, Any], *, port: int = 0, token: str | None = None,
                 engine_factory: Callable | None = None, do_argv: Callable[[str], list[str]] | None = None,
                 reader: C.MailReader | None = None, services: Services | None = None,
                 clock: Callable[[], float] = time.time, watch_every_s: float = 1.0, sse_keepalive_s: float = 15.0,
                 health: Callable[[str], bool] | None = None, events_every_s: float = 0.3):
        super().__init__((BIND, int(port)), ConsoleHandler)
        self.cfg, self.clock = cfg, clock
        self.token = token or secrets.token_urlsafe(24)
        self.port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        self.bus = Bus()
        self.reader = reader if reader is not None else C.MailReader.from_config(cfg)
        kw = {"health": health} if health else {}
        self.services = services if services is not None else Services(cfg.get("services") or {},
                                                                        on_event=self.bus.publish, clock=clock, **kw)
        self.asker = Asker(cfg, self.bus, engine_factory=engine_factory, do_argv=do_argv, clock=clock)
        self.watch_every_s, self.sse_keepalive_s = watch_every_s, sse_keepalive_s
        self._stop = threading.Event()
        self._fp: dict[str, Any] | None = None
        self._act: dict[str, int] = {}
        self._act_started = False
        self._watcher: threading.Thread | None = None
        # CMD-GA50: ga.events/1 — the file every ga writes; history in a ring, new lines to the bus as type "ev"
        ef = cfg.get("events") or EV.path()
        self.events_path = Path(ef).expanduser() if ef else None
        self.events_every_s = events_every_s
        self.evring: deque[dict[str, Any]] = deque(maxlen=5000)
        self._evfollow: EV.Follow | None = None

    # -- the live event stream (CMD-GA50 S3) ---------------------------------------------------------------------------
    def events_start(self) -> None:
        if self.events_path is None:
            return
        self.evring.extend(reversed(EV.read(self.events_path, limit=self.evring.maxlen or 5000)))
        self._evfollow = EV.Follow(self.events_path, start_at_end=True)

    def events_once(self) -> int:
        if self._evfollow is None:
            if self.events_path is None:
                return 0
            self._evfollow = EV.Follow(self.events_path, start_at_end=False)
        new = self._evfollow.read()
        for ev in new:
            ev = C.clean(ev)
            self.evring.append(ev)
            self.bus.publish("ev", ev)
        return len(new)

    def events_recent(self, q: dict[str, list[str]]) -> list[dict[str, Any]]:
        lim = (q.get("limit") or ["200"])[0]
        types = ",".join(q.get("type") or []).split(",")
        task = (q.get("task") or [""])[0] or None
        return EV.select(list(self.evring), limit=min(int(lim), 5000) if lim.isdigit() else 200, types=types,
                         task=task)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):  # a closed tab is not an error
            super().handle_error(request, client_address)

    # -- reads ------------------------------------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        return C.state(self.cfg, self.reader, self.services, self.clock)

    def work(self) -> list[dict[str, Any]]:
        return C.work(self.cfg, self.reader, self.services.running_ids())

    # -- the watcher (S4): code finds what changed ---------------------------------------------------------------------
    def watch_once(self) -> list[str]:
        fp = C.fingerprint(self.cfg, self.reader)
        sent: list[str] = []
        old, self._fp = self._fp, fp
        for row in self._act_turns():
            self.bus.publish("act_turn", row)
            sent.append("act_turn")
        if old is None:
            return sent
        if fp["mail"] != old["mail"]:
            self.bus.publish("mail", C.mail(self.reader, 20))
            sent.append("mail")
        if fp != old:
            self.bus.publish("work", self.work())
            self.bus.publish("state", self.state())
            sent += ["work", "state"]
        return sent

    def _act_turns(self) -> list[dict[str, Any]]:
        out = []
        first, self._act_started = not self._act_started, True  # turns before the console started are not news
        for d in (self.cfg.get("token_sources") or {}).get("act", []):
            for f in sorted(Path(d).glob("*.jsonl")) if Path(d).is_dir() else []:
                key, size = str(f), f.stat().st_size
                off = self._act.get(key, size if first else 0)
                if size > off:
                    with f.open("rb") as h:
                        h.seek(off)
                        data = h.read(size - off)
                    end = data.rfind(b"\n")
                    if end >= 0:
                        for line in data[:end + 1].splitlines():
                            try:
                                r = json.loads(line)
                            except ValueError:
                                continue
                            out.append({k: r.get(k) for k in ("item", "turn", "backend", "model", "input", "output",
                                                              "cache_read", "seconds", "applied", "error")})
                        off += end + 1
                self._act[key] = off
        return out

    def start_watcher(self) -> None:
        self.watch_once()
        self.events_start()

        def evloop() -> None:
            while not self._stop.wait(self.events_every_s):
                try:
                    self.events_once()
                except Exception:  # a read that failed this round is retried next round
                    pass
        threading.Thread(target=evloop, daemon=True).start()

        def loop() -> None:
            while not self._stop.wait(self.watch_every_s):
                try:
                    self.watch_once()
                except Exception:  # a read that failed this round is retried next round
                    pass
        self._watcher = threading.Thread(target=loop, daemon=True)
        self._watcher.start()

    def close(self) -> None:
        self._stop.set()
        self.services.shutdown()
        self.server_close()


class ConsoleHandler(UiHandler):
    """GA36's handler (its checks and answers) with the console's routes."""
    server: Console  # type: ignore[assignment]

    def not_found(self) -> None:
        return self.json(404, {"error": "not found"})

    def _static(self, rel: str) -> bool:
        parts = rel.split("/")
        if not rel or any(not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$", p) for p in parts):
            return False
        f = (STATIC / rel).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file() or f.suffix not in CTYPES or f.name == "index.html":
            return False
        self._send(200, f.read_bytes(), CTYPES[f.suffix])
        return True

    def do_GET(self) -> None:
        if not self._host_ok():
            return self.forbidden()
        u = urlparse(self.path)
        path = unquote(u.path)
        if path not in ("/", "/index.html") and not path.startswith("/api/") and self._static(path.lstrip("/")):
            return None  # static files carry no data; the page itself and every API read need the token
        if not self._token_ok(allow_query=True):
            return self.forbidden()
        q = parse_qs(u.query)
        srv = self.server
        if path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), CTYPES[".html"])
        if path == "/api/state":
            return self.json(200, srv.state())
        if path == "/api/work":
            return self.json(200, srv.work())
        if path == "/api/branches":
            return self.json(200, C.branches(srv.cfg))
        if path == "/api/ask/setting":
            return self.json(200, {"confirm_model_turns": srv.asker.confirm_model_turns()})
        if path == "/api/tokens":
            return self.json(200, C.tokens(srv.cfg, srv.reader, clock=srv.clock))
        if path == "/api/decisions":
            return self.json(200, C.decisions(srv.cfg, (q.get("q") or [""])[0]))
        if path == "/api/ops":  # CMD-GA57 S5: the last ops decisions (<ga dir>/ops/decisions.jsonl)
            from ..ops.core import last_decisions
            return self.json(200, last_decisions(srv.cfg.get("ga_dir") or Path(srv.cfg["baseline"]).expanduser().parent / ".ga"))
        if path == "/api/mail":
            lim = (q.get("limit") or ["50"])[0]
            return self.json(200, C.mail(srv.reader, int(lim) if lim.isdigit() else 50))
        if path == "/api/events":
            return self.events(q)
        if path == "/api/events/recent":
            return self.json(200, srv.events_recent(q))
        m = re.match(r"^/api/services/([A-Za-z0-9_.-]+)/logs$", path)
        if m:
            if m.group(1) not in srv.services:
                return self.not_found()
            after = (q.get("after") or ["0"])[0]
            return self.json(200, srv.services.logs(m.group(1), int(after) if after.isdigit() else 0))
        return self.not_found()

    def do_POST(self) -> None:
        if not self._host_ok() or not self._token_ok(allow_query=False):
            return self.forbidden()
        try:
            body = self._body()
        except (ValueError, UnicodeDecodeError):
            return self.json(400, {"error": "bad body"})
        path = urlparse(self.path).path
        srv = self.server
        m = re.match(r"^/api/(?:services/([A-Za-z0-9_.-]+)|(bridge))/(start|stop)$", path)
        if m:
            name = m.group(1) or "bridge"
            if name not in srv.services or (m.group(1) == "bridge"):
                return self.not_found()
            fn = srv.services.start if m.group(3) == "start" else srv.services.stop
            return self.json(200, fn(name))
        if path == "/api/ask":
            code, obj = srv.asker.plan(str(body.get("q") or ""), str(body.get("mode") or "ask"))
            return self.json(code, obj)
        if path == "/api/ask/setting":
            srv.asker.set_confirm_model_turns(bool(body.get("confirm_model_turns")))
            return self.json(200, {"confirm_model_turns": srv.asker.confirm_model_turns()})
        if path == "/api/ask/confirm":
            code, obj = srv.asker.confirm(str(body.get("confirm_id") or ""))
            return self.json(code, obj)
        return self.not_found()

    def events(self, q: dict[str, list[str]]) -> None:
        srv = self.server
        raw = self.headers.get("Last-Event-ID") or (q.get("after") or [""])[0]
        last = int(raw) if raw.isdigit() else None
        self.send_response(200)
        for k, v in HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            if last is None or srv.bus.after(last)[1]:  # a new reader, or one that missed events: a full state first
                last = srv.bus.n if last is None else last  # a gap: the state, then what the ring still holds
                self._event(srv.bus.n, "state", C.clean(srv.state()))
            while not srv._stop.is_set():
                evs, _gap = srv.bus.after(last)
                for n, t, d in evs:
                    self._event(n, t, d)
                    last = n
                if not evs:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    srv.bus.wait(last, srv.sse_keepalive_s)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _event(self, n: int, t: str, d: Any) -> None:
        self.wfile.write(f"id: {n}\nevent: {t}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()


# ---- the command ------------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["init"]:
        ap = argparse.ArgumentParser(prog="ga console init", description="write the default GA Console config "
                                     "(the Mac layout: ~/baseline, ~/token, ~/ga-sdk-check, the agy bridge)")
        ap.add_argument("--config", default=K.DEFAULT_PATH)
        ap.add_argument("--force", action="store_true")
        a = ap.parse_args(argv[1:])
        try:
            p = K.init(a.config, force=a.force)
        except K.ConfigError as e:
            print(f"ga console init: {e}", file=sys.stderr)
            return 2
        print(f"ga console init: wrote {p} — check the paths, then: ga console")
        return 0
    ap = argparse.ArgumentParser(prog="ga console", description="GA Console: the GA API server on 127.0.0.1 "
                                 "(`ga console init` writes the config first)")
    ap.add_argument("--config", default=K.DEFAULT_PATH)
    ap.add_argument("--port", type=int, default=0, help="the port on 127.0.0.1 (default: a free one)")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser")
    a = ap.parse_args(argv)
    try:
        cfg = K.load(a.config)
    except K.ConfigError as e:
        print(f"ga console: {e}", file=sys.stderr)
        return 2
    srv = Console(cfg, port=a.port)
    srv.start_watcher()
    print(f"GA Console: {srv.url}", flush=True)  # the one place the token is shown
    print("(이 주소는 이 컴퓨터에서만 열립니다. 끝내려면 Ctrl+C — 콘솔이 띄운 서비스도 함께 멈춥니다)", flush=True)
    if not a.no_open:
        import webbrowser
        webbrowser.open(srv.url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
    return 0
