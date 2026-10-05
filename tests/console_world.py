"""CMD-CON2 test world: a temp baseline-shaped checkout with a bare remote and a ga-mailbox branch, a code repo with an
integration branch and topic branches, act / supervise ledgers, and a fake service command. 0 network, 0 models."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from ga.console import config as K

FAKE_SECRET = "sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKEFAKE"   # looks like a key (rule R6); fake
ENV_VALUE = "env-value-7f3c9a1b-not-a-real-one"               # an env-file value that must never come back

SERVICE = textwrap.dedent('''\
    import os, sys, time, http.server, threading
    mode = sys.argv[1]
    print("argv:" + "|".join(sys.argv[2:]), flush=True)
    print("has FAKE_TOKEN:", "FAKE_TOKEN" in os.environ, flush=True)
    if mode == "leak":
        print("value is " + os.environ.get("FAKE_TOKEN", ""), flush=True)
        print("secret " + os.environ.get("FAKE_SECRET_LINE", ""), file=sys.stderr, flush=True)
    if mode == "serve":
        port = int(sys.argv[2])
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
            def log_message(self, *a): pass
        srv = http.server.HTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    if mode == "stubborn":
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == "bridge":
        print("bridge: waiting for CMD-A3", flush=True)
    print("worker ready", flush=True)
    if mode == "exit1":
        sys.exit(1)
    while True:
        time.sleep(0.05)
''')


def git(cwd, *args, env=None):
    e = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t",
             GIT_COMMITTER_EMAIL="t@x", GIT_TERMINAL_PROMPT="0", **(env or {}))
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=e)
    if p.returncode:
        raise RuntimeError(f"git {args}: {p.stderr}")
    return p.stdout.strip()


def head(obj):
    return "```ga\n" + json.dumps(obj, ensure_ascii=False) + "\n```\n"


DIRECTIVES = {
    "CMD-A1": "Build the parser. It reads rows.",
    "CMD-A2": "작은 일을 한다. 두번째 문장.",
    "CMD-A3": "Sent and waiting.",
    "CMD-A4": "Only a draft.",
    "CMD-A5": "Reported, waiting for a verdict.",
    "CMD-A6": "Reported as failed.",
}
DECISIONS = [  # newest first
    ("BD-12", "CMD-A1 통합 (integrated into main). Tests green."),
    ("BD-11", "CMD-A2 sent back: the tests were missing. Retry."),
    ("BD-10", "CMD-A1 reviewed; waiting for the merge."),
    ("BD-9", "Token stack: path B chosen for the dev loop."),
]


class World:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="con2-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.remote = self.tmp / "remote.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.base = self.tmp / "baseline"
        git(self.tmp, "clone", "-q", str(self.remote), str(self.base))
        git(self.base, "checkout", "-q", "-b", "main")
        b = self.base
        (b / "directives").mkdir()
        for i, goal in DIRECTIVES.items():
            (b / "directives" / f"{i}.md").write_text(head({"schema": "directive/2", "id": i, "rev": 1, "to": "AGY",
                                                            "goal": goal}) + "body\n")
        (b / "ops" / "agy_bridge" / "items").mkdir(parents=True)
        (b / "ops" / "agy_bridge" / "items" / "ITEM-7.json").write_text(json.dumps(
            {"id": "ITEM-7", "title": "Fix the login test.", "to": "AGY", "status": "running"}))
        (b / "DECISION_LOG.md").write_text("# Decisions\n\n| id | decision | ref |\n|---|---|---|\n" + "".join(
            f"| {i} | {t} | {i} |\n" for i, t in DECISIONS))
        (b / "research" / "realwork").mkdir(parents=True)
        (b / "research" / "realwork" / "RW1_HUB_LEDGER.md").write_text(
            "| 작업 | 역할 | 모델 | 판정 되돌림 | 사람 개입 | input | output | cache read | cache write | cost_usd | 최종 | BD |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| CMD-A1 | hub | claude-opus | 0 | 1 | 12,000 | 3,000 | 50,000 | 1,000 | 0.42 | ok | BD-12 |\n")
        git(b, "add", "-A")
        git(b, "commit", "-q", "-m", "baseline: directives")
        git(b, "push", "-q", "origin", "main")
        # the mailbox: directive/2 for A1 A2 A3 A5 A6, report/2 for A1 A2 A5 (and A6 failed), one secret-looking
        self.mail = [
            ("AGY", "20261001T000001.000001Z", "baseline", "CMD-A1", head({"schema": "directive/2", "id": "CMD-A1",
                                                                          "rev": 1, "to": "AGY", "goal": "x"})),
            ("AGY", "20261001T000002.000001Z", "baseline", "CMD-A2", head({"schema": "directive/2", "id": "CMD-A2",
                                                                          "rev": 1, "to": "AGY", "goal": "x"})),
            ("AGY", "20261001T000003.000001Z", "baseline", "CMD-A3", head({"schema": "directive/2", "id": "CMD-A3",
                                                                          "rev": 1, "to": "AGY", "goal": "x"})),
            ("AGY", "20261001T000004.000001Z", "baseline", "CMD-A5", head({"schema": "directive/2", "id": "CMD-A5",
                                                                          "rev": 1, "to": "AGY", "goal": "x"})),
            ("AGY", "20261001T000005.000001Z", "baseline", "CMD-A6", head({"schema": "directive/2", "id": "CMD-A6",
                                                                          "rev": 1, "to": "AGY", "goal": "x"})),
            ("baseline", "20261002T000001.000001Z", "AGY", "CMD-A1", self.report("CMD-A1", "done", 1200, 300, 4)),
            ("baseline", "20261002T000002.000001Z", "AGY", "CMD-A2", self.report("CMD-A2", "done", 100, 10, 1)),
            ("baseline", "20261002T000003.000001Z", "AGY", "CMD-A5", self.report("CMD-A5", "done", 500, 50, 2,
                                                                                branch="claude/a5")),
            ("baseline", "20261002T000004.000001Z", "AGY", "CMD-A6", self.report("CMD-A6", "failed", 50, 5, 1)),
            ("baseline", "20261002T000005.000001Z", "AGY", "leak", head({"schema": "report/2", "from": "AGY",
                                                                         "note": FAKE_SECRET})),
        ]
        self.write_mail(self.mail)
        # a code repo: main (integration), a merged topic branch, an open one ahead of main
        self.code = self.tmp / "code"
        self.code_remote = self.tmp / "code.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.code_remote))
        git(self.tmp, "clone", "-q", str(self.code_remote), str(self.code))
        c = self.code
        git(c, "checkout", "-q", "-b", "main")
        (c / "a.txt").write_text("1\n")
        git(c, "add", "-A"); git(c, "commit", "-q", "-m", "one")
        git(c, "checkout", "-q", "-b", "claude/merged")
        (c / "b.txt").write_text("2\n")
        git(c, "add", "-A"); git(c, "commit", "-q", "-m", "merged work")
        git(c, "checkout", "-q", "main")
        git(c, "merge", "-q", "--ff-only", "claude/merged")
        git(c, "checkout", "-q", "-b", "claude/open")
        (c / "c.txt").write_text("3\n")
        git(c, "add", "-A"); git(c, "commit", "-q", "-m", "open work")
        (c / "c.txt").write_text("4\n")
        git(c, "add", "-A"); git(c, "commit", "-q", "-m", "more open work")
        git(c, "push", "-q", "origin", "main", "claude/merged", "claude/open")
        (c / "a.txt").write_text("dirty\n")  # a tracked change: dirty
        # ledgers
        self.act = self.tmp / "act" / "ledger"
        self.act.mkdir(parents=True)
        (self.act / "2026-10-05.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
            {"schema": "act-ledger/1", "item": "ITEM-7", "turn": 1, "model": "m1", "input": 100, "output": 10,
             "cache_read": 5, "at": "2026-10-05T01:00:00+00:00"},
            {"schema": "act-ledger/1", "item": "ITEM-7", "turn": 2, "model": "m1", "input": 200, "output": 20,
             "cache_read": 0, "at": "2026-10-05T01:01:00+00:00"}]))
        self.sup = self.tmp / "sup" / "ledger.jsonl"
        self.sup.parent.mkdir()
        self.sup.write_text(json.dumps({"directive": "CMD-A3", "model": "gemini", "turns": 3, "input": 900,
                                        "output": 90, "at": "2026-10-04T00:00:00+00:00"}) + "\n")
        self.ask_home = self.tmp / "ask"
        self.ask_home.mkdir()
        (self.ask_home / "day.json").write_text(json.dumps({"date": "2026-10-05", "agy_turns": 3}) + "\n")
        self.script = self.tmp / "fake_service.py"
        self.script.write_text(SERVICE)
        self.env_file = self.tmp / "token.env"
        self.env_file.write_text(f"# fake\nexport FAKE_TOKEN={ENV_VALUE}\nFAKE_SECRET_LINE='{FAKE_SECRET}'\n")

    @staticmethod
    def report(i, status, inp, out, turns, branch="claude/x"):
        return head({"schema": "report/2", "from": "AGY", "handled": [{"id": i, "rev_seen": 1, "status": status}],
                     "commits": [{"repo": "o/r", "branch": branch, "sha": "a" * 40}],
                     "results": {"input_tokens": inp, "output_tokens": out, "tokens": inp + out, "turns": turns,
                                 "model": "gpt-5.1"}})

    def write_mail(self, msgs):
        """The ga-mailbox branch on the remote, written with plumbing (files as `ga mail send` names them)."""
        wt = self.tmp / "mailwt"
        if not wt.exists():
            git(self.tmp, "clone", "-q", str(self.remote), str(wt))
            git(wt, "checkout", "-q", "--orphan", "ga-mailbox")
            git(wt, "rm", "-rq", "--cached", ".", "--ignore-unmatch")
            for p in wt.iterdir():
                if p.name != ".git":
                    subprocess.run(["rm", "-rf", str(p)])
        for to, utc, sender, fid, text in msgs:
            p = wt / "to" / to / f"{utc}-{sender}-{fid}.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "mail")
        git(wt, "push", "-q", "origin", "HEAD:refs/heads/ga-mailbox")

    def svc(self, mode, *extra, env_file=True, health_port=None, ready=None):
        s = {"argv": [sys.executable, str(self.script), mode, *extra], "cwd": str(self.tmp)}
        if env_file:
            s["env_file"] = str(self.env_file)
        if health_port:
            s["health_url"] = f"http://127.0.0.1:{health_port}/healthz"
            s["port"] = health_port
        if ready:
            s["ready_line"] = ready
        return s

    def config(self, services=None, bridge=True):
        raw = {"schema": K.SCHEMA, "baseline": str(self.base),
               "repos": [{"name": "baseline", "path": str(self.base), "integration_branch": "main"},
                         {"name": "code", "path": str(self.code), "integration_branch": "main"},
                         {"name": "gone", "path": str(self.tmp / "nope"), "integration_branch": "main"}],
               "mailbox": {"repo": str(self.base), "remote": "origin", "fetch_every_s": 0, "name": "baseline"},
               "ask_home": str(self.ask_home),
               "token_sources": {"act": [str(self.act)], "supervise": [str(self.sup)]},
               "services": services if services is not None else {"fake-api": self.svc("run")}}
        if bridge:
            raw["bridge"] = {**self.svc("bridge", env_file=False), "config_path": str(self.tmp / "agy-bridge.json")}
        return K.check(raw)
