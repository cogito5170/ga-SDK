"""CMD-CON2 D1: GA Console's services — start / stop / log / health with a fake process, never through a shell, the env
file's values only in the child (never in a response, a log line or an event), SIGTERM then SIGKILL."""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_world import ENV_VALUE, FAKE_SECRET, World  # noqa: E402
from test_console_api import Base  # noqa: E402

from ga.console import services as SV  # noqa: E402
from ga.runlog import WITHHELD  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class Mgr(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(shutil.rmtree, self.w.tmp, True)
        self.events = []

    def mgr(self, specs, **kw):
        m = SV.Services(specs, on_event=lambda t, d: self.events.append((t, d)), **kw)
        self.addCleanup(m.shutdown)
        return m

    def text(self, m, name):
        return [x["text"] for x in m.logs(name)["lines"]]

    def test_start_log_stop(self):
        m = self.mgr({"svc": self.w.svc("run", "a b", "$HOME;echo injected", "`id`")})
        snap = m.start("svc")
        self.assertEqual(snap["state"], "running")
        self.assertIsInstance(snap["pid"], int)
        self.assertIsNotNone(snap["started_at"])
        self.assertTrue(wait(lambda: "worker ready" in self.text(m, "svc")))
        # the argv reached the child word for word: no shell expanded or split anything
        self.assertIn("argv:a b|$HOME;echo injected|`id`", self.text(m, "svc"))
        self.assertNotIn("injected", "\n".join(t for t in self.text(m, "svc") if not t.startswith("argv:")))
        self.assertIn("has FAKE_TOKEN: True", self.text(m, "svc"))  # the env file reached the child
        logs = m.logs("svc")
        later = m.logs("svc", after=logs["next"] - 1)
        self.assertEqual(len(later["lines"]), 1)
        self.assertEqual(set(later["lines"][0]), {"n", "at", "stream", "text"})
        pid = snap["pid"]
        out = m.stop("svc")
        self.assertEqual((out["state"], out["pid"]), ("stopped", None))
        self.assertTrue(wait(lambda: _gone(pid), 5))
        self.assertIn("service", [t for t, _d in self.events])

    def test_popen_is_never_given_a_shell(self):
        seen = []
        real = subprocess.Popen

        def spy(*a, **k):
            seen.append((a, k))
            return real(*a, **k)
        m = self.mgr({"svc": self.w.svc("run")})
        with mock.patch.object(SV.subprocess, "Popen", spy):
            m.start("svc")
        (args, kw), = seen
        self.assertIsInstance(args[0], list)
        self.assertIs(kw.get("shell"), False)
        self.assertNotIn(args[0][0], ("sh", "/bin/sh", "bash", "/bin/bash", "zsh"))

    def test_env_values_never_logged_or_returned(self):
        other = "ghp_" + "Fake0123456789" * 3  # secret-looking, not from the env file: withheld by rule R6's patterns
        m = self.mgr({"svc": self.w.svc("leak", other)})
        m.start("svc")
        self.assertTrue(wait(lambda: "worker ready" in self.text(m, "svc")))
        m.stop("svc")
        blob = json.dumps([m.logs("svc"), m.snapshot(), self.events])
        self.assertNotIn(ENV_VALUE, blob)
        self.assertNotIn(FAKE_SECRET[:20], blob)
        self.assertNotIn(other[:12], blob)
        self.assertIn("value is " + SV.MASK, self.text(m, "svc"))
        self.assertIn(WITHHELD, self.text(m, "svc"))
        self.assertIn("console: env file loaded (2 names, values not shown)", self.text(m, "svc"))
        self.assertNotIn("FAKE_TOKEN", os.environ)  # loaded into the child only

    def test_health_polled(self):
        port = free_port()
        spec = self.w.svc("serve", str(port), health_port=port)
        m = self.mgr({"api": spec}, health_every_s=0.05)
        self.assertEqual(m.start("api")["state"], "starting")
        self.assertTrue(wait(lambda: m.get("api")["health"] == "ok"), m.logs("api"))
        self.assertEqual(m.get("api")["state"], "running")
        self.assertEqual(m.get("api")["port"], port)
        m.stop("api")
        self.assertEqual(m.get("api")["state"], "stopped")
        self.assertTrue(SV.http_ok(f"http://127.0.0.1:{port}/") is False)

    def test_health_down_keeps_starting(self):
        port = free_port()
        m = self.mgr({"api": self.w.svc("run", health_port=port)}, health=lambda url: False, health_every_s=0.05)
        m.start("api")
        self.assertTrue(wait(lambda: m.get("api")["health"] == "down"))
        self.assertEqual(m.get("api")["state"], "starting")

    def test_ready_line(self):
        m = self.mgr({"worker": self.w.svc("run", ready="worker ready")})
        self.assertEqual(m.start("worker")["state"], "starting")
        self.assertTrue(wait(lambda: m.get("worker")["state"] == "running"))

    def test_exit_is_failed(self):
        m = self.mgr({"svc": self.w.svc("exit1")})
        m.start("svc")
        self.assertTrue(wait(lambda: m.get("svc")["state"] == "failed"))
        self.assertIn("console: exited with 1", self.text(m, "svc"))

    def test_missing_program_is_failed(self):
        m = self.mgr({"svc": {"argv": [str(self.w.tmp / "no-such-program")], "cwd": str(self.w.tmp)}})
        self.assertEqual(m.start("svc")["state"], "failed")

    def test_stop_is_terminate_then_kill(self):
        m = self.mgr({"svc": self.w.svc("stubborn", env_file=False)}, grace_s=0.3)
        m.start("svc")
        self.assertTrue(wait(lambda: "worker ready" in self.text(m, "svc")))
        t0 = time.time()
        self.assertEqual(m.stop("svc")["state"], "stopped")
        self.assertLess(time.time() - t0, 5)
        self.assertIn("console: still running after SIGTERM; SIGKILL", self.text(m, "svc"))

    def test_start_twice_is_one_process(self):
        m = self.mgr({"svc": self.w.svc("run")})
        a = m.start("svc")["pid"]
        self.assertEqual(m.start("svc")["pid"], a)


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        return "Z" in open(f"/proc/{pid}/stat").read().split()[2]
    except OSError:
        return True


class Api(Base):
    @property
    def services(self):
        return {"fake-api": World.svc(self.w, "leak")}

    def test_start_stop_logs_through_the_api_never_show_env_values(self):
        st, snap = self.post("/api/services/fake-api/start")
        self.assertEqual(st, 200)
        self.assertEqual(set(snap), {"name", "state", "port", "health", "started_at", "pid"})
        self.assertTrue(wait(lambda: "worker ready" in [x["text"] for x in self.get(
            "/api/services/fake-api/logs")["lines"]]))
        self.assertEqual(self.get("/api/state")["counts"]["services_running"], 1)
        logs = self.get("/api/services/fake-api/logs?after=2")
        self.assertTrue(all(x["n"] > 2 for x in logs["lines"]))
        self.assertEqual(set(logs), {"lines", "next"})
        self.assertEqual(self.post("/api/services/fake-api/stop")[1]["state"], "stopped")
        self.assertEqual(self.post("/api/services/nope/start")[0], 404)
        self.assertEqual(self.post("/api/services/bridge/start")[0], 404)  # the bridge only by /api/bridge
        blob = b""
        for path in ("/api/state", "/api/services/fake-api/logs", "/api/work", "/api/tokens"):
            blob += self.req("GET", path)[2]
        blob += json.dumps([list(e) for e in self.srv.bus.ring], ensure_ascii=False).encode()
        self.assertNotIn(ENV_VALUE.encode(), blob)
        self.assertNotIn(FAKE_SECRET[:20].encode(), blob)
        self.assertIn(b"value is " + SV.MASK.encode(), blob)

    def test_service_events_reach_sse(self):
        last = self.srv.bus.n
        self.post("/api/services/fake-api/start")
        evs = self.events(last_id=last, want=3)
        self.assertIn("service", [e["event"] for e in evs])
        self.assertIn("log", [e["event"] for e in evs])
        self.post("/api/services/fake-api/stop")

    def test_closing_the_console_stops_its_children(self):
        pid = self.post("/api/services/fake-api/start")[1]["pid"]
        self.srv.services.shutdown()
        self.assertTrue(wait(lambda: _gone(pid), 10))


if __name__ == "__main__":
    unittest.main()
