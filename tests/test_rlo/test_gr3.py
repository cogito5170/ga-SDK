"""CMD-GR3: rlo 0.5.1 pins, Sensor state lines through ga's seam, and moving an existing remote guard's pin.

D3 mutations: an old PIN left in install.sh, a state line with raw text, a state line dropped, ga-rlo running git.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ga import _pins
from ga.rlo import hook, preset, remote

from .world import run_cli

OLD = "a152e14bc84dc282f66bb3426a12a70934fc530d"
NEW = "3323f88741c198f453370936c481c00fbd26d398"
# the shape of amp's W1 install.sh (amp 344a604): its own venv variable and marker, the PIN line ga-rlo moves
AMP_INSTALL = f"""#!/usr/bin/env bash
# Install rlo-sdk (stage-8, pinned) into a private venv for the W1 guard. Idempotent.
set -u
PIN="{OLD}"
VENV="${{AMP_RLO_VENV:-$HOME/.cache/amp-rlo-venv}}"
MARK="$VENV/.pinned-$PIN"
[ -f "$MARK" ] && exit 0
"""


class PinTest(unittest.TestCase):
    def test_everything_pins_rlo_051(self):
        self.assertEqual(_pins.PINS["rlo"][3], NEW)
        with tempfile.TemporaryDirectory() as d:
            remote.write(d, "W")
            self.assertEqual(remote.pin_of(Path(d) / "ops/rlo/install.sh"), NEW)

    def test_installed_rlo_is_051(self):
        import rlo

        self.assertEqual(rlo.versions()["sdk"], "rlo-sdk/0.6.0")  # GR4 moved the pin on (K11)

    def test_old_pin_left_in_install_sh_is_caught(self):
        with tempfile.TemporaryDirectory() as d:
            remote.write(d, "W")
            p = Path(d) / "ops/rlo/install.sh"
            p.write_text(p.read_text().replace(NEW, OLD))
            self.assertTrue(any("install.sh: PIN" in x for x in remote.problems(d)))


class StateLineTest(unittest.TestCase):
    def setUp(self):
        from rlo.example_hooks import hook_input, now_after

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.model = preset.write_model(self.tmp / "m.json")
        self.rec = self.tmp / "rec.jsonl"
        self.input, self.now = hook_input("after_failure"), now_after("after_failure")

    def run_hook(self, data, mode="enforce"):
        out = io.StringIO()
        argv = ["--model", str(self.model), "--mode", mode, "--grant", "Bash", "--record", str(self.rec),
                "--now-ms", repr(self.now)]
        rc = hook.main(argv, io.StringIO(data if isinstance(data, str) else json.dumps(data)), out)
        return rc, out.getvalue()

    def lines(self):
        return [json.loads(x) for x in self.rec.read_text().splitlines()]

    def test_one_state_line_after_the_verdict_with_label_values(self):
        rc, out = self.run_hook(self.input)
        self.assertEqual((rc, out), (0, ""))  # the verdict is rlo's: allow = nothing printed
        kinds = [x["kind"] for x in self.lines()]
        self.assertEqual(kinds, ["guard", "state"])
        st = self.lines()[1]["labels"]
        self.assertEqual(st["execution_health"], "UNRESOLVED_FAILURES")
        self.assertTrue(all(hook.LABEL.match(k) and hook.LABEL.match(v) for k, v in st.items()))

    def test_ga_guard_summary_carries_it(self):
        from ga.hub import guard_summary

        self.run_hook(self.input)
        s = guard_summary(self.rec.read_text().splitlines())
        self.assertEqual((s["allow"], s["deny"], s["errors"]), (1, 0, 0))
        self.assertEqual(s["state"]["execution_health"], "UNRESOLVED_FAILURES")

    def test_raw_text_never_reaches_a_state_line(self):
        raw = {"execution_health": "/home/user/secret path", "progress_state": "https://x.invalid/a?b",
               "liveness_state": "two words", "completion_state": "RUNNING", "k with space": "OK", "x" * 41: "OK",
               "resource_state": "y" * 41}
        with mock.patch("ga.rlo.bridge.transcript_state", return_value=raw):
            self.run_hook(self.input)
        self.assertEqual(self.lines()[-1], {"kind": "state", "labels": {"completion_state": "RUNNING"}})
        self.assertEqual(hook.clean(raw), {"completion_state": "RUNNING"})

    def test_no_state_line_when_nothing_is_clean_or_not_pretooluse(self):
        with mock.patch("ga.rlo.bridge.transcript_state", return_value={"a": "b c"}):
            self.run_hook(self.input)
        self.assertEqual([x["kind"] for x in self.lines()], ["guard"])
        self.run_hook(dict(self.input, hook_event_name="Stop"))
        self.assertEqual([x["kind"] for x in self.lines()], ["guard"])

    def test_a_failing_state_line_never_changes_the_verdict(self):
        probe = dict(self.input, tool_name="WebFetch", tool_input={"url": "u"}, tool_use_id="p")
        with mock.patch("ga.rlo.bridge.transcript_state", side_effect=RuntimeError("boom")):
            rc, out = self.run_hook(probe)
        self.assertEqual(rc, 0)
        self.assertIn('"permissionDecision": "deny"', out)
        self.assertIn("(A1)", out)
        self.assertEqual([x["kind"] for x in self.lines()], ["guard"])

    def test_malformed_input_is_rlos_deny_and_no_state_line(self):
        for data in ("", "not json", json.dumps({"hook_event_name": "PreToolUse"})):
            with self.subTest(data=data[:12]):
                rc, out = self.run_hook(data)
                self.assertEqual(rc, 0)
                self.assertIn("rlo hook input error", out)
        self.assertNotIn("state", [x["kind"] for x in self.lines()])

    def test_if_rlo_raises_enforce_closes(self):
        with mock.patch("rlo.hooks.main", side_effect=RuntimeError("x")):
            rc, out = self.run_hook(self.input)
            self.assertEqual(rc, 0)
            self.assertIn("deny", out)
            rc, out = self.run_hook(self.input, mode="shadow")
            self.assertEqual(out, "")

    def test_the_local_preset_uses_the_hook(self):
        g = preset.rlo_guard("/m.json")
        self.assertIn("-m ga.rlo.hook ", g["command"])
        self.assertEqual(preset.parse(g["command"]).mode, "enforce")
        self.assertTrue(preset.is_rlo("python3 -m rlo.hooks --model m"))  # the bare rlo command still counts as a guard


class UpgradeRemoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.amp = self.tmp / "amp"
        (self.amp / "ops/rlo").mkdir(parents=True)
        (self.amp / "ops/rlo/install.sh").write_text(AMP_INSTALL)

    def test_prints_commands_that_move_amps_pin_and_runs_none(self):
        before = (self.amp / "ops/rlo/install.sh").read_text()
        rc, out, err = run_cli("upgrade-remote", "--work-repo", str(self.amp))
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.amp / "ops/rlo/install.sh").read_text(), before)  # nothing edited
        cmds = [l.strip() for l in out.splitlines() if l.startswith("  ")]
        self.assertTrue(cmds[0].startswith("sed -i.bak "))  # GNU and BSD (macOS) sed alike (BD-203)
        self.assertNotRegex(" ".join(cmds), r"sed -i '")  # never the GNU-only bare -i
        self.assertTrue(any(c.startswith("git -C ") and " push origin HEAD" in c for c in cmds))
        subprocess.run(cmds[0], shell=True, check=True)  # what the human's first command does
        self.assertFalse((self.amp / "ops/rlo/install.sh.bak").exists())  # the backup is removed
        after = (self.amp / "ops/rlo/install.sh").read_text()
        self.assertEqual(after, before.replace(OLD, NEW))
        self.assertIn("AMP_RLO_VENV", after)  # only the PIN line moved
        rc, out, _ = run_cli("upgrade-remote", "--work-repo", str(self.amp))
        self.assertIn("nothing to do", out)

    def test_no_pin_line(self):
        (self.amp / "ops/rlo/install.sh").write_text("#!/bin/sh\n")
        rc, _, err = run_cli("upgrade-remote", "--work-repo", str(self.amp))
        self.assertEqual(rc, 2)
        self.assertIn("no PIN", err)

    def test_ga_rlo_runs_no_git_here_either(self):
        exe = Path(sys.executable).with_name("ga")
        if not exe.exists():
            self.skipTest("ga console script is not installed next to this python")
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        log = self.tmp / "git.log"
        (bin_ / "git").write_text(f"#!/bin/sh\necho \"$@\" >> {log}\nexit 1\n")
        (bin_ / "git").chmod(0o755)
        p = subprocess.run([str(exe), "rlo", "upgrade-remote", "--work-repo", str(self.amp)], capture_output=True, text=True,
                           env=dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
