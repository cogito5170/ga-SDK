"""CMD-GA48 D1: `ga vm update` offline — a temp HOME, local bare remotes, a fake systemctl/pip/version in the Runner."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ga.vm import cli, core

GB = core.GB
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


def sh(*a, cwd=None):
    return subprocess.run(list(a), cwd=cwd, check=True, capture_output=True, text=True, env={**os.environ, **ENV}).stdout


class R(core.Runner):
    def __init__(self, enabled=(core.CONSOLE_UNIT, core.BRIDGE_UNIT), version="1.0.0"):
        self.calls, self.enabled, self.version = [], set(enabled), version

    def run(self, argv, *, env=None, cwd=None, timeout=600):
        a = list(argv)
        if a[0] == "systemctl":
            self.calls.append(a)
            if "is-enabled" in a:
                return (0, "enabled") if a[-1] in self.enabled else (1, "disabled")
            return 0, ""
        if a[0].endswith("ga-venv/bin/python"):
            self.calls.append(a)
            return (0, self.version) if a[1] == "-c" else (0, "ok")
        return super().run(argv, env=env, cwd=cwd, timeout=timeout)

    def n(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]

    def pips(self):
        return [c for c in self.calls if c[1:3] == ["-m", "pip"]]

    def restarts(self):
        return [c[-1] for c in self.calls if c[:3] == ["systemctl", "--user", "restart"]]


class Upd(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.work, self.mailbox_checks = {}, None
        for n in ("ga-sdk", "baseline", "token"):
            bare, w = self.root / f"{n}.git", self.root / f"w-{n}"
            sh("git", "init", "-q", "--bare", str(bare))
            sh("git", "init", "-q", "-b", core.BRANCH, str(w))
            (w / "a.txt").write_text("1\n")
            (w / "pyproject.toml").write_text("[project]\nname='x'\n")
            sh("git", "add", "-A", cwd=w)
            sh("git", "commit", "-q", "-m", "c1", cwd=w)
            sh("git", "remote", "add", "origin", str(bare), cwd=w)
            sh("git", "push", "-q", "origin", core.BRANCH, cwd=w)
            sh("git", "clone", "-q", "--branch", core.BRANCH, str(bare), str(self.home / n))
            self.work[n] = w
        py = self.home / "ga-venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text("#!/bin/sh\n")
        self.free = 50 * GB

    def push(self, n, text="x"):
        w = self.work[n]
        (w / "a.txt").write_text((w / "a.txt").read_text() + text + "\n")
        sh("git", "commit", "-q", "-am", "more", cwd=w)
        sh("git", "push", "-q", "origin", core.BRANCH, cwd=w)

    def up(self, r, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = core.update(self.home, runner=r, free=lambda p: self.free, **kw)
        return rc, out.getvalue()

    def lines(self):
        f = self.home / ".ga" / core.UPDATE_LOG
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def mails(self):
        box = self.root / "baseline.git"
        files = sh("git", "--git-dir", str(box), "ls-tree", "-r", "--name-only", "ga-mailbox").split() \
            if "ga-mailbox" in sh("git", "--git-dir", str(box), "branch", "--list", "ga-mailbox") else []
        return [f for f in files if f.startswith(f"to/{core.NOTICE_TO}/") and not f.endswith(f"-{core.R0_ASK}.md")]  # acks only (DEV-VMSHA: R0 reports are counted apart)

    def snapshot(self):
        return {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file() and ".git/" not in str(p)}

    def head(self, n):
        return sh("git", "-C", str(self.home / n), "rev-parse", "HEAD").strip()

    def first(self):  # settle the marker and the notice, so the next run starts from "nothing new"
        r = R()
        self.up(r)
        return r


class Tests(Upd):
    def test_new_sdk_commit_ff_pip_once_restart_one_mail(self):
        self.first()
        self.push("ga-sdk")
        r = R(version="2.0.0")
        rc, _ = self.up(r)
        self.assertEqual(rc, 0)
        self.assertEqual(self.head("ga-sdk"), sh("git", "-C", str(self.work["ga-sdk"]), "rev-parse", "HEAD").strip())
        self.assertEqual(len(r.pips()), 1)
        self.assertEqual(sorted(r.restarts()), sorted([core.CONSOLE_UNIT, core.BRIDGE_UNIT]))
        ln = self.lines()[-1]
        self.assertTrue(ln["pip"])
        self.assertNotEqual(ln["heads"]["before"]["ga-sdk"], ln["heads"]["after"]["ga-sdk"])
        self.assertEqual(len(self.mails()), 2)  # 1.0.0 on the first run, 2.0.0 now
        self.assertEqual(ln["mail"], "sent")

    def test_ack_mail_content(self):
        self.first()
        box = self.root / "baseline.git"
        f = self.mails()[0]
        text = sh("git", "--git-dir", str(box), "show", f"ga-mailbox:{f}")
        self.assertIn('"kind": "ack"', text.replace('":"', '": "'))
        self.assertIn("1.0.0", text)
        self.assertIn("-vm-", f)

    def test_second_run_changes_nothing(self):
        self.first()
        before = self.snapshot()
        mails = self.mails()
        r = R()
        self.up(r)
        self.assertEqual(r.pips(), [])
        self.assertEqual(r.restarts(), [])
        self.assertEqual(self.mails(), mails)
        after = self.snapshot()
        changed = [k for k in after if after[k] != before.get(k)]
        self.assertEqual([Path(k).name for k in changed], [core.UPDATE_LOG])  # only the one log line
        self.assertEqual(self.lines()[-1]["mail"], "same")

    def test_dirty_token_left_alone_sdk_updates(self):
        self.first()
        (self.home / "token" / "scratch.txt").write_text("local edit\n")  # untracked: a merge alone would not refuse
        tok = self.head("token")
        self.push("token")
        self.push("ga-sdk")
        r = R()
        self.up(r)
        self.assertEqual(self.head("token"), tok)
        self.assertEqual((self.home / "token" / "scratch.txt").read_text(), "local edit\n")
        self.assertEqual(self.head("ga-sdk"), sh("git", "-C", str(self.work["ga-sdk"]), "rev-parse", "HEAD").strip())
        self.assertTrue(any(s.startswith("token:") for s in self.lines()[-1]["skipped"]))
        self.assertEqual(len(r.pips()), 1)

    def test_non_ff_refused_never_reset(self):
        self.first()
        sdk = self.home / "ga-sdk"
        (sdk / "b.txt").write_text("mine\n")
        sh("git", "add", "-A", cwd=sdk)
        sh("git", "commit", "-q", "-m", "local", cwd=sdk)
        mine = self.head("ga-sdk")
        self.push("ga-sdk")
        r = R()
        self.up(r)
        self.assertEqual(self.head("ga-sdk"), mine)
        self.assertTrue((sdk / "b.txt").exists())
        self.assertTrue(any("ga-sdk:" in s for s in self.lines()[-1]["skipped"]))
        self.assertEqual(r.restarts(), [])
        self.assertTrue(all("--hard" not in " ".join(c) and "--force" not in " ".join(c) and "-f" not in c for c in r.calls))

    def test_baseline_only_no_pip_no_restart(self):
        self.first()
        self.push("baseline")
        r = R()
        self.up(r)
        self.assertEqual(r.pips(), [])
        self.assertEqual(r.restarts(), [])
        ln = self.lines()[-1]
        self.assertNotEqual(ln["heads"]["before"]["baseline"], ln["heads"]["after"]["baseline"])

    def test_disabled_bridge_not_restarted(self):
        self.first()
        self.push("ga-sdk")
        r = R(enabled=(core.CONSOLE_UNIT,))
        self.up(r)
        self.assertEqual(r.restarts(), [core.CONSOLE_UNIT])
        self.assertEqual(self.lines()[-1]["restarted"], [core.CONSOLE_UNIT])

    def test_disk_guard_stops_before_fetch(self):
        self.push("ga-sdk")
        self.free = 1 * GB
        r = R()
        rc, _ = self.up(r)
        self.assertEqual(rc, 1)
        self.assertEqual([c for c in r.calls if "fetch" in c], [])
        self.assertEqual(r.calls, [])
        self.assertFalse((self.home / ".ga" / core.UPDATE_LOG).exists())

    def test_mail_failure_retried_next_run(self):
        self.first()
        n = len(self.mails())
        r = R(version="3.0.0")
        from ga.mailbox import MailError
        with mock.patch("ga.mailbox.Mailbox.send", side_effect=MailError("down")):
            self.up(r)
        self.assertTrue(self.lines()[-1]["mail"].startswith("failed"))
        self.assertEqual(len(self.mails()), n)
        self.up(R(version="3.0.0"))
        self.assertEqual(len(self.mails()), n + 1)
        self.up(R(version="3.0.0"))
        self.assertEqual(len(self.mails()), n + 1)

    def test_dry_run_changes_nothing(self):
        self.push("ga-sdk")
        before = self.snapshot()
        h = self.head("ga-sdk")
        r = R()
        rc, out = self.up(r, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.head("ga-sdk"), h)
        self.assertEqual(r.pips() + r.restarts(), [])

    def test_cli_wired(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["update", "--home", str(self.home), "--dry-run"], runner=R(), free=lambda p: self.free)
        self.assertEqual(rc, 0)


class Units(unittest.TestCase):
    def test_update_units(self):
        u = core.update_units(Path("/h"))
        self.assertIn("Type=oneshot", u[core.UPDATE_UNIT])
        self.assertIn("-m ga vm update --home /h", u[core.UPDATE_UNIT])
        self.assertIn("OnBootSec=2min", u[core.UPDATE_TIMER])
        self.assertIn("OnUnitActiveSec=30min", u[core.UPDATE_TIMER])
        self.assertNotIn(core.UPDATE_UNIT, core.unit_files(Path("/h")))


class Version(unittest.TestCase):
    def test_version_0_17_0(self):
        import ga
        self.assertGreaterEqual(tuple(map(int, ga.__version__.split("."))), (0, 17, 0))  # GA49 moved it on to 0.17.1
        self.assertIn(f'version = "{ga.__version__}"', (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())


from tests.test_ops2 import Full  # noqa: E402


class FullTimer(Full):
    def test_full_writes_and_enables_timer_status_shows_line(self):
        with self.agy(), self.ready():
            rc, r = self.full()
        self.assertEqual(rc, 0, self.out)
        udir = self.home / ".config/systemd/user"
        self.assertEqual((udir / core.UPDATE_TIMER).read_text(), core.update_units(self.home)[core.UPDATE_TIMER])
        self.assertTrue((udir / core.UPDATE_UNIT).is_file())
        self.assertIn(f"enable --now {core.UPDATE_TIMER}", self.logtext())
        # a second full install does not enable it again
        n = self.logtext().count(f"enable --now {core.UPDATE_TIMER}")
        with self.agy(), self.ready():
            self.full()
        self.assertEqual(self.logtext().count(f"enable --now {core.UPDATE_TIMER}"), n)
        (self.home / ".ga" / core.UPDATE_LOG).write_text('{"at":"T","pip":false}\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            core.status(self.home, runner=r, free=self.free_fn)
        self.assertIn('last update: {"at":"T","pip":false}', out.getvalue())

    def test_plain_install_has_no_timer(self):
        rc, _ = self.inst()
        self.assertEqual(rc, 0, self.out)
        self.assertFalse((self.home / ".config/systemd/user" / core.UPDATE_TIMER).exists())


if __name__ == "__main__":
    unittest.main()
