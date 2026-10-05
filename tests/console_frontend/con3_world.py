"""CMD-CON3 fixture: CON2's test world (tests/console_world.py) behind a running ``Console``, with the services the
screens show (api runs, worker fails, the bridge) and data strings that look like HTML. 0 network, 0 models."""
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from console_world import World, git  # noqa: E402
from ga.console import server as S  # noqa: E402

XSS = "<script>window.__xss=1</script><img src=x onerror=window.__xss=2>"
ROUTES = ["now", "work", "branches", "services", "tokens", "ask", "decisions"]


class FakeEngine:
    """GA36's ask Engine stand-in: says what each would cost, records what ran, calls no model."""
    ran: list = []

    def __init__(self, out):
        self.out, self.log = out, None

    def prepare(self, name):
        cost = {"status": "free", "run": "model"}.get(name, "free")
        return {"intent": name, "cost": cost, "lines": [f"would run {name}"], "refuse": None,
                **({"turns": 3, "estimate": 30000} if cost == "model" else {})}

    def prepare_model(self, q):
        return {"cost": "model", "prompt_tokens": 1234, "lines": ["agy 1 turn", XSS], "refuse": None, "text": q}

    def new_log(self, kind):
        class L:
            done_path = Path("/nonexistent/done")

            def close(self, *a):
                pass
        return L()

    def execute(self, name, confirmed=False):
        FakeEngine.ran.append((name, confirmed))
        self.out(f"ran {name}")
        return 0

    def run_model(self, p, confirmed=False):
        FakeEngine.ran.append(("model", confirmed))
        self.out("model answered: 42")
        return 0


def world():
    """A CON2 world with an HTML-looking directive title and decision row (committed, as the hub would)."""
    w = World()
    d = w.base / "directives" / "CMD-X9.md"
    d.write_text("```ga\n" + '{"schema":"directive/2","id":"CMD-X9","rev":1,"to":"AGY","goal":"' + XSS.replace('"', '\\"')
                 + ' Title."}\n```\nbody\n')
    log = w.base / "DECISION_LOG.md"
    log.write_text(log.read_text().replace("| BD-12 |", f"| BD-13 | {XSS} CMD-X9 | BD-13 |\n| BD-12 |", 1))
    git(w.base, "add", "-A")
    git(w.base, "commit", "-q", "-m", "xss rows")
    return w


def serve(w, static=None, **kw):
    """(server, thread). ``static`` points the server at another static folder (the mutation tests)."""
    cfg = w.config(services={"api": w.svc("run"), "worker": w.svc("exit1")})
    if static is not None:
        S.STATIC = Path(static)
    srv = S.Console(cfg, engine_factory=lambda out: FakeEngine(out), watch_every_s=kw.pop("watch_every_s", 0.2),
                    sse_keepalive_s=0.5, health=lambda url: False, **kw)
    srv.start_watcher()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t
