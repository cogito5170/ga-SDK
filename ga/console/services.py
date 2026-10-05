"""GA Console's service manager (CMD-CON2 S3): the named services of the console config as children of the console.

- started from a fixed argv, never through a shell (``shell=False``), in their own process group
- the env file is read into that child's environment only; its values are never logged or returned, and a line a
  child prints that holds one is shown with the value replaced
- stdout and stderr go to a ring buffer per service (numbered lines, ``logs(after=n)``); secret-looking lines withheld
- health: the health URL polled (127.0.0.1 only, no proxy), or a ready line, or alive; stop = SIGTERM to the group,
  then SIGKILL after a grace period
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Callable

from ..runlog import redact
from .collectors import iso
from .config import read_env_file

RING = 2000
MASK = "‹env›"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # a local health URL never goes to a proxy


def http_ok(url: str, timeout: float = 1.5) -> bool:
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


class Service:
    def __init__(self, name: str, spec: dict[str, Any]):
        self.name, self.spec = name, spec
        self.state, self.health = "stopped", "unknown"
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.lines: deque[dict[str, Any]] = deque(maxlen=RING)
        self.n = 0
        self.stopping = False
        self.masks: list[str] = []
        self.lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "port": self.spec.get("port"), "health": self.health,
                "started_at": iso(self.started_at), "pid": self.proc.pid if self.proc and self.state in
                ("starting", "running") else None}


class Services:
    def __init__(self, specs: dict[str, dict[str, Any]], *, on_event: Callable[[str, dict], None] = lambda t, d: None,
                 clock: Callable[[], float] = time.time, health: Callable[[str], bool] = http_ok,
                 health_every_s: float = 2.0, grace_s: float = 5.0):
        self.svc = {n: Service(n, s) for n, s in specs.items()}
        self.on_event, self.clock, self.health_fn = on_event, clock, health
        self.health_every_s, self.grace_s = health_every_s, grace_s
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None

    # -- reads ---------------------------------------------------------------------------------------------------------
    def names(self) -> list[str]:
        return list(self.svc)

    def __contains__(self, name: str) -> bool:
        return name in self.svc

    def get(self, name: str) -> dict[str, Any]:
        return self.svc[name].snapshot()

    def snapshot(self) -> list[dict[str, Any]]:
        return [s.snapshot() for s in self.svc.values()]

    def logs(self, name: str, after: int = 0) -> dict[str, Any]:
        s = self.svc[name]
        with s.lock:
            lines = [dict(x) for x in s.lines if x["n"] > after]
            nxt = s.n
        return {"lines": lines, "next": nxt}

    def last_line(self, name: str, prefix: str = "") -> dict[str, Any] | None:
        s = self.svc[name]
        with s.lock:
            for x in reversed(s.lines):
                if x["text"].startswith(prefix):
                    return dict(x)
        return None

    def running_ids(self) -> set[str]:
        """Directive ids the running bridge's newest 'bridge:' line names (code, not a model)."""
        import re
        if "bridge" not in self.svc or self.svc["bridge"].state not in ("starting", "running"):
            return set()
        last = self.last_line("bridge", "bridge:")
        return set(re.findall(r"\b(?:CMD|ITEM)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b", last["text"])) if last else set()

    # -- the log ring --------------------------------------------------------------------------------------------------
    def _line(self, s: Service, stream: str, text: str) -> None:
        text = text.rstrip("\r\n")
        for v in s.masks:
            text = text.replace(v, MASK)
        text = redact(text)
        with s.lock:
            s.n += 1
            row = {"n": s.n, "at": iso(self.clock()), "stream": stream, "text": text}
            s.lines.append(row)
        self.on_event("log", {"service": s.name, **row})
        if s.name == "bridge" and text.startswith("bridge:"):
            self.on_event("bridge", {"last_seen": row["at"], "last_message": text})
        ready = s.spec.get("ready_line")
        if ready and s.state == "starting" and ready in text:
            self._set(s, "running", "ok" if not s.spec.get("health_url") else s.health)

    def _set(self, s: Service, state: str, health: str | None = None) -> None:
        changed = s.state != state or (health is not None and s.health != health)
        s.state = state
        if health is not None:
            s.health = health
        if changed:
            self.on_event("service", s.snapshot())

    def _note(self, s: Service, text: str) -> None:
        self._line(s, "console", text)

    # -- start / stop --------------------------------------------------------------------------------------------------
    def start(self, name: str) -> dict[str, Any]:
        s = self.svc[name]
        if s.state in ("starting", "running") and s.proc and s.proc.poll() is None:
            return s.snapshot()
        spec = s.spec
        env = dict(os.environ)
        s.masks = []
        if spec.get("env_file"):
            try:
                extra = read_env_file(spec["env_file"])
            except OSError as e:
                self._note(s, f"console: env file not readable ({type(e).__name__}); not started")
                self._set(s, "failed", "unknown")
                return s.snapshot()
            env.update(extra)
            s.masks = sorted((v for v in extra.values() if len(v) >= 4), key=len, reverse=True)
            self._note(s, f"console: env file loaded ({len(extra)} names, values not shown)")
        try:
            proc = subprocess.Popen(list(spec["argv"]), cwd=spec.get("cwd") or None, env=env, shell=False,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
        except OSError as e:
            self._note(s, f"console: could not start {spec['argv'][0]}: {type(e).__name__}: {e.strerror or ''}")
            self._set(s, "failed", "unknown")
            return s.snapshot()
        s.proc, s.started_at, s.stopping = proc, self.clock(), False
        self._note(s, f"console: started pid {proc.pid}")
        waits_for = spec.get("health_url") or spec.get("ready_line")
        self._set(s, "starting" if waits_for else "running", "unknown")
        for stream, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            threading.Thread(target=self._read, args=(s, stream, pipe), daemon=True).start()
        threading.Thread(target=self._wait, args=(s, proc), daemon=True).start()
        if spec.get("health_url"):
            self.ensure_poller()
        return s.snapshot()

    def _read(self, s: Service, stream: str, pipe: Any) -> None:
        for raw in iter(pipe.readline, b""):
            self._line(s, stream, raw.decode("utf-8", "replace"))
        pipe.close()

    def _wait(self, s: Service, proc: subprocess.Popen) -> None:
        rc = proc.wait()
        if s.proc is not proc:
            return
        self._note(s, f"console: exited with {rc}")
        self._set(s, "stopped" if s.stopping or rc == 0 else "failed", "down" if s.spec.get("health_url") else "unknown")

    def stop(self, name: str) -> dict[str, Any]:
        s = self.svc[name]
        proc = s.proc
        if proc is None or proc.poll() is not None:
            self._set(s, "stopped" if s.state != "failed" else "failed")
            return s.snapshot()
        s.stopping = True
        self._signal(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=self.grace_s)
        except subprocess.TimeoutExpired:
            self._note(s, "console: still running after SIGTERM; SIGKILL")
            self._signal(proc, signal.SIGKILL)
            proc.wait(timeout=5)
        for _ in range(100):  # the waiter thread records the exit
            if s.state == "stopped":
                break
            time.sleep(0.01)
        return s.snapshot()

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

    def shutdown(self) -> None:
        self._stop.set()
        for n, s in self.svc.items():
            if s.proc is not None and s.proc.poll() is None:
                self.stop(n)

    # -- health ----------------------------------------------------------------------------------------------------------
    def poll_health(self) -> None:
        for s in self.svc.values():
            url = s.spec.get("health_url")
            if not url or s.state not in ("starting", "running"):
                continue
            ok = self.health_fn(url)
            if ok:
                self._set(s, "running", "ok")
            else:
                self._set(s, s.state, "down")

    def ensure_poller(self) -> None:
        if self._poller is not None and self._poller.is_alive():
            return

        def loop() -> None:
            while not self._stop.wait(self.health_every_s):
                self.poll_health()
        self._poller = threading.Thread(target=loop, daemon=True)
        self._poller.start()
