"""``ga ui`` (CMD-GA36 S4): GA UI — a local browser page over the same engine as ``ga ask`` (the first form of GA API).

- standard library http server on 127.0.0.1 only, a random port
- a one-time token in the opened URL: printed once, never logged, never written to a file; every request but the two
  static files needs it (query ``t`` for GET and SSE, header ``X-GA-Token`` for POST)
- Host must be 127.0.0.1:<port> or localhost:<port>; an Origin, when sent, must be that origin; a cross-site fetch
  (Sec-Fetch-Site) is refused — all 403
- GET reads, POST + token for actions; a model or mail action without a confirmation answers 409 with what it would
  run and cost (the engine's own guard refuses it too)
- strict CSP (no inline script, nothing remote), Korean labels; the run log streams over SSE from the run log file,
  resumable by Last-Event-ID — never from a model; secret-looking text is shown as withheld
"""
from __future__ import annotations

import argparse
import hmac
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..runlog import RunLog, redact

BIND = "127.0.0.1"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
STATIC = Path(__file__).resolve().parent
HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
           "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Cross-Origin-Resource-Policy": "same-origin"}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, engine_factory: Callable[[Callable[[str], None]], Any], *, token: str | None = None,
                 sse_poll_s: float = 0.2, sse_idle_s: float = 15.0):
        super().__init__((BIND, 0), Handler)
        self.token = token or secrets.token_urlsafe(24)
        self.engine_factory, self.sse_poll_s, self.sse_idle_s = engine_factory, sse_poll_s, sse_idle_s
        self.port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        self.threads: dict[str, threading.Thread] = {}
        self.lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def engine(self, out: Callable[[str], None] = lambda s: None) -> Any:
        return self.engine_factory(out)

    def start_run(self, kind: str, fn: Callable[[Any, RunLog], None]) -> str:
        eng = self.engine()
        log = eng.new_log(kind)

        def work() -> None:
            eng.log = log
            try:
                fn(eng, log)
            except Exception as e:  # shown in the log, never retried by asking a model
                log.write(f"오류: {type(e).__name__}: {str(e)[:200]}")
            finally:
                if not log.done_path.exists():
                    log.close()
        th = threading.Thread(target=work, daemon=True)
        with self.lock:
            self.threads[log.id] = th
        th.start()
        return log.id


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # the request line holds the token: nothing is logged
        return

    # -- answers -------------------------------------------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        for k, v in HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def forbidden(self) -> None:
        self._send(403, b"forbidden\n", "text/plain; charset=utf-8")

    # -- checks ----------------------------------------------------------------------------------------------------
    def _host_ok(self) -> bool:
        if self.headers.get("Host", "") not in self.server.hosts:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.origins:
            return False
        return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    def _token_ok(self, allow_query: bool) -> bool:
        got = self.headers.get("X-GA-Token", "")
        if not got and allow_query:
            got = (parse_qs(urlparse(self.path).query).get("t") or [""])[0]
        return bool(got) and hmac.compare_digest(got.encode(), self.server.token.encode())

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b"{}"
        obj = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(obj, dict):
            raise ValueError("body must be an object")
        return obj

    # -- GET: reads ------------------------------------------------------------------------------------------------
    def do_GET(self) -> None:
        if not self._host_ok():
            return self.forbidden()
        path = urlparse(self.path).path
        if path in ("/app.js", "/app.css"):  # static, no data in them
            ctype = "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
            return self._send(200, (STATIC / path[1:]).read_bytes(), ctype)
        if not self._token_ok(allow_query=True):
            return self.forbidden()
        if path == "/":
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.json(200, state(self.server.engine()))
        if path == "/api/log":
            return self.sse()
        return self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def sse(self) -> None:
        q = parse_qs(urlparse(self.path).query)
        rid = (q.get("run") or [""])[0]
        eng = self.server.engine()
        log = RunLog(eng.runs, rid) if rid and all(c.isalnum() or c in "-_" for c in rid) else None
        if log is None or not log.path.exists():
            return self._send(404, b"no such run\n", "text/plain; charset=utf-8")
        try:
            offset = int(self.headers.get("Last-Event-ID") or (q.get("from") or ["0"])[0])
        except ValueError:
            offset = 0
        self.send_response(200)
        for k, v in HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        idle = 0.0
        try:
            while True:
                done = log.done_path.exists()
                with log.path.open("rb") as f:
                    f.seek(offset)
                    data = f.read()
                end = data.rfind(b"\n")
                if end >= 0:
                    for raw in data[:end + 1].splitlines(keepends=True):
                        offset += len(raw)
                        text = redact(raw.decode("utf-8", "replace").rstrip("\n"))
                        self.wfile.write(f"id: {offset}\ndata: {json.dumps(text, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                    idle = 0.0
                elif done:
                    status = log.done_path.read_text(encoding="utf-8").strip()
                    self.wfile.write(f"event: end\ndata: {json.dumps(status)}\n\n".encode())
                    self.wfile.flush()
                    return
                else:
                    idle += self.server.sse_poll_s
                    if idle >= self.server.sse_idle_s:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        idle = 0.0
                    time.sleep(self.server.sse_poll_s)
        except (BrokenPipeError, ConnectionResetError):
            return

    # -- POST: actions ---------------------------------------------------------------------------------------------
    def do_POST(self) -> None:
        if not self._host_ok() or not self._token_ok(allow_query=False):
            return self.forbidden()
        try:
            body = self._body()
        except (ValueError, UnicodeDecodeError):
            return self.json(400, {"error": "bad body"})
        path = urlparse(self.path).path
        if path == "/api/route":
            return self.json(200, route(self.server.engine(), str(body.get("q") or "")))
        if path == "/api/run":
            return self.run_intent(body)
        if path == "/api/model":
            return self.run_model(body)
        if path == "/api/solve":
            return self.run_solve(body)
        return self.json(404, {"error": "not found"})

    def run_intent(self, body: dict[str, Any]) -> None:
        from ..ask import intents as I
        from ..ask import needs_confirm
        name = str(body.get("intent") or "")
        if name not in I.BY_NAME:
            return self.json(400, {"error": "unknown intent"})
        eng = self.server.engine()
        p = eng.prepare(name)
        if p["refuse"]:
            return self.json(409, {"refuse": redact(p["refuse"])})
        confirmed = body.get("confirm") is True
        if needs_confirm(p["cost"]) and not confirmed:
            return self.json(409, {"needs_confirm": True, "lines": [redact(x) for x in p["lines"]], "cost": p["cost"]})
        rid = self.server.start_run(name, lambda e, log: e.execute(name, confirmed=confirmed))
        return self.json(200, {"run": rid})

    def run_model(self, body: dict[str, Any]) -> None:
        q = str(body.get("q") or "").strip()
        if not q:
            return self.json(400, {"error": "empty question"})
        eng = self.server.engine()
        p = eng.prepare_model(q)
        if p["refuse"]:
            return self.json(409, {"refuse": p["refuse"]})
        if body.get("confirm") is not True:
            return self.json(409, {"needs_confirm": True, "lines": [redact(x) for x in p["lines"]], "cost": "model"})
        rid = self.server.start_run("model", lambda e, log: e.run_model(e.prepare_model(q), confirmed=True))
        return self.json(200, {"run": rid})

    def run_solve(self, body: dict[str, Any]) -> None:
        from ..ask.solve import SolveRefused
        q = str(body.get("q") or "").strip()
        if not q:
            return self.json(400, {"error": "empty question"})
        kw = {"backend": body.get("backend") or None, "model": body.get("model") or None,
              "cap": int(body["cap"]) if str(body.get("cap") or "").isdigit() else None,
              "base_url": body.get("base_url") or None}
        if body.get("confirm") is not True:  # the same path, told no: it shows the plan and runs nothing
            seen: list[str] = []
            try:
                self.server.engine().solve(q, lambda lines: seen.extend(lines) or False, **kw)
            except SolveRefused as e:
                if not seen:
                    return self.json(409, {"refuse": str(e)})
            return self.json(409, {"needs_confirm": True, "lines": [redact(x) for x in seen], "cost": "model"})
        eng = self.server.engine()
        log = eng.new_log("solve")

        def work() -> None:
            try:
                eng.solve(q, lambda lines: True, runlog=log, **kw)
            except SolveRefused as e:
                log.write(str(e))
            except Exception as e:
                log.write(f"오류: {type(e).__name__}: {str(e)[:200]}")
            finally:
                if not log.done_path.exists():
                    log.close("failed")
        log.write("solve 준비 중…")
        th = threading.Thread(target=work, daemon=True)
        with self.server.lock:
            self.server.threads[log.id] = th
        th.start()
        return self.json(200, {"run": log.id})


def state(eng: Any) -> dict[str, Any]:
    from ..ask import intents as I
    d = eng.ledger.day()
    return {"today": d, "agy_turns": eng.day.used(), "cap": eng.day.cap, "last": eng.last_run(),
            "solve": {k: v for k, v in eng.settings["solve"].items() if k in ("backend", "model", "cap", "base_url")},
            "intents": [{"name": i.name, "label": i.label, "cost": i.cost, "example": i.examples[0]} for i in I.TABLE]}


def route(eng: Any, q: str) -> dict[str, Any]:
    from ..ask import intents as I
    from ..ask import needs_confirm
    m = I.route(q)
    if m.intent is None:
        return {"intent": None, "suggestions": [{"name": n, "label": I.BY_NAME[n].label} for n in m.suggestions]}
    p = eng.prepare(m.intent.name)
    return {"intent": m.intent.name, "label": m.intent.label, "cost": p["cost"],
            "needs_confirm": needs_confirm(p["cost"]), "lines": [redact(x) for x in p["lines"]],
            "refuse": redact(p["refuse"]) if p["refuse"] else None}


def main(argv: list[str] | None = None, *, engine_factory: Callable | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ga ui", description="GA UI: a local page (127.0.0.1 only) over the ga ask engine")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser")
    a = ap.parse_args(argv)
    if engine_factory is None:
        from ..ask import Engine
        engine_factory = lambda out: Engine(out=out)  # noqa: E731
    srv = Server(engine_factory)
    print(f"GA UI: {srv.url}", flush=True)  # the one place the token is shown
    print("(이 주소는 이 컴퓨터에서만 열립니다. 끝내려면 Ctrl+C)", flush=True)
    if not a.no_open:
        import webbrowser
        webbrowser.open(srv.url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
