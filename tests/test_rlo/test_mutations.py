"""GA_RLO.md §4 3: the tests catch it when the guard goes missing, enforce becomes shadow, the grant widens (or the
model does), or the guard lands in the person's settings -- in the preset check and in `ga-rlo doctor`."""
import json
import os
import unittest
from pathlib import Path

from ga.rlo import preset

from .world import World, run_cli


class DoctorMutationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = World()
        cls.w.permit()
        cls.good = cls.w.raw()
        cls.model = preset.parse(cls.good["runner"]["guards"][0]["command"]).model

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    def setUp(self):
        self.w.config.write_text(json.dumps(self.good), encoding="utf-8")
        self.model_text = Path(self.model).read_text(encoding="utf-8")
        self.addCleanup(Path(self.model).write_text, self.model_text, encoding="utf-8")

    def failed(self) -> set:
        rc, out, _ = run_cli("--config", str(self.w.config), "doctor", "--json")
        checks = json.loads(out)["checks"]
        bad = {c["check"] for c in checks if not c["ok"]}
        self.assertEqual(rc != 0, bool(bad))
        return bad

    def guard(self, fn):
        def edit(raw):
            g = raw["runner"]["guards"][0]
            g["command"] = fn(g["command"])
        self.w.edit(edit)

    def test_the_good_config_passes(self):
        from ga.adapters import sandbox

        self.assertEqual(self.failed(), set() if sandbox.available() else {"sandbox"})

    def test_guard_missing(self):
        self.w.edit(lambda raw: raw["runner"].update(guards=[]))
        bad = self.failed()
        self.assertIn("preset", bad)
        self.assertFalse(any(c.startswith("replay[") for c in bad))  # nothing left to replay; the preset says why

    def test_guard_removed_from_the_runner_key(self):
        self.w.edit(lambda raw: raw["runner"].pop("guards"))
        self.assertIn("preset", self.failed())

    def test_enforce_to_shadow(self):
        self.guard(lambda c: c.replace("--mode enforce", "--mode shadow"))
        bad = self.failed()
        self.assertIn("preset", bad)
        self.assertIn("replay[0].probe WebFetch", bad)  # the replay itself sees nothing blocked
        self.assertIn("replay[0].probe Agent", bad)

    def test_mode_flag_dropped(self):  # rlo.hooks defaults to shadow
        self.guard(lambda c: c.replace(" --mode enforce", ""))
        self.assertIn("replay[0].probe WebFetch", self.failed())

    def test_grant_widened(self):
        self.guard(lambda c: c.replace("--grant Bash", "--grant Bash --grant WebFetch"))
        self.assertIn("preset", self.failed())

    def test_grant_dropped(self):  # BD-163: every Bash call would be A7
        self.guard(lambda c: c.replace(" --grant Bash", ""))
        self.assertIn("preset", self.failed())

    def test_model_widened(self):
        m = json.loads(self.model_text)
        bash = next(s for s in m["specs"] if s["name"] == "Bash")
        m["specs"].append(dict(bash, name="WebFetch", params={"url": {"type": "string"}}, risk="read"))
        with open(self.model, "w", encoding="utf-8") as f:
            json.dump(m, f)
        bad = self.failed()
        self.assertIn("replay[0].probe WebFetch", bad)
        self.assertNotIn("preset", bad)  # the config is the same: only the replay can see a wider model

    def test_model_broken(self):
        with open(self.model, "w", encoding="utf-8") as f:
            f.write("{}")
        bad = self.failed()
        self.assertTrue({"preset", "model[0]"} <= bad)

    def test_matcher_narrowed(self):
        self.w.edit(lambda raw: raw["runner"]["guards"][0].update(matcher="Bash"))
        self.assertIn("preset", self.failed())

    def test_record_dropped(self):
        self.w.edit(lambda raw: raw["runner"]["guards"][0].pop("record"))
        self.assertIn("preset", self.failed())

    def test_gas_own_guard_off(self):
        self.w.edit(lambda raw: raw["runner"].update(guard=False))
        self.assertIn("preset", self.failed())

    def test_sandbox_not_required(self):
        self.w.edit(lambda raw: raw["runner"].update(sandbox="auto"))
        bad = self.failed()
        self.assertIn("preset", bad)
        self.assertIn("permission", bad)  # the person permitted sandbox require, not auto (ga's own check)

    def test_guard_program_gone(self):
        self.guard(lambda c: "/nonexistent/python" + c[c.index(" -m "):])
        self.assertTrue({"preset", "guard.program.W"} <= self.failed())

    def test_guard_in_the_persons_settings(self):
        p = self.w.person / ".claude" / "settings.json"
        cmd = self.good["runner"]["guards"][0]["command"]
        p.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}]}}))
        self.addCleanup(p.unlink)
        self.assertEqual(self.failed() - {"sandbox"}, {"person.settings"})

    def test_guard_in_claude_config_dir(self):
        d = self.w.tmp / "ccd"
        d.mkdir(exist_ok=True)
        (d / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "python3 -m rlo.hooks --model m.json"}]}]}}))
        os.environ["CLAUDE_CONFIG_DIR"] = str(d)
        self.addCleanup(os.environ.pop, "CLAUDE_CONFIG_DIR")
        self.assertIn("person.settings", self.failed())

    def test_guard_in_the_hub_sessions_settings(self):
        p = self.w.hub / ".claude" / "settings.json"
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "ga-rlo hooks --x ga_rlo"}]}]}}))
        self.addCleanup(p.unlink)
        self.assertIn("person.settings", self.failed())

    def test_now_ms_in_a_turn(self):
        self.guard(lambda c: c + " --now-ms 1")
        self.assertIn("preset", self.failed())


class PresetTest(unittest.TestCase):
    def test_round_trip_and_the_defaults(self):
        g = preset.rlo_guard("/m/model.json", python="/py/bin/python")
        self.assertEqual(g["name"], "rlo")
        self.assertEqual(g["record"], "{home}/rlo-{session}.jsonl")
        p = preset.parse(g["command"])
        self.assertEqual((p.python, p.model, p.mode, p.grants, p.record, p.unknown),
                         ("/py/bin/python", "/m/model.json", "enforce", ["Bash"], g["record"], []))
        self.assertIsNone(preset.parse("python3 -m other.hooks --mode enforce"))
        self.assertIsNone(preset.parse("'unclosed"))

    def test_ga_takes_the_entry_and_puts_it_after_its_own_guard(self):
        import tempfile
        from pathlib import Path

        from ga.adapters.headless import HeadlessRunner

        with tempfile.TemporaryDirectory() as d:
            r = HeadlessRunner(Path(d), guards=[preset.rlo_guard("/m with space/model.json")])
            pre = json.loads(r.settings_file("S").read_text())["hooks"]["PreToolUse"]
            self.assertIn("bash_guard", pre[0]["hooks"][0]["command"])
            cmd = pre[1]["hooks"][0]["command"]
            self.assertEqual(pre[1]["matcher"], "*")
            p = preset.parse(cmd)  # quoting survives ga's {home} fill, spaces included
            self.assertEqual((p.model, p.record), ("/m with space/model.json", str(Path(d) / "S" / "rlo-S.jsonl")))
            self.assertEqual(r.guard_records("S"), [("rlo", Path(d) / "S" / "rlo-S.jsonl")])


if __name__ == "__main__":
    unittest.main()
