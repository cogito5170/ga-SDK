"""CMD-CON1: GA Console design -- DTCG tokens, golden screens, the V judge. 0 model calls, 0 network beyond 127.0.0.1.

D1  tokens.css is exactly the generator's output of tokens.json; the token pairs meet WCAG AA by math on the dark
    ground; every golden page passes the judge at 375 and 1440 px (when Chromium is here); the judge fails pages
    planted with overflow, low contrast (gross and just under 4.5), an external request, a JS error, an unnamed
    button, a skipped heading level, an animation running under reduced motion, a red block (monochrome), a
    decorative <hr> / bordered card / 1 px rule (display-line), 11 px text at 375 and a > 512 KB stylesheet -- each
    page fails exactly the one check it plants; a progress bar marked data-line="functional" passes.
D2  (rev 2) the mutants named in the directive die: monochrome disabled, display-line accepting any border, the
    functional allowlist widened to every element, the contrast threshold lowered.
"""
import functools
import http.server
import json
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from ga.console import judge as J
from ga.console import tokens as T

CONSOLE = Path(J.__file__).resolve().parent
GOLDEN = CONSOLE / "golden"
SCREENS = ("now", "work", "branches", "services", "tokens", "ask", "decisions")
NAV = ("지금", "작업", "브랜치", "서비스", "토큰", "묻기", "결정")


def _lum(h: str) -> float:
    h = h.lstrip("#")
    f = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    f = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in f]
    return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]


def contrast(a: str, b: str) -> float:
    x, y = _lum(a), _lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


class Tokens(unittest.TestCase):
    def setUp(self):
        self.t = T.load()

    def test_css_is_the_generator_output(self):
        self.assertEqual((CONSOLE / "static" / "tokens.css").read_text(encoding="utf-8"), T.css(self.t))
        self.assertEqual(T.main(["--check"]), 0)

    def test_check_catches_a_hand_edit(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "tokens.css"
            out.write_text(T.css(self.t).replace("#0b0b0b", "#000000"), encoding="utf-8")
            orig = T.OUT
            T.OUT = out
            try:
                self.assertEqual(T.main(["--check"]), 1)
            finally:
                T.OUT = orig

    def test_dtcg_shape(self):
        for group in ("color", "fontFamily", "fontSize", "space", "duration", "cubicBezier"):
            self.assertIn("$type", self.t[group], group)
            leaves = list(T._leaves(self.t[group]))
            self.assertTrue(leaves, group)
            for name, v in leaves:
                self.assertIn("$value", v, f"{group}.{name}")
        for _, v in T._leaves(self.t["color"]):
            self.assertRegex(v["$value"], r"^#[0-9a-fA-F]{6}$")
        self.assertEqual([k for k in self.t["fontSize"] if k.startswith("step")],
                         ["step-m1", "step-0", "step-1", "step-2", "step-3", "step-4", "step-5"])
        self.assertEqual([v["$value"] for _, v in T._leaves(self.t["space"])],
                         ["0.25rem", "0.5rem", "0.75rem", "1rem", "1.5rem", "2.5rem", "4rem", "6.5rem"])

    def test_dark_monochrome_grammar(self):
        c = {k: v["$value"] for k, v in T._leaves(self.t["color"])}
        for k, v in c.items():                                       # no chromatic colour at all: R == G == B
            self.assertEqual(len({v[1:3], v[3:5], v[5:7]}), 1, f"{k} {v} is not a gray")
        self.assertLess(_lum(c["bg"]), 0.01)                          # near-black ground, near-white ink: dark is the theme
        self.assertGreater(_lum(c["ink"]), 0.85)
        self.assertGreater(_lum(c["inverse-bg"]), 0.85)               # attention is inversion
        self.assertLess(_lum(c["inverse-ink"]), 0.01)
        self.assertFalse([k for k in c if "accent" in k or "tint" in k], "the accent and its tints are gone")
        # line tokens exist only for lines that carry function; no plain `line` / hairline / divider token
        self.assertEqual(sorted(k for k in c if k.startswith("line-")),
                         sorted(f"line-{x}" for x in ("track", "fill", "current", "focus", "control", "chart", "live")))
        self.assertNotIn("line", c)
        self.assertFalse([k for k in c if any(w in k for w in ("hairline", "divider", "rule", "decor", "border"))])
        palette = {c[k].lower() for k in ("ink", "sub", "line-track")}
        for k in ("state-ok", "state-live", "state-wait", "state-off", "state-fail"):
            self.assertIn(c[k].lower(), palette, k)
        fam = self.t["fontFamily"]
        self.assertEqual(fam["body"]["$value"][:4], ["Archivo", "Helvetica Neue", "Arial", "Liberation Sans"])
        self.assertEqual(fam["mono"]["$value"][:3], ["JetBrains Mono", "DejaVu Sans Mono", "Menlo"])
        fl = self.t["$extensions"]["ga.console"]["fluid"]
        self.assertEqual((fl["min-vw"], fl["max-vw"], fl["ratio"]), (375, 1440, 1.175))

    def test_fluid_steps_are_monotone_and_at_least_12px(self):
        lo = [T._px(v["$extensions"]["ga.console"]["min"]) for _, v in T._leaves(self.t["fontSize"])]
        hi = [T._px(v["$extensions"]["ga.console"]["max"]) for _, v in T._leaves(self.t["fontSize"])]
        self.assertTrue(all(a <= b for a, b in zip(lo, hi)))
        self.assertEqual(lo[:7], sorted(lo[:7]))
        self.assertGreaterEqual(min(lo) * 0.88, 12)          # mono text is .88em of the smallest step
        self.assertIn("clamp(1.0000rem, 0.9340rem + 0.2817vw, 1.1875rem)", T.css(self.t))   # step-0: 16 -> 19 px

    def test_token_pairs_meet_aa_by_math(self):
        c = {k: v["$value"] for k, v in T._leaves(self.t["color"])}
        for fg, bg, need in (("ink", "bg", 7), ("sub", "bg", 4.5), ("sub", "surface", 4.5), ("ink", "surface", 7),
                             ("inverse-ink", "inverse-bg", 7), ("inverse-sub", "inverse-bg", 4.5),
                             ("inverse-ink", "inverse-surface", 7), ("inverse-sub", "inverse-surface", 4.5),
                             # functional lines are UI components: >= 3:1 against what they sit on (WCAG 1.4.11)
                             ("line-track", "bg", 3), ("line-control", "bg", 3), ("line-chart", "bg", 3),
                             ("line-fill", "line-track", 3), ("line-focus", "bg", 3), ("line-current", "bg", 3),
                             ("line-live", "bg", 3), ("state-wait", "bg", 3), ("state-off", "bg", 3),
                             ("inverse-line-track", "inverse-bg", 3), ("inverse-line-control", "inverse-bg", 3)):
            self.assertGreaterEqual(contrast(c[fg], c[bg]), need, f"{fg} on {bg}")


class GoldenStatic(unittest.TestCase):
    def test_seven_screens_share_tokens_and_nav(self):
        for s in SCREENS:
            html = (GOLDEN / f"{s}.html").read_text(encoding="utf-8")
            self.assertIn('href="../static/tokens.css"', html, s)
            self.assertIn('href="golden.css"', html, s)
            self.assertIn('<html lang="ko">', html, s)
            self.assertEqual(html.count('aria-current="page"'), 1, s)
            self.assertEqual(html.count("<h1"), 1, s)
            self.assertEqual(html.count('class="hot"'), 1, s)     # markup kept for CON3; rev 2 renders it as space only
            for word in NAV:
                self.assertIn(f">{word}<", html, f"{s}: nav {word}")

    def test_no_remote_reference_and_no_raw_colour_in_layout(self):
        for f in list(GOLDEN.glob("*.html")) + [GOLDEN / "golden.css", CONSOLE / "static" / "tokens.css"]:
            text = f.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(src|href)=\"(https?:)?//", f.name)
            self.assertNotRegex(text, r"url\((['\"]?)(https?:)?//", f.name)
            self.assertNotIn("@import", text, f.name)
        css = (GOLDEN / "golden.css").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,6}\b", css), [], "golden.css takes colours from tokens only")

    def test_lines_only_where_they_carry_function(self):
        css = (GOLDEN / "golden.css").read_text(encoding="utf-8")
        body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        self.assertNotIn("var(--accent", body)
        self.assertNotIn("var(--line)", body)
        self.assertNotRegex(body, r"(^|[;{\s])hr[\s,{]")
        # every border / outline that draws takes a functional line token (or is transparent / removed)
        for prop, val in re.findall(r"(?<![-\w])(border(?:-(?:top|right|bottom|left))?(?:-color)?|outline):([^;}]+)", body):
            self.assertRegex(val, r"var\(--(line-|state-)|transparent|^\s*(0|none)\s*$", f"{prop}:{val}")
        for s in SCREENS:
            html = (GOLDEN / f"{s}.html").read_text(encoding="utf-8")
            for cls in ("track", "bars", "mark live", "mark wait", "mark off"):
                self.assertEqual(html.count(f'class="{cls}"'), html.count(f'class="{cls}" data-line="functional"'), f"{s}: {cls}")
            self.assertNotIn("<hr", html, s)

    def test_breath_only_without_reduced_motion(self):
        css = (GOLDEN / "golden.css").read_text(encoding="utf-8")
        head, _, rest = css.partition("@media (prefers-reduced-motion:no-preference)")
        self.assertNotIn("animation:", head)
        self.assertIn("@media (prefers-reduced-motion:reduce)", css)


# --- the judge on rendered pages ---------------------------------------------------------------------------------

SKIP = J.available()

PLANTS = {
    "overflow-375": '<p style="width:900px">이 줄은 폰 화면보다 넓게 박혀 있어서 옆으로 넘칩니다</p>',
    "contrast": '<p style="color:#333333">흐린 글씨는 읽기 어렵습니다</p>',
    "offline": '<img src="https://example.invalid/pixel.png" alt="바깥 그림" width="1" height="1">',
    "js-errors": '<script>throw new Error("planted")</script>',
    "names": '<button type="button"></button>',
    "headings": "<h4>건너뛴 제목</h4>",
    "reduced-motion": ('<div id="spin" style="width:24px;height:24px;background:#f2f2f2"></div>'
                       "<script>document.getElementById('spin').animate([{transform:'rotate(0)'},"
                       "{transform:'rotate(360deg)'}],{duration:1000,iterations:Infinity})</script>"),
    "monochrome": '<div style="height:40px;background:#c0392b"></div>',
    "display-line": "<hr>",
    "min-font-375": '<p style="font-size:11px">아주 작은 글씨는 폰에서 읽기 어렵습니다</p>',
    "weight": '<link rel="stylesheet" href="heavy.css">',    # heavy.css: > 512 KB, written in setUpClass
}
# more plants that fail exactly one check: (check, snippet)
MORE = (
    ("contrast", '<p style="color:#757575">4.27 대 1: 4.5 에 조금 못 미칩니다</p>'),
    ("display-line", '<div style="border:1px solid #a3a3a3;padding:1rem"><p>꾸밈 테두리만 있는 카드</p></div>'),
    ("display-line", '<div style="height:1px;background:#a3a3a3"></div>'),
)
FUNCTIONAL = ('<div data-line="functional" aria-hidden="true" style="height:8px;border:1px solid #8c8c8c">'
              '<i style="display:block;width:40%;height:100%;background:#f2f2f2"></i></div>'
              '<div data-line="functional" aria-hidden="true" style="height:2px;background:#6b6b6b">'
              '<i style="display:block;width:60%;height:2px;background:#f2f2f2"></i></div>')


@unittest.skipIf(SKIP, f"judge unavailable: {SKIP}")
class Judge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "console"
        shutil.copytree(CONSOLE / "static", cls.root / "static")
        shutil.copytree(GOLDEN, cls.root / "golden")
        # a fixed 600 KB, not derived from J.MAX_BYTES: a raised cap must let this page through
        (cls.root / "golden" / "heavy.css").write_text("/*" + "x" * 600 * 1024 + "*/\n", encoding="utf-8")
        cls.shots = Path(cls.tmp.name) / "shots"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_golden_page_passes(self):
        for s in SCREENS:
            with self.subTest(screen=s):
                r = J.measure(str(GOLDEN / f"{s}.html"), shots=self.shots)
                self.assertTrue(r["pass"], json.dumps({"failed": r["failed"], "facts": r["facts"]}, ensure_ascii=False))
                self.assertEqual(r["facts"]["external"], [])
                self.assertEqual(r["facts"]["display-lines"], [])
                self.assertLessEqual(max(r["facts"]["chroma-share"].values()), J.CHROMA_SHARE)
                self.assertGreaterEqual(r["facts"]["running-with-motion"], 0)
                for w in J.WIDTHS:
                    self.assertTrue((self.shots / f"{s}-{w}.png").stat().st_size > 1000)

    def test_each_plant_fails_exactly_its_check(self):
        base = (self.root / "golden" / "now.html").read_text(encoding="utf-8")
        for check, snippet in PLANTS.items():
            with self.subTest(check=check):
                page = self.root / "golden" / f"plant-{check}.html"
                page.write_text(base.replace("</main>", snippet + "\n</main>"), encoding="utf-8")
                r = J.measure(str(page))
                self.assertFalse(r["pass"])
                self.assertEqual(r["failed"], [check], json.dumps(r["facts"], ensure_ascii=False)[:600])
                if check == "monochrome":
                    self.assertGreater(r["facts"]["max-chroma"], 100)
                if check == "display-line":
                    self.assertEqual(r["facts"]["display-lines"][0]["el"], "hr")
                if check == "min-font-375":
                    self.assertEqual(r["facts"]["min-font-375"], 11)
                if check == "weight":
                    self.assertGreater(r["facts"]["bytes"], 600 * 1024)

    def _plant(self, name: str, snippet: str) -> dict:
        page = self.root / "golden" / f"plant-{name}.html"
        base = (self.root / "golden" / "now.html").read_text(encoding="utf-8")
        page.write_text(base.replace("</main>", snippet + "\n</main>"), encoding="utf-8")
        return J.measure(str(page))

    def test_more_plants_fail_exactly_their_check(self):
        for i, (check, snippet) in enumerate(MORE):
            with self.subTest(check=check, i=i):
                r = self._plant(f"more-{i}", snippet)
                self.assertEqual(r["failed"], [check], json.dumps(r["facts"], ensure_ascii=False)[:600])

    def test_functional_progress_bar_passes(self):
        r = self._plant("functional", FUNCTIONAL)
        self.assertTrue(r["pass"], r["failed"])
        r = self._plant("functional-bare", FUNCTIONAL.replace(' data-line="functional"', ""))
        self.assertEqual(r["failed"], ["display-line"])            # the same bar without the mark is decoration

    def _caught(self, check: str, snippet: str, **patch) -> bool:
        """Does the plant fail exactly its check (under the mutant, when module attributes are patched)?"""
        saved = {k: getattr(J, k) for k in patch}
        for k, v in patch.items():
            setattr(J, k, v)
        try:
            r = self._plant("mutant", snippet)
        finally:
            for k, v in saved.items():
                setattr(J, k, v)
        return r["failed"] == [check]

    def test_mutants_die(self):
        lines = J._JS_LINES
        mutants = {
            "monochrome disabled": ("monochrome", PLANTS["monochrome"], {"CHROMA_SHARE": 1.0}),
            "monochrome threshold raised to 255": ("monochrome", PLANTS["monochrome"], {"CHROMA": 255}),
            "display-line accepts any border": ("display-line", MORE[1][1],
                                                {"_JS_LINES": lines.replace("kinds.push('border-'", "void ('border-'")}),
            "functional allowlist widened to every element": ("display-line", PLANTS["display-line"],
                                                              {"LINE_OK": {"self": "*", "inside": "*"}}),
            "contrast threshold lowered to 4.2": ("contrast", MORE[0][1], {"CONTRAST": (4.2, 3.0)}),
            "contrast threshold lowered to 1.5": ("contrast", PLANTS["contrast"], {"CONTRAST": (1.5, 1.5)}),
        }
        for name, (check, snippet, patch) in mutants.items():
            with self.subTest(mutant=name):
                self.assertTrue(self._caught(check, snippet), "the plant must fail its check under the real judge")
                self.assertFalse(self._caught(check, snippet, **patch), f"mutant survived: {name}")

    def test_url_target_and_same_origin(self):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(self.root))
        handler.log_message = lambda *a, **k: None
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            url = f"http://127.0.0.1:{srv.server_port}/golden/tokens.html"
            r = J.measure(url)
            self.assertTrue(r["pass"], r["failed"])
            self.assertGreater(r["facts"]["bytes"], 5000)
            page = self.root / "golden" / "plant-port.html"
            other = f"http://127.0.0.1:{srv.server_port + 1 if srv.server_port < 65535 else 1}/x.css"
            base = (self.root / "golden" / "now.html").read_text(encoding="utf-8")
            page.write_text(base.replace("</head>", f'<link rel="stylesheet" href="{other}">\n</head>'), encoding="utf-8")
            r = J.measure(f"http://127.0.0.1:{srv.server_port}/golden/plant-port.html")
            self.assertEqual(r["failed"], ["offline"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_cli_prints_a_verdict(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = J.main([str(GOLDEN / "decisions.html")])
        self.assertEqual(code, 0)
        v = json.loads(out.getvalue())
        self.assertEqual(sorted(v["V"]), sorted(J.CHECKS))


class JudgeSkips(unittest.TestCase):
    def test_skip_has_a_reason(self):
        saved = list(J._AVAILABLE)
        J._AVAILABLE[:] = ["playwright is not installed (pip install playwright)"]
        try:
            r = J.measure(str(GOLDEN / "now.html"))
            self.assertIsNone(r["pass"])
            self.assertIn("playwright", r["skipped"])
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(J.main([str(GOLDEN / "now.html")]), 2)
        finally:
            J._AVAILABLE[:] = saved


if __name__ == "__main__":
    unittest.main()
