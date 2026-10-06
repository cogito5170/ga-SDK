"""CMD-OPS2 D1: ga vm install --full, bridge-adopt, enable-bridge, url — offline: a temp HOME, local bare remotes (baseline,
ga-sdk, Token), fake systemctl/loginctl/pg_isready/psql/journalctl/agy on PATH, a faked venv/pip/npm/ga agy-agent."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from ga import mailbox as mb
from ga.console import config as K
from ga.console.services import Services
from ga.forms import dump_text
from ga.vm import cli, core
from tests.test_ops1 import GB, Base, FakeRunner, sh

FAKE = "FAKEVALUE-not-a-secret-7f3a"
PG = """#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_LOG"
[ "$FAKE_PG" = ok ] && exit 0
exit 2
"""
AGY = """#!/bin/sh
echo "agy $*" >> "$FAKE_LOG"
[ "$1" = --version ] && echo "agy 1.2.3"
exit 0
"""
JOURNAL = """#!/bin/sh
echo "journalctl $*" >> "$FAKE_LOG"
echo "GA Console: http://127.0.0.1:8765/?t=old"
echo "something else"
echo "GA Console: http://127.0.0.1:8765/?t=newtok"
"""


def directive(id_, to="AGY"):
    return dump_text({"schema": "directive/2", "id": id_, "rev": 1, "to": to, "goal": "g", "why": "w",
                      "scope": [{"id": "S1", "text": "s"}], "done_when": [{"id": "D1", "text": "d"}]}, "## Directive\nx\n")


class Runner2(FakeRunner):
    """Adds the Token venv/pip, npm ci and `ga agy-agent install` (recorded, not run)."""

    def __init__(self, test=None, on_npm=None, **kw):
        super().__init__(**kw)
        self.npm, self.agents, self.token_pip, self.on_npm = [], [], [], on_npm

    def run(self, argv, *, env=None, cwd=None, timeout=600):
        if argv[0].endswith("token/.venv/bin/python") and argv[1:3] == ["-m", "pip"]:
            self.calls.append(list(argv))
            self.token_pip.append((env["TMPDIR"], os.path.isdir(env["TMPDIR"])))
            return 0, "ok"
        if argv[0] == "npm":
            self.calls.append(list(argv))
            self.npm.append((list(argv), cwd, dict(env or {})))
            if self.on_npm:
                self.on_npm()
            Path(cwd, "node_modules").mkdir(exist_ok=True)
            return 0, "added 1 package"
        if argv[0].endswith("ga-venv/bin/python") and argv[1:4] == ["-m", "ga", "agy-agent"]:
            self.calls.append(list(argv))
            self.agents.append(list(argv))
            return 0, "installed"
        return super().run(argv, env=env, cwd=cwd, timeout=timeout)


class Full(Base):
    def setUp(self):
        super().setUp()
        root = Path(self.td.name)
        for n, body in (("pg_isready", PG), ("psql", PG), ("journalctl", JOURNAL)):
            (self.bin / n).write_text(body)
            (self.bin / n).chmod(0o755)
        self.agybin = root / "agybin"
        self.agybin.mkdir()
        (self.agybin / "agy").write_text(AGY)
        (self.agybin / "agy").chmod(0o755)
        bare, work = root / "token.git", root / "w-token"
        sh("git", "init", "-q", "--bare", str(bare))
        sh("git", "init", "-q", "-b", core.BRANCH, str(work))
        for rel, text in (("backend/pyproject.toml", "[project]\nname='token'\n"), ("frontend/package-lock.json", "{}\n"),
                          ("frontend/package.json", "{}\n"),
                          (".gitignore", ".env\n.venv/\nnode_modules/\n.ga/\n*.egg-info/\n"), ("docs/schema.sql", "select 1;\n"),
                          (".env.example", f"POSTGRES_PASSWORD={FAKE}\nDATABASE_URL=postgresql://u:{FAKE}@db/x\n"
                                           f"# GC_COMMENTED={FAKE}\nGC_JWT_SECRET={FAKE}\n")):
            p = work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        sh("git", "-C", str(work), "add", ".")
        sh("git", "-C", str(work), "commit", "-qm", "c1")
        sh("git", "-C", str(work), "push", "-q", str(bare), core.BRANCH)
        self.urls["token"] = str(bare)

    def agy(self, on=True):
        path = os.environ["PATH"]
        if on:
            path = f"{self.agybin}{os.pathsep}{path}"
        else:
            path = os.pathsep.join(p for p in path.split(os.pathsep) if p != str(self.agybin))
        return mock.patch.dict(os.environ, {"PATH": path})

    def full(self, runner=None, **kw):
        return self.inst(runner or Runner2(), full=True, token_url=self.urls["token"], **kw)

    def cfg(self):
        return json.loads((self.home / ".ga/console.json").read_text())

    def ready(self):
        """What the user does by hand: .env (in the Token checkout, git-ignored), PostgreSQL, agy-bridge.json."""
        if not (self.home / "token").exists():
            sh("git", "clone", "-q", "-b", core.BRANCH, self.urls["token"], str(self.home / "token"))
        (self.home / "token/.env").write_text(f"GC_JWT_SECRET={FAKE}\n")
        (self.home / "agy-bridge.json").write_text("{}\n")
        return mock.patch.dict(os.environ, {"FAKE_PG": "ok"})


class Install(Full):
    def test_dry_run_changes_nothing(self):
        before = self.snap()
        with self.agy():
            rc, r = self.full(dry_run=True)
        self.assertEqual(rc, 0, self.out)
        self.assertEqual(self.snap(), before)
        self.assertFalse((self.home / "token").exists() or (self.home / ".ga-ask").exists())
        self.assertEqual((r.npm, r.agents, r.token_pip, r.pip_tmp), ([], [], [], []))
        self.assertFalse([c for c in r.calls if c[:2] == ["git", "clone"]])
        self.assertNotIn("enable", self.logtext())
        self.assertIn("npm ci in", self.out)

    def test_full_layout(self):
        with self.agy(), self.ready():
            rc, r = self.full()
        self.assertEqual(rc, 0, self.out)
        h = str(self.home)
        cfg = self.cfg()
        self.assertEqual(set(cfg["services"]), {"token-api", "token-worker", "token-web"})
        self.assertEqual({x["name"]: x["path"] for x in cfg["repos"]},
                         {"baseline": f"{h}/baseline", "token": f"{h}/token", "ga-sdk": f"{h}/ga-sdk"})
        api = cfg["services"]["token-api"]
        self.assertEqual(api["argv"][0], f"{h}/token/.venv/bin/uvicorn")
        self.assertEqual(api["cwd"], f"{h}/token/backend")
        self.assertEqual(api["env_file"], f"{h}/token/.env")
        self.assertEqual(cfg["services"]["token-worker"]["argv"], [f"{h}/token/.venv/bin/python", "-m", "app.worker"])
        web = cfg["services"]["token-web"]
        self.assertEqual(web["cwd"], f"{h}/token/frontend")
        self.assertEqual(web["argv"][web["argv"].index("--hostname") + 1], "127.0.0.1")
        self.assertEqual(cfg["bridge"]["argv"], [f"{h}/ga-venv/bin/python", f"{h}/baseline/ops/agy_bridge/bridge.py",
                                                 "--config", f"{h}/agy-bridge.json"])
        self.assertIn(f"{h}/token/.ga/act/ledger", cfg["token_sources"]["act"])
        for n in cfg["services"]:
            self.assertNotIn("disabled", cfg["services"][n], n)
        self.assertIn("disabled", cfg["bridge"])  # the unit runs the bridge, never the console too
        loaded = K.load(self.home / ".ga/console.json")
        self.assertIn("bridge", loaded["services"])
        self.assertEqual(r.npm[0][0][:2], ["npm", "ci"])
        self.assertEqual(r.npm[0][1], f"{h}/token/frontend")
        tmp = r.npm[0][2]["TMPDIR"]
        self.assertTrue(tmp.startswith(f"{h}/.ga/") and not os.path.exists(tmp))
        self.assertTrue(r.npm[0][2]["npm_config_cache"].startswith(tmp))
        self.assertEqual(len(r.token_pip), 1)
        self.assertTrue(r.token_pip[0][1])
        unit = (self.home / ".config/systemd/user" / core.BRIDGE_UNIT).read_text()
        self.assertIn("Restart=always", unit)
        self.assertIn("vm check --disk-only", unit)
        self.assertIn("vm bridge-ready", unit)
        self.assertNotIn(f"enable --now {core.BRIDGE_UNIT}", self.logtext())
        self.assertNotIn(f"enable {core.BRIDGE_UNIT}", self.logtext())

    def test_second_full_changes_nothing(self):
        with self.agy(), self.ready():
            self.full()
            before = self.snap()
            rc, r = self.full()
        self.assertEqual(rc, 0)
        self.assertEqual(self.snap(), before)
        self.assertEqual((r.npm, r.token_pip, r.agents), ([], [], []))
        self.assertNotIn("write ", self.out)

    def test_missing_prereqs_printed_and_disabled(self):
        with self.agy(on=False):
            rc, r = self.full()
        self.assertEqual(rc, 0, self.out)
        cfg = self.cfg()
        for n in ("token-api", "token-worker", "token-web"):
            self.assertIn("missing", cfg["services"][n]["disabled"], n)
        self.assertIn("createdb -O $USER gaconsole", self.out)
        self.assertIn("python3 scripts/dev_env.py --db-host localhost", self.out)
        self.assertIn("POSTGRES_PASSWORD, DATABASE_URL, GC_JWT_SECRET", self.out)
        self.assertNotIn("GC_COMMENTED", self.out)
        self.assertIn("agy-bridge.json is missing", self.out)
        self.assertNotIn(FAKE, self.out)
        self.assertNotIn(FAKE, self.logtext())
        self.assertFalse((self.home / "token/.env").exists())  # never written by the installer
        self.assertFalse([c for c in r.calls if "sudo" in c or any("sudo" in a for a in c)])
        self.assertNotIn("sudo", self.logtext())
        self.assertIn("sudo apt-get install -y postgresql", self.out)  # printed for the user only
        # the console refuses a disabled service
        specs = K.load(self.home / ".ga/console.json")["services"]
        with mock.patch("ga.console.services.subprocess.Popen") as pop:
            snap = Services(specs).start("token-api")
        pop.assert_not_called()
        self.assertEqual(snap["state"], "stopped")

    def test_env_missing_only(self):
        with self.agy(), mock.patch.dict(os.environ, {"FAKE_PG": "ok"}):
            self.full()
        self.assertIn("~/token/.env", self.cfg()["services"]["token-api"]["disabled"])
        self.assertNotIn("PostgreSQL", self.cfg()["services"]["token-api"]["disabled"])
        self.assertNotIn("createdb", self.out)

    def test_ask_json_only_when_absent(self):
        with self.agy():
            self.full()
        ask = self.home / ".ga-ask/ask.json"
        self.assertEqual(json.loads(ask.read_text()), {"agent": "minimal"})
        ask.write_text('{"agent": "mine", "daily_agy_turns": 3}\n')
        os.utime(ask, ns=(1, 1))
        with self.agy():
            self.full()
        self.assertEqual(ask.read_text(), '{"agent": "mine", "daily_agy_turns": 3}\n')
        self.assertEqual(ask.stat().st_mtime_ns, 1)

    def test_agy_agents_only_with_agy(self):
        with self.agy(on=False):
            _, r = self.full()
        self.assertEqual(r.agents, [])
        for n in core.AGENTS:
            self.assertIn(f"ga agy-agent install --name {n}", self.out)
        with self.agy():
            _, r = self.full()
        py = str(self.home / "ga-venv/bin/python")
        self.assertEqual(r.agents, [[py, "-m", "ga", "agy-agent", "install", "--name", n] for n in ("minimal", "ga-plan", "ga-act")])

    def test_disk_guard_before_npm_ci(self):
        def low():
            self.free = 1 * GB
        r = Runner2()
        orig = r.run

        def run(argv, **kw):  # the disk fills while the backend installs: npm ci must not start
            out = orig(argv, **kw)
            if argv[0].endswith("token/.venv/bin/python") and "pip" in argv:
                low()
            return out
        r.run = run
        with self.agy(), self.ready():
            rc, _ = self.full(runner=r)
        self.assertEqual(rc, 1)
        self.assertEqual(r.npm, [])
        self.assertIn("disk guard before npm ci", self.out)
        self.assertFalse((self.home / ".ga/console.json").exists())
        self.assertFalse((self.home / ".config/systemd/user" / core.BRIDGE_UNIT).exists())
        self.assertNotIn("enable", self.logtext())

    def test_disk_guard_after_npm_ci(self):
        def fill():
            self.free = 1 * GB
        with self.agy(), self.ready():
            rc, r = self.full(runner=Runner2(on_npm=fill))
        self.assertEqual(rc, 1)
        self.assertIn("disk guard after npm ci", self.out)
        self.assertFalse((self.home / ".ga/console.json").exists())
        self.assertNotIn("enable", self.logtext())

    def test_no_wildcard_bind(self):
        with self.agy(), self.ready():
            self.full()
        texts = [(self.home / ".ga/console.json").read_text()]
        texts += [p.read_text() for p in (self.home / ".config/systemd/user").iterdir()]
        for t in texts:
            self.assertNotIn("0.0.0.0", t)
            self.assertNotIn("--host 0", t)
        unit = (self.home / ".config/systemd/user" / core.CONSOLE_UNIT).read_text()
        self.assertNotIn("--host", unit)  # ga console's own default: 127.0.0.1
        doc = (Path(__file__).resolve().parents[1] / "docs/VM.md").read_text()
        self.assertNotIn("0.0.0.0", doc)
        self.assertIn("ssh -N -L 8765:127.0.0.1:8765", doc)
        for cfg in (K.vm_full("/h"), K.vm_full("/h", {"token-api": "x", "bridge": "y"})):
            self.assertNotIn("0.0.0.0", json.dumps(cfg))

    def test_check_reports_agy(self):
        with self.agy():
            v = core.check(self.home, runner=FakeRunner(), free=self.free_fn, tmp=self.tmp,
                           baseline_url=self.urls["baseline"], sdk_url=self.urls["ga-sdk"])
        self.assertTrue(v["agy"])
        self.assertEqual(v["agy_version"], "agy 1.2.3")
        with self.agy(on=False):
            v = core.check(self.home, runner=FakeRunner(), free=self.free_fn, tmp=self.tmp,
                           baseline_url=self.urls["baseline"], sdk_url=self.urls["ga-sdk"])
        self.assertFalse(v["agy"])
        self.assertIsNone(v["agy_version"])

    def test_default_and_vm_unchanged(self):
        self.assertIsNone(K.vm("/h")["bridge"])
        self.assertEqual(K.vm("/h")["services"], {})
        self.assertEqual(K.default("/h")["services"]["token-web"]["argv"], ["npm", "run", "dev"])


class Bridge(Full):
    def setUp(self):
        super().setUp()
        sh("git", "clone", "-q", "-b", core.BRANCH, self.urls["baseline"], str(self.home / "baseline"))
        self.sender = Path(self.td.name) / "hub"
        sh("git", "clone", "-q", "-b", core.BRANCH, self.urls["baseline"], str(self.sender))
        self.hub = mb.Mailbox(self.sender)
        for i in range(3):
            self.hub.send("AGY", directive(f"CMD-OLD{i + 1}"), sender="baseline")
        self.hub.send("W1", directive("CMD-OTHER1", to="W1"), sender="baseline")
        self.vm = mb.Mailbox(self.home / "baseline")

    def adopt(self, *a):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["bridge-adopt", "--home", str(self.home), *a])
        self.out = out.getvalue()
        return rc

    def test_adopt_marks_old_then_new_is_handled(self):
        self.assertEqual(len(list(self.vm.unread("AGY"))), 3)  # what a fresh bridge would re-run
        self.assertEqual(self.adopt(), 0)
        self.assertIn("marked 3 message(s)", self.out)
        self.assertEqual(list(self.vm.unread("AGY")), [])  # the bridge's next pass handles none of them
        self.assertEqual(len(list(self.vm.unread("W1"))), 1)  # only the named recipient
        self.assertTrue((self.home / ".ga" / core.ADOPTED).is_file())
        self.hub.send("AGY", directive("CMD-NEW1"), sender="baseline")
        new = list(self.vm.unread("AGY"))
        self.assertEqual(len(new), 1)
        self.assertIn("CMD-NEW1", new[0].text)
        self.assertEqual(self.adopt(), 0)  # again: idempotent, the new one is now marked too
        self.assertIn("marked 1 message(s)", self.out)

    def test_enable_bridge_refusals(self):
        udir = self.home / ".config/systemd/user"
        udir.mkdir(parents=True)
        (udir / core.BRIDGE_UNIT).write_text(core.bridge_unit(self.home))
        (self.home / "agy-bridge.json").write_text("{}\n")

        def enable(*a):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["enable-bridge", "--home", str(self.home), *a], runner=FakeRunner())
            return rc, out.getvalue()
        with self.agy():
            rc, out = enable()
            self.assertEqual(rc, 1)
            self.assertIn(core.TWO_BRIDGES, out)
            rc, out = enable("--yes")  # no adopt marker yet
            self.assertEqual(rc, 1)
            self.assertIn("not adopted", out)
            self.assertNotIn("enable", self.logtext())
            self.adopt()
            rc, out = enable()  # everything ready but no --yes: still refused
            self.assertEqual(rc, 1)
            self.assertIn("give --yes", out)
            self.assertNotIn("enable", self.logtext())
            rc, out = enable("--yes")
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith(core.TWO_BRIDGES))
        self.assertIn(f"enable --now {core.BRIDGE_UNIT}", self.logtext())

    def test_bridge_ready(self):
        err = io.StringIO()
        with self.agy(on=False), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["bridge-ready", "--home", str(self.home)]), 1)
        self.assertIn("agy is not on PATH", err.getvalue())
        self.assertIn("not adopted", err.getvalue())
        self.adopt()
        (self.home / "agy-bridge.json").write_text("{}\n")
        with self.agy(), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["bridge-ready", "--home", str(self.home)]), 0)


class Url(Full):
    def test_url_and_tunnel(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["url", "--home", str(self.home), "--user", "ubuntu", "--host", "203.0.113.7"], runner=FakeRunner())
        o = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("console: http://127.0.0.1:8765/?t=newtok\n", o)
        self.assertNotIn("t=old", o)
        self.assertIn("http://127.0.0.1:8765/?t=newtok#/ask", o)
        self.assertIn("ssh -N -L 8765:127.0.0.1:8765 ubuntu@203.0.113.7", o)
        self.assertIn("journalctl --user -u ga-console.service", self.logtext())

    def test_no_url_yet(self):
        (self.bin / "journalctl").write_text("#!/bin/sh\necho 'starting'\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["url", "--home", str(self.home)], runner=FakeRunner())
        self.assertEqual(rc, 1)
        self.assertIn("ssh -N -L 8765:127.0.0.1:8765", out.getvalue())


class Version(unittest.TestCase):
    def test_version_0_12_0(self):
        import ga
        self.assertEqual(ga.__version__, "0.12.0")
        self.assertIn('version = "0.12.0"', (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())


if __name__ == "__main__":
    unittest.main()
