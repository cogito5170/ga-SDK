"""CMD-GA57: ga ops tick — observe -> rule/1 -> guarded action-spec/1 -> VERIFY -> escalate; a model only when no rule matches."""
import io
import json
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ga.forms import parse_text, validate
from ga.forms.core import hard
from ga.ops import core as O

DAY0 = 1_790_000_000.0  # 2026-09-21T...Z


class Clock:
    def __init__(self, t=DAY0):
        self.t = t

    def __call__(self):
        return self.t


class FakeFx:
    def __init__(self, answer="alert_ops", usage=None, model_raises=False):
        self.calls, self.mails, self.model_calls = [], [], []
        self.answer, self.usage, self.model_raises = answer, usage, model_raises
        self.fail_send_back = False

    def retry(self, mail, model):
        self.calls.append(("retry", mail, model))
        return "shadow ASK_HUMAN x"

    def mail(self, to, text):
        head, _ = parse_text(text)
        assert not hard(validate(head)), validate(head)
        self.mails.append((to, head))
        return f"to/{to}/{len(self.mails)}.md"

    def send_back(self, o, asks):
        if self.fail_send_back:
            raise RuntimeError("no directive on file")
        self.calls.append(("send_back", o["id"], list(asks)))
        return f"{o['id']} rev 2 -> worker"

    def model_turn(self, card, model):
        if self.model_raises:
            raise AssertionError("no model turn may run here")
        self.model_calls.append((card, model))
        n = len(re.findall(r"^ITEM \d+:", card, re.M))
        ans = self.answer if not isinstance(self.answer, str) or "\n" in self.answer or self.answer[:1].isdigit() \
            else "\n".join(f"{i} {self.answer}" for i in range(1, n + 1))
        ans = ans(card, model) if callable(ans) else ans
        usage = self.usage(card, model, n) if callable(self.usage) else self.usage
        return {"answer": ans, "usage": usage or {"input": 321, "output": 4}, "served": model}


def row(mail="to/baseline/r1.md", **kw):
    r = {"at": "2026-09-21T10:00:00Z", "id": "CMD-GA99", "rev": 1, "sha": None, "judge_class": "success", "needs": [],
         "decision": "ASK_HUMAN", "asks": ["the model's answer was not ACCEPT / SEND_BACK / ASK_HUMAN"], "error": None,
         "served": None, "tokens": {"input": None, "output": None}, "mail": mail,
         "model_configured": "auto", "model_resolved": "gpt-oss-120b-medium"}
    r.update(kw)
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ga = self.tmp / ".ga"
        (self.ga / "hub").mkdir(parents=True)
        self.clock = Clock()
        self.fx = FakeFx()
        self.conf = {"name": "baseline", "repos": {}, "model": "gpt-oss-120b-medium", "backend": "agv"}

    def add(self, *rows):
        with open(self.ga / "hub" / "shadow.jsonl", "a", encoding="utf-8") as h:
            for r in rows:
                h.write(json.dumps(r) + "\n")

    def ops(self, **kw):
        return O.Ops(self.conf, ga_dir=self.ga, effects=self.fx, clock=self.clock, **kw)

    def state(self):
        return json.loads((self.ga / "ops" / "state.json").read_text())

    def alerts(self, kind=None):
        return [h for _, h in self.fx.mails if h["kind"] == "alert" and (kind is None or h["id"] == f"ops:{kind}")]


class TableAndGuard(Base):
    def test_table_is_rule1_and_rlo_action_spec1(self):
        t = O.load_table()
        self.assertTrue(t["rules"] and all(r["schema"] == "rule/1" and r["id"] and r["kind"] and r["when"] for r in t["rules"]))
        for name, a in t["actions"].items():
            self.assertEqual(a["schema"], "action-spec/1")
            self.assertIn(a["risk"], ("low", "medium", "high"))
            self.assertEqual(a["window_ms"], a["window_s"] * 1000)
            for k in ("preconditions", "postcondition", "description", "version", "params"):
                self.assertIn(k, a)
        for need in ("retry_next_rung", "alert_ops", "send_back_base_drift", "send_back_survivors"):
            self.assertIn(need, t["actions"])
        self.assertEqual(t["actions"]["alert_ops"]["risk"], "low")
        self.assertEqual(t["actions"]["send_back_base_drift"]["risk"], "medium")

    def test_guard(self):
        self.assertEqual(O.guard({"risk": "low"}), "run")
        self.assertEqual(O.guard({"risk": "medium"}), "run+alert")
        self.assertEqual(O.guard({"risk": "high"}), "blocked")
        self.assertEqual(O.guard({"risk": "external"}), "blocked")

    def test_bad_table_refused(self):
        f = self.tmp / "t.json"
        f.write_text(json.dumps({"rules": [], "actions": [{"name": "x", "risk": "huge"}]}))
        with self.assertRaises(ValueError):
            O.load_table(f)
        f.write_text(json.dumps({"rules": [{"id": "r", "action": "nope", "when": {}}], "actions": []}))
        with self.assertRaises(ValueError):
            O.load_table(f)

    def test_high_risk_never_runs_alert_only(self):
        self.fx.model_raises = True
        self.add(row(tokens={"input": 900, "output": 9}, served="gpt-oss-120b-medium",
                     asks=["integration refused: CalledProcessError: push rejected"]))
        # a model ASK_HUMAN with tokens is not an anomaly by itself: make it one with an error
        self.add(row(mail="to/baseline/r2.md", error="integration", asks=["integration refused: push rejected"]))
        out = self.ops().tick()
        d = [r for r in out if r.get("action") == "push_integration"]
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["guard"], "blocked")
        self.assertEqual(self.fx.calls, [])
        self.assertEqual(len(self.alerts("high:push_integration")), 1)
        self.assertEqual(self.fx.model_calls, [])


class O2Retry(Base):
    def test_no_call_rule_retries_next_rung_then_verify_met(self):
        self.fx.model_raises = True  # a rule matches: zero model calls
        self.add(row())
        out = self.ops().tick()
        self.assertEqual([r["rule"] for r in out], ["o2_no_call"])
        self.assertEqual(self.fx.calls, [("retry", "to/baseline/r1.md", "gemini-3.6-flash-low")])
        st = self.state()
        self.assertEqual(st["pending"]["to/baseline/r1.md"]["post"], ["shadow_row_has_model_call"])
        # nothing new yet: inside the window, no action, no verify row
        self.clock.t += 60
        self.assertEqual(self.ops().tick(), [])
        self.add(row(at="2026-09-21T10:02:00Z", served="gemini-3.6-flash-low", tokens={"input": 2000, "output": 20},
                     decision="ACCEPT", model_resolved="gemini-3.6-flash-low"))
        out = self.ops().tick()
        self.assertEqual([(r["verify"], r["action"]) for r in out], [("met", "retry_next_rung")])
        self.assertEqual(self.state()["pending"], {})
        self.assertEqual(len(self.fx.calls), 1)
        self.assertEqual(self.ops().tick(), [])  # done: never again
        self.assertEqual(self.fx.mails, [])

    def test_backend_error_rule(self):
        self.fx.model_raises = True
        self.add(row(error="backend:served_model_mismatch", judge_class="failure"))
        out = self.ops().tick()
        self.assertEqual(out[0]["rule"], "o2_backend_error")
        self.assertEqual(self.fx.calls[0][0], "retry")

    def test_same_failure_twice_is_one_blocked_alert(self):
        self.add(row())
        self.ops().tick()
        # retry 1 decided again without a model call: failure 1 -> one rung higher
        self.add(row(at="2026-09-21T10:02:00Z", model_resolved="gemini-3.6-flash-low"))
        out = self.ops().tick()
        self.assertEqual([r.get("verify") for r in out if r.get("verify")], ["failed"])
        self.assertEqual(self.fx.calls[-1], ("retry", "to/baseline/r1.md", "gemini-3.6-flash-medium"))
        self.assertEqual(self.alerts(), [])
        # retry 2 fails the same way: blocked, one alert with evidence, and nothing more
        self.add(row(at="2026-09-21T10:04:00Z", model_resolved="gemini-3.6-flash-medium"))
        self.ops().tick()
        a = self.alerts("blocked")
        self.assertEqual(len(a), 1)
        self.assertIn("o2_no_call", a[0]["note"])
        self.assertIn("CMD-GA99", a[0]["note"])
        self.assertIn("to/baseline/r1.md", self.state()["blocked"])
        n = len(self.fx.calls)
        for k in range(3):
            self.add(row(at=f"2026-09-21T11:0{k}:00Z", model_resolved="gemini-3.6-flash-medium"))
            self.clock.t += 3600
            self.ops().tick()
        self.assertEqual(len(self.fx.calls), n)
        self.assertEqual(len(self.alerts()), 1)

    def test_window_expiry_is_a_failure_one_rung_higher(self):
        self.add(row())
        self.ops().tick()
        self.clock.t += 899
        self.assertEqual(self.ops().tick(), [])
        self.clock.t += 2
        out = self.ops().tick()
        self.assertEqual(out[0]["verify"], "failed")
        self.assertIn("not met within", out[0]["result"])
        self.assertEqual(self.fx.calls[-1][0], "retry")
        self.assertEqual(len(self.fx.calls), 2)

    def test_no_rung_above_is_a_failure(self):
        self.add(row(model_resolved="claude-opus-5-5-high"))
        out = self.ops().tick()
        self.assertTrue(out[0]["result"].startswith("failed"))
        self.assertEqual(self.fx.calls, [])

    def test_retry_raising_counts_as_failure(self):
        def boom(mail, model):
            raise RuntimeError("mail gone")
        self.fx.retry = boom
        self.add(row())
        self.ops().tick()
        st = self.state()
        self.assertIn("to/baseline/r1.md", st["blocked"])  # failed twice in one tick: rung 0 and rung 1
        self.assertEqual(len(self.alerts("blocked")), 1)


class Alerts(Base):
    def test_pre_decide_alert_once_per_kind_per_utc_day(self):
        self.fx.model_raises = True
        self.add(row(judge_class=None, asks=["no directive CMD-GA99 on file"]),
                 row(mail="to/baseline/r2.md", judge_class=None, asks=["ga judge failed: CalledProcessError"]))
        out = self.ops().tick()
        self.assertEqual([r["rule"] for r in out if r.get("rule")], ["o2_pre_decide", "o2_pre_decide"])
        a = self.alerts("hub_pre_decide")
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["to"], "baseline-ops")
        self.assertIn("no directive CMD-GA99 on file", a[0]["note"])
        self.clock.t += 86400
        self.add(row(mail="to/baseline/r3.md", judge_class=None, asks=["no directive CMD-GA98 on file"]))
        self.ops().tick()
        self.assertEqual(len(self.alerts("hub_pre_decide")), 2)

    def test_model_ask_with_tokens_is_not_an_anomaly(self):
        self.fx.model_raises = True
        self.add(row(tokens={"input": 1000, "output": 10}, served="gpt-oss-120b-medium", asks=["which repo?"]))
        self.assertEqual(self.ops().tick(), [])
        self.assertEqual(self.fx.calls + self.fx.mails, [])


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout.strip()


class Drift(Base):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "a.py").write_text("x = 1\n\ndef f():\n    return x + 1\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "a")
        self.a = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/integ", self.a)
        git(self.repo, "checkout", "-q", "--orphan", "stray")
        (self.repo / "b.py").write_text("y = 2\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "b")
        self.stray = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "c.py").write_text("z = 3\n")
        git(self.repo, "add", "c.py")
        git(self.repo, "commit", "-q", "-m", "c")
        self.good = git(self.repo, "rev-parse", "HEAD")
        self.conf["repos"] = {"o/r": {"path": str(self.repo), "base": "integ"}}

    def test_descends(self):
        self.assertEqual(O.descends(self.conf, self.stray), (False, "origin/integ"))
        self.assertEqual(O.descends(self.conf, self.good), (True, "origin/integ"))
        self.assertEqual(O.descends(self.conf, "0" * 40), (None, None))
        self.assertEqual(O.descends(self.conf, None), (None, None))

    def test_base_drift_sends_back_remerge_medium_alert_then_verify(self):
        self.fx.model_raises = True
        self.add(row(sha=self.stray, judge_class="failure", tokens={"input": 900, "output": 9},
                     served="gpt-oss-120b-medium", decision="SEND_BACK"))
        out = self.ops().tick()
        self.assertEqual(out[0]["rule"], "o7_base_drift")
        self.assertEqual(out[0]["guard"], "run+alert")
        kind, did, asks = self.fx.calls[0]
        self.assertEqual((kind, did), ("send_back", "CMD-GA99"))
        self.assertIn("re-merge origin/integ", asks[0])
        self.assertIn(self.stray[:12], asks[0])
        self.assertEqual(len(self.alerts("medium:send_back_base_drift")), 1)
        # the worker re-merged: a newer report for the same id, its head descends
        self.add(row(mail="to/baseline/r2.md", sha=self.good, judge_class="success", tokens={"input": 900, "output": 9},
                     served="gpt-oss-120b-medium", decision="ACCEPT"))
        out = self.ops().tick()
        self.assertEqual([r.get("verify") for r in out], ["met"])

    def test_send_back_still_drifting_escalates_to_alert(self):
        self.add(row(sha=self.stray, decision="SEND_BACK", tokens={"input": 9, "output": 1}))
        self.ops().tick()
        git(self.repo, "checkout", "-q", "stray")
        (self.repo / "d.py").write_text("w = 4\n")
        git(self.repo, "add", "d.py")
        git(self.repo, "commit", "-q", "-m", "d")
        stray2 = git(self.repo, "rev-parse", "HEAD")
        self.add(row(mail="to/baseline/r2.md", sha=stray2, decision="SEND_BACK", tokens={"input": 9, "output": 1}))
        out = self.ops().tick()
        self.assertEqual(out[0]["verify"], "failed")
        self.assertEqual(len(self.alerts("base_drift")), 1)

    def test_send_back_refused_is_a_failure(self):
        self.fx.fail_send_back = True
        self.add(row(sha=self.stray, decision="SEND_BACK", tokens={"input": 9, "output": 1}))
        out = self.ops().tick()
        self.assertTrue(out[0]["result"].startswith("failed"))
        self.assertEqual(len(self.alerts("base_drift")), 1)  # one rung higher: alert_ops

    def test_survivors_ask_names_file_line_expected(self):
        spec = self.tmp / "mut.json"
        spec.write_text(json.dumps([{"id": "m1", "file": "a.py", "find": "return x + 1", "replace": "return x - 1",
                                     "tests": ["tests/test_a.py"], "expect": "f() returns x + 1"}]))
        self.conf["repos"]["o/r"]["mutations"] = str(spec)
        self.fx.model_raises = True
        self.add(row(sha=self.good, judge_class="insufficient", tokens={"input": 9, "output": 1}, decision="SEND_BACK",
                     needs=["mutation m1 survived: tests tests/test_a.py do not cover it"]))
        out = self.ops().tick()
        self.assertEqual(out[0]["rule"], "o4_survivors")
        asks = self.fx.calls[0][2]
        self.assertEqual(len(asks), 1)
        self.assertIn("a.py:4", asks[0])
        self.assertIn("tests/test_a.py", asks[0])
        self.assertIn("expected: f() returns x + 1", asks[0])
        self.add(row(mail="to/baseline/r2.md", sha=self.a, judge_class="success", tokens={"input": 9, "output": 1},
                     decision="ACCEPT"))
        self.assertEqual([r.get("verify") for r in self.ops().tick()], ["met"])


class Model(Base):
    def test_model_only_when_no_rule_cheapest_working_rung_card_capped_ledger(self):
        (self.ga / "hub" / "ledger").mkdir()
        (self.ga / "hub" / "ledger" / "2026-09-20.jsonl").write_text(
            json.dumps({"served": "claude-sonnet-5-5-low", "input": 10, "output": 1, "error": ""}) + "\n"
            + json.dumps({"served": "gemini-3.7-flash-low", "input": 10, "output": 1, "error": ""}) + "\n"
            + json.dumps({"served": "gpt-oss-120b-medium", "input": None, "error": "backend:x"}) + "\n")
        self.add(row(judge_class="insufficient", needs=["x" * 5000] * 6, asks=["y" * 4000] * 3))
        out = self.ops().tick()
        self.assertEqual(len(self.fx.model_calls), 1)
        card, model = self.fx.model_calls[0]
        self.assertEqual(model, "gemini-3.7-flash-low")
        from ga.ctxpack import tokens
        self.assertLessEqual(tokens(card), 1500)
        self.assertIn("retry_next_rung", card)
        d = [r for r in out if r.get("rule") == "model"]
        self.assertEqual(len(d), 1)
        self.assertTrue(d[0]["model_turn"])
        self.assertEqual(d[0]["action"], "alert_ops")
        self.assertEqual(len(self.alerts("model")), 1)

    def test_card_capped_whatever_the_table(self):
        t = O.load_table()
        t["actions"]["wait"]["description"] = "z" * 20000
        from ga.ctxpack import tokens
        card = self.ops(table=t).evidence_card({"subject": "m", "asks": [], "needs": []})
        self.assertLessEqual(tokens(card), 1500)
        self.assertIn("\n...\n", card)

    def test_three_items_one_call_fixed_prefix(self):
        self.add(*[row(mail=f"to/baseline/r{k}.md", judge_class="insufficient") for k in range(3)])
        self.fx.answer = "1 wait\n2 alert_ops\n3: wait"
        out = self.ops().tick()
        self.assertEqual(len(self.fx.model_calls), 1)
        card = self.fx.model_calls[0][0]
        self.assertEqual(len(re.findall(r"^ITEM \d+:", card, re.M)), 3)
        self.assertTrue(card.startswith(O.Ops.PREFIX))
        acts = {r["subject"]: r["action"] for r in out if r.get("rule") == "model"}
        self.assertEqual(acts, {"to/baseline/r0.md": "wait", "to/baseline/r1.md": "alert_ops", "to/baseline/r2.md": "wait"})
        # the next call's prefix is byte-identical (it caches)
        self.add(row(mail="to/baseline/r9.md", judge_class="failure"))
        self.ops().tick()
        c2 = self.fx.model_calls[1][0]
        p = c2.index("## items\n")
        self.assertEqual(c2[:p], card[:p])

    def test_ledger_fields(self):
        self.fx.usage = {"input": 900, "output": 30, "cache_read": 700, "cache_write": 0}
        self.add(row(judge_class="insufficient"), row(mail="to/baseline/r2.md", judge_class="insufficient"))
        self.ops().tick()
        led = O.read_jsonl(self.ga / "ops" / "ledger" / "2026-09-21.jsonl")
        self.assertEqual(len(led), 1)
        r = led[0]
        self.assertEqual((r["input"], r["output"], r["cache_read"], r["cache_write"]), (900, 30, 700, 0))
        self.assertEqual((r["n_items"], r["tokens_per_item"], r["rung"]), (2, 465.0, "gpt-oss-120b-medium"))

    def test_batch_size_bounds_one_call_rest_next_tick(self):
        self.add(*[row(mail=f"to/baseline/r{k}.md", judge_class="insufficient") for k in range(10)])
        self.fx.answer = lambda card, model: "\n".join(f"{i} {'wait' if i % 2 else 'alert_ops'}" for i in range(1, 9))
        self.ops().tick()
        self.assertEqual(len(self.fx.model_calls), 1)
        self.assertEqual(len(re.findall(r"^ITEM", self.fx.model_calls[0][0], re.M)), 8)
        self.ops().tick()
        self.assertEqual(len(re.findall(r"^ITEM", self.fx.model_calls[1][0], re.M)), 2)
        self.ops().tick()
        self.assertEqual(len(self.fx.model_calls), 2)

    def test_answer_outside_table_alerts_unclassified(self):
        self.fx.answer = "1 reboot"
        self.add(row(judge_class="insufficient"))
        self.ops().tick()
        self.assertEqual(len(self.alerts("unclassified")), 1)

    def _steps(self):
        return O.read_jsonl(self.ga / "ops" / "optimize.jsonl")

    def test_over_threshold_tries_smaller_card_and_regression_reverts(self):
        self.fx.usage = {"input": 2000, "output": 10}  # 2010 per item > 800
        self.add(row(mail="to/baseline/a.md", judge_class="insufficient"))
        self.ops().tick()
        self.assertEqual(self.state()["tune"]["item_bytes"], 300)
        self.assertEqual(self._steps()[-1]["result"], "trying")
        self.fx.usage = {"input": 2500, "output": 10}  # worse: revert
        self.add(row(mail="to/baseline/b.md", judge_class="insufficient"))
        self.ops().tick()
        st = self.state()
        self.assertEqual(st["tune"]["item_bytes"], 600)
        self.assertNotIn("trial", st["tune"])
        self.assertEqual(self._steps()[-1]["result"], "reverted")
        # the next step in order: a cheaper rung (none below gpt-oss) is skipped -> larger batch
        self.fx.usage = {"input": 2500, "output": 10}
        self.add(row(mail="to/baseline/c.md", judge_class="insufficient"))
        self.ops().tick()
        self.assertEqual(self._steps()[-1]["step"], "larger_batch")
        self.assertEqual(self.state()["tune"]["batch"], 16)

    def test_improvement_is_kept(self):
        self.fx.usage = {"input": 2000, "output": 10}
        self.add(row(mail="to/baseline/a.md", judge_class="insufficient"))
        self.ops().tick()
        self.fx.usage = {"input": 900, "output": 10}
        self.add(row(mail="to/baseline/b.md", judge_class="insufficient"))
        self.ops().tick()
        self.assertEqual(self.state()["tune"]["item_bytes"], 300)
        self.assertEqual(self._steps()[-1]["result"], "kept")

    def test_cheaper_rung_step(self):
        self.conf["model"] = "gemini-3.6-flash-medium"
        self.fx.usage = {"input": 2000, "output": 10}
        self.ops().tick()
        st = self.ops().load_state()
        st["tune"]["next"] = 1
        self.ops().save_state(st)
        self.add(row(judge_class="insufficient"))
        self.ops().tick()
        self.assertEqual(self.state()["tune"]["rung"], "gemini-3.6-flash-low")
        self.add(row(mail="to/baseline/r2.md", judge_class="insufficient"))
        self.ops().tick()
        self.assertEqual(self.fx.model_calls[-1][1], "gemini-3.6-flash-low")

    def test_repeated_decision_becomes_a_rule(self):
        self.fx.answer = "wait"
        for k in range(3):
            self.add(row(mail=f"to/baseline/r{k}.md", judge_class="insufficient"))
            self.ops().tick()
        self.assertEqual(len(self.fx.model_calls), 3)
        st = self.state()
        self.assertEqual(len(st["rules"]), 1)
        self.assertEqual(st["rules"][0]["action"], "wait")
        self.assertEqual(self._steps()[-1]["step"], "promote")
        self.fx.model_raises = True  # the same shape again: the learned rule, zero model calls
        self.add(row(mail="to/baseline/r9.md", judge_class="insufficient"))
        out = self.ops().tick()
        self.assertEqual(out[0]["rule"], "learned_1")
        # another shape still goes to the model
        self.fx.model_raises = False
        self.add(row(mail="to/baseline/rx.md", judge_class="failure"))
        self.ops().tick()
        self.assertEqual(len(self.fx.model_calls), 4)

    def test_mixed_answers_do_not_promote(self):
        for k, a in enumerate(["wait", "alert_ops", "wait"]):
            self.fx.answer = a
            self.add(row(mail=f"to/baseline/r{k}.md", judge_class="insufficient"))
            self.ops().tick()
        self.assertEqual(self.state()["rules"], [])

    def test_no_working_rung_uses_hub_model(self):
        self.assertEqual(self.ops().cheapest_working(), "gpt-oss-120b-medium")


class DryCliVmConsole(Base):
    def test_dry_run_writes_and_runs_nothing(self):
        self.fx.model_raises = True
        self.add(row(), row(mail="to/baseline/r2.md", judge_class=None))
        out = self.ops().tick(dry_run=True)
        self.assertEqual({r["result"] for r in out}, {"dry run"})
        self.assertFalse((self.ga / "ops").exists())
        self.assertEqual(self.fx.calls + self.fx.mails, [])

    def test_cli(self):
        from ga.__main__ import main
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["ops", "tick", "--config", str(self.tmp / "none.json"), "--ga-dir", str(self.ga)]), 2)
        cf = self.tmp / "hub.json"
        cf.write_text(json.dumps(self.conf))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["ops", "tick", "--dry-run", "--config", str(cf), "--ga-dir", str(self.ga)]), 0)
        self.assertEqual(out.getvalue(), "")
        self.add(row())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["ops", "tick", "--dry-run", "--config", str(cf), "--ga-dir", str(self.ga)]), 0)
        self.assertEqual(json.loads(out.getvalue().splitlines()[0])["action"], "retry_next_rung")

    def test_vm_hub_unit_runs_ops_after_hub(self):
        from ga.vm.core import HUB_UNIT, unit_files
        u = unit_files(Path("/home/u"))[HUB_UNIT]
        lines = [x for x in u.splitlines() if x.startswith("ExecStart=")]
        self.assertEqual(len(lines), 2)
        self.assertIn("-m ga hub tick --shadow", lines[0])
        self.assertIn("-m ga ops tick --config /home/u/.ga/hub.json --ga-dir /home/u/.ga", lines[1])

    def test_last_decisions_newest_first(self):
        self.add(row(judge_class=None), row(mail="to/baseline/r2.md"))
        self.ops().tick()
        rows = O.last_decisions(self.ga)
        self.assertTrue(rows)
        self.assertEqual(rows[0], O.read_jsonl(self.ga / "ops" / "decisions.jsonl")[-1])

    def test_alert_form_is_valid_notify(self):
        head = {"schema": "notify/1", "to": "baseline-ops", "kind": "alert", "id": "ops:blocked",
                "ref": "https://github.com/cogito5170/baseline", "note": "blocked"}
        self.assertFalse(hard(validate(head)))

    def test_console_api_ops(self):
        from ga.console import server
        self.assertIn("/api/ops", server.__doc__)
        js = (Path(server.__file__).parent / "static" / "app.js").read_text()
        self.assertIn('ops: () => api("/api/ops")', js)


class RealEffects(Base):
    def test_retry_and_send_back_through_mailhub_shadow(self):
        """Effects drives MailHub: retry decides one mail again (shadow: one more row); send_back in shadow mode goes
        to the shadow recipient as the directive's next revision."""
        sent = []

        class Msg:
            path, sender, schema, problems = "to/baseline/r1.md", "GA", "report/2", []
            text = ""

        class MB:
            def tip(self):
                return "t"

            def message(self, tip, path):
                return Msg()

            def send(self, to, text, sender=None):
                sent.append((to, text, sender))
                return f"to/{to}/x.md"

        d = self.tmp / "directives"
        d.mkdir()
        from ga.forms import dump_wire
        (d / "CMD-GA99.md").write_text(dump_wire({"schema": "directive/2", "id": "CMD-GA99", "rev": 1, "goal": "g",
                                                  "scope": [{"id": "S1", "text": "s"}],
                                                  "done_when": [{"id": "D1", "text": "d"}]}))
        conf = dict(self.conf, shadow=True, directives_dir=str(d), mailbox_repo=str(self.tmp))
        fx = O.Effects(conf, self.ga, mailbox=MB())
        r = fx.send_back({"id": "CMD-GA99", "mail": "to/baseline/r1.md"}, ["re-merge origin/x"])
        self.assertIn("rev 2 -> baseline-shadow", r)
        self.assertEqual(sent[0][0], "baseline-shadow")
        self.assertIn("re-merge origin/x", sent[0][1])
        self.assertFalse((self.ga / "hub" / "state.json").exists())  # shadow: the hub state is not touched
        with self.assertRaises(RuntimeError):
            fx.send_back({"id": "CMD-GA00", "mail": "m"}, ["a"])
        r = fx.retry("to/baseline/r1.md", "gemini-3.6-flash-low")
        rows = O.read_jsonl(self.ga / "hub" / "shadow.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model_resolved"], "gemini-3.6-flash-low")


if __name__ == "__main__":
    unittest.main()
