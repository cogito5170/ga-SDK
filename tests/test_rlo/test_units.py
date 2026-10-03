"""Pins (GA_RLO.md §3), init (G3), one entry (G5), the evidence bridge's reductions (G2), §4 1 (empty directory)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ga import _pins
from ga.rlo import bridge, init, preset

from .world import World, run_cli

ROOT = Path(__file__).resolve().parent.parent


def _req(s: str) -> tuple:
    try:
        from packaging.requirements import Requirement
    except ImportError:  # a clean venv may have no packaging; pip carries one
        from pip._vendor.packaging.requirements import Requirement

    r = Requirement(s)
    return (r.name.lower().replace("_", "-"), tuple(sorted(r.extras)), str(r.specifier), r.url, str(r.marker or ""))


class PinsTest(unittest.TestCase):
    """Inside ga the pins come from ga/_pins.py (one place, GA20); its pyproject and metadata checks are GA's
    (tests/test_unified.py). Here: ga.rlo uses that pin for the rlo it installs and checks."""

    def test_the_rlo_pin_is_the_one_in_the_directive(self):
        self.assertEqual(_pins.PINS["rlo"][3], "3323f88741c198f453370936c481c00fbd26d398")
        self.assertEqual(_pins.PINS["rlo"][1], ("sensor",))
        self.assertEqual(_pins.requirement(_pins.PINS["rlo"]),
                         "rlo-sdk[sensor] @ git+https://github.com/cogito5170/rlo-SDK@3323f88741c198f453370936c481c00fbd26d398")

    def test_ga_rlo_keeps_no_pin_list_of_its_own(self):
        import ga.rlo.remote as remote

        self.assertIs(remote._pins, _pins)
        self.assertFalse((Path(remote.__file__).parent / "_pins.py").exists())


class InitTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.d)
        self.kw = dict(hub="hub", repos={"r": "r"}, sessions={"S": {"prefix": "S", "branch": "b", "repos": ["r"]}},
                       integration_branch="i")

    def test_writes_config_and_model_and_never_a_permission(self):
        out = init.run(directory=self.d, **self.kw)
        raw = json.loads(Path(out["config"]).read_text())
        self.assertEqual(preset.problems(raw), [])
        self.assertEqual((raw["runner"]["kind"], raw["runner"]["sandbox"], raw["judge"]), ("headless", "require", {"kind": "file"}))
        self.assertNotIn("permission", raw["runner"])
        self.assertTrue(out["permit"].startswith("ga permit --runner headless --sandbox require --budget runs=6"))
        preset.load_model(out["model"])
        self.assertEqual(sorted(p.name for p in self.d.iterdir()), [".ga-rlo", "ga.json"])  # no .ga, no records

    def test_does_not_overwrite(self):
        init.run(directory=self.d, **self.kw)
        with self.assertRaises(init.InitError):
            init.run(directory=self.d, **self.kw)
        init.run(directory=self.d, force=True, **dict(self.kw, runner="agent_sdk"))
        self.assertEqual(json.loads((self.d / "ga.json").read_text())["runner"]["kind"], "agent_sdk")

    def test_keeps_an_operators_model(self):
        m = self.d / ".ga-rlo" / "cc_tools_model.json"
        init.run(directory=self.d, **self.kw)
        data = json.loads(m.read_text())
        data["version"] = "operator-1"
        m.write_text(json.dumps(data))
        init.run(directory=self.d, force=True, **self.kw)
        self.assertEqual(json.loads(m.read_text())["version"], "operator-1")

    def test_refusals_write_nothing(self):
        for kw in (dict(profile="remote"), dict(runner="manual"),
                   dict(sessions={"S": {"prefix": "S", "branch": "b", "repos": ["nope"]}})):
            with self.subTest(kw=kw), self.assertRaises((init.InitError, Exception)):
                init.run(directory=self.d, **dict(self.kw, **kw))
            self.assertEqual(list(self.d.iterdir()), [])

    def test_specs(self):
        self.assertEqual(init.parse_session("W:W:sess-w:a,b"), ("W", {"prefix": "W", "branch": "sess-w", "repos": ["a", "b"]}))
        self.assertEqual(init.parse_repo("a=../x"), ("a", "../x"))
        for bad in ("W:W:b", "W::b:r"):
            with self.assertRaises(init.InitError):
                init.parse_session(bad)


class CliTest(unittest.TestCase):
    def test_help_and_install_doctor_in_an_empty_directory(self):
        """GR5 D1 / GA_RLO §4 1: from an empty directory, through the installed `ga` console script."""
        exe = Path(sys.executable).with_name("ga")
        if not exe.exists():
            self.skipTest("ga console script is not installed next to this python")
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, HOME=d)
            p = subprocess.run([str(exe), "rlo", "--help"], cwd=d, capture_output=True, text=True, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            for c in ("init", "doctor", "evidence", "preset", "upgrade-remote", "hooks"):
                self.assertIn(c, p.stdout)
            p = subprocess.run([str(exe), "rlo", "doctor", "--install-only"], cwd=d, capture_output=True, text=True, env=env)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("doctor: ok", p.stdout)
            p = subprocess.run([str(exe), "rlo", "doctor"], cwd=d, capture_output=True, text=True, env=env)
            self.assertEqual(p.returncode, 1)  # no ga.json: it runs, and says so
            self.assertIn("ga rlo init", p.stdout)
            self.assertEqual(os.listdir(d), [])  # and writes nothing

    def test_ga_commands_are_gas_own(self):
        """ga_rlo passed ga's commands through; inside ga they are `ga <command>`, and `ga rlo <command>` says so."""
        from ga.rlo import cli

        w = World()
        self.addCleanup(w.close)
        rc, out, err = run_cli("--config", str(w.config), "prompt", "W")  # routed to ga, as `ga prompt W`
        self.assertEqual(rc, 0, err)
        self.assertIn("W 세션", out)
        rc, out, err = run_cli("--config", str(w.config), "check", str(w.directive_file()))
        self.assertEqual(rc, 0, out + err)
        import contextlib
        import io

        e = io.StringIO()
        with contextlib.redirect_stderr(e):
            self.assertEqual(cli.main(["--config", str(w.config), "prompt", "W"]), 2)
        self.assertIn("run `ga prompt ...`", e.getvalue())

    def test_rlo_hooks_pass_through_but_not_into_the_persons_settings(self):
        w = World()
        self.addCleanup(w.close)
        model = str(preset.packaged_model())
        rc, _, err = run_cli("hooks", "install-hook", "--model", model)
        self.assertEqual(rc, 2)
        self.assertIn("--settings is required", err)
        rc, _, err = run_cli("hooks", "install-hook", "--settings", str(w.person / ".claude" / "settings.json"), "--model", model)
        self.assertEqual(rc, 2)
        self.assertIn("person's settings", err)
        self.assertEqual(list((w.person / ".claude").iterdir()), [])
        turn = w.tmp / "turn-settings.json"
        rc, out, err = run_cli("hooks", "install-hook", "--settings", str(turn), "--model", model, "--mode", "enforce")
        self.assertEqual(rc, 0, err)
        self.assertTrue(preset.is_rlo(json.loads(turn.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]))

    def test_preset_prints_one_entry(self):
        rc, out, _ = run_cli("--config", "/x/ga.json", "preset")
        self.assertEqual(rc, 0)
        g = json.loads(out)
        self.assertEqual(preset.parse(g["command"]).model, "/x/.ga-rlo/cc_tools_model.json")


class BridgeTest(unittest.TestCase):
    def test_record_summary_keeps_counts_and_labels_only(self):
        lines = [
            json.dumps({"kind": "guard", "tool_name": "Bash", "tool_input_keys": ["command"], "complete": True,
                        "missing_required": [], "result": {"verdict": "ALLOW", "rule": "0", "reasons": ["Bash -> rm -rf /x"]}}),
            json.dumps({"kind": "guard", "tool_name": "Bash", "complete": False, "missing_required": ["agent.execution_health"],
                        "result": {"verdict": "DENY", "rule": "D", "reasons": ["secret sk-123"]}}),
            json.dumps({"kind": "guard", "tool_name": "WebFetch", "complete": True,
                        "result": {"verdict": "DENY", "rule": "A1", "reasons": ["url https://x"]}}),
            json.dumps({"kind": "guard_error", "exception": "KeyError"}),
            "not json",
        ]
        s = bridge.record_summary(lines)
        self.assertEqual(s, {"allow": 1, "deny": 2, "errors": 2, "labels": ["A1", "D"], "incomplete": 1,
                             "missing": ["agent.execution_health"], "denied_tools": ["Bash", "WebFetch"]})
        for raw in ("rm -rf", "sk-123", "https://x", "KeyError"):
            self.assertNotIn(raw, json.dumps(s))

    def test_state_from_recorded_transcripts_at_their_own_time(self):
        from rlo.example_hooks import data

        model = preset.load_model(preset.packaged_model())
        want = {"normal": "NO_FAILURE_OBSERVED", "after_failure": "UNRESOLVED_FAILURES", "parallel": "UNKNOWN"}
        for name, health in want.items():
            with self.subTest(name=name):
                st = bridge.transcript_state(str(data(f"transcripts/{name}.jsonl")), model)
                self.assertEqual(st["execution_health"], health)
                self.assertTrue(set(st) <= set(bridge.STATE_KEYS))
        st = bridge.transcript_state(str(data("transcripts/ended.jsonl")), model)
        self.assertEqual(st["liveness_state"], "AWAITING_INPUT")

    def test_find_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".claude" / "projects" / "-w" / "abc.jsonl"
            p.parent.mkdir(parents=True)
            p.write_text("")
            self.assertEqual(bridge.find_transcript(d, "abc"), p)
            for bad in (None, "", "../abc", ".abc", "nope"):
                self.assertIsNone(bridge.find_transcript(d, bad))


if __name__ == "__main__":
    unittest.main()
