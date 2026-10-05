"""CMD-GA43: GA Console follow-ups from BD-445. 0 model calls, 0 network beyond 127.0.0.1.

D1  the bridge runs under the Python that ran `ga console init`; the judge's undefined-var check fails a page whose CSS
    uses an undefined var() and passes every golden and the real app; a page holding an open event stream is judged
    in bounded time with the stream left out of the weight; the mutation generator reaches JavaScript held in Python
    strings (>= 3 JS mutations inside judge.py on the CON1 rev 2 diff)
D2  the mutants named by the directive die here
"""
from __future__ import annotations

import http.server
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "console_frontend"))

from ga.console import config as K  # noqa: E402
from ga.console import judge as J  # noqa: E402
from ga.verify import mutate as M  # noqa: E402

GOLDEN = ROOT / "ga" / "console" / "golden"
STATIC = ROOT / "ga" / "console" / "static"
CON1_REV2 = ("507da93", "068681c")
GONE = '<style>.gone{border-top:2px solid var(--gone);padding-top:8px}</style><p class="gone">사라진 선이 있는 문단</p>'
WHY = J.available()


def git(*a):
    return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True)


def measure_bounded(target: str, limit_s: float, **kw) -> "tuple[dict | None, float]":
    """the verdict, or None when the judge did not finish within limit_s (it runs in its own thread, kept on
    measure_bounded.last so a caller can wait for a hung one after ending what it waits on)"""
    out: dict = {}
    t0 = time.time()
    th = threading.Thread(target=lambda: out.update(J.measure(target, **kw)), daemon=True)
    th.start()
    measure_bounded.last = th
    th.join(limit_s)
    return (out if not th.is_alive() else None), time.time() - t0


class S5Version(unittest.TestCase):
    def test_version_0_11_0_in_both_places(self):
        import ga
        self.assertEqual(ga.__version__, "0.11.0")
        self.assertIn('version = "0.11.0"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))


class D1Bridge(unittest.TestCase):
    def test_bridge_argv0_is_the_python_running_init(self):
        argv = K.default()["bridge"]["argv"]
        self.assertEqual(argv[0], sys.executable)
        self.assertNotEqual(argv[0], "python3")
        self.assertEqual(argv[1:], ["~/baseline/ops/agy_bridge/bridge.py", "--config", "~/agy-bridge.json"])


class D1JsMutations(unittest.TestCase):
    def test_heuristic_holds_for_every_judge_script(self):
        for name in ("_JS_TEXT", "_JS_DOC", "_JS_PIXELS", "_JS_LINES", "_JS_VARS"):
            self.assertTrue(M.is_js(getattr(J, name)), name)
        self.assertFalse(M.is_js("const width = 3 => the value"))          # no return
        self.assertFalse(M.is_js(M.__doc__))

    def test_each_operator_on_the_judge_scripts(self):
        ops = {}
        for name in ("_JS_TEXT", "_JS_DOC", "_JS_PIXELS", "_JS_LINES"):
            for op, i, j, new, _ in M.js_edits(getattr(J, name)):
                ops.setdefault(op, []).append((name, getattr(J, name)[i:j], new))
        self.assertEqual(set(ops), {"js-compare", "js-not", "js-term", "js-limit"})
        self.assertIn(("_JS_TEXT", "12", "(12 * 1000)"), ops["js-limit"])           # fs < 12: the small-text limit
        self.assertIn(("_JS_TEXT", "24", "1"), ops["js-limit"])                     # fs >= 24: large text
        self.assertIn(("_JS_PIXELS", ">", ">="), ops["js-compare"])                 # ch > limit -> ch >= limit
        self.assertTrue(any(n == "_JS_LINES" and s == "!" for n, s, _ in ops["js-not"]))
        self.assertTrue(any(s.startswith("&& ") for _, s, _ in ops["js-term"]))
        for name, s, _ in ops["js-compare"]:
            self.assertNotIn(s, ("=>",), name)                                          # an arrow is not a comparison

    def test_quoted_js_strings_are_left_alone(self):
        code = "const f = x => { const t = 'a>b && c'; return !x && t > 3; };"
        edits = list(M.js_edits(code))
        for _, i, j, _, _ in edits:
            self.assertFalse(code.index("'a>b") <= i < code.index("c'") + 2, code[i:j])
        self.assertIn(("js-limit", "1"), [(e[0], e[3]) for e in edits])
        self.assertEqual(sorted(code[i:j] for op, i, j, _, _ in edits if op == "js-term"), ["!x && ", "&& t > 3"])
        self.assertIn(("js-not", "!"), [(e[0], code[e[1]:e[2]]) for e in edits])

    def test_mutated_file_still_parses_and_finds_are_unique(self):
        text = (ROOT / "ga" / "console" / "judge.py").read_text(encoding="utf-8")
        lines = set(range(1, text.count("\n") + 2))
        muts = [m for m in M.file_mutations("ga/console/judge.py", text, lines) if m["op"].startswith("js-")]
        self.assertGreater(len(muts), 20)
        for m in muts:
            self.assertEqual(text.count(m["find"]), 1, m["id"])
            import ast
            ast.parse(text.replace(m["find"], m["replace"]))

    def test_con1_rev2_diff_has_js_mutations_in_judge(self):
        for sha in CON1_REV2:
            if git("cat-file", "-e", f"{sha}^{{commit}}").returncode:
                if git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
                    git("fetch", "--quiet", "--unshallow", "origin")
                git("fetch", "--quiet", "origin")
        if any(git("cat-file", "-e", f"{s}^{{commit}}").returncode for s in CON1_REV2):
            self.skipTest("CON1 rev 2 history not reachable from origin")
        spec = M.mutations(ROOT, *CON1_REV2)
        js = [m for m in spec if m["file"] == "ga/console/judge.py" and m["op"].startswith("js-")]
        self.assertGreaterEqual(len(js), 3, [m["id"] for m in spec])
        self.assertGreaterEqual(len({m["op"] for m in js}), 3)
        self.assertEqual(len({m["id"] for m in spec}), len(spec))


@unittest.skipIf(WHY, f"judge unavailable: {WHY}")
class D1UndefinedVar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "console"
        shutil.copytree(STATIC, cls.root / "static")
        shutil.copytree(GOLDEN, cls.root / "golden")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def plant(self, name: str, snippet: str) -> dict:
        page = self.root / "golden" / f"plant-{name}.html"
        base = (self.root / "golden" / "now.html").read_text(encoding="utf-8")
        page.write_text(base.replace("</main>", snippet + "\n</main>"), encoding="utf-8")
        return J.measure(str(page))

    def test_gone_var_on_a_visible_border_fails_exactly_undefined_var(self):
        r = self.plant("gone", GONE)
        self.assertEqual(r["failed"], ["undefined-var"], json.dumps(r["facts"], ensure_ascii=False)[:600])
        self.assertEqual(r["facts"]["undefined-vars"], ["--gone"])
        self.assertEqual(r["facts"]["undefined-var-rules"][0]["rule"], ".gone")

    def test_a_defined_var_or_a_fallback_passes(self):
        for i, css in enumerate(("border-top:2px solid var(--gone, transparent)", "color:var(--ink)")):
            with self.subTest(css=css):
                r = self.plant(f"ok-{i}", GONE.replace("border-top:2px solid var(--gone)", css))
                self.assertTrue(r["pass"], (r["failed"], r["facts"]["undefined-vars"]))

    def test_a_rule_matching_nothing_rendered_passes(self):
        r = self.plant("unused", GONE.replace('class="gone"', 'class="kept"'))
        self.assertTrue(r["pass"], r["failed"])

    def test_mutant_check_disabled_survives_not(self):
        saved = J._JS_VARS
        J._JS_VARS = "() => []"
        try:
            r = self.plant("gone-mutant", GONE)
        finally:
            J._JS_VARS = saved
        self.assertNotEqual(r["failed"], ["undefined-var"])       # under the mutant the plant passes: the test above dies


class _Stream(http.server.BaseHTTPRequestHandler):
    PAGE = ""
    stop = threading.Event()

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            n = 0
            try:
                while not self.stop.is_set():
                    self.wfile.write(f"id: {n}\ndata: {{\"n\": {n}}}\n\n".encode())
                    self.wfile.flush()
                    n += 1
                    time.sleep(0.2)
            except OSError:
                pass
            return
        name = self.path.split("?")[0].lstrip("/") or "page.html"
        f = Path(self.PAGE) / name
        if not f.is_file():
            self.send_error(404)
            return
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/css" if name.endswith(".css") else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipIf(WHY, f"judge unavailable: {WHY}")
class D1EventStream(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        shutil.copytree(GOLDEN, d / "golden")
        shutil.copytree(STATIC, d / "static")
        html = (d / "golden" / "now.html").read_text(encoding="utf-8")
        (d / "golden" / "page.html").write_text(html.replace(
            "</body>", "<script>new EventSource('/api/events')</script>\n</body>"), encoding="utf-8")
        _Stream.PAGE = str(d / "golden")
        _Stream.stop = threading.Event()
        # golden pages link ../static/tokens.css: serve it at /static too
        (d / "golden" / "static").symlink_to(d / "static")
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stream)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(_Stream.stop.set)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/page.html"

    def test_open_stream_is_judged_in_bounded_time_and_not_weighed(self):
        r, took = measure_bounded(self.url, 30, settle_ms=800)
        self.assertIsNotNone(r, "the judge hung on the open event stream")
        self.assertLess(took, 30)
        self.assertTrue(r["pass"], (r["failed"], r["facts"]))
        self.assertEqual([u.split("?")[0] for u in r["facts"]["streams"]], [f"http://127.0.0.1:{self.srv.server_port}/api/events"])
        self.assertLess(r["facts"]["bytes"], 512 * 1024)

    def test_mutant_reading_the_stream_to_the_end_hangs(self):
        saved = J.STREAM
        J.STREAM = "x-never/none"
        try:
            r, _ = measure_bounded(self.url, 15, settle_ms=800)
        finally:
            _Stream.stop.set()                     # ends the stream: the mutant's read returns and its thread finishes
            measure_bounded.last.join(120)
            J.STREAM = saved
        self.assertIsNone(r, "reading an open stream to the end must not finish")


@unittest.skipIf(WHY, f"judge unavailable: {WHY}")
class D1RealApp(unittest.TestCase):
    """the real console (CON3 world): every route passes with undefined-var, and with the live stream open."""

    @classmethod
    def setUpClass(cls):
        from con3_world import ROUTES, serve, world
        cls.routes = ROUTES
        cls.w = world()
        cls.srv, _t = serve(cls.w)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.close()
        shutil.rmtree(cls.w.tmp, True)

    def test_seven_routes_pass_with_the_stream_open(self):
        self.assertEqual(len(self.routes), 7)
        for route in self.routes:
            with self.subTest(route=route):
                r, took = measure_bounded(self.srv.url + "#/" + route, 30, settle_ms=2200)   # app.js opens it at 1.5 s
                self.assertIsNotNone(r, "judge hung")
                self.assertTrue(r["pass"], (route, r["failed"], r["facts"]["undefined-vars"]))
                self.assertTrue(r["V"]["undefined-var"])
                self.assertTrue(any("/api/events" in u for u in r["facts"]["streams"]), r["facts"]["streams"])


if __name__ == "__main__":
    unittest.main()
