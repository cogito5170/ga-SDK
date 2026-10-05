"""CMD-OPS1 D1: ga vm check / install / status / enable-hub / uninstall, offline: a temp HOME, local bare git remotes, a fake
systemctl/loginctl on PATH that records argv, a fake disk-free function and a fake venv/pip/ga."""
import builtins
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ga.console import config as K
from ga.vm import cli, core

GB = core.GB
SC = """#!/usr/bin/env python3
import os, sys
a = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("systemctl " + " ".join(a) + "\\n")
st = os.environ["FAKE_STATE"]
os.makedirs(st, exist_ok=True)
flag = os.path.join(st, a[-1].replace("/", "_"))
if "show-environment" in a:
    sys.exit(1 if os.environ.get("FAKE_SC_FAIL") else 0)
if "is-enabled" in a:
    sys.exit(0 if os.path.exists(flag) else 1)
if "enable" in a:
    open(flag, "w").close()
if "disable" in a and os.path.exists(flag):
    os.unlink(flag)
"""
LC = """#!/bin/sh
echo "loginctl $*" >> "$FAKE_LOG"
echo "Linger=${FAKE_LINGER:-no}"
"""


def sh(*a, cwd=None):
    subprocess.run(list(a), cwd=cwd, check=True, capture_output=True, text=True)


class FakeRunner(core.Runner):
    """Real git and fake systemctl/loginctl (on PATH); venv, pip and `ga hub tick --help` are faked and recorded."""

    def __init__(self, shadow=False, pip_rc=0):
        self.calls, self.shadow, self.pip_rc, self.pip_tmp = [], shadow, pip_rc, []

    def run(self, argv, *, env=None, cwd=None, timeout=600):
        self.calls.append(list(argv))
        if argv[:3] == [sys.executable, "-m", "venv"]:
            py = Path(argv[3]) / "bin" / "python"
            py.parent.mkdir(parents=True)
            py.write_text("#!/bin/sh\n")
            return 0, ""
        if argv[0].endswith("ga-venv/bin/python") and argv[1:3] == ["-m", "pip"]:
            t = env["TMPDIR"]
            self.pip_tmp.append((t, os.path.isdir(t), env.get("PIP_NO_CACHE_DIR")))
            Path(t, "junk").write_text("x" * 100)
            return self.pip_rc, "boom" if self.pip_rc else "ok"
        if argv[0].endswith("ga-venv/bin/python") and argv[1:] == ["-m", "ga", "hub", "tick", "--help"]:
            return (0, "usage: ga hub tick [--shadow]") if self.shadow else (2, "usage: ga hub tick")
        return super().run(argv, env=env, cwd=cwd, timeout=timeout)


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        self.home, self.bin, self.log = root / "home", root / "bin", root / "fake.log"
        self.home.mkdir()
        self.bin.mkdir()
        for n, body in (("systemctl", SC), ("loginctl", LC)):
            (self.bin / n).write_text(body)
            (self.bin / n).chmod(0o755)
        env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(self.log), "FAKE_STATE": str(root / "state"),
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
               "FAKE_LINGER": "yes"}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.urls = {}
        for name in ("baseline", "ga-sdk"):
            bare, work = root / f"{name}.git", root / f"w-{name}"
            sh("git", "init", "-q", "--bare", str(bare))
            sh("git", "init", "-q", "-b", core.BRANCH, str(work))
            (work / "pyproject.toml").write_text(f"[project]\nname='{name}'\n")
            sh("git", "-C", str(work), "add", ".")
            sh("git", "-C", str(work), "commit", "-qm", "c1")
            sh("git", "-C", str(work), "push", "-q", str(bare), core.BRANCH)
            self.urls[name] = str(bare)
        self.tmp = root / "systmp"
        self.tmp.mkdir()
        self.free = 20 * GB

    def free_fn(self, _p):
        return self.free

    def inst(self, runner=None, **kw):
        runner = runner or FakeRunner()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = core.install(self.home, runner=runner, free=self.free_fn, tmp=self.tmp, baseline_url=self.urls["baseline"],
                              sdk_url=self.urls["ga-sdk"], **kw)
        self.out = out.getvalue()
        return rc, runner

    def snap(self):
        s = {}
        for p in sorted(self.home.rglob("*")):
            if p.is_file() and ".git" not in p.parts and "ga-venv" not in p.parts:
                s[str(p)] = (p.read_bytes(), p.stat().st_mtime_ns)
        return s

    def logtext(self):
        return self.log.read_text() if self.log.exists() else ""


class Check(Base):
    def chk(self, **kw):
        return core.check(self.home, runner=kw.pop("runner", None) or FakeRunner(), free=self.free_fn, tmp=self.tmp,
                          baseline_url=kw.pop("baseline_url", self.urls["baseline"]), sdk_url=self.urls["ga-sdk"], **kw)

    def test_ready(self):
        v = self.chk()
        self.assertTrue(v["ready"], v)
        self.assertEqual(v["problems"], [])
        self.assertTrue(v["linger"] and v["python_ok"] and v["systemctl_user"] and v["git"])
        self.assertTrue(v["arch"] and v["os"] and "agy" in v)

    def test_low_disk(self):
        self.free = 1 * GB
        v = self.chk()
        self.assertFalse(v["ready"])
        self.assertFalse(v["disk"]["ok"])
        self.assertTrue(any("disk" in p for p in v["problems"]))

    def test_min_free_param(self):
        self.free = 5 * GB
        self.assertTrue(self.chk(min_free_gb=4)["ready"])
        self.assertFalse(self.chk(min_free_gb=6)["ready"])

    def test_systemctl_unreachable(self):
        with mock.patch.dict(os.environ, {"FAKE_SC_FAIL": "1"}):
            v = self.chk()
        self.assertFalse(v["ready"] or v["systemctl_user"])

    def test_remote_unreachable(self):
        v = self.chk(baseline_url=str(self.home / "nope.git"))
        self.assertFalse(v["ready"])
        self.assertFalse(v["remotes"]["baseline"])
        self.assertTrue(v["remotes"]["ga-sdk"])

    def test_old_python_and_no_git(self):
        with mock.patch.object(core.sys, "version_info", (3, 9, 1)):
            v = self.chk()
        self.assertFalse(v["python_ok"] or v["ready"])
        with mock.patch.object(core.shutil, "which", return_value=None):
            v = self.chk()
        self.assertFalse(v["git"] or v["ready"])
        self.assertFalse(v["agy"])

    def test_linger_off_is_reported_not_blocking(self):
        with mock.patch.dict(os.environ, {"FAKE_LINGER": "no"}):
            v = self.chk()
        self.assertFalse(v["linger"])
        self.assertTrue(v["ready"])

    def test_stale_pip_listed_not_deleted(self):
        for n, size in (("pip-target-aa", 300), ("pip-unpack-bb", 900), ("pip-build-cc", 10), ("other", 5000)):
            d = self.tmp / n
            d.mkdir()
            (d / "f").write_bytes(b"x" * size)
        v = self.chk()
        names = [Path(d["path"]).name for d in v["stale_pip"]]
        self.assertEqual(sorted(names), ["pip-build-cc", "pip-target-aa", "pip-unpack-bb"])
        self.assertTrue(all((self.tmp / n).exists() for n in names))
        self.assertEqual(core.stale_pip(self.tmp)[0]["path"], str(self.tmp / "pip-unpack-bb"))

    def test_cli_exit_codes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["check", "--home", str(self.home), "--baseline-url", self.urls["baseline"], "--sdk-url", self.urls["ga-sdk"]],
                          runner=FakeRunner(), free=self.free_fn, tmp=str(self.tmp))
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["ready"])
        self.free = 1 * GB
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["check", "--home", str(self.home), "--disk-only"], free=self.free_fn)
        self.assertEqual(rc, 1)


class Install(Base):
    def test_clean_install_layout(self):
        rc, r = self.inst()
        self.assertEqual(rc, 0, self.out)
        h = self.home
        self.assertTrue((h / "baseline" / ".git").is_dir() and (h / "ga-sdk" / "pyproject.toml").is_file())
        for u in core.UNITS:
            self.assertTrue((h / ".config/systemd/user" / u).is_file(), u)
        cfg = K.load(h / ".ga/console.json")
        self.assertEqual(cfg["baseline"], str(h / "baseline"))
        self.assertEqual({x["name"]: x["path"] for x in cfg["repos"]}, {"baseline": str(h / "baseline"), "ga-sdk": str(h / "ga-sdk")})
        self.assertEqual(cfg["services"], {})
        self.assertIsNone(cfg["bridge"])
        self.assertIn("enable --now ga-console.service", self.logtext())

    def test_sudo_line_printed_when_linger_off(self):
        with mock.patch.dict(os.environ, {"FAKE_LINGER": "no"}):
            self.inst()
        self.assertIn("sudo loginctl enable-linger $USER", self.out)

    def test_never_sudo(self):
        with mock.patch.dict(os.environ, {"FAKE_LINGER": "no"}):  # the branch that prints the sudo line
            _, r = self.inst()
            self.inst(r)
        self.assertFalse([c for c in r.calls if "sudo" in c])
        self.assertNotIn("sudo", self.logtext())

    def test_second_install_changes_nothing(self):
        self.inst()
        before = self.snap()
        n = len(self.logtext().splitlines())
        rc, r = self.inst()
        self.assertEqual(rc, 0)
        self.assertEqual(self.snap(), before)
        self.assertEqual(r.pip_tmp, [])
        self.assertNotIn("daemon-reload", "\n".join(self.logtext().splitlines()[n:]))
        self.assertNotIn("write ", self.out)

    def test_changed_remote_fast_forwards(self):
        self.inst()
        w = Path(self.td.name) / "w-baseline"
        (w / "new.txt").write_text("n")
        sh("git", "-C", str(w), "add", ".")
        sh("git", "-C", str(w), "commit", "-qm", "c2")
        sh("git", "-C", str(w), "push", "-q", self.urls["baseline"], core.BRANCH)
        rc, _ = self.inst()
        self.assertEqual(rc, 0)
        self.assertTrue((self.home / "baseline" / "new.txt").is_file())

    def test_dirty_checkout_stops_install(self):
        self.inst()
        dirty = self.home / "ga-sdk" / "pyproject.toml"
        dirty.write_text("local edit\n")
        before = self.snap()
        rc, r = self.inst()
        self.assertEqual(rc, 1)
        self.assertIn("local changes", self.out)
        self.assertEqual(dirty.read_text(), "local edit\n")  # not reset
        self.assertEqual(self.snap(), before)
        self.assertEqual(r.pip_tmp, [])
        self.assertFalse([c for c in r.calls if c[:1] == ["git"] and ("reset" in c or "checkout" in c or "merge" in c)])

    def test_non_ff_never_forced(self):
        self.inst()
        c = self.home / "ga-sdk"
        (c / "mine.txt").write_text("m")
        sh("git", "-C", str(c), "add", ".")
        sh("git", "-C", str(c), "commit", "-qm", "local")
        w = Path(self.td.name) / "w-ga-sdk"
        (w / "up.txt").write_text("u")
        sh("git", "-C", str(w), "add", ".")
        sh("git", "-C", str(w), "commit", "-qm", "up")
        sh("git", "-C", str(w), "push", "-q", self.urls["ga-sdk"], core.BRANCH)
        rc, _ = self.inst()
        self.assertEqual(rc, 1)
        self.assertTrue((c / "mine.txt").is_file())
        self.assertFalse((c / "up.txt").exists())

    def test_plain_folder_refused_and_untouched(self):
        d = self.home / "baseline"
        d.mkdir()
        (d / "mine.txt").write_text("m")
        rc, r = self.inst()
        self.assertEqual(rc, 1)
        self.assertIn("not a git checkout", self.out)
        self.assertEqual([p.name for p in d.iterdir()], ["mine.txt"])
        self.assertEqual(r.pip_tmp, [])
        self.assertFalse((self.home / "ga-sdk").exists())

    def test_plain_folder_in_a_git_home_leaves_the_parent_repo_alone(self):
        sh("git", "init", "-q", "-b", "main", str(self.home))
        (self.home / "keep.txt").write_text("k")
        sh("git", "-C", str(self.home), "add", ".")
        sh("git", "-C", str(self.home), "commit", "-qm", "home")
        (self.home / "baseline").mkdir()
        (self.home / "baseline" / "mine.txt").write_text("m")
        head = subprocess.run(["git", "-C", str(self.home), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        rc, r = self.inst()
        self.assertEqual(rc, 1)
        self.assertIn("not a git checkout", self.out)
        self.assertEqual(subprocess.run(["git", "-C", str(self.home), "rev-parse", "HEAD"], capture_output=True, text=True).stdout, head)
        self.assertFalse([c for c in r.calls if c[:1] == ["git"] and c[1:2] in (["-C"],) and any(x in c for x in ("checkout", "merge", "reset", "fetch"))])
        self.assertEqual((self.home / "baseline" / "mine.txt").read_text(), "m")

    def test_failing_git_status_stops_install(self):
        self.inst()

        class Bad(FakeRunner):
            def run(self, argv, **kw):
                if argv[:1] == ["git"] and "status" in argv:
                    self.calls.append(list(argv))
                    return 128, "fatal"
                return super().run(argv, **kw)
        before = self.snap()
        rc, r = self.inst(Bad())
        self.assertEqual(rc, 1)
        self.assertIn("local changes", self.out)
        self.assertEqual(self.snap(), before)
        self.assertEqual(r.pip_tmp, [])

    def test_low_disk_stops_before_any_write(self):
        self.free = 1 * GB
        before = set(os.listdir(self.home))
        rc, r = self.inst()
        self.assertEqual(rc, 1)
        self.assertEqual(set(os.listdir(self.home)), before)
        self.assertEqual(r.pip_tmp, [])
        self.assertNotIn("clone", " ".join(" ".join(c) for c in r.calls))
        self.assertNotIn("enable", self.logtext())

    def test_pip_no_cache_and_private_tmpdir_removed(self):
        _, r = self.inst()
        pip = [c for c in r.calls if c[1:3] == ["-m", "pip"]]
        self.assertEqual(len(pip), 1)
        self.assertIn("--no-cache-dir", pip[0])
        self.assertIn("-e", pip[0])
        tmpdir, existed, nocache = r.pip_tmp[0]
        self.assertTrue(existed)
        self.assertEqual(nocache, "1")
        self.assertFalse(os.path.exists(tmpdir))
        self.assertNotEqual(os.path.dirname(tmpdir), "/tmp")
        self.assertEqual(list((self.home / ".ga").glob("vm-pip-*")), [])

    def test_pip_failure_still_removes_tmpdir_and_stops(self):
        rc, r = self.inst(FakeRunner(pip_rc=1))
        self.assertEqual(rc, 1)
        self.assertIn("pip install failed", self.out)
        self.assertFalse(os.path.exists(r.pip_tmp[0][0]))
        self.assertEqual(list((self.home / ".ga").glob("vm-pip-*")), [])
        self.assertFalse((self.home / ".config/systemd/user/ga-console.service").exists())
        rc, r2 = self.inst()  # a later run retries pip (the marker was not written)
        self.assertEqual(rc, 0)
        self.assertEqual(len(r2.pip_tmp), 1)

    def test_dry_run_writes_nothing(self):
        rc, r = self.inst(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(os.listdir(self.home), [])
        self.assertIn("would: ", self.out)
        self.assertEqual(self.logtext().count("enable"), 0)
        self.assertEqual(r.pip_tmp, [])
        self.assertFalse([c for c in r.calls if c[0] == "git" and c[1] in ("clone", "fetch")])


class Units(Base):
    def unit(self, n):
        return (self.home / ".config/systemd/user" / n).read_text()

    def test_console_unit(self):
        self.inst()
        u = self.unit(core.CONSOLE_UNIT)
        self.assertIn(f"ExecStart={self.home}/ga-venv/bin/python -m ga console --config {self.home}/.ga/console.json --port 8765 --no-open", u)
        self.assertIn("NoNewPrivileges=yes", u)
        self.assertIn("Restart=always", u)
        self.assertIn("ga vm check --disk-only", u)
        self.assertNotRegex(u, r"--host|0\.0\.0\.0|::")
        self.assertEqual(set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", u)) - {"127.0.0.1"}, set())

    def test_hub_units_guard_and_flags(self):
        self.inst()
        h = self.unit(core.HUB_UNIT)
        self.assertIn("ga hub tick --shadow", h)
        self.assertIn("NoNewPrivileges=yes", h)
        self.assertIn("ga vm check --disk-only", h)
        self.assertIn("OnUnitActiveSec=60", self.unit(core.HUB_TIMER))

    def test_hub_stays_disabled_without_shadow_then_enable_hub(self):
        self.inst(FakeRunner(shadow=False))
        self.assertNotIn("enable --now ga-hub.timer", self.logtext())
        self.assertIn("left disabled", self.out)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(core.enable_hub(self.home, runner=FakeRunner(shadow=False)), 1)
        self.assertNotIn("enable --now ga-hub.timer", self.logtext())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(core.enable_hub(self.home, runner=FakeRunner(shadow=True)), 0)
        self.assertIn("enable --now ga-hub.timer", self.logtext())

    def test_hub_enabled_at_install_when_shadow_present(self):
        self.inst(FakeRunner(shadow=True))
        self.assertIn("enable --now ga-hub.timer", self.logtext())

    def test_status(self):
        self.inst()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            core.status(self.home, runner=FakeRunner(), free=self.free_fn)
        t = out.getvalue()
        self.assertIn("ga-console.service", t)
        self.assertIn("ga-hub.timer", t)
        self.assertIn("free disk: 20.0 GB", t)
        self.assertIn("ssh -N -L 8765:127.0.0.1:8765", t)


class Uninstall(Base):
    def test_removes_only_what_install_wrote(self):
        mine = self.home / ".ga" / "mine.json"
        mine.parent.mkdir()
        mine.write_text("keep")
        other = self.home / ".config/systemd/user/other.service"
        other.parent.mkdir(parents=True)
        other.write_text("keep")
        self.inst()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(core.uninstall(self.home, runner=FakeRunner()), 0)
        self.assertEqual((mine.read_text(), other.read_text()), ("keep", "keep"))
        for u in core.UNITS:
            self.assertFalse((self.home / ".config/systemd/user" / u).exists())
        self.assertFalse((self.home / ".ga/console.json").exists())
        self.assertTrue((self.home / "baseline/.git").is_dir() and (self.home / "ga-sdk/.git").is_dir())
        self.assertIn("disable --now ga-console.service", self.logtext())


class NoCredentials(Base):
    def test_no_credential_file_is_opened(self):
        for d in [".ssh/id_ed25519", ".config/gh/hosts.yml", ".git-credentials", ".config/token/store", ".netrc"]:
            p = self.home / d
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("FAKE-SECRET")
        bad = re.compile(r"\.ssh|\.config/gh|\.git-credentials|\.netrc|/token|id_rsa|id_ed25519")
        seen = []
        real_open, real_io_open, real_rt, real_rb = builtins.open, io.open, Path.read_text, Path.read_bytes

        def guard(real):
            def f(file, *a, **k):
                if isinstance(file, (str, os.PathLike)) and bad.search(str(file)):
                    seen.append(str(file))
                    raise AssertionError(f"credential file opened: {file}")
                return real(file, *a, **k)
            return f

        def rd(real):
            def f(self_, *a, **k):
                if bad.search(str(self_)):
                    seen.append(str(self_))
                    raise AssertionError(f"credential file read: {self_}")
                return real(self_, *a, **k)
            return f
        with mock.patch.object(builtins, "open", guard(real_open)), mock.patch.object(io, "open", guard(real_io_open)), \
                mock.patch.object(Path, "read_text", rd(real_rt)), mock.patch.object(Path, "read_bytes", rd(real_rb)):
            self.inst()
            self.inst()
            with contextlib.redirect_stdout(io.StringIO()):
                core.status(self.home, runner=FakeRunner(), free=self.free_fn)
                core.check(self.home, runner=FakeRunner(), free=self.free_fn, tmp=self.tmp, baseline_url=self.urls["baseline"],
                           sdk_url=self.urls["ga-sdk"])
                core.uninstall(self.home, runner=FakeRunner())
        self.assertEqual(seen, [])
        self.assertEqual((self.home / ".ssh/id_ed25519").read_text(), "FAKE-SECRET")


if __name__ == "__main__":
    unittest.main()
