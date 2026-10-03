"""CMD-GR6 (BD-228): one source for the rlo pin, ga/_pins.py; and a doctor replay that runs as the turn does.

D2 mutations: a golden byte change outside the pin, upgrade-remote not printing the move, a test reading a literal pin.
"""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ga import _pins
from ga.rlo import doctor, preset, remote

from .test_remote import GoldenTest
from .world import run_cli

HERE = Path(__file__).resolve().parent
TEXT = (".py", ".sh", ".md", ".json")


class OnePinSourceTest(unittest.TestCase):
    def test_no_literal_pin_in_grs_tests(self):
        """A pin bump in ga/_pins.py must change no file here: no 40-hex sha and no form of the current pin."""
        sha = _pins.PINS["rlo"][3]
        for p in sorted(HERE.rglob("*")):
            if p.is_file() and p.suffix in TEXT and "__pycache__" not in p.parts:
                text = p.read_text(encoding="utf-8")
                with self.subTest(file=p.relative_to(HERE).as_posix()):
                    self.assertNotIn(sha[:7], text)
                    self.assertIsNone(re.search(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])", text))

    def test_a_pin_bump_changes_no_golden_byte(self):
        """With another pin in ga/_pins.py (as GA21's 0.7.0), the generator follows it and the golden comparison holds."""
        dist, extras, url, _ = _pins.PINS["rlo"]
        other = "f" * 40
        with mock.patch.dict(_pins.PINS, {"rlo": (dist, extras, url, other)}):
            GoldenTest("test_same_bytes_as_ga_rlo_c2fcbc3").test_same_bytes_as_ga_rlo_c2fcbc3()
            with tempfile.TemporaryDirectory() as d:
                remote.write(d, "W1")
                self.assertEqual(remote.pin_of(Path(d) / "ops/rlo/install.sh"), other)
                self.assertEqual(remote.problems(d), [])

    def test_a_golden_byte_change_outside_the_pin_is_caught(self):
        golden = HERE / "golden_remote_W1" / "ops/rlo/guard.sh"
        good = golden.read_bytes()
        self.addCleanup(golden.write_bytes, good)
        golden.write_bytes(good.replace(b"--mode enforce", b"--mode  enforce"))
        with self.assertRaises(AssertionError):
            GoldenTest("test_same_bytes_as_ga_rlo_c2fcbc3").test_same_bytes_as_ga_rlo_c2fcbc3()

    def test_upgrade_remote_prints_the_move_to_ga_pins(self):
        """S3: amp's guard keeps its own pin until a person runs what upgrade-remote prints: old -> ga/_pins.py's."""
        new = _pins.PINS["rlo"][3]
        old = "1" * 40 if new != "1" * 40 else "2" * 40
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "ops/rlo").mkdir(parents=True)
            (Path(d) / "ops/rlo/install.sh").write_text(f'#!/usr/bin/env bash\nPIN="{old}"\n')
            rc, out, _ = run_cli("upgrade-remote", "--work-repo", d)
            self.assertEqual(rc, 0)
            sed = next(l.strip() for l in out.splitlines() if l.strip().startswith("sed "))
            self.assertIn(f'^PIN="{old}"$/', sed)
            self.assertIn(f'/PIN="{new}"/', sed)
            self.assertIn(f"pins rlo-sdk {old[:7]}; ga pins {new[:7]}", out)


class TurnLikeReplayTest(unittest.TestCase):
    """S4: doctor replays the guard from a temp directory with a clean environment plus the Runner's extra_env. From a
    source checkout, `python -m ga.rlo.hook` would otherwise import ga from the caller's directory and pass, while the
    real turn (run in its worktree, clean environment) cannot import ga -- the 'exit 1 in the worker turn' GA saw."""

    def calls(self, **kw):
        seen = []

        def fake_run(argv, **k):
            seen.append(k)
            return mock.Mock(returncode=0, stdout="")

        with mock.patch.object(doctor.subprocess, "run", side_effect=fake_run), \
                mock.patch.dict("os.environ", {"PYTHONPATH": "/caller/path"}):
            doctor.replay(preset.rlo_guard("/m.json"), **kw)
        return seen

    def test_not_the_callers_directory_nor_its_pythonpath(self):
        import os

        for k in self.calls():
            self.assertNotEqual(Path(k["cwd"]).resolve(), Path(os.getcwd()).resolve())
            self.assertTrue(Path(k["cwd"]).name.startswith("ga-rlo-doctor-"))
            self.assertNotIn("PYTHONPATH", k["env"])

    def test_the_runners_extra_env_is_passed(self):
        for k in self.calls(extra_env={"PYTHONPATH": "/src", "X": "1"}):
            self.assertEqual((k["env"]["PYTHONPATH"], k["env"]["X"]), ("/src", "1"))


if __name__ == "__main__":
    unittest.main()
