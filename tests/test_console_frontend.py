"""CMD-CON3: GA Console's static frontend (ga/console/static/{index.html,app.css,app.js}).

D1: `ga console` on a fake world (CON2's test world), every route in Chromium at 375 and 1440: CON1's judge passes on
real API data; SSE events (new mail, work reported, service started) change the page without a reload; service
start/stop and ask -> confirm round trips through the UI; a data string holding <script> renders as text.
D2: each mutation (innerHTML with data, a remote URL, animation under reduced motion, the token left in the address
bar, a skipped heading) is applied to a copy of the static folder and the check that guards it fails.
The browser parts skip (with the reason) where Playwright or Chromium is missing. 0 network, 0 models.
"""
import json
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "console_frontend"))

from ga.console import judge as J  # noqa: E402
from ga.console import server as S  # noqa: E402

STATIC = ROOT / "ga" / "console" / "static"
WHY = J.available()
SETTLE_MS = 600  # the page has its first reads by then; the stream opens later (app.js STREAM_AFTER_MS)


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


# ---- checks, each one a function so the mutation tests can run it on a mutated copy ---------------------------------

def measure(url):
    """CON1's judge on a URL, in its own thread: it starts its own Playwright, which may not share this thread's."""
    import threading
    out = {}
    t = threading.Thread(target=lambda: out.update(J.measure(url, settle_ms=SETTLE_MS)))
    t.start()
    t.join(180)
    return out


def static_problems(folder=STATIC):
    """What the plain files must never hold: HTML built from strings, a remote URL, an animation outside the
    no-preference block, the tokens copied instead of linked."""
    out = []
    js = (folder / "app.js").read_text(encoding="utf-8")
    css = (folder / "app.css").read_text(encoding="utf-8")
    html = (folder / "index.html").read_text(encoding="utf-8")
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        if bad in js:
            out.append(f"app.js uses {bad}")
    for name, text in (("index.html", html), ("app.css", css), ("app.js", js)):
        if re.search(r"(?:https?:)?//[A-Za-z0-9-]+\.[A-Za-z]", re.sub(r"/\*.*?\*/", "", text, flags=re.S)
                     .replace("http://www.w3.org", "")):
            out.append(f"{name} names a remote URL")
    if 'href="tokens.css"' not in html:
        out.append("index.html does not link tokens.css")
    if re.search(r"--bg:\s*#", css):
        out.append("app.css copies the tokens")
    calm = re.sub(r"@media \(prefers-reduced-motion:no-preference\)\{(?:[^{}]|\{[^{}]*\}|\{(?:[^{}]|\{[^{}]*\})*\})*\}", "", css)
    if re.search(r"animation\s*:\s*(?!none)", calm) or "@keyframes" in calm:
        out.append("app.css animates outside @media (prefers-reduced-motion:no-preference)")
    if re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", re.sub(r"/\*.*?\*/", "", css, flags=re.S)):
        out.append("app.css has a raw colour (take it from tokens.css)")
    if "prefers-reduced-motion:reduce" not in css:
        out.append("app.css has no reduced-motion stop rule")
    return out


@unittest.skipIf(WHY, f"no browser: {WHY}")
class Browser(unittest.TestCase):
    """One fake world, one server, one Chromium for the class."""

    @classmethod
    def setUpClass(cls):
        from world import FakeEngine, serve, world
        from playwright.sync_api import sync_playwright
        cls.FakeEngine = FakeEngine
        cls.w = world()
        cls.srv, _t = serve(cls.w)
        cls.srv.services.start("worker")  # exits 1: a failed service to show
        cls.pw = sync_playwright().start()
        cls.browser = J._launch(cls.pw)
        cls.static = S.STATIC

    @classmethod
    def tearDownClass(cls):
        S.STATIC = cls.static
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()
        cls.srv.close()
        shutil.rmtree(cls.w.tmp, True)

    def setUp(self):
        S.STATIC = self.static

    def page(self, route, width=1440, motion="reduce", init=None, video=None):
        ctx = self.browser.new_context(viewport={"width": width, "height": 900}, reduced_motion=motion,
                                       bypass_csp=True,  # the server's CSP is a second wall; these checks test the files
                                       **({"record_video_dir": video} if video else {}))
        self.addCleanup(ctx.close)
        ext = []

        def gate(r):
            if r.request.url.startswith(f"http://127.0.0.1:{self.srv.port}/"):
                r.continue_()
            else:
                ext.append(r.request.url)
                r.abort()
        ctx.route("**/*", gate)
        if init:
            ctx.add_init_script(init)
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(self.srv.url + "#/" + route, wait_until="load")
        pg.wait_for_selector("#main section", timeout=10000)
        pg.ext, pg.errs = ext, errs
        return pg

    def live(self, pg):
        pg.wait_for_selector("html[data-live=open]", state="attached", timeout=10000)

    # -- D1 -----------------------------------------------------------------------------------------------------------
    def test_d1_static_files_are_plain(self):
        self.assertEqual(static_problems(), [])
        html = read("index.html")
        self.assertIn('<script type="module" src="app.js">', html)
        self.assertIn('lang="ko"', html)
        for r in ("now", "work", "branches", "services", "tokens", "ask", "decisions"):
            self.assertIn(f'<a data-line="functional" href="#/{r}"', html)  # the current-tab marker carries meaning

    def test_d1_functional_lines_are_marked(self):
        pg = self.page("work")
        self.assertGreater(pg.locator(".track[data-line=functional]").count(), 0)
        self.assertEqual(pg.locator(".mark.live:not([data-line=functional])").count(), 0)
        self.assertGreater(pg.locator(".mark.live[data-line=functional]").count(), 0)
        tok = self.page("tokens")
        self.assertEqual(tok.locator(".bar:not([data-line=functional])").count(), 0)

    def test_d1_judge_passes_every_screen_on_real_data(self):
        want = {"now": "우편함", "work": "CMD-A2", "branches": "claude/open", "services": "worker",
                "tokens": "ITEM-7", "ask": "무엇을 할까요?", "decisions": "BD-12"}
        for route, text in want.items():
            with self.subTest(route=route):
                for width in (375, 1440):
                    pg = self.page(route, width)
                    self.assertIn(text, pg.inner_text("#main") + pg.inner_text("h1"))
                    self.assertNotIn("불러오는 중", pg.inner_text(".mast"))
                    self.assertEqual(pg.ext, [])
                    self.assertEqual(pg.errs, [])
                v = measure(self.srv.url + "#/" + route)
                self.assertTrue(v["pass"], (route, v["failed"], v["facts"]))

    def test_d1_data_holding_script_renders_as_text(self):
        for route in ("decisions", "work"):
            pg = self.page(route)
            self.assertIsNone(pg.evaluate("window.__xss"))
            self.assertIn("<script>window.__xss=1</script>", pg.inner_text("#main"))
            self.assertEqual(pg.evaluate("document.querySelectorAll('#main script, #main img').length"), 0)

    def test_d1_token_leaves_the_address_bar(self):
        pg = self.page("now")
        self.assertNotIn(self.srv.token, pg.url)
        self.assertNotIn("t=", pg.url)
        self.assertTrue(pg.url.endswith("/#/now"), pg.url)
        self.assertEqual(pg.evaluate("sessionStorage.getItem('ga-console-token')"), self.srv.token)
        self.assertNotIn(self.srv.token, pg.content())
        pg.click("text=작업")  # routes keep working on the kept token
        pg.wait_for_selector("text=CMD-A2")
        self.assertNotIn("t=", pg.url)

    def test_d1_live_events_update_the_page_without_reload(self):
        now, work, svc = self.page("now"), self.page("work"), self.page("services")
        for pg in (now, work, svc):
            self.live(pg)
            pg.evaluate("window.__same = 1")
        self.assertNotIn("CMD-A3 보고됨", work.inner_text("#main"))
        # new mail that reports CMD-A3: a mail event (지금) and a work event (작업)
        self.w.write_mail([("baseline", "20261003T000001.000001Z", "AGY", "CMD-A3",
                            self.w.report("CMD-A3", "done", 900, 90, 3))])
        now.wait_for_selector("text=새 편지", timeout=15000)
        work.wait_for_function("document.querySelector('#main').innerText.includes('보고됨')"
                               " && [...document.querySelectorAll('#main li')].some(li => li.innerText.includes('CMD-A3')"
                               " && li.innerText.includes('보고됨'))", timeout=15000)
        # a service started outside the page: a service event (서비스)
        self.assertIn("켜기 api", svc.inner_text("#main"))
        self.srv.services.start("api")
        self.addCleanup(self.srv.services.stop, "api")
        svc.wait_for_selector("text=멈추기 api", timeout=10000)
        for pg in (now, work, svc):
            self.assertEqual(pg.evaluate("window.__same"), 1)  # no reload happened

    def test_d1_service_start_and_stop_through_the_ui(self):
        slow = ("const f = window.fetch; window.fetch = (u, i) => (i && i.method === 'POST')"
                " ? new Promise(r => setTimeout(r, 700)).then(() => f(u, i)) : f(u, i);")
        pg = self.page("services", init=slow)
        pg.click("button:text-is('켜기 api')")
        pend = pg.locator("button[data-key='start-api']")
        self.assertTrue(pend.is_disabled())
        self.assertIn("기다리는 중", pend.inner_text())
        pg.wait_for_selector("text=api: 켰어요", timeout=10000)
        self.assertEqual(self.srv.services.get("api")["state"], "running")
        pg.click("button:text-is('멈추기 api')")  # stopping asks first
        self.assertEqual(self.srv.services.get("api")["state"], "running")
        pg.click("button:text-is('그대로 두기 api')")
        self.assertEqual(pg.locator("text=멈추기 api 확인").count(), 0)
        pg.click("button:text-is('멈추기 api')")
        pg.click("button:text-is('멈추기 api 확인')")
        pg.wait_for_selector("text=api: 멈췄어요", timeout=10000)
        self.assertEqual(self.srv.services.get("api")["state"], "stopped")
        log = pg.inner_text("#main")
        self.assertNotIn("env-value-7f3c9a1b", log)  # env names only, never a value
        self.assertEqual(pg.errs, [])

    def test_d1_ask_shows_cost_first_then_runs_on_confirm(self):
        self.FakeEngine.ran = []
        pg = self.page("ask")
        pg.fill("#q", "hello there")
        pg.click("button:text-is('비용 보기')")
        pg.wait_for_selector(".cost", timeout=10000)
        card = pg.inner_text(".cost")
        self.assertIn("~1,234", card)
        self.assertIn("<script>window.__xss=1</script>", card)  # a plan line from the engine, as text
        self.assertIsNone(pg.evaluate("window.__xss"))
        self.assertEqual(pg.locator("button.primary").inner_text(), "확인하고 돌리기")
        self.assertEqual(self.FakeEngine.ran, [])  # nothing ran before the confirm
        pg.click("button:text-is('확인하고 돌리기')")
        pg.wait_for_selector("text=model answered: 42", timeout=10000)
        pg.wait_for_selector("#main .state:has-text('끝남')", timeout=10000)
        self.assertEqual(self.FakeEngine.ran, [("model", True)])
        self.assertIn("hello there", pg.inner_text("[data-part='ask-past']"))
        # cancel: a second plan that is not run
        pg.click("button:text-is('비용 보기')")
        pg.wait_for_selector(".cost")
        pg.click("button:text-is('물음 취소')")
        pg.wait_for_selector("text=취소함")
        self.assertEqual(self.FakeEngine.ran, [("model", True)])

    def test_d1_empty_search_says_so(self):
        pg = self.page("decisions")
        pg.fill("#find", "zzqq")
        pg.click("button:text-is('결정 찾기')")
        pg.wait_for_selector("h1:text-is('찾은 결정 없음')")
        self.assertIn("q=zzqq", pg.url)

    def test_d1_only_living_things_move_and_never_under_reduced_motion(self):
        running = "document.getAnimations().filter(a => a.playState === 'running').length"
        self.srv.services.start("api")
        self.addCleanup(self.srv.services.stop, "api")
        calm = self.page("services", motion="reduce")
        self.assertEqual(calm.evaluate(running), 0)
        moving = self.page("services", motion="no-preference")
        self.assertGreater(moving.evaluate(running), 0)
        # the breathing marks are the live ones only
        self.assertEqual(moving.evaluate("document.getAnimations().every(a => a.effect.target.matches('.mark.live'))"), True)
        branches = self.page("branches", motion="no-preference")
        self.assertEqual(branches.evaluate(running), 0)  # git is not alive

    # -- D2: mutations ------------------------------------------------------------------------------------------------
    def mutated(self, file, old, new):
        tmp = Path(tempfile.mkdtemp(prefix="con3-mut-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        dst = tmp / "static"
        shutil.copytree(STATIC, dst)
        text = (dst / file).read_text(encoding="utf-8")
        self.assertIn(old, text, f"mutation anchor gone from {file}")
        (dst / file).write_text(text.replace(old, new, 1), encoding="utf-8")
        S.STATIC = dst
        return dst

    def test_d2_mutation_innerhtml_with_data_is_caught(self):
        dst = self.mutated("app.js", "n.append(k instanceof Node ? k : String(k));",
                           "if (k instanceof Node) n.append(k); else n.innerHTML += String(k);")
        self.assertTrue(any("innerHTML" in x for x in static_problems(dst)))
        pg = self.page("decisions")
        self.assertNotIn("<script>window.__xss=1</script>", pg.inner_text("#main"))  # the text check fails

    def test_d2_mutation_remote_url_is_caught(self):
        dst = self.mutated("index.html", '<link rel="stylesheet" href="app.css">',
                           '<link rel="stylesheet" href="app.css"><link rel="stylesheet" href="https://fonts.example.com/a.css">')
        self.assertTrue(any("remote URL" in x for x in static_problems(dst)))
        pg = self.page("now")
        self.assertTrue(pg.ext)  # the page asks a remote host (the server's CSP would refuse it; the file is still wrong)

    def test_d2_mutation_animation_under_reduced_motion_is_caught(self):
        self.srv.services.start("api")
        self.addCleanup(self.srv.services.stop, "api")
        dst = self.mutated("app.css", "@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}",
                           ".mark.live{animation:breath2 5s infinite}@keyframes breath2{50%{transform:scale(.7)}}")
        self.assertTrue(static_problems(dst))
        pg = self.page("services", motion="reduce")
        self.assertGreater(pg.evaluate("document.getAnimations().filter(a => a.playState === 'running').length"), 0)

    def test_d2_mutation_token_left_in_the_address_bar_is_caught(self):
        self.mutated("app.js", 'history.replaceState(null, "", clean);', 'void clean;')
        pg = self.page("now")
        self.assertIn(self.srv.token, pg.url)

    def test_d2_mutation_skipped_heading_is_caught(self):
        self.mutated("app.js", 'h("h2", { id: "h-" + id }', 'h("h4", { id: "h-" + id }')
        v = measure(self.srv.url + "#/tokens")
        self.assertIn("headings", v["failed"])


@unittest.skipIf(WHY, f"no browser: {WHY}")
class Offline(unittest.TestCase):
    """The stream drops: a banner says so, live marks stop breathing, an unread screen shows the error; the stream
    comes back on the same port and token and the banner goes."""

    def test_d1_stale_banner_error_state_and_reconnect(self):
        from world import serve, world
        from playwright.sync_api import sync_playwright
        w = world()
        self.addCleanup(shutil.rmtree, w.tmp, True)
        srv, _t = serve(w)
        port, token = srv.port, srv.token
        srv.services.start("api")
        with sync_playwright() as p:
            b = J._launch(p)
            ctx = b.new_context(viewport={"width": 375, "height": 900}, reduced_motion="no-preference", bypass_csp=True)
            pg = ctx.new_page()
            pg.goto(srv.url + "#/services", wait_until="load")
            pg.wait_for_selector("html[data-live=open]", state="attached", timeout=10000)
            self.assertTrue(pg.is_hidden("#banner"))
            srv.shutdown()
            srv.close()
            pg.wait_for_selector("#banner:has-text('실시간 연결이 끊겼어요')", timeout=10000)
            self.assertTrue(pg.evaluate("document.body.classList.contains('stale')"))
            self.assertEqual(pg.evaluate("document.getAnimations().filter(a => a.playState === 'running').length"), 0)
            # a kept-alive connection may still be answered by its old thread: the reads fail here as they would
            ctx.route("**/api/tokens*", lambda r: r.abort())
            pg.evaluate("location.hash = '#/tokens'")  # never read before, and the server is gone
            pg.wait_for_selector("h1:has-text('서버에 닿지 않음')", timeout=10000)
            self.assertIn("ga console", pg.inner_text(".lede"))
            ctx.unroute("**/api/tokens*")
            srv2, _t2 = serve(w, port=port, token=token)
            self.addCleanup(srv2.close)
            self.addCleanup(srv2.shutdown)
            pg.wait_for_selector("html[data-live=open]", state="attached", timeout=40000)
            pg.wait_for_selector("#banner", state="hidden", timeout=10000)
            pg.wait_for_selector("h1:has-text('토큰')", timeout=10000)  # what no event carries is read again
            b.close()
        srv2.services.shutdown()


if __name__ == "__main__":
    unittest.main()
