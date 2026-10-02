"""G4: each of R1–R13 catches at least one violation (and passes a clean case). G7 detection: each gate fires."""
import unittest
from pathlib import Path

from ga import config as gacfg
from ga import rules
from ga.forms import dump_text
from ga.gates import detect, question_for

from examples import valid

BASELINE = Path(__file__).resolve().parent.parent / "examples" / "baseline"


def cfg(**over):
    import json
    raw = json.loads((BASELINE / "config.json").read_text(encoding="utf-8"))
    raw.update(over)
    return gacfg.from_dict(raw, BASELINE)


def fake(*parts):
    """Assemble a fake secret at runtime so the repository never contains one."""
    return "".join(parts)


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.c = cfg()

    def assertCaught(self, problems, rule, strength):
        self.assertTrue(problems, f"{rule} caught nothing")
        self.assertTrue(all(p.rule == rule for p in problems))
        self.assertEqual({p.strength for p in problems}, {strength})

    def test_r1_direct_talk(self):
        self.assertCaught(rules.r1_post(self.c, "MS", "Sensor"), "R1", "soft")
        self.assertEqual(rules.r1_post(self.c, "MS", "MS"), [])
        self.assertEqual(rules.r1_post(self.c, "MS", "baseline"), [])

    def test_r2_ownership(self):
        self.assertCaught(rules.r2_ownership(self.c, "Sensor", "Sensor", ["llmsensor/telemetry/l0.py"]), "R2", "hard")
        self.assertCaught(rules.r2_ownership(self.c, "ga-SDK", "ga-SDK", ["METHOD.md"]), "R2", "hard")
        self.assertEqual(rules.r2_ownership(self.c, "Sensor", "Sensor", ["llmsensor/sensing/a.py", "README.md"]), [])
        unowned = rules.r2_ownership(self.c, "Telemetry", "Telemetry", [])
        self.assertEqual(unowned, [])

    def test_r3_push(self):
        b = self.c.sessions["ga-SDK"].branch
        self.assertCaught(rules.r3_push(self.c, "ga-SDK", "ga-SDK", self.c.integration_branch), "R3", "hard")
        self.assertCaught(rules.r3_push(self.c, "ga-SDK", "MS", b), "R3", "hard")
        self.assertCaught(rules.r3_push(self.c, "ga-SDK", "ga-SDK", b, force=True), "R3", "hard")
        self.assertCaught(rules.r3_push(self.c, "baseline", "ga-SDK", b), "R3", "hard")
        self.assertCaught(rules.r3_integration_moved(self.c, "MS", "a" * 40, "b" * 40), "R3", "hard")
        self.assertEqual(rules.r3_push(self.c, "ga-SDK", "ga-SDK", b), [])
        self.assertEqual(rules.r3_push(self.c, "baseline", "ga-SDK", self.c.integration_branch), [])

    def test_r4_ff(self):
        self.assertCaught(rules.r4_ff(self.c, "MS", "MS", False), "R4", "hard")
        self.assertEqual(rules.r4_ff(self.c, "MS", "MS", True), [])

    def test_r5_actions(self):
        for a in ("pr", "merge_default", "tag", "stage_close"):
            self.assertCaught(rules.r5_action(self.c, a), "R5", "hard")
        self.assertEqual(rules.r5_action(self.c, None), [])

    def test_r6_secrets(self):
        samples = [
            fake("sk-", "ant-", "api03-", "x" * 30),
            fake("gh", "p_", "A" * 36),
            fake("AK", "IA", "ABCDEFGHIJKLMNOP"),
            fake("-----BEGIN ", "RSA PRIVATE", " KEY-----"),
            fake("api_key", " = '", "abcdefghijklmnopqrstuv", "'"),
        ]
        for s in samples:
            with self.subTest(s=s[:8]):
                found = rules.r6_secrets(self.c, f"diff\n+{s}\n", "MS diff")
                self.assertCaught(found, "R6", "hard")
                self.assertNotIn(s, " ".join(p.message for p in found))  # never echo the secret
        self.assertEqual(rules.r6_secrets(self.c, "token = os.environ['X']\nsk-short", "x"), [])

    def test_r7_done_when(self):
        d = valid("directive/1"); del d["done_when"]
        self.assertCaught(rules.r7_done_when(self.c, d), "R7", "soft")
        self.assertEqual(rules.r7_done_when(self.c, valid("directive/1")), [])
        raised = cfg(rules={"raise": ["R7"]})
        self.assertCaught(rules.r7_done_when(raised, d), "R7", "hard")

    def test_r8_duplicate(self):
        d = valid("directive/1")
        other = dict(valid("directive/1"), id="CMD-M1", to="MS")
        self.assertCaught(rules.r8_duplicate(self.c, d, [{"directive": other, "status": "open", "round": 3}]), "R8", "soft")
        self.assertCaught(rules.r8_duplicate(self.c, d, [{"directive": dict(other, to="GA"), "status": "failed", "round": 4}]), "R8", "soft")
        self.assertEqual(rules.r8_duplicate(self.c, d, [{"directive": dict(other, goal="다른 일"), "status": "open", "round": 3}]), [])

    def test_r9_quiet(self):
        self.assertCaught(rules.r9_quiet(self.c, 0, 1), "R9", "hard")
        self.assertEqual(rules.r9_quiet(self.c, 0, 0), [])
        self.assertEqual(rules.r9_quiet(self.c, 2, 3), [])

    def test_r10_wait(self):
        self.assertCaught(rules.r10_wait(self.c, 0, "continue"), "R10", "soft")
        self.assertEqual(rules.r10_wait(self.c, 0, "wait"), [])
        self.assertEqual(rules.r10_wait(self.c, 1, "continue"), [])

    def test_r11_counts(self):
        self.assertCaught(rules.r11_counts(self.c, "MS", {"passed": 215, "failed": 0, "skipped": 0}, {"passed": 212, "failed": 0, "skipped": 3}), "R11", "soft")
        same = {"passed": 1, "failed": 0, "skipped": 0}
        self.assertEqual(rules.r11_counts(self.c, "MS", same, dict(same)), [])

    def test_r12_budget(self):
        d = valid("directive/1")  # budget llm_runs 6, config limit 6
        self.assertCaught(rules.r12_budget(self.c, d, {"llm_runs": 1}), "R12", "hard")
        self.assertEqual(rules.r12_budget(self.c, d, {}), [])
        self.assertCaught(rules.r12_budget(self.c, None, {"llm_runs": 7}), "R12", "hard")
        exact = cfg(budget={"cost": 0.05})
        self.assertCaught(rules.r12_budget(exact, None, {"cost": 0.05}), "R12", "hard")  # limit reached: next turn's cost is unknown
        self.assertEqual(rules.r12_budget(exact, None, {"cost": 0.04}), [])
        self.assertEqual(rules.r12_budget(self.c, {"budget": {"model": "haiku"}}, {}), [])  # non-numeric entries are not limits

    def test_r13_commands(self):
        bad = [
            'pip install "x" # install it',
            "pip install rlo-sdk[sensor]",
            "curl https://example.com/a?b=1",
        ]
        for line in bad:
            with self.subTest(line=line):
                self.assertCaught(rules.r13_commands(self.c, f"```\n{line}\n```"), "R13", "soft")
        good = [
            'pip install "rlo-sdk[sensor] @ git+https://github.com/cogito5170/rlo-SDK@d313414"',
            "# a whole-line comment is fine\ngit status",
            "echo 'a # b'",
        ]
        for text in good:
            with self.subTest(text=text):
                self.assertEqual(rules.r13_commands(self.c, f"```sh\n{text}\n```"), [])

    def test_r1b_unclaimed_change(self):
        self.assertCaught(rules.r1b_unclaimed(self.c, "MS", "MS", "a" * 40, "no directive"), "R1b", "hard")

    def test_every_rule_has_a_check(self):
        self.assertEqual(list(rules.ALL), list(gacfg.RULE_IDS))
        self.assertEqual(gacfg.RULE_IDS, ("R1", "R1b", *(f"R{i}" for i in range(2, 14))))


class GateDetectTest(unittest.TestCase):
    def setUp(self):
        self.c = cfg()

    def numbers(self, **kw):
        return sorted({g.number for g in detect(self.c, **kw)})

    def test_each_gate_fires(self):
        user_bds = {"BD-125"}
        rep = valid("report/1")
        self.assertEqual(self.numbers(proposal={"action": "stage_close"}), [1])
        self.assertEqual(self.numbers(report=dict(rep, change_size="architecture")), [2])
        self.assertEqual(self.numbers(report=rep, body="## Deviation\nBD-125 와 부딪힌다\n", user_decision=user_bds.__contains__), [3])
        self.assertEqual(self.numbers(findings=rules.r2_ownership(self.c, "Telemetry", "MS", []) + [
            rules.Problem("x:y", "no row in the ownership table (gate 4)", "hard", "R2")]), [4])
        self.assertEqual(self.numbers(report=rep, body="## Request\nOQ-19 값을 정해 달라\n"), [5])
        self.assertEqual(self.numbers(findings=rules.r12_budget(self.c, None, {"llm_runs": 9})), [6])
        self.assertEqual(self.numbers(report=dict(rep, needs=["credential"])), [6])
        self.assertEqual(self.numbers(report=dict(rep, needs=["new_repo"])), [7])
        self.assertEqual(self.numbers(report=rep, body="## Request\n요청: Billing 세션이 X 를\n"), [7])

    def test_no_gate_on_plain_report(self):
        self.assertEqual(self.numbers(report=valid("report/1"), body="## Request\n요청: MS 가 Y 를 고친다\n"), [])
        self.assertEqual(self.numbers(report=valid("report/1"), body="## Deviation\nBD-125 와 부딪힌다\n"), [])  # hub decision

    def test_question_form(self):
        g = detect(self.c, proposal={"action": "tag"})[0]
        q = question_for(g, why="단계를 고정", changed="통합 머리 sha", next_="태그 명령을 사람에게")
        self.assertEqual(q["gate"], 1)
        self.assertEqual(q["recommendation"], "보류")
        self.assertIn("```ga", dump_text(q))


if __name__ == "__main__":
    unittest.main()
