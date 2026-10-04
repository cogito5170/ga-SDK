"""CMD-GA18: METHOD rev 16 §3.6 (BD-173) — version 2 forms.

D1 the three forms (good and bad) · D2 changes applied (add · edit · drop; an unknown item is an error) · D3 the four
machine rules · D4 version 1 still runs, with a soft notice · D6 CMD-GA18 written as directive/2 checks.
"""
import contextlib
import io
import json
import unittest
from pathlib import Path

from ga.__main__ import main
from ga.forms import FormError, apply_changes, dump_text, hard, parse_text, validate
from ga.prompts import turn_prompt, worker_prompt

from world import World, directive, proposal

ROOT = Path(__file__).resolve().parents[1]


def d2(id_="CMD-A1", to="A", rev=1, **kw):
    d = {"schema": "directive/2", "id": id_, "rev": rev, "to": to, "goal": f"{id_} 의 목표", "why": "시험"}
    if rev == 1:
        d.update(scope=[{"id": "S1", "text": "x.txt 를 만든다"}, {"id": "S2", "text": "시험을 단다"}],
                 done_when=[{"id": "D1", "text": "x.txt 가 있다"}, {"id": "D2", "text": "시험이 초록"}])
    d.update(kw)
    return d


def r2(frm, did, rev, items, commits=(), status="done", **kw):
    head = {"schema": "report/2", "from": frm, "handled": [{"id": did, "rev_seen": rev, "status": status}],
            "items": [{"id": i, "state": s, "evidence": ["x.txt"]} for i, s in items]}
    if commits:
        head["commits"] = [{"repo": r, "branch": b, "sha": sha} for r, b, sha in commits]
    head.update(kw)
    return head


def hard_paths(doc):
    return sorted(p.path for p in hard(validate(doc)))


class FormsTest(unittest.TestCase):
    def test_directive2(self):
        self.assertEqual(hard_paths(d2()), [])
        self.assertEqual(hard_paths(d2(rev=2, changes=[{"item": "S3", "op": "add", "text": "더"}])), [])
        bad = [
            (dict(d2(), scope=None), "$.scope"),                                         # rev 1 needs items
            (dict(d2(), scope="통짜 글"), "$.scope"),                                     # not a list of items
            (dict(d2(), done_when=[{"id": "X1", "text": "t"}]), "$.done_when"),           # D<n>
            (dict(d2(), scope=[{"id": "S1", "text": "a"}, {"id": "S1", "text": "b"}]), "$.scope"),  # repeated id
            (d2(rev=2), "$.changes"),                                                     # rev > 1 without changes: hard
            (d2(rev=2, changes=[{"item": "S3", "op": "add"}]), "$.changes[0].text"),
            (d2(rev=2, changes=[{"item": "X3", "op": "drop"}]), "$.changes"),
            (dict(d2(), changes=[{"item": "S1", "op": "drop"}]), "$.changes"),            # rev 1 has nothing to change
        ]
        for doc, path in bad:
            with self.subTest(path=path, doc=doc.get("scope") or doc.get("changes")):
                self.assertIn(path, hard_paths(doc))

    def test_report2(self):
        good = r2("A", "CMD-A1", 1, [("D1", "met"), ("D2", "na")],
                  results=[{"name": "p50", "value": 1.5, "unit": "s", "ci": [1.2, 1.9], "evidence": "x.json"}],
                  blockers=[{"kind": "dependency", "what": "rlo 판", "gate": 6}], deviations=["a"], proposals=["b"])
        self.assertEqual(hard_paths(good), [])
        for doc, path in [
            ({k: v for k, v in good.items() if k != "items"}, "$.items"),
            (dict(good, items=[{"id": "D1", "state": "done"}]), "$.items"),
            (dict(good, items=[{"id": "D1", "state": "met"}, {"id": "D1", "state": "unmet"}]), "$.items"),
            (dict(good, results=[{"name": "n", "value": True}]), "$.results"),
            (dict(good, results=[{"name": "n", "value": 1, "ci": [2, 1]}]), "$.results"),
            (dict(good, blockers=[{"kind": "mood", "what": "x"}]), "$.blockers"),
        ]:
            with self.subTest(path=path):
                self.assertIn(path, hard_paths(doc))

    def test_notify1(self):
        ok = {"schema": "notify/1", "to": "GA", "kind": "directive", "ref": "https://github.com/o/r/issues/12", "id": "CMD-GA18"}
        self.assertEqual(hard_paths(ok), [])
        for doc, path in [(dict(ok, kind="hello"), "$.kind"), (dict(ok, ref="issue 12"), "$.ref"),
                          ({k: v for k, v in ok.items() if k != "to"}, "$.to")]:
            with self.subTest(path=path):
                self.assertIn(path, hard_paths(doc))


class ChangesTest(unittest.TestCase):
    def test_add_edit_drop(self):
        prev = d2()
        full = apply_changes(prev, d2(rev=2, changes=[{"item": "S3", "op": "add", "text": "새 범위"},
                                                      {"item": "D1", "op": "edit", "text": "x.txt 에 한 줄"},
                                                      {"item": "S2", "op": "drop"}]))
        self.assertEqual(full["scope"], [{"id": "S1", "text": "x.txt 를 만든다"}, {"id": "S3", "text": "새 범위"}])
        self.assertEqual(full["done_when"], [{"id": "D1", "text": "x.txt 에 한 줄"}, {"id": "D2", "text": "시험이 초록"}])
        self.assertNotIn("changes", full)
        self.assertEqual(prev["scope"][1]["id"], "S2")  # the previous revision is not touched

    def test_errors(self):
        for ch, why in [({"item": "D9", "op": "edit", "text": "t"}, "D9 is not in the previous revision"),
                        ({"item": "S9", "op": "drop"}, "S9 is not in the previous revision"),
                        ({"item": "S1", "op": "add", "text": "t"}, "S1 is already there")]:
            with self.subTest(ch=ch), self.assertRaises(FormError) as e:
                apply_changes(d2(), d2(rev=2, changes=[ch]))
            self.assertIn(why, str(e.exception))
        with self.assertRaises(FormError):
            apply_changes(None, d2(rev=2, changes=[{"item": "S3", "op": "add", "text": "t"}]))

    def test_hub_records_the_full_revision_and_the_turn_sees_it(self):
        w = World()
        self.addCleanup(w.close)
        w.hub.send(d2())
        w.paste("A")
        rev2 = d2(rev=2, supersedes={"id": "CMD-A1", "rev": 1}, changes=[{"item": "D3", "op": "add", "text": "README 한 줄"}])
        post, findings, _ = w.hub.send(rev2)
        st = w.hub.load_state()["directives"]["CMD-A1"]
        self.assertEqual([x["id"] for x in st["doc"]["done_when"]], ["D1", "D2", "D3"])
        self.assertEqual(st["sent"]["changes"], rev2["changes"])
        posted, _ = parse_text(post.text)
        self.assertNotIn("done_when", posted)  # only what changed travels
        prompt = w.paste("A")
        self.assertIn("## 지금 판 (앞 판에 changes 를 얹은 전체)", prompt)
        self.assertIn("  - D3: README 한 줄", prompt)
        tmpl, _ = parse_text(prompt[prompt.rindex("```ga"):])
        self.assertEqual((tmpl["schema"], [i["id"] for i in tmpl["items"]]), ("report/2", ["D1", "D2", "D3"]))
        self.assertFalse([p for p in findings if "deprecated" in p.message])

    def test_a_bad_change_is_not_sent(self):
        w = World()
        self.addCleanup(w.close)
        w.hub.send(d2())
        with self.assertRaises(FormError):
            w.hub.send(d2(rev=2, changes=[{"item": "D9", "op": "edit", "text": "t"}]))
        self.assertEqual(w.hub.load_state()["directives"]["CMD-A1"]["rev"], 1)


class MachineRulesTest(unittest.TestCase):
    def world(self, judge="success"):
        w = World(judge_fn=lambda ctx: proposal(judge, "wait", "판정"))
        self.addCleanup(w.close)
        w.hub.send(d2())
        w.paste("A")
        self.sha = w.work("A", "alpha", {"x.txt": "1\n"})
        return w

    def post(self, w, head):
        w.mail.post("A", "A", dump_text(head, "## Result\n했다\n"))
        return w.hub.tick()

    def commits(self):
        return [("alpha", "sess-a", self.sha)]

    def test_all_met_is_no_floor(self):
        w = self.world()
        res = self.post(w, r2("A", "CMD-A1", 1, [("D1", "met"), ("D2", "met")], self.commits()))
        self.assertEqual((res.integrated, res.verdict["class"]), ({"alpha": self.sha}, "success"))

    def test_done_but_not_met_is_partial(self):
        w = self.world()
        res = self.post(w, r2("A", "CMD-A1", 1, [("D1", "met"), ("D2", "unmet")], self.commits()))
        self.assertEqual((res.verdict["class"], res.verdict["cause"]), ("partial", "requirement"))
        self.assertIn("report/2 A: done, but not met: D2 unmet", res.verdict["evidence"]["notes"])

    def test_paused_with_unmet_items_is_not_floored_by_this_rule(self):
        w = self.world()
        res = self.post(w, r2("A", "CMD-A1", 1, [("D1", "met"), ("D2", "unmet")], self.commits(), status="paused"))
        self.assertEqual(res.verdict["class"], "success")

    def test_blocked_item_is_blocked(self):
        w = self.world()
        res = self.post(w, r2("A", "CMD-A1", 1, [("D1", "met"), ("D2", "blocked")], self.commits(),
                              blockers=[{"kind": "dependency", "what": "rlo 0.6 이 아직 없다"}]))
        self.assertEqual((res.verdict["class"], res.verdict["cause"]), ("blocked", "dependency"))
        self.assertEqual(res.gates, [])

    def test_permission_or_credential_blocker_asks_gate_6(self):
        for kind in ("permission", "credential"):
            with self.subTest(kind=kind):
                w = self.world()
                res = self.post(w, r2("A", "CMD-A1", 1, [("D1", "met"), ("D2", "blocked")], self.commits(),
                                      blockers=[{"kind": kind, "what": "토큰이 없다"}]))
                self.assertEqual([q["gate"] for q in res.gates], [6])
                self.assertIn(f"report blocker {kind}", res.gates[0]["about"])
        w = self.world()
        res = self.post(w, r2("A", "CMD-A1", 1, [("D1", "met"), ("D2", "blocked")], self.commits(),
                              blockers=[{"kind": "env", "what": "디스크"}]))
        self.assertEqual(res.gates, [])

    def test_items_missing_a_done_when_item_is_r7_hard(self):
        w = self.world()
        res = self.post(w, r2("A", "CMD-A1", 1, [("D1", "met")], self.commits()))
        self.assertEqual(res.integrated, {})
        notices = json.loads((w.ga / "records" / "rounds" / "round-0001.json").read_text())["notices"]
        self.assertTrue(any("R7 [hard] report/2 items miss CMD-A1:D2: refused" in n for n in notices))
        self.assertEqual((res.verdict["class"], res.verdict["cause"]), ("insufficient", "requirement"))  # rev 8: all refused


class VersionOneStillRunsTest(unittest.TestCase):
    def test_directive1_and_report1_with_soft_notices(self):
        w = World(judge_fn=lambda ctx: proposal("success", "wait", "판정"))
        self.addCleanup(w.close)
        post, findings, _ = w.hub.send(directive("CMD-A1", "A"))
        self.assertIsNotNone(post)
        self.assertTrue(any("directive/1 is deprecated: use directive/2" in p.message and p.strength == "soft" for p in findings))
        sha = w.work("A", "alpha", {"x.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        res = w.hub.tick()
        self.assertEqual((res.integrated, res.verdict["class"]), ({"alpha": sha}, "success"))
        notices = json.loads((w.ga / "records" / "rounds" / "round-0001.json").read_text())["notices"]
        self.assertTrue(any("report/1 is deprecated: use report/2" in n for n in notices))
        _, f2, _ = w.hub.send(directive("CMD-A1", "A", rev=2, supersedes={"id": "CMD-A1", "rev": 1}))
        self.assertTrue(any("rev > 1 without changes (directive/1)" in p.message for p in f2))

    def test_r4_and_drafts_write_directive2_for_a_directive2(self):
        w = World(judge_fn=lambda ctx: proposal("success", "wait", "판정"), shared_alpha=True)
        self.addCleanup(w.close)
        w.hub.send(d2("CMD-A1", "A"))
        w.hub.send(d2("CMD-B1", "B"))
        w.paste("A"), w.paste("B")
        a = w.work("A", "alpha", {"one.txt": "a\n"})
        b = w.work("B", "alpha", {"B_OWNS.txt": "b\n"})
        w.mail.post("A", "A", dump_text(r2("A", "CMD-A1", 1, [("D1", "met"), ("D2", "met")], [("alpha", "sess-a", a)]), ""))
        w.mail.post("B", "B", dump_text(r2("B", "CMD-B1", 1, [("D1", "met"), ("D2", "met")], [("alpha", "sess-b", b)]), ""))
        res = w.hub.tick()
        self.assertEqual(res.sent, ["CMD-B1"])
        d = w.hub.load_state()["directives"]["CMD-B1"]
        self.assertEqual((d["sent"]["schema"], [c["op"] for c in d["sent"]["changes"]]), ("directive/2", ["add", "drop", "drop", "add"]))
        self.assertEqual([x["id"] for x in d["doc"]["done_when"]], ["D3"])  # the merge-and-report item replaces the old ones
        self.assertEqual([x["id"] for x in d["doc"]["scope"]], ["S1", "S2", "S3"])


class CliAndPromptTest(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_d6_the_directive_as_directive2_checks(self):
        f = ROOT / "examples" / "v2" / "CMD-GA18.directive2.md"
        code, out, _ = self.run_cli("--config", "/nonexistent.json", "check", str(f))
        self.assertEqual(code, 0)
        # a pre-GA27 post: pretty head plus prose, so only the soft S1 wire notes (CMD-GA27)
        self.assertEqual(sorted(x.split("] ")[1].split(" ")[0] for x in out.splitlines()), ["wire:pretty", "wire:prose"])
        head, _ = parse_text(f.read_text(encoding="utf-8"))
        self.assertEqual(([x["id"] for x in head["scope"]], [x["id"] for x in head["done_when"]]),
                         ([f"S{i}" for i in range(1, 8)], [f"D{i}" for i in range(1, 7)]))

    def test_check_marks_version_1_deprecated(self):
        w = World()
        self.addCleanup(w.close)
        f = w.tmp / "d1.md"
        f.write_text(dump_text(directive("CMD-A1", "A")), encoding="utf-8")
        code, out, _ = self.run_cli("--config", "/nonexistent.json", "check", str(f))
        self.assertEqual(code, 0)
        self.assertIn("directive/1 is deprecated", out)

    def test_notify(self):
        code, out, _ = self.run_cli("notify", "--to", "baseline", "--kind", "report", "--ref", "https://github.com/o/r/issues/12#c1", "--id", "CMD-GA18")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"schema": "notify/1", "to": "baseline", "kind": "report",
                                           "ref": "https://github.com/o/r/issues/12#c1", "id": "CMD-GA18"})
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(self.run_cli("notify", "--to", "b", "--kind", "report", "--ref", "not a url")[0], 2)

    def test_post_takes_report2(self):
        w = World()
        self.addCleanup(w.close)
        f = w.tmp / "r.md"
        f.write_text(dump_text(r2("A", "CMD-A1", 1, [("D1", "met")]), "## Result\n했다\n"), encoding="utf-8")
        code, _, err = self.run_cli("--config", str(w.tmp / "ga.json"), "--ga-dir", str(w.ga), "post", "--channel", "A", "--from", "A", str(f))
        self.assertEqual(code, 0, err)
        self.assertNotIn("deprecated", err)

    def test_session_prompt_guides_the_forms(self):
        w = World()
        self.addCleanup(w.close)
        p = worker_prompt(w.cfg, "A")
        self.assertIn("### 꼴 (판 2, METHOD rev 16 §3.6)", p)
        self.assertIn("notify/1", p)
        t = turn_prompt(w.cfg, "A", "x", directive("CMD-A1", "A"))
        head, _ = parse_text(t[t.rindex("```ga"):])
        self.assertEqual((head["schema"], head["items"]), ("report/2", []))  # a directive/1 has no items to list


if __name__ == "__main__":
    unittest.main()
