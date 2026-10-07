"""VI-11a (baseline acceptance test, VMAUTO GA57; research/VM_INTERIOR_DESIGN.md §4.2-§4.3, GA_ENGINE_OPS §2-§3):
the Ops worker's rule/1 table, action-spec/1 table and Guard as pure code. 0 model calls.

ga/vm/ops_rules.py (imports only __future__, re, typing):
  RISKS = ("low", "medium", "high");  DAILY_TURNS = 20 (= ga.hub.DAILY_TURNS)
  RULES (rule/1 {schema, id, kind, source, action}), in this order:
    O1 cap_reached -> alert_ops;  O2 decide_no_model -> retry_next_rung;
    O4 mut_survived -> send_back_template;  O7 base_drift -> send_back_template;  source "shadow" for all
  ACTIONS (action-spec/1 {schema, name, risk, preconditions, postcondition, window_ms}):
    alert_ops low ["day"];  retry_next_rung low ["id", "error"];  send_back_template low ["id", "sha", "detail"];
    raise_cap_once high ["day"];  postcondition "the anomaly is not raised at a later tick";  window_ms None
  table_problems(rules, actions) -> list[str]: [] for RULES/ACTIONS; one or more strings when an action has a bad
    schema / duplicate name / risk not in RISKS / preconditions not a list of str / empty postcondition / window_ms
    not None and not an int >= 0 (bool is not an int), or a rule has a bad schema / an id not ^O\\d+$ or duplicate /
    empty kind / an action not in the table.
  action_for(rule_id) -> the ACTIONS row named by that rule.
  classify(error): "timeout" if it contains timeout / timed out (any case); else "form" if it contains form, parse
    or json; else "backend".
  evaluate(rows, *, today, daily_turns=DAILY_TURNS) -> anomalies [{rule, kind, key, evidence}] sorted by (rule, key);
    rows are the hub's shadow.jsonl rows:
    O1  n = rows whose "at" starts with today; n >= daily_turns -> key today, evidence {day, rows: n, daily_turns}
    per id (str, non-empty), only its LAST row counts (a later row of the same id replaces the earlier one):
    O2  error non-empty and decision in (ASK_HUMAN, SHADOW) -> key id, evidence {id, rev, error, class: classify}
    O4  decision SEND_BACK and asks[0] starts with "mutations:" -> key id, evidence {id, rev, sha, detail: asks[0]}
    O7  decision SEND_BACK and asks[0] starts with "ancestry:"  -> key id, evidence {id, rev, sha, detail: asks[0]}
  guard(spec, anomaly, mode="shadow") -> {action, risk, decision, why}; mode not shadow/enforce -> ValueError;
    1. a precondition key missing (or None / "") in anomaly["evidence"] -> decision "refused", why starts
       "precondition missing: " and names the keys;  2. risk high -> "blocked" (in every mode);
    3. mode shadow -> "would_do";  4. enforce: low -> "execute", medium -> "execute_report".
"""
import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAY = "2026-10-07"


def R():
    from ga.vm import ops_rules
    return ops_rules


def row(i, at, decision, asks=(), error=None, rev=1, sha="abc1234"):
    return {"at": f"{DAY}T{at}Z", "id": i, "rev": rev, "sha": sha, "decision": decision, "asks": list(asks),
            "error": error}


class Tables(unittest.TestCase):
    def test_rules_and_actions(self):
        m = R()
        self.assertEqual(m.RISKS, ("low", "medium", "high"))
        self.assertEqual([(r["schema"], r["id"], r["kind"], r["source"], r["action"]) for r in m.RULES], [
            ("rule/1", "O1", "cap_reached", "shadow", "alert_ops"),
            ("rule/1", "O2", "decide_no_model", "shadow", "retry_next_rung"),
            ("rule/1", "O4", "mut_survived", "shadow", "send_back_template"),
            ("rule/1", "O7", "base_drift", "shadow", "send_back_template")])
        got = {a["name"]: (a["schema"], a["risk"], a["preconditions"], a["window_ms"]) for a in m.ACTIONS}
        self.assertEqual(got, {"alert_ops": ("action-spec/1", "low", ["day"], None),
                               "retry_next_rung": ("action-spec/1", "low", ["id", "error"], None),
                               "send_back_template": ("action-spec/1", "low", ["id", "sha", "detail"], None),
                               "raise_cap_once": ("action-spec/1", "high", ["day"], None)})
        self.assertTrue(all(a["postcondition"] == "the anomaly is not raised at a later tick" for a in m.ACTIONS))
        self.assertEqual(m.table_problems(m.RULES, m.ACTIONS), [])
        self.assertEqual(m.action_for("O7")["name"], "send_back_template")
        self.assertEqual(m.action_for("O1")["name"], "alert_ops")

    def test_daily_turns_is_the_hubs(self):
        from ga.hub import DAILY_TURNS
        self.assertEqual(R().DAILY_TURNS, DAILY_TURNS)

    def test_bad_tables_have_problems(self):
        m = R()
        good = [dict(a) for a in m.ACTIONS]
        for bad in ({"risk": "huge"}, {"window_ms": True}, {"window_ms": -1}, {"postcondition": " "},
                    {"preconditions": "day"}, {"schema": "action/1"}, {"name": "retry_next_rung"}):
            acts = [dict(a) for a in good]
            acts[0] = {**acts[0], **bad}
            self.assertNotEqual(m.table_problems(m.RULES, acts), [], bad)
        for bad in ({"id": "X1"}, {"id": "O2"}, {"kind": ""}, {"action": "nope"}, {"schema": "rule/2"}):
            rules = [dict(r) for r in m.RULES]
            rules[0] = {**rules[0], **bad}
            self.assertNotEqual(m.table_problems(rules, good), [], bad)
        self.assertEqual(m.table_problems(m.RULES, [*good[:3], {**good[3], "window_ms": 60000}]), [])


class Evaluate(unittest.TestCase):
    def test_each_rule(self):
        rows = [row("CMD-A1", "01:00:00", "SEND_BACK", ["mutations: survived: m2, m4"]),
                row("CMD-B2", "01:05:00", "SHADOW", ["ga verdict failed: TimeoutExpired"], "verdict:TimeoutExpired", 2),
                row("CMD-C3", "01:10:00", "SEND_BACK", ["ancestry: not a descendant of the base"], sha="def5678"),
                row("CMD-D4", "01:15:00", "ACCEPT", ["all executable checks pass"])]
        got = R().evaluate(rows, today=DAY, daily_turns=4)
        self.assertEqual(got, [
            {"rule": "O1", "kind": "cap_reached", "key": DAY, "evidence": {"day": DAY, "rows": 4, "daily_turns": 4}},
            {"rule": "O2", "kind": "decide_no_model", "key": "CMD-B2",
             "evidence": {"id": "CMD-B2", "rev": 2, "error": "verdict:TimeoutExpired", "class": "timeout"}},
            {"rule": "O4", "kind": "mut_survived", "key": "CMD-A1",
             "evidence": {"id": "CMD-A1", "rev": 1, "sha": "abc1234", "detail": "mutations: survived: m2, m4"}},
            {"rule": "O7", "kind": "base_drift", "key": "CMD-C3",
             "evidence": {"id": "CMD-C3", "rev": 1, "sha": "def5678", "detail": "ancestry: not a descendant of the base"}}])

    def test_cap_counts_only_today_and_defaults_to_daily_turns(self):
        m = R()
        rows = [row(f"CMD-X{i}", "00:00:01", "ACCEPT") for i in range(19)]
        rows.append({**row("CMD-Y1", "00:00:01", "ACCEPT"), "at": "2026-10-06T23:59:59Z"})
        self.assertEqual(m.evaluate(rows, today=DAY), [])
        rows.append(row("CMD-Z1", "09:00:00", "ACCEPT"))
        self.assertEqual([(a["rule"], a["key"], a["evidence"]["rows"]) for a in m.evaluate(rows, today=DAY)],
                         [("O1", DAY, 20)])

    def test_only_the_last_row_of_an_id_counts(self):
        m = R()
        rows = [row("CMD-A1", "01:00:00", "SEND_BACK", ["mutations: survived: m2"]),
                row("CMD-A1", "02:00:00", "ACCEPT", ["all executable checks pass"], rev=2),
                row("CMD-B2", "01:00:00", "ACCEPT"),
                row("CMD-B2", "02:00:00", "SEND_BACK", ["ancestry: diverged"], rev=3)]
        self.assertEqual([(a["rule"], a["key"], a["evidence"]["rev"]) for a in m.evaluate(rows, today=DAY, daily_turns=99)],
                         [("O7", "CMD-B2", 3)])

    def test_o2_needs_an_error_and_an_undecided_decision(self):
        m = R()
        rows = [row("CMD-A1", "01:00:00", "ASK_HUMAN", [], "backend exit 1"),
                row("CMD-B2", "01:00:00", "SHADOW", ["no acceptance test for CMD-B2"], ""),
                row("CMD-C3", "01:00:00", "SEND_BACK", ["files: x.py"], "verdict:Boom"),
                row("CMD-D4", "01:00:00", "SHADOW", [], "answer did not parse as a form")]
        got = [(a["rule"], a["key"], a["evidence"]["class"]) for a in m.evaluate(rows, today=DAY, daily_turns=99)]
        self.assertEqual(got, [("O2", "CMD-A1", "backend"), ("O2", "CMD-D4", "form")])
        self.assertEqual(m.classify("Request TIMED OUT"), "timeout")
        self.assertEqual(m.classify("bad JSON line"), "form")
        self.assertEqual(m.classify("verdict:CalledProcessError"), "backend")

    def test_send_back_for_other_reasons_is_no_anomaly(self):
        rows = [row("CMD-A1", "01:00:00", "SEND_BACK", ["red_base: the test passes on the base"]),
                row("CMD-B2", "01:00:00", "SEND_BACK", [])]
        self.assertEqual(R().evaluate(rows, today=DAY, daily_turns=99), [])


class Guard(unittest.TestCase):
    def a(self, **ev):
        return {"rule": "O4", "kind": "mut_survived", "key": "CMD-A1", "evidence": ev}

    def test_order_of_the_guard(self):
        m = R()
        sb = m.action_for("O4")
        full = self.a(id="CMD-A1", sha="abc1234", detail="mutations: survived: m2")
        g = m.guard(sb, full)
        self.assertEqual((g["action"], g["risk"], g["decision"]), ("send_back_template", "low", "would_do"))
        self.assertTrue(g["why"])
        self.assertEqual(m.guard(sb, full, "enforce")["decision"], "execute")
        r = m.guard(sb, self.a(id="CMD-A1", sha=None, detail=""))
        self.assertEqual(r["decision"], "refused")
        self.assertTrue(r["why"].startswith("precondition missing: "))
        self.assertIn("sha", r["why"])
        self.assertIn("detail", r["why"])
        high = next(a for a in m.ACTIONS if a["name"] == "raise_cap_once")
        for mode in ("shadow", "enforce"):
            self.assertEqual(m.guard(high, {"evidence": {"day": DAY}}, mode)["decision"], "blocked")
        self.assertEqual(m.guard(high, {"evidence": {}}, "enforce")["decision"], "refused")
        med = {**sb, "risk": "medium"}
        self.assertEqual(m.guard(med, full, "enforce")["decision"], "execute_report")
        self.assertEqual(m.guard(med, full, "shadow")["decision"], "would_do")
        with self.assertRaises(ValueError):
            m.guard(sb, full, "live")


class NoModel(unittest.TestCase):
    def test_imports(self):
        tree = ast.parse((ROOT / "ga" / "vm" / "ops_rules.py").read_text(encoding="utf-8"))
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                names.add("." * n.level + (n.module or ""))
        self.assertLessEqual(names, {"__future__", "re", "typing"})
        code = "import sys, ga.vm.ops_rules\nprint([m for m in sys.modules if m.startswith(('ga.llm', 'ga.backends', 'ga.gemini', 'ga.act', 'ga.hub'))])"
        p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "[]"), p.stderr)


if __name__ == "__main__":
    unittest.main()
