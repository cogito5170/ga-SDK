"""CMD-GA52 D1: GA Console's 클라우드 screen — the cloud sessions snapshot the baseline hub publishes (cloud-sessions/1).

Offline, temp git repos: the snapshot is read from the integration ref (never the worktree), the worktree file is the
fallback, a missing or invalid snapshot is {available: false}, the stale flag past 2 h, only the known fields reach the
API, the fetch is throttled, SSE `cloud` goes out on a change only, and the screen renders from a fixture and passes the
judge (with every string as text). The browser parts skip (with the reason) where Playwright or Chromium is missing.
0 network, 0 models. The mutations are in tests/mutations_ga52.py.
"""
import http.client
import json
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "console_frontend"))

from console_world import FAKE_SECRET, git  # noqa: E402

from ga.console import cloud as CL  # noqa: E402
from ga.console import config as K  # noqa: E402
from ga.console import judge as J  # noqa: E402
from ga.console import server as S  # noqa: E402
from ga.runlog import WITHHELD  # noqa: E402

BR = "claude/integ"
PATH = "ops/hub/cloud_sessions.json"
AT = "2026-10-06T18:00:00+09:00"
T0 = datetime.fromisoformat(AT).timestamp()
XSS = "<script>window.__xss=1</script><img src=x onerror=window.__xss=2>"
API_KEYS = {"available", "at", "age_s", "stale", "source", "sessions"}
SESSION_KEYS = set(CL.FIELDS)


def sess(i, **kw):
    """One session as baseline ops/hub/cloud_snapshot.py writes it."""
    base = {"id": f"session_{i}", "title": f"CMD-GA{i} worker", "status": "running", "bucket": "working",
            "detail": "running tests", "repo": "cogito5170/ga-sdk", "branch": f"claude/CMD-GA{i}", "model": "m-fixture",
            "ctx": 42000, "cost_usd": 1.25, "tags": ["ga", "worker"], "updated_at": "2026-10-06T17:55:00+09:00",
            "url": f"https://claude.ai/code/session_{i}"}
    return {**base, **kw}


def snap(at=AT, sessions=None, **extra):
    return {"schema": "cloud-sessions/1", "at": at, "sessions": [sess(1)] if sessions is None else sessions, **extra}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Repos:
    """A bare remote; the hub's clone that publishes the snapshot on the integration branch; the console's baseline
    clone, checked out on main, whose own worktree file says something else."""

    def __init__(self, ref_snapshot=None, worktree_snapshot=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="ga52-"))
        self.remote = self.tmp / "remote.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.hub = self.tmp / "hub"
        git(self.tmp, "clone", "-q", str(self.remote), str(self.hub))
        git(self.hub, "checkout", "-q", "-b", "main")
        (self.hub / "README").write_text("baseline\n")
        if worktree_snapshot is not None:
            self._write(self.hub, worktree_snapshot)
        git(self.hub, "add", "-A")
        git(self.hub, "commit", "-q", "-m", "main")
        git(self.hub, "push", "-q", "origin", "main")
        git(self.hub, "checkout", "-q", "-b", BR)
        if worktree_snapshot is not None:
            git(self.hub, "rm", "-q", PATH)
            git(self.hub, "commit", "-q", "-m", "the integration branch has no snapshot yet")
        if ref_snapshot is not None:
            self.publish(ref_snapshot)
        else:
            git(self.hub, "push", "-q", "origin", BR)
        self.base = self.tmp / "baseline"
        git(self.tmp, "clone", "-q", "-b", "main", str(self.remote), str(self.base))

    @staticmethod
    def _write(repo, obj):
        p = repo / PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))

    def publish(self, obj):
        """The hub commits a new snapshot on the integration branch and pushes it."""
        self._write(self.hub, obj)
        git(self.hub, "add", "-A")
        git(self.hub, "commit", "-q", "-m", "cloud snapshot")
        git(self.hub, "push", "-q", "origin", BR)

    def config(self, **cloud):
        raw = {"schema": K.SCHEMA, "baseline": str(self.base),
               "repos": [{"name": "baseline", "path": str(self.base), "integration_branch": BR}],
               "mailbox": {"repo": str(self.base), "remote": "origin", "fetch_every_s": 0, "name": "baseline"},
               "services": {}}
        if cloud:
            raw["cloud"] = cloud
        return K.check(raw)

    def close(self):
        shutil.rmtree(self.tmp, True)


class Reader(unittest.TestCase):
    def repos(self, **kw):
        r = Repos(**kw)
        self.addCleanup(r.close)
        return r

    def view(self, r, t=T0 + 60, **cloud):
        return CL.CloudReader(r.config(**cloud), clock=Clock(t)).view()

    def test_reads_the_ref_not_the_worktree(self):
        r = self.repos(ref_snapshot=snap(sessions=[sess(1, title="from the ref")]),
                       worktree_snapshot=snap(at="2026-10-06T10:00:00+09:00", sessions=[sess(1, title="from the worktree")]))
        head = git(r.base, "rev-parse", "HEAD")
        before = (r.base / PATH).read_text()
        v = self.view(r)
        self.assertTrue(v["available"])
        self.assertEqual(v["source"], "ref")
        self.assertEqual(v["at"], AT)
        self.assertEqual([s["title"] for s in v["sessions"]], ["from the ref"])
        # the worktree was not touched: same branch, same HEAD, same file, nothing changed
        self.assertEqual(git(r.base, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(git(r.base, "rev-parse", "HEAD"), head)
        self.assertEqual((r.base / PATH).read_text(), before)
        self.assertEqual(git(r.base, "status", "--porcelain"), "")

    def test_the_ref_is_fetched_into_the_remote_tracking_ref(self):
        r = self.repos(ref_snapshot=snap())
        self.assertEqual(git(r.base, "branch", "--list", BR), "")  # no local branch: only origin/<branch>
        self.assertTrue(self.view(r)["available"])
        self.assertEqual(git(r.base, "branch", "--list", BR), "")
        self.assertEqual(git(r.base, "rev-parse", f"refs/remotes/origin/{BR}"), git(r.hub, "rev-parse", "HEAD"))

    def test_worktree_fallback(self):
        r = self.repos(ref_snapshot=None, worktree_snapshot=snap(sessions=[sess(2, title="from the worktree")]))
        v = self.view(r)
        self.assertTrue(v["available"], v)
        self.assertEqual(v["source"], "worktree")
        self.assertEqual([s["title"] for s in v["sessions"]], ["from the worktree"])
        # and when the integration branch is not there at all
        v = self.view(r, integration_branch="claude/nope")
        self.assertEqual((v["available"], v["source"]), (True, "worktree"))

    def test_missing_or_invalid_is_not_available(self):
        r = self.repos()
        v = self.view(r)
        self.assertEqual((v["available"], v["sessions"], v["at"]), (False, [], None))
        self.assertTrue(v["reason"])
        for bad, why in (("{not json", "not JSON"), (json.dumps({**snap(), "schema": "cloud-sessions/2"}), "not cloud"),
                         (json.dumps(snap(at="2026-10-06T18:00:00")), "zone"), (json.dumps(snap(at=7)), "zone"),
                         (json.dumps({**snap(), "sessions": {"a": 1}}), "list"), (json.dumps([snap()]), "not cloud")):
            with self.subTest(why=why):
                r.publish(bad)
                v = CL.CloudReader(r.config(), clock=Clock(T0)).view()
                self.assertFalse(v["available"])
                self.assertIn(why, v["reason"])
        v = CL.CloudReader(K.check({"schema": K.SCHEMA, "baseline": str(r.tmp / "gone")})).view()
        self.assertEqual(v["available"], False)

    def test_stale_after_two_hours(self):
        r = self.repos(ref_snapshot=snap())
        fresh = self.view(r, t=T0 + 2 * 3600)
        self.assertEqual((fresh["stale"], fresh["age_s"]), (False, 7200))
        old = self.view(r, t=T0 + 2 * 3600 + 1)
        self.assertEqual((old["stale"], old["age_s"]), (True, 7201))
        self.assertFalse(self.view(r, t=T0 + 5)["stale"])

    def test_field_whitelist_and_types(self):
        s1 = sess(1, transcript="the whole conversation", prompt="do the thing", ctx="lots", cost_usd=True,
                  tags=["hub", 3, None], updated_at="yesterday", url="https://evil.example/x", bucket="exploded")
        r = self.repos(ref_snapshot=snap(sessions=[s1, {"id": "../../etc", "title": "bad id"}, "not a dict",
                                                   sess(3, title=XSS, detail=FAKE_SECRET)],
                                         transcripts=["x"], token="t"))
        v = self.view(r)
        self.assertEqual(set(v), API_KEYS)
        self.assertEqual([s["id"] for s in v["sessions"]], ["session_1", "session_3"])
        a, b = v["sessions"]
        self.assertEqual(set(a), SESSION_KEYS)
        self.assertNotIn("the whole conversation", json.dumps(v))
        self.assertNotIn("do the thing", json.dumps(v))
        self.assertEqual((a["ctx"], a["cost_usd"], a["tags"], a["updated_at"], a["bucket"]), (None, None, ["hub"], None, ""))
        self.assertEqual(a["url"], "https://claude.ai/code/session_1")  # built from the id, never the snapshot's
        self.assertEqual(a["role"], "hub")
        self.assertEqual(b["title"], XSS)  # kept as text: the page shows it as text
        self.assertEqual(b["detail"], WITHHELD)

    def test_at_most_100_sessions_and_roles(self):
        r = self.repos(ref_snapshot=snap(sessions=[sess(i) for i in range(150)]))
        self.assertEqual(len(self.view(r)["sessions"]), CL.MAX_SESSIONS)
        for tags, title, want in ((["hub"], "x", "hub"), ([], "GA integrator", "integrator"), (["watcher"], "x", "watcher"),
                                  ([], "CMD-GA52 클라우드", "worker"), ([], "감시", "watcher"), ([], "lunch", None)):
            self.assertEqual(CL.role({"tags": tags, "title": title}), want, (tags, title))

    def test_fetch_is_throttled(self):
        r = self.repos(ref_snapshot=snap())
        clock = Clock(T0)
        rd = CL.CloudReader(r.config(fetch_every_s=300), clock=clock)
        self.assertEqual(rd.view()["at"], AT)
        self.assertEqual(rd.fetches, 1)
        r.publish(snap(at="2026-10-06T18:10:00+09:00"))
        for dt in (1, 100, 299):
            clock.t = T0 + dt
            self.assertEqual(rd.view()["at"], AT)  # not fetched again yet: the last fetched ref
        self.assertEqual(rd.fetches, 1)
        clock.t = T0 + 300
        self.assertEqual(rd.view()["at"], "2026-10-06T18:10:00+09:00")
        self.assertEqual(rd.fetches, 2)
        self.assertEqual(CL.settings(r.config())["fetch_every_s"], 300)  # the default


class Api(unittest.TestCase):
    def setUp(self):
        self.r = Repos(ref_snapshot=snap())
        self.addCleanup(self.r.close)
        self.clock = Clock(T0 + 60)
        self.srv = S.Console(self.r.config(fetch_every_s=300), clock=self.clock, health=lambda u: False)
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()
        self.addCleanup(self.srv.close)
        self.addCleanup(self.srv.shutdown)

    def get(self, path, host=None, token=True):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.request("GET", path + (("&" if "?" in path else "?") + "t=" + self.srv.token if token else ""),
                  headers={"Host": host or f"127.0.0.1:{self.srv.port}"})
        resp = c.getresponse()
        body = resp.read()
        c.close()
        return resp.status, body

    def test_api_cloud_and_its_checks(self):
        code, body = self.get("/api/cloud")
        self.assertEqual(code, 200)
        d = json.loads(body)
        self.assertEqual(set(d), API_KEYS)
        self.assertEqual((d["available"], d["at"], d["age_s"], d["stale"]), (True, AT, 60, False))
        self.assertEqual(self.get("/api/cloud", token=False)[0], 403)
        self.assertEqual(self.get("/api/cloud", host="evil.example:80")[0], 403)

    def test_sse_cloud_on_change_only(self):
        self.assertEqual(self.srv.watch_once(), [])  # the baseline
        for dt in (1, 2, 400):  # the clock moves, a fetch happens at 400: the same snapshot sends nothing
            self.clock.t += dt
            self.assertNotIn("cloud", self.srv.watch_once())
        self.r.publish(snap(at="2026-10-06T18:20:00+09:00", sessions=[sess(7)]))
        self.clock.t += 301
        last = self.srv.bus.n
        self.assertIn("cloud", self.srv.watch_once())
        evs = [(t, d) for n, t, d in self.srv.bus.ring if n > last and t == "cloud"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0][1]["at"], "2026-10-06T18:20:00+09:00")
        self.assertEqual([s["id"] for s in evs[0][1]["sessions"]], ["session_7"])
        self.assertNotIn("cloud", self.srv.watch_once())
        # a new commit whose snapshot keeps the same `at` is not a change
        self.r.publish(snap(at="2026-10-06T18:20:00+09:00", sessions=[sess(8)]))
        self.clock.t += 301
        self.assertNotIn("cloud", self.srv.watch_once())
        # the snapshot going away is a change
        git(self.r.hub, "rm", "-q", PATH)
        git(self.r.hub, "commit", "-q", "-m", "gone")
        git(self.r.hub, "push", "-q", "origin", BR)
        self.clock.t += 301
        self.assertIn("cloud", self.srv.watch_once())
        self.assertFalse([d for n, t, d in self.srv.bus.ring if t == "cloud"][-1]["available"])


WHY = J.available()


def fixture(world):
    """The CON3 test world with a cloud snapshot on its baseline's integration branch (main)."""
    sessions = [
        sess(1, title="baseline hub", tags=["hub"], bucket="working", ctx=162000, cost_usd=12.5, detail="integrating GA50"),
        sess(2, title=XSS, bucket="review_ready", detail=XSS),
        sess(3, title="CMD-GA51 worker", bucket="blocked", detail="needs a token the VM does not have", ctx=None),
        sess(4, title="watcher", tags=["watcher"], bucket="failed", cost_usd=0.4),
        sess(5, title="CMD-GA49 worker", status="idle", bucket="completed", updated_at="2026-10-06T12:00:00+09:00"),
        sess(6, title="old integrator", status="archived", bucket=""),
    ]
    p = world.base / PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    p.write_text(json.dumps(snap(at=now, sessions=sessions), ensure_ascii=False))
    git(world.base, "add", "-A")
    git(world.base, "commit", "-q", "-m", "cloud snapshot")
    git(world.base, "push", "-q", "origin", "main")


@unittest.skipIf(WHY, f"no browser: {WHY}")
class Screen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from con3_world import serve, world
        from playwright.sync_api import sync_playwright
        cls.w = world()
        fixture(cls.w)
        cls.srv, _t = serve(cls.w)
        cls.pw = sync_playwright().start()
        cls.browser = J._launch(cls.pw)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()
        cls.srv.close()
        shutil.rmtree(cls.w.tmp, True)

    def page(self, width=1440):
        ctx = self.browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce", bypass_csp=True)
        self.addCleanup(ctx.close)
        ext = []

        def gate(r):
            if r.request.url.startswith(f"http://127.0.0.1:{self.srv.port}/"):
                r.continue_()
            else:
                ext.append(r.request.url)
                r.abort()
        ctx.route("**/*", gate)
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(self.srv.url + "#/cloud", wait_until="load")
        pg.wait_for_selector("[data-part=cloud-live] li", timeout=10000)
        pg.ext, pg.errs = ext, errs
        return pg

    def test_screen_renders_from_the_fixture(self):
        for width in (375, 1440):
            pg = self.page(width)
            main, mast = pg.inner_text("#main"), pg.inner_text(".mast")
            self.assertIn("일하는 세션 4", mast)
            self.assertIn("작업 중 1 · 검토 대기 1 · 막힘·실패 2 · 비용 $15.40", mast)  # 12.5 + 1.25 + 1.25 + 0.4
            for word in ("허브", "감시", "작업", "검토 대기", "막힘", "실패", "맥락 162k · 넘침", "integrating GA50", "기준 시각"):
                self.assertIn(word, main)
            self.assertEqual(pg.locator("[data-part=cloud-live] .ctx.over mark").count(), 1)
            # blocked / failed first, then review, then working
            ids = pg.eval_on_selector_all("[data-part=cloud-live] h3 a", "l => l.map(a => a.getAttribute('href'))")
            self.assertEqual(ids, [f"https://claude.ai/code/session_{i}" for i in (4, 3, 2, 1)])
            # finished / idle sessions: collapsed
            self.assertFalse(pg.evaluate("document.querySelector('[data-part=cloud-rest] details').open"))
            self.assertIn("펼쳐 보기 (2개)", pg.inner_text("[data-part=cloud-rest]"))
            self.assertEqual(pg.inner_text("#n-cloud"), "4")
            self.assertEqual(pg.ext, [])
            self.assertEqual(pg.errs, [])

    def test_strings_are_text(self):
        pg = self.page()
        self.assertIsNone(pg.evaluate("window.__xss"))
        self.assertIn("<script>window.__xss=1</script>", pg.inner_text("#main"))
        self.assertEqual(pg.evaluate("document.querySelectorAll('#main script, #main img').length"), 0)

    def test_stale_warning(self):
        pg = self.page()
        self.assertNotIn("2시간 넘게", pg.inner_text("#main"))
        pg.evaluate("""() => { const d = Date.now; Date.now = () => d() + 3 * 3600 * 1000; }""")
        pg.evaluate("location.hash = '#/now'")
        pg.wait_for_selector("[data-part=now-mast]")
        pg.evaluate("location.hash = '#/cloud'")
        pg.wait_for_selector("text=2시간 넘게", timeout=10000)
        self.assertIn("오래됨", pg.inner_text("[data-part=cloud-when]"))

    @staticmethod
    def measure(target, **kw):
        """The judge in its own thread: it starts its own Playwright, which may not share this thread's."""
        out = {}
        t = threading.Thread(target=lambda: out.update(J.measure(target, **kw)))
        t.start()
        t.join(180)
        return out

    def test_judge_passes_the_screen_and_its_golden(self):
        v = self.measure(self.srv.url + "#/cloud", settle_ms=600)
        self.assertTrue(v["pass"], (v["failed"], v["facts"]))
        golden = self.measure(str(ROOT / "ga" / "console" / "golden" / "cloud.html"))
        self.assertTrue(golden["pass"], (golden["failed"], golden["facts"]))


class Golden(unittest.TestCase):
    def test_golden_shares_tokens_and_nav(self):
        html = (ROOT / "ga" / "console" / "golden" / "cloud.html").read_text(encoding="utf-8")
        self.assertIn('href="../static/tokens.css"', html)
        self.assertIn('href="golden.css"', html)
        self.assertEqual(html.count('aria-current="page"'), 1)
        self.assertEqual(html.count("<h1"), 1)
        for word in ("지금", "작업", "브랜치", "서비스", "토큰", "묻기", "결정", "클라우드"):
            self.assertIn(f">{word}<", html)
        self.assertNotRegex(html, r"(src|href)=\"(https?:)?//")  # the app links to claude.ai/code/<id>; the golden to itself

    def test_index_has_the_route(self):
        html = (ROOT / "ga" / "console" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<a data-line="functional" href="#/cloud" data-route="cloud">클라우드', html)


if __name__ == "__main__":
    unittest.main()
