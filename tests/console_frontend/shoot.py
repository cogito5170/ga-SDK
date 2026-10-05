"""CMD-CON3 evidence: every screen at 1440 and 375 (PNG, reduced motion, full page) and a short webm of the page
following live events, written to reports/con3/. Fake world, 0 network, 0 models.

    python tests/console_frontend/shoot.py [out_dir]
"""
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

from con3_world import ROUTES, serve, world  # noqa: E402
from ga.console.judge import _launch  # noqa: E402


def ready(pg):
    pg.wait_for_selector("#main section", timeout=10000)
    pg.wait_for_timeout(400)


def main(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    w = world()
    srv, _t = serve(w)
    try:
        srv.services.start("worker")
        srv.services.start("api")
        srv.services.start("bridge")
        time.sleep(0.6)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = _launch(p)
            for width in (1440, 375):
                ctx = b.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
                pg = ctx.new_page()
                for r in ROUTES:
                    pg.goto(srv.url + "#/" + r, wait_until="load")
                    ready(pg)
                    pg.screenshot(path=str(out / f"{r}-{width}.png"), full_page=True)
                ctx.close()
            # the live update: mail arrives, a turn is written, a service starts; the page follows without reload
            vid = out / "_video"
            ctx = b.new_context(viewport={"width": 1280, "height": 800}, reduced_motion="no-preference",
                                record_video_dir=str(vid), record_video_size={"width": 1280, "height": 800})
            pg = ctx.new_page()
            pg.goto(srv.url + "#/now", wait_until="load")
            ready(pg)
            pg.wait_for_selector("html[data-live=open]", state="attached", timeout=10000)
            pg.wait_for_timeout(800)
            w.write_mail([("baseline", "20261003T000001.000001Z", "AGY", "CMD-A3", w.report("CMD-A3", "done", 900, 90, 3))])
            with (w.act / "2026-10-05.jsonl").open("a") as f:
                f.write(json.dumps({"schema": "act-ledger/1", "item": "ITEM-7", "turn": 3, "model": "m1", "input": 310,
                                    "output": 42, "cache_read": 0}) + "\n")
            pg.wait_for_selector("text=새 편지", timeout=15000)
            pg.mouse.wheel(0, 500)
            pg.wait_for_timeout(1500)
            pg.click("text=서비스")
            ready(pg)
            srv.services.stop("api")
            pg.wait_for_selector("text=켜기 api", timeout=10000)
            pg.wait_for_timeout(800)
            srv.services.start("api")
            pg.wait_for_selector("text=멈추기 api", timeout=10000)
            pg.wait_for_timeout(1500)
            path = pg.video.path()
            ctx.close()
            shutil.move(path, out / "live-update.webm")
            shutil.rmtree(vid, True)
            b.close()
    finally:
        srv.shutdown()
        srv.close()
        shutil.rmtree(w.tmp, True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else HERE.parents[1] / "reports" / "con3"))
