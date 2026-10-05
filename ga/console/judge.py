"""GA Console judge: code, not a model. Opens a page in headless Chromium and measures the rendered page.

    python -m ga.console.judge <file-or-url> [--shots DIR]   prints a verdict JSON; exit 0 pass, 1 fail, 2 skipped

V -- invariants (after gentleMonster's engine judge). Every one must hold; unknown counts as fail.
    overflow-375 / overflow-1440   no sideways scroll, and no text box outside the viewport (a clipping
                                   container hides overflow from scrollWidth, so both are measured)
    contrast                       every visible text element: computed colour, times ancestor opacity, over its
                                   composited background >= 4.5 (>= 3 for >= 24 px or bold >= 18.66 px); text on
                                   an image/gradient cannot be measured -> fail
    min-font-375                   no text under 12 px at 375 px
    offline                        0 requests leave the page's own origin (file: for a file, scheme+host+port for a URL)
    js-errors                      0 uncaught page errors
    names                          every control (a[href], button, input, select, textarea, summary, [role=button|link|
                                   tab|checkbox|switch]) has an accessible name; every img has alt; svg[role=img] and
                                   canvas are named
    headings                       html[lang] set, exactly one h1, the first heading is h1, no skipped level
    reduced-motion                 with prefers-reduced-motion: reduce, no animation or transition is running
    accent                         pixels within 40 of the hot accent are 0.3-4 % of the full page at 1440 px
    weight                         everything the page loads (document, css, js, images) <= 512 KB

The page is opened three times: 375 and 1440 px with reduced motion (all V), 1440 px with motion (reported only).
"""
from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

WIDTHS = (375, 1440)
MAX_BYTES = 512 * 1024
ACCENT = "#D9480F"
ACCENT_BAND = (0.003, 0.04)
CHECKS = ("overflow-375", "overflow-1440", "contrast", "min-font-375", "offline", "js-errors", "names", "headings",
          "reduced-motion", "accent", "weight")

_JS_TEXT = r"""() => {
 const P = s => { const m = /rgba?\(([^)]+)\)/.exec(s || ''); if (!m) return null;
   const p = m[1].split(/[\s,\/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
 const over = (t, b) => { const a = t[3]; return [t[0]*a + b[0]*(1-a), t[1]*a + b[1]*(1-a), t[2]*a + b[2]*(1-a), 1]; };
 const lum = c => { const f = v => { v /= 255; return v <= 0.04045 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
   return 0.2126*f(c[0]) + 0.7152*f(c[1]) + 0.0722*f(c[2]); };
 const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x,y)+0.05)/(Math.min(x,y)+0.05); };
 const out = {items: [], unmeasured: [], minfont: null, small: [], cut: []};
 for (const el of document.body.querySelectorAll('*')) {
   if (['SCRIPT','STYLE','NOSCRIPT','TEMPLATE'].includes(el.tagName) || el.closest('svg')) continue;
   const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').replace(/\s+/g, ' ').trim();
   if (!own) continue;
   const cs = getComputedStyle(el), r = el.getBoundingClientRect();
   if (r.width === 0 || r.height === 0 || cs.visibility === 'hidden') continue;
   if (cs.clipPath && cs.clipPath !== 'none' && r.width <= 1 && r.height <= 1) continue;   // visually hidden (.vh)
   const offscreen = r.right < 0 || r.bottom < 0;                                          // a skip link parked off screen
   if (!offscreen && (r.right > innerWidth + 1 || r.left < -1)) out.cut.push({text: own.slice(0, 40), left: Math.round(r.left), right: Math.round(r.right)});
   if (offscreen) continue;
   const fs = parseFloat(cs.fontSize), fw = parseInt(cs.fontWeight) || 400;
   if (out.minfont === null || fs < out.minfont) out.minfont = fs;
   if (fs < 12) out.small.push({text: own.slice(0, 40), px: fs});
   const layers = []; let bad = null;
   for (let a = el; a; a = a.parentElement) {
     const s = getComputedStyle(a);
     const bg = P(s.backgroundColor);
     if (bg && bg[3] > 0) layers.push(bg);
     if (bg && bg[3] >= 1) break;
     if (s.backgroundImage !== 'none') { bad = a.tagName.toLowerCase() + '.' + a.className; break; }
   }
   if (bad) { out.unmeasured.push({text: own.slice(0, 40), on: bad}); continue; }
   let bg = [255, 255, 255, 1];
   for (const l of layers.reverse()) bg = over(l, bg);
   let fg = P(cs.color);
   if (!fg) { out.unmeasured.push({text: own.slice(0, 40), on: 'colour ' + cs.color}); continue; }
   let op = 1; for (let a = el; a; a = a.parentElement) op *= parseFloat(getComputedStyle(a).opacity);
   fg = over([fg[0], fg[1], fg[2], fg[3] * op], bg);
   const large = fs >= 24 || (fs >= 18.66 && fw >= 700);
   out.items.push({text: own.slice(0, 40), ratio: Math.round(ratio(fg, bg) * 100) / 100, need: large ? 3 : 4.5, px: fs});
 }
 return out;
}"""

_JS_DOC = r"""() => {
 const hs = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(h => +h.tagName[1]);
 const skips = []; for (let i = 1; i < hs.length; i++) if (hs[i] > hs[i-1] + 1) skips.push('h' + hs[i-1] + '>h' + hs[i]);
 const shown = el => { const s = getComputedStyle(el); return s.display !== 'none' && s.visibility !== 'hidden' && !el.closest('[hidden],[aria-hidden=true]'); };
 const txt = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
 const name = el => {
   if ((el.getAttribute('aria-label') || '').trim()) return true;
   const lb = el.getAttribute('aria-labelledby');
   if (lb && lb.split(/\s+/).some(id => { const t = document.getElementById(id); return t && txt(t); })) return true;
   if ((el.getAttribute('title') || '').trim()) return true;
   if (['INPUT','SELECT','TEXTAREA'].includes(el.tagName)) {
     if (['submit','button','reset'].includes(el.type) && (el.value || '').trim()) return true;
     if (el.labels && [...el.labels].some(l => txt(l))) return true;
     return false;
   }
   if (txt(el)) return true;
   return [...el.querySelectorAll('img[alt]')].some(i => i.alt.trim());
 };
 const ctl = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],[role=tab],[role=checkbox],[role=switch]';
 const unnamed = [...document.querySelectorAll(ctl)].filter(el => shown(el) && !name(el)).map(el => el.outerHTML.slice(0, 80));
 for (const el of document.querySelectorAll('img:not([alt]), svg[role=img]:not([aria-label]):not([aria-labelledby]), canvas:not([aria-label]):not([aria-hidden=true])'))
   unnamed.push(el.outerHTML.slice(0, 80));
 return {lang: document.documentElement.lang || '', h1: document.querySelectorAll('h1').length, first: hs[0] || 0, skips, unnamed,
         overflow: document.documentElement.scrollWidth - window.innerWidth,
         running: document.getAnimations().filter(a => a.playState === 'running').map(a => a.animationName || a.constructor.name)};
}"""

_JS_PIXELS = r"""async ([src, rgb]) => {
 const img = new Image(); img.src = src; await img.decode();
 const c = document.createElement('canvas'); c.width = img.width; c.height = img.height;
 const g = c.getContext('2d'); g.drawImage(img, 0, 0);
 const d = g.getImageData(0, 0, c.width, c.height).data; let n = 0;
 for (let i = 0; i < d.length; i += 4) { const a = d[i]-rgb[0], b = d[i+1]-rgb[1], e = d[i+2]-rgb[2]; if (a*a + b*b + e*e < 1600) n++; }
 return n / (d.length / 4);
}"""


_AVAILABLE: list = []


def available() -> "str | None":
    """None when the judge can run, else the reason it cannot (asked once per process)."""
    if not _AVAILABLE:
        _AVAILABLE.append(_probe())
    return _AVAILABLE[0]


def _probe() -> "str | None":
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "playwright is not installed (pip install playwright)"
    try:
        with sync_playwright() as p:
            _launch(p).close()
    except Exception as e:  # noqa: BLE001 -- any launch failure means no browser here
        return f"chromium cannot start: {str(e).splitlines()[0][:160]}"
    return None


def _launch(p):
    """Playwright's own Chromium, else $GA_CHROMIUM, else $PLAYWRIGHT_BROWSERS_PATH/chromium (a pinned build of
    another Playwright version -- the managed containers ship one), else the error of the first try."""
    import os
    try:
        return p.chromium.launch()
    except Exception as first:  # noqa: BLE001
        for exe in (os.environ.get("GA_CHROMIUM"), os.path.join(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"), "chromium")):
            if exe and os.path.exists(exe):
                return p.chromium.launch(executable_path=exe)
        raise first


def _target(t: str) -> "tuple[str, callable]":
    """URL to open and a predicate 'this request stays inside the page's own origin'."""
    if "://" in t:
        o = urlsplit(t)
        home = (o.scheme, o.hostname, o.port)
        return t, lambda u: u.startswith(("data:", "blob:")) or (urlsplit(u).scheme, urlsplit(u).hostname, urlsplit(u).port) == home
    return Path(t).resolve().as_uri(), lambda u: u.startswith(("file:", "data:", "blob:"))


def _rgb(h: str) -> list:
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def measure(target: str, shots: "str | Path | None" = None, settle_ms: int = 200) -> dict:
    """The verdict for one page: {"pass", "V": {check: bool}, "failed": [...], "facts": {...}}."""
    why = available()
    if why:
        return {"pass": None, "skipped": why, "target": target}
    from playwright.sync_api import sync_playwright
    url, inside = _target(target)
    shots = Path(shots) if shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    stem = Path(urlsplit(url).path).stem or "page"
    ext, errs, raw = [], [], {}
    sizes: dict = {}
    t0 = time.time()
    with sync_playwright() as p:
        b = _launch(p)
        for w, motion in ((375, "reduce"), (1440, "reduce"), (1440, "no-preference")):
            ctx = b.new_context(viewport={"width": w, "height": 900}, reduced_motion=motion)

            def route(r):              # one parameter only: Playwright passes (route, request) to a handler taking two
                if inside(r.request.url):
                    r.continue_()
                else:
                    ext.append(r.request.url)
                    r.abort()
            ctx.route("**/*", route)
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errs.append(str(e)))

            resps: list = []
            pg.on("response", lambda r_: resps.append(r_))   # bodies are read after load, outside the event
            pg.goto(url, wait_until="load")
            pg.wait_for_timeout(settle_ms)
            for resp in resps:
                if resp.request.url not in sizes:
                    try:
                        sizes[resp.request.url] = len(resp.body())
                    except Exception:  # noqa: BLE001 -- a body Chromium did not keep (redirect, file:)
                        sizes[resp.request.url] = 0
            key = f"{w}-{motion}"
            raw[key] = {"doc": pg.evaluate(_JS_DOC)}
            if motion == "reduce":
                raw[key]["text"] = pg.evaluate(_JS_TEXT)
                png = pg.screenshot(full_page=True)
                if shots:
                    (shots / f"{stem}-{w}.png").write_bytes(png)
                if w == 1440:
                    src = "data:image/png;base64," + base64.b64encode(png).decode()
                    blank = ctx.new_page()
                    raw[key]["accent"] = blank.evaluate(_JS_PIXELS, [src, _rgb(ACCENT)])
                    blank.close()
            ctx.close()
        b.close()
    if url.startswith("file:"):           # file: responses carry no body in every Chromium; count the files on disk
        from urllib.request import url2pathname
        for u in list(sizes):
            if u.startswith("file:"):
                f = Path(url2pathname(urlsplit(u).path))
                sizes[u] = f.stat().st_size if f.exists() else 0
        sizes.setdefault(url, Path(url2pathname(urlsplit(url).path)).stat().st_size)
    m, d = raw["375-reduce"], raw["1440-reduce"]
    items = m["text"]["items"] + d["text"]["items"]
    low = sorted((x for x in items if x["ratio"] < x["need"]), key=lambda x: x["ratio"])
    unm = m["text"]["unmeasured"] + d["text"]["unmeasured"]
    weight = sum(sizes.values())
    acc = d["accent"]
    v = {"overflow-375": m["doc"]["overflow"] <= 1 and not m["text"]["cut"],
         "overflow-1440": d["doc"]["overflow"] <= 1 and not d["text"]["cut"],
         "contrast": bool(items) and not low and not unm,
         "min-font-375": m["text"]["minfont"] is not None and m["text"]["minfont"] >= 12,
         "offline": not ext,
         "js-errors": not errs,
         "names": not m["doc"]["unnamed"] and not d["doc"]["unnamed"],
         "headings": d["doc"]["lang"] != "" and d["doc"]["h1"] == 1 and d["doc"]["first"] == 1 and not d["doc"]["skips"],
         "reduced-motion": not m["doc"]["running"] and not d["doc"]["running"],
         "accent": ACCENT_BAND[0] <= acc <= ACCENT_BAND[1],
         "weight": 0 < weight <= MAX_BYTES}
    failed = [k for k in CHECKS if not v[k]]
    return {"pass": not failed, "target": target, "V": v, "failed": failed,
            "facts": {"min-contrast": min((x["ratio"] for x in items), default=None), "low-contrast": low[:6], "unmeasured": unm[:6],
                      "min-font-375": m["text"]["minfont"], "small-text": m["text"]["small"][:6],
                      "overflow": {"375": m["doc"]["overflow"], "1440": d["doc"]["overflow"]},
                      "cut-text": (m["text"]["cut"] + d["text"]["cut"])[:6],
                      "external": ext[:6], "errors": [x[:200] for x in errs[:3]],
                      "unnamed": (m["doc"]["unnamed"] + d["doc"]["unnamed"])[:6],
                      "headings": {"lang": d["doc"]["lang"], "h1": d["doc"]["h1"], "first": d["doc"]["first"], "skips": d["doc"]["skips"]},
                      "running-reduced": m["doc"]["running"] + d["doc"]["running"],
                      "running-with-motion": len(raw["1440-no-preference"]["doc"]["running"]),
                      "accent-share": round(acc, 4), "bytes": weight, "requests": len(sizes),
                      "seconds": round(time.time() - t0, 1)}}


def main(argv: "list[str] | None" = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m ga.console.judge", description="V checks on a rendered page")
    ap.add_argument("target", help="an HTML file or a URL")
    ap.add_argument("--shots", help="save full-page PNGs <stem>-375.png and <stem>-1440.png here")
    a = ap.parse_args(argv)
    r = measure(a.target, a.shots)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 2 if r["pass"] is None else (0 if r["pass"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
