"""GA_RLO.md §4 2: with a fake Runner, end to end -- `ga-rlo init` -> a fake work turn (a transcript holding one tool
that rlo blocks) -> `ga tick`. The round record carries rlo's denial count and label; ga_rlo's evidence carries the
rest (labels and counts only). No model call, no network.
"""
import json
import unittest

from ga.adapters import sandbox

from .world import World, run_cli

TOOLS = [
    {"name": "Bash", "input": {"command": "ls"}},
    {"name": "WebFetch", "input": {"url": "https://example.invalid/secret-path?token=abc"}},  # not in the model: A1
    {"name": "Read", "input": {"file_path": "README.md"}},
]


@unittest.skipUnless(sandbox.available(), "no OS write sandbox here (the preset requires it)")
class OneRoundTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)

    def test_init_turn_tick(self):
        w = self.w
        rc, out, err = w.init_out
        self.assertEqual(rc, 0, err)
        self.assertIn("ga permit --runner headless --model haiku --sandbox require --budget runs=6", out)
        self.assertFalse((w.ga / "records" / "decisions").exists())  # init prints the permit, never makes it

        rc, out, _ = run_cli("--config", str(w.config), "doctor", "--json")  # without the person's permission: fails
        failed = {c["check"] for c in json.loads(out)["checks"] if not c["ok"]}
        self.assertEqual((rc, failed), (1, {"permission"}))

        bd = w.permit()
        rc, out, _ = run_cli("--config", str(w.config), "doctor")
        self.assertEqual(rc, 0, out)

        w.plan(TOOLS)
        rc, out, err = run_cli("--config", str(w.config), "send", str(w.directive_file()))
        self.assertEqual(rc, 0, err)
        turn = w.state()["turns"][-1]
        crash = w.ga / "headless" / "home" / "W" / "fake-error.txt"  # CMD-GR6 S4: the fake's own error, if any
        self.assertEqual((turn["runner"], turn["error"], turn["sandboxed"]), ("headless", "", True),
                         crash.read_text() if crash.exists() else "no fake-error.txt")
        g = turn["guards"][0]
        self.assertEqual({k: g[k] for k in ("guard", "allow", "deny", "errors", "labels")},
                         {"guard": "rlo", "allow": 2, "deny": 1, "errors": 0, "labels": ["A1"]})
        # CMD-GR3 S3: Sensor state through ga's own seam (state lines in the rlo record -> guard_summary)
        self.assertEqual(g["state"]["execution_health"], "UNRESOLVED_FAILURES")  # the blocked call is a failed result
        self.assertEqual(g["state"]["completion_state"], "RUNNING")
        self.assertEqual(turn["diag"]["guards"][0]["state"], g["state"])
        rec = (w.ga / "headless" / "home" / "W" / "rlo-W.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(x)["kind"] for x in rec], ["guard", "state"] * 3)  # one state line per verdict

        hooks = [json.loads(x) for x in (w.ga / "headless" / "home" / "W" / "fake-hooks.jsonl").read_text().splitlines()]
        rlo = [(h["tool"], h["decision"]) for h in hooks if h["hook"] == 1]  # hook 0 is ga's bash_guard (Bash only)
        self.assertEqual(rlo, [("Bash", ""), ("WebFetch", "deny"), ("Read", "")])
        self.assertNotIn("allow", {h["decision"] for h in hooks if h["hook"] == 1})  # P3

        rc, out, err = run_cli("--config", str(w.config), "tick")  # the person has not judged yet
        self.assertEqual(rc, 0, err)
        self.assertIn("round-1.request.md", json.loads(out)["waiting_for"] if out.strip() else "")
        w.judge(1)
        rc, out, err = run_cli("--config", str(w.config), "tick")
        self.assertEqual(rc, 0, err)
        rounds = sorted((w.ga / "records" / "rounds").glob("round-*.json"))
        self.assertEqual(len(rounds), 1)
        rd = json.loads(rounds[0].read_text())
        self.assertIn("guard rlo refused 1 tool call(s) in W's CMD-W1 rev 1 turn: A1", rd["notices"])
        self.assertEqual(rd["directives"], ["CMD-W1"])
        self.assertEqual(rd["repos"][0]["repo"], "work")

        self.assertFalse((w.hub / ".ga-rlo" / "evidence.jsonl").exists())  # the side file is retired
        everything = json.dumps(w.state()) + rounds[0].read_text()
        for raw in ("secret-path", "token=abc", "example.invalid", "README.md", "행동 WebFetch"):  # no raw text anywhere
            self.assertNotIn(raw, everything)

        rc, out, _ = run_cli("--config", str(w.config), "evidence")
        self.assertIn("deny 1", out)
        self.assertIn("labels A1", out)
        self.assertIn("execution_health=UNRESOLVED_FAILURES", out)
        self.assertEqual(bd, w.raw()["runner"]["permission"])

    def test_the_persons_settings_stay_untouched(self):
        w = self.w
        w.permit()
        w.plan(TOOLS)
        run_cli("--config", str(w.config), "send", str(w.directive_file()))
        self.assertEqual(sorted(p.name for p in (w.person / ".claude").iterdir()), [])
        self.assertFalse((w.hub / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
