"""G8 (session start prompts) and G2 (record store)."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from ga import config as gacfg
from ga.forms import FormError
from ga.prompts import hub_prompt, worker_prompt
from ga.records import RecordStore

from examples import valid

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "examples" / "baseline"


def baseline_cfg():
    return gacfg.load(BASELINE / "config.json")


class PromptTest(unittest.TestCase):
    def test_worker_prompt_has_guidance_channel_and_ownership(self):
        cfg = baseline_cfg()
        text = worker_prompt(cfg, "ga-SDK")
        guidance = (BASELINE / "SESSION_GUIDANCE.md").read_text(encoding="utf-8").rstrip("\n")
        self.assertIn(guidance, text)  # full text
        self.assertTrue(text.startswith("넌 이제부터 ga-SDK 세션이야. commit된 결과는 <ga> 태그를 붙여서 <cogito5170/baseline> 레포로 보내."))
        self.assertIn("https://github.com/cogito5170/baseline/issues/12", text)
        self.assertIn("session_013GnrUQPpcfK4ea1a1Y6SuY", text)
        self.assertIn("`[ga] 보고 <글 링크>`", text)
        self.assertIn("`CMD-GA`", text)
        self.assertIn("| ga-SDK | `*` | ga-SDK |", text)
        self.assertNotIn("| ga-SDK | `METHOD.md` |", text)  # not this session's row
        self.assertIn("CMD-GA1 — https://github.com/cogito5170/baseline/issues/12", text)
        self.assertIn("교신은 action 이 아니다", text)
        self.assertIn("exchange/1", text)
        self.assertTrue(text.rstrip().endswith("baseline이랑 통신 시작해라"))

    def test_worker_prompt_for_each_session(self):
        cfg = baseline_cfg()
        for name, s in cfg.sessions.items():
            with self.subTest(session=name):
                text = worker_prompt(cfg, name)
                self.assertIn(s.channel, text)
                for row in cfg.ownership_rows(name):
                    self.assertIn(f"| {row['repo']} | `{row['path']}` | {name} |", text)

    def test_hub_prompt_has_guidance_sessions_and_ownership(self):
        cfg = baseline_cfg()
        text = hub_prompt(cfg)
        guidance = (BASELINE / "GUIDANCE.md").read_text(encoding="utf-8").rstrip("\n")
        self.assertIn(guidance, text)
        for s in cfg.sessions.values():
            self.assertIn(s.channel, text)
            self.assertIn(f"`CMD-{s.prefix}`", text)
        for row in cfg.ownership:
            self.assertIn(f"| {row['repo']} | `{row['path']}` | {row['session']} |", text)
        self.assertIn("사용자에게 묻는다", text)

    def test_prompts_are_deterministic(self):
        cfg = baseline_cfg()
        self.assertEqual(worker_prompt(cfg, "Sensor"), worker_prompt(baseline_cfg(), "Sensor"))
        self.assertEqual(hub_prompt(cfg), hub_prompt(baseline_cfg()))


class ConfigTest(unittest.TestCase):
    def raw(self):
        return json.loads((BASELINE / "config.json").read_text(encoding="utf-8"))

    def test_ownership_first_match_wins(self):
        cfg = baseline_cfg()
        self.assertEqual(cfg.owner_of("Sensor", "llmsensor/telemetry/l0.py"), "Telemetry")
        self.assertEqual(cfg.owner_of("Sensor", "llmsensor/state/export.py"), "DC")
        self.assertEqual(cfg.owner_of("Sensor", "llmsensor/sensing/x.py"), "Sensor")
        self.assertEqual(cfg.owner_of("ga-SDK", "METHOD.md"), "baseline")
        self.assertEqual(cfg.owner_of("ga-SDK", "ga/hub.py"), "ga-SDK")
        self.assertIsNone(cfg.owner_of("nope", "x"))

    def test_bad_configs_rejected(self):
        cases = []
        r = self.raw(); r["sessions"]["MS"]["prefix"] = "GA"; cases.append(("dup prefix", r))
        r = self.raw(); r["ownership"].append({"repo": "DC", "path": "*", "session": "MS"}); cases.append(("two owners", r))
        r = self.raw(); r["rules"] = {"lower": ["R2"]}; cases.append(("lower rule", r))
        r = self.raw(); r["rules"] = {"raise": ["R99"]}; cases.append(("unknown rule", r))
        r = self.raw(); r["sessions"]["MS"]["repos"] = ["ghost"]; cases.append(("unknown repo", r))
        r = self.raw(); r["ownership"][0]["session"] = "ghost"; cases.append(("unknown owner", r))
        r = self.raw(); r["sessions"]["baseline"] = {"prefix": "B", "branch": "b"}; cases.append(("hub name", r))
        r = self.raw(); r["budget"] = {"llm_runs": -1}; cases.append(("budget", r))
        r = self.raw(); r["schema"] = "ga-config/2"; cases.append(("schema", r))
        for what, raw in cases:
            with self.subTest(what=what):
                with self.assertRaises(FormError):
                    gacfg.from_dict(raw)

    def test_raise_soft_rule(self):
        r = self.raw()
        r["rules"] = {"raise": ["R7"]}
        cfg = gacfg.from_dict(r)
        self.assertEqual(cfg.strength("R7"), "hard")
        self.assertEqual(cfg.strength("R8"), "soft")
        self.assertEqual(cfg.strength("R2"), "hard")


class RecordTest(unittest.TestCase):
    def fill(self, root, order):
        store = RecordStore(root)
        docs = []
        for n in (3, 1, 2):
            r = valid("round/1"); r["n"] = n; r["summary"] = f"회차 {n}"; docs.append(r)
        d = valid("decision/1"); docs.append(d)
        d2 = valid("decision/1"); d2["id"] = "BD-9"; d2["supersedes"] = ["BD-131"]; d2["basis"] = "a | b"; docs.append(d2)
        docs.append(valid("stage/1"))
        for i in order:
            store.put(copy.deepcopy(docs[i]))
        return store

    def test_same_input_same_bytes_regardless_of_order(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            ra = self.fill(a, [0, 1, 2, 3, 4, 5]).render({"rlo-SDK": "cogito5170/rlo-SDK"})
            rb = self.fill(b, [5, 4, 3, 2, 1, 0]).render({"rlo-SDK": "cogito5170/rlo-SDK"})
            self.assertEqual(ra, rb)
            for name in ("rounds", "decisions", "stages"):
                fa = sorted(p.name for p in (Path(a) / name).iterdir())
                self.assertEqual(fa, sorted(p.name for p in (Path(b) / name).iterdir()))
                for f in fa:
                    self.assertEqual((Path(a) / name / f).read_bytes(), (Path(b) / name / f).read_bytes())

    def test_render_content(self):
        with tempfile.TemporaryDirectory() as a:
            out = self.fill(a, range(6)).render({"rlo-SDK": "cogito5170/rlo-SDK"})
            rounds = out["ROUNDS.md"].splitlines()
            self.assertEqual([l.split(" 회차")[0] for l in rounds if l.startswith("- ")], ["- 1", "- 2", "- 3"])
            self.assertIn("ga-SDK `f2bdeab`(10)", out["ROUNDS.md"])
            self.assertIn("| BD-9 |", out["DECISIONS.md"])
            self.assertIn("a \\| b", out["DECISIONS.md"])
            self.assertLess(out["DECISIONS.md"].index("| BD-9 |"), out["DECISIONS.md"].index("| BD-131 |"))
            self.assertIn("gh api repos/cogito5170/rlo-SDK/git/refs -f ref=refs/tags/stage-4 -f sha=d313414429ca097b02a545da92bbef9138927844", out["STAGES.md"])
            self.assertNotIn(" #", out["STAGES.md"].split("```")[1])  # R13: no inline comments in commands

    def test_append_only_and_idempotent(self):
        with tempfile.TemporaryDirectory() as a:
            store = RecordStore(a)
            r = valid("round/1")
            self.assertTrue(store.put(r))
            self.assertFalse(store.put(copy.deepcopy(r)))  # same bytes: no write
            r2 = copy.deepcopy(r); r2["summary"] = "다름"
            with self.assertRaises(FormError):
                store.put(r2)
            self.assertTrue(store.put(r2, replace=True))
            self.assertEqual(store.next_round(), 92)
            self.assertEqual(store.write_rendered(Path(a) / "md"), ["ROUNDS.md", "DECISIONS.md", "STAGES.md"])
            self.assertEqual(store.write_rendered(Path(a) / "md"), [])

    def test_invalid_records_rejected(self):
        with tempfile.TemporaryDirectory() as a:
            store = RecordStore(a)
            bad = valid("round/1"); bad["verdict"] = "fine"
            with self.assertRaises(FormError):
                store.put(bad)
            with self.assertRaises(FormError):
                store.put(valid("directive/1"))
            self.assertFalse(Path(a, "rounds").exists())


if __name__ == "__main__":
    unittest.main()
