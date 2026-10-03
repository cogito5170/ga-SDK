"""CMD-GR2: the remote profile -- the rlo guard as project settings in a worker's repo (generalised from amp 344a604).

D1: the generated files behave like amp's v2 on the S4 cases (fresh-time transcripts, the real guard.sh under bash).
D2: mutations are killed: a plumbing tool dropped, a deny path turned into allow, shadow mode, a widened grant,
push code added (in the guard files, or ga-rlo itself running git).
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ga.rlo import remote

from .world import run_cli

# named here, not taken from remote.PLUMBING: dropping one from the preset must fail a test (D2)
PLUMBING = {"ToolSearch": "read", "ReadNotifications": "read", "mcp__github__issue_read": "read",
            "mcp__github__add_issue_comment": "external", "mcp__claude-code-remote__send_message": "external"}
VENV = sys.prefix  # a venv with rlo installed stands for the one install.sh makes


def guard_sh(repo: Path) -> Path:
    return repo / "ops" / "rlo" / "guard.sh"


class RemoteBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ga-rlo-remote-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "work"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("# work\n")
        self.home = self.tmp / "person"
        (self.home / ".claude").mkdir(parents=True)
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old) if old else os.environ.pop("HOME"))

    def init(self, *extra):
        return run_cli("init", "--profile", "remote", "--work-repo", str(self.repo), "--name", "W1", *extra)

    def doctor_failed(self) -> set:
        rc, out, _ = run_cli("doctor", "--profile", "remote", "--work-repo", str(self.repo), "--venv", VENV, "--json")
        bad = {c["check"] for c in json.loads(out)["checks"] if not c["ok"]}
        self.assertEqual(rc != 0, bool(bad))
        return bad

    def edit(self, rel, old, new):
        p = self.repo / rel
        text = p.read_text()
        self.assertIn(old, text)
        p.write_text(text.replace(old, new))


class GenerateTest(RemoteBase):
    def test_writes_the_files_and_prints_commands_never_runs_them(self):
        rc, out, err = self.init()
        self.assertEqual(rc, 0, err)
        for rel in (".claude/settings.json", "ops/rlo/install.sh", "ops/rlo/guard.sh", "ops/rlo/model.json", "ops/rlo/GUARD.md"):
            self.assertTrue((self.repo / rel).is_file(), rel)
        self.assertTrue(os.access(guard_sh(self.repo), os.X_OK))
        self.assertIn("git -C", out)
        self.assertIn("push origin HEAD", out)
        self.assertIn("does not commit or push", out)
        self.assertEqual(remote.problems(self.repo), [])
        s = json.loads((self.repo / ".claude/settings.json").read_text())
        self.assertEqual(s["hooks"]["PreToolUse"], [{"matcher": "*", "hooks": [
            {"type": "command", "command": 'bash "$CLAUDE_PROJECT_DIR/ops/rlo/guard.sh"', "timeout": 600}]}])
        self.assertEqual(s["hooks"]["SessionStart"][0]["hooks"][0]["command"], 'bash "$CLAUDE_PROJECT_DIR/ops/rlo/install.sh"')
        g = guard_sh(self.repo).read_text()
        self.assertIn('REC="$HOME/.rlo/W1.jsonl"', g)
        self.assertNotIn("amp", g.lower())  # generalised: no amp-specific names
        self.assertIn("3323f88741c198f453370936c481c00fbd26d398", (self.repo / "ops/rlo/install.sh").read_text())

    def test_the_model_always_has_the_plumbing(self):
        self.init()
        specs = {s["name"]: s for s in json.loads((self.repo / "ops/rlo/model.json").read_text())["specs"]}
        for name, risk in PLUMBING.items():
            self.assertEqual(specs[name]["risk"], risk, name)
        for name in ("Bash", "Read", "Edit", "Write", "Grep", "Glob"):
            self.assertIn(name, specs)
        for name in ("WebFetch", "Agent", "mcp__claude-code-remote__create_session"):
            self.assertNotIn(name, specs)
        self.assertIn("offset", specs["Read"]["params"])  # what W1 measured
        md = (self.repo / "ops/rlo/GUARD.md").read_text()
        self.assertIn(remote.DENY_REPORT, md)
        for g in remote.GRANTS:
            self.assertIn(f"`{g}`", md)

    def test_keeps_other_settings_and_hooks_and_installs_once(self):
        sp = self.repo / ".claude" / "settings.json"
        sp.parent.mkdir()
        sp.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-hook"}]}]}}))
        self.assertEqual(self.init()[0], 0)
        self.assertEqual(self.init()[0], 0)  # again: same files, ours placed once
        s = json.loads(sp.read_text())
        self.assertEqual(s["permissions"], {"allow": ["Read"]})
        cmds = [h["command"] for g in s["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertEqual(cmds, ["other-hook", remote.GUARD_CMD])
        self.assertEqual(len(s["hooks"]["SessionStart"]), 1)

    def test_does_not_overwrite_a_changed_guard_file(self):
        self.init()
        guard_sh(self.repo).write_text("#!/bin/sh\nexit 0\n")
        rc, _, err = self.init()
        self.assertEqual(rc, 2)
        self.assertIn("not overwritten", err)
        self.assertEqual(guard_sh(self.repo).read_text(), "#!/bin/sh\nexit 0\n")
        self.assertEqual(self.init("--force")[0], 0)
        self.assertEqual(remote.problems(self.repo), [])

    def test_refusals(self):
        rc, _, err = run_cli("init", "--profile", "remote")
        self.assertEqual(rc, 2)
        self.assertIn("--work-repo", err)
        rc, _, err = run_cli("init", "--profile", "remote", "--work-repo", str(self.repo), "--name", "a b")
        self.assertEqual(rc, 2)
        sp = self.repo / ".claude" / "settings.json"
        sp.parent.mkdir()
        sp.write_text("not json")
        rc, _, err = self.init()
        self.assertEqual(rc, 2)
        self.assertFalse((self.repo / "ops").exists())


class ReplayTest(RemoteBase):
    """D1/S4 through `ga-rlo doctor --profile remote`, and D2 mutations."""

    def setUp(self):
        super().setUp()
        self.init()

    def test_the_generated_guard_passes_doctor(self):
        self.assertEqual(self.doctor_failed(), set())

    def test_s4_cases_are_all_there(self):
        names = {c.name: c.want for c in remote.cases()}
        for t in PLUMBING:
            self.assertEqual(names[f"plumbing {t}"], "pass")
        self.assertEqual(names["A1 WebFetch"], "A1")
        self.assertEqual(names["A1 mcp__claude-code-remote__create_session"], "A1")
        self.assertEqual(names["Bash after 2h idle"], "D")
        self.assertEqual(names["Bash after 2h idle, then Read"], "pass")

    def test_plumbing_tool_dropped_from_the_model(self):
        p = self.repo / "ops/rlo/model.json"
        m = json.loads(p.read_text())
        m["specs"] = [s for s in m["specs"] if s["name"] != "ReadNotifications"]
        p.write_text(json.dumps(m))
        bad = self.doctor_failed()
        self.assertIn("remote.preset", bad)
        self.assertIn("remote.replay.plumbing ReadNotifications", bad)

    def test_shadow(self):
        self.edit("ops/rlo/guard.sh", "--mode enforce", "--mode shadow")
        bad = self.doctor_failed()
        self.assertTrue({"remote.preset", "remote.replay.A1 WebFetch", "remote.replay.Bash after 2h idle"} <= bad)

    def test_widened_grant(self):
        self.edit("ops/rlo/guard.sh", "--grant Bash", "--grant Bash --grant mcp__claude-code-remote__create_session")
        self.assertIn("remote.preset", self.doctor_failed())

    def test_clock_override(self):
        self.edit("ops/rlo/guard.sh", '--record "$REC"', '--now-ms 1 --record "$REC"')
        self.assertIn("remote.preset", self.doctor_failed())

    def test_push_code_in_the_guard_files(self):
        for f in ("install.sh", "guard.sh"):
            with self.subTest(f=f):
                p = self.repo / "ops/rlo" / f
                old = p.read_text()
                p.write_text(old.replace("set -u\n", "set -u\ngit push origin HEAD >/dev/null 2>&1\n", 1))
                self.assertIn("remote.preset", self.doctor_failed())
                p.write_text(old)

    def test_settings_lose_a_hook(self):
        sp = self.repo / ".claude/settings.json"
        good = sp.read_text()
        for mutate in (lambda s: s["hooks"].pop("PreToolUse"), lambda s: s["hooks"].pop("SessionStart"),
                       lambda s: s["hooks"]["PreToolUse"][0].update(matcher="Bash")):
            s = json.loads(good)
            mutate(s)
            sp.write_text(json.dumps(s))
            self.assertIn("remote.preset", self.doctor_failed())
        sp.write_text(good)

    def test_deny_report_rule_removed(self):
        self.edit("ops/rlo/GUARD.md", remote.DENY_REPORT, "Say what you like")
        self.assertIn("remote.preset", self.doctor_failed())

    def test_each_deny_path_turned_into_allow_is_caught(self):
        g = guard_sh(self.repo)
        good = g.read_text()
        paths = {'deny "empty hook input"': "remote.replay.empty stdin",
                 'deny "rlo not installed and install failed"': "remote.replay.no venv, install fails",
                 'deny "model file missing"': "remote.replay.model missing",
                 'deny "rlo recorded no verdict"': "remote.replay.model broken"}
        for call, case in paths.items():
            with self.subTest(path=call):
                self.assertIn(call, good)
                g.write_text(good.replace(call, "exit 0"))
                self.assertIn(case, self.doctor_failed())
        g.write_text(good)

    def stand_in(self, body: str) -> Path:
        """A venv whose `python -m rlo.hooks` misbehaves as real rlo 0.5.1 cannot (other calls go to this python)."""
        fake = self.tmp / f"fakevenv{len(list(self.tmp.glob('fakevenv*')))}" / "bin"
        fake.mkdir(parents=True)
        py = fake / "python"
        py.write_text(f"""#!{sys.executable}
import os, sys
a = sys.argv[1:]
if a[:2] == ["-m", "rlo.hooks"]:
    sys.stdin.read()
{body}
os.execv({sys.executable!r}, [{sys.executable!r}] + a)
""")
        py.chmod(py.stat().st_mode | stat.S_IXUSR)
        return fake.parent

    def guard_detail(self, venv: Path) -> str:
        return dict((n, d) for n, ok, d in remote.replay(self.repo, venv, only=["Bash after Bash"]))["Bash after Bash"]

    def test_wrapper_only_paths_and_their_allow_mutations(self):
        """Two deny paths real rlo 0.5.1 no longer reaches (it exits 0 and prints JSON on every input): a stand-in
        rlo reaches them, the good guard denies, and the mutant (deny -> exit 0) lets the call through."""
        paths = {'deny "rlo output is not JSON"':
                 ('    open(a[a.index("--record") + 1], "a").write("{}\\n")\n    print("garbage")\n    sys.exit(0)',
                  "rlo output is not JSON"),
                 'deny "rlo exited $RC"': ("    sys.exit(3)", "rlo exited 3")}
        g = guard_sh(self.repo)
        good = g.read_text()
        for call, (body, reason) in paths.items():
            with self.subTest(path=call):
                venv = self.stand_in(body)
                self.assertEqual(self.guard_detail(venv), f"blocked: rlo guard (fail closed): {reason}")
                g.write_text(good.replace(call, "exit 0"))
                self.assertEqual(self.guard_detail(venv), "not blocked")  # the mutant: the assertion above kills it
                g.write_text(good)


class NoGitTest(RemoteBase):
    def test_ga_rlo_never_runs_git(self):
        """S5 / D2 'push code added': a git on PATH that logs every call sees none from init or doctor."""
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        log = self.tmp / "git.log"
        g = bin_ / "git"
        g.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\nexit 1\n")
        g.chmod(0o755)
        exe = Path(sys.executable).with_name("ga")
        if not exe.exists():
            self.skipTest("ga console script is not installed next to this python")
        env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}")
        for argv in (["init", "--profile", "remote", "--work-repo", str(self.repo), "--name", "W1"],
                     ["doctor", "--profile", "remote", "--work-repo", str(self.repo), "--venv", VENV]):
            p = subprocess.run([str(exe), "rlo", *argv], capture_output=True, text=True, env=env)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(log.exists(), log.read_text() if log.exists() else "")


if __name__ == "__main__":
    unittest.main()


class GoldenTest(unittest.TestCase):
    """CMD-GR5 D2: `ga rlo init --profile remote` writes byte for byte what ga_rlo c2fcbc3 (`ga-rlo`) wrote for the same
    input (golden_remote_W1/ was generated by ga_rlo c2fcbc3). The remote files carry no hook module path."""

    def test_same_bytes_as_ga_rlo_c2fcbc3(self):
        golden = Path(__file__).resolve().parent / "golden_remote_W1"
        with tempfile.TemporaryDirectory() as d:
            remote.write(d, "W1")
            got = sorted(p.relative_to(d).as_posix() for p in Path(d).rglob("*") if p.is_file())
            want = sorted(p.relative_to(golden).as_posix() for p in golden.rglob("*") if p.is_file())
            self.assertEqual(got, want)
            for rel in want:
                with self.subTest(rel=rel):
                    self.assertEqual((Path(d) / rel).read_bytes(), (golden / rel).read_bytes())
