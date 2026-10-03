"""CMD-GA12: METHOD rev 10 (BD-147).

- review/1: an outside verdict on an integrated result — only ever makes the reviewed round stricter, appended to the
  records (the round record is not rewritten); the next round's Judge context, floor and draft see it
- an empty draft (refine · verify without a directive) is filled by the hub from the verdict's grounds, checked as
  directive/1 before it is sent
- Bundle (b) evidence carries the build tool versions
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from ga.forms import FormError, validate
from ga.hub import Hub

from world import World, directive, proposal

ROOT = Path(__file__).resolve().parents[1]


def judged(choice, cls="success", cause=None, draft=None):
    calls = []

    def judge(ctx):
        calls.append(ctx)
        p = proposal(cls, choice, f"판정자: {choice}", cause=cause)
        p["directive"] = draft
        return p
    return judge, calls


class ReviewFormTest(unittest.TestCase):
    def doc(self, **kw):
        d = {"schema": "review/1", "id": "RV-1", "date": "2026-10-03", "by": "baseline", "repo": "rlo", "sha": "6c33b85",
             "class": "partial", "cause": "implementation", "why": "깨끗한 설치에 rlo.suggest_model 이 없다"}
        d.update(kw)
        return [p for p in validate(d) if p.strength == "hard"]

    def test_shape(self):
        self.assertEqual(self.doc(), [])
        self.assertEqual(self.doc(**{"class": "success", "cause": None}), [])
        for bad in ({"cause": None}, {"why": "  "}, {"id": "R-1"}, {"sha": "6c3"}, {"class": "fine"}, {"cause": "luck"},
                    {"amends": {"round": 0, "from": "success", "to": "partial"}}):
            with self.subTest(bad=bad):
                self.assertNotEqual(self.doc(**bad), [])


class ReviewInletTest(unittest.TestCase):
    def setUp(self):
        self.judge, self.calls = judged("wait")
        self.w = World(judge_fn=self.judge)
        self.addCleanup(self.w.close)
        w = self.w
        w.hub.send(directive("CMD-A1", "A"))
        w.paste("A")
        self.sha = w.work("A", "alpha", {"x.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", self.sha)])
        self.r1 = w.hub.tick()
        self.assertEqual((self.r1.round, self.r1.verdict["class"]), (1, "success"))
        self.round_file = w.ga / "records" / "rounds" / "round-0001.json"
        self.round_bytes = self.round_file.read_bytes()

    def test_stricter_review_amends_by_appending(self):
        w = self.w
        doc = w.hub.review("baseline", "alpha", self.sha[:7], "partial", "깨끗한 설치에서 모듈이 빠짐", cause="implementation")
        self.assertEqual((doc["id"], doc["sha"], doc["round"]), ("RV-1", self.sha, 1))
        self.assertEqual(doc["amends"], {"round": 1, "from": "success", "to": "partial"})
        self.assertEqual(self.round_file.read_bytes(), self.round_bytes)  # append-only: the round is not rewritten
        self.assertTrue((w.ga / "records" / "reviews" / "RV-0001.json").exists())
        rounds_md = (w.ga / "records" / "ROUNDS.md").read_text(encoding="utf-8")
        self.assertIn("바깥 판정 RV-1 (baseline", rounds_md)
        self.assertIn("성공 → 부분 성공", rounds_md)

    def test_not_stricter_review_changes_nothing(self):
        doc = self.w.hub.review("baseline", "alpha", self.sha, "success", "다시 봐도 좋다")
        self.assertEqual(doc["round"], 1)
        self.assertNotIn("amends", doc)

    def test_unknown_repo_or_sha_is_refused(self):
        with self.assertRaises(FormError):
            self.w.hub.review("baseline", "nope", self.sha, "partial", "x", cause="implementation")
        with self.assertRaises(FormError):
            self.w.hub.review("baseline", "alpha", "deadbee", "partial", "x", cause="implementation")
        with self.assertRaises(FormError):  # cause missing for a non-success class
            self.w.hub.review("baseline", "alpha", self.sha, "partial", "x")

    def test_next_round_sees_it_floors_on_it_and_drafts_from_it(self):
        w = self.w
        w.hub.review("baseline", "alpha", self.sha, "partial", "깨끗한 설치에 alphapkg.sub 이 없다", cause="implementation")
        w.judge_fn = judged("refine")[0]  # the Judge alone would say success: the review is the floor
        w.contexts.clear()
        res = w.hub.tick()  # nothing new from sessions: the open review alone makes a round
        self.assertFalse(res.quiet)
        ctx = w.contexts[-1]
        self.assertEqual([r["id"] for r in ctx.reviews], ["RV-1"])
        self.assertEqual((res.verdict["class"], res.verdict["cause"]), ("partial", "implementation"))
        self.assertTrue(any(n.startswith("judge proposed success") for n in res.verdict["evidence"]["notes"]))
        self.assertTrue(any(n.startswith("review RV-1 (baseline)") for n in res.verdict["evidence"]["notes"]))
        # the Judge said refine without a draft: the hub drafted CMD-A1 rev 2 from the review and sent it
        self.assertEqual(res.sent, ["CMD-A1"])
        st = w.hub.load_state()
        self.assertEqual((st["directives"]["CMD-A1"]["rev"], st["reviews"]["RV-1"]["status"]), (2, "used"))
        prompt = w.paste("A")
        self.assertIn("깨끗한 설치에 alphapkg.sub 이 없다", prompt)
        self.assertIn("허브가 판정 근거로 만든 초안", prompt)
        self.assertIn('"rev_seen": 2', prompt)
        rec = json.loads((w.ga / "records" / "rounds" / "round-0002.json").read_text(encoding="utf-8"))
        self.assertTrue(any(n.startswith("hub drafted CMD-A1 rev 2") for n in rec["notices"]))
        self.assertTrue(w.hub.tick().quiet)  # the review is used once

    def test_a_review_of_an_older_sha_is_a_note_not_a_floor(self):
        w = self.w
        w.hub.send(directive("CMD-A2", "A"))
        w.paste("A")
        newer = w.work("A", "alpha", {"y.txt": "2\n"})
        w.report("A", [("CMD-A2", 1, "done")], [("alpha", newer)])
        w.hub.tick()
        w.hub.review("baseline", "alpha", self.sha, "failure", "옛 판의 결함", cause="implementation")
        res = w.hub.tick()
        self.assertEqual(res.verdict["class"], "success")  # the head moved on since the reviewed sha
        self.assertTrue(any(n.startswith("review RV-1") for n in res.verdict["evidence"]["notes"]))


class EmptyDraftTest(unittest.TestCase):
    def world(self, choice, draft=None):
        judge, calls = judged(choice, cls="partial", cause="requirement", draft=draft)
        w = World(judge_fn=judge)
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        w.paste("A")
        return w

    def test_unclaimed_commit_gets_a_note_and_a_draft(self):
        # GA10 round 1: the session committed, its report claimed nothing, the Judge said verify without a draft
        w = self.world("verify")
        sha = w.work("A", "alpha", {"x.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")])
        res = w.hub.tick()
        self.assertEqual(res.integrated, {})
        self.assertTrue(any(n.startswith(f"R1b: A committed {sha[:7]}") for n in res.verdict["evidence"]["notes"]))
        self.assertEqual(res.sent, ["CMD-A1"])
        prompt = w.paste("A")
        self.assertIn("its report claims no commit", prompt)
        self.assertIn('"commits": [{"repo": "alpha"', prompt)

    def test_ga10_round1_shape_drafts_for_both_sessions(self):
        judge, _ = judged("verify", cls="partial", cause="measurement")
        w = World(judge_fn=judge)
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        w.hub.send(directive("CMD-B1", "B"))
        w.paste("A"), w.paste("B")
        a = w.work("A", "alpha", {"x.txt": "1\n"})
        b = w.work("B", "beta", {"y.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")])  # no commits claimed
        w.report("B", [])                       # handled [] and no commits (GA10 W2)
        res = w.hub.tick()
        self.assertEqual(res.integrated, {})
        self.assertEqual(sorted(res.sent), ["CMD-A1", "CMD-B1"])
        pa, pb = w.paste("A"), w.paste("B")
        self.assertIn(f"R1b: A committed {a[:7]}", pa)
        self.assertNotIn("R1b: B ", pa)
        self.assertIn(f"R1b: B committed {b[:7]}", pb)
        self.assertIn('"rev_seen": 2', pb)

    def test_no_draft_for_wait_or_when_the_judge_drafted(self):
        w = self.world("wait")
        w.report("A", [("CMD-A1", 1, "done")])
        self.assertEqual(w.hub.tick().sent, [])
        own = directive("CMD-A1", "A", rev=2, supersedes={"id": "CMD-A1", "rev": 1}, goal="판정자의 초안")
        w = self.world("refine", draft=own)
        w.report("A", [("CMD-A1", 1, "done")])
        res = w.hub.tick()
        self.assertEqual(res.sent, ["CMD-A1"])
        self.assertEqual(w.hub.load_state()["directives"]["CMD-A1"]["doc"]["goal"], "판정자의 초안")

    def test_nothing_to_point_at_means_no_draft(self):
        judge, _ = judged("refine", cls="partial", cause="requirement")
        w = World(judge_fn=judge)
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-B1", "B"))
        w.mail.post("A", "A", "```ga\n{\"schema\": \"report/1\", \"from\": \"A\", \"handled\": []}\n```\n## Result\n없음\n")
        res = w.hub.tick()
        self.assertEqual(res.sent, [])

    def test_a_draft_that_does_not_check_is_not_sent(self):
        w = self.world("refine")
        st = w.hub.load_state()
        st["directives"]["CMD-A1"]["doc"]["goal"] = 5  # a stored doc no longer checks as directive/1
        w.hub.save_state(st)
        w.report("A", [("CMD-A1", 1, "done")])
        self.assertEqual(w.hub.tick().sent, [])


class BuildToolsTest(unittest.TestCase):
    def test_install_evidence_has_the_build_tool_versions(self):
        judge, _ = judged("wait")
        w = World(judge_fn=judge, remote=True, modes=("path", "install"))
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        sha = w.work("A", "alpha", {"x.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        notes = w.hub.tick().verdict["evidence"]["notes"]
        line = next(n for n in notes if n.startswith("bundle (install): built with"))
        for tool in ("python ", "pip ", "setuptools "):
            self.assertIn(tool, line)


class ReviewCliTest(unittest.TestCase):
    def test_ga_review(self):
        w = World()
        self.addCleanup(w.close)
        w.hub.send(directive("CMD-A1", "A"))
        sha = w.work("A", "alpha", {"x.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        w.proposals.append(proposal("success", "wait", "좋다"))
        w.hub.tick()
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        p = subprocess.run([sys.executable, "-m", "ga", "--config", str(w.tmp / "ga.json"), "--ga-dir", str(w.ga), "review",
                            "--by", "baseline", "--repo", "alpha", "--sha", sha[:8], "--class", "failure",
                            "--cause", "implementation", "--why", "설치 결함"], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual((out["id"], out["round"], out["amends"]["to"]), ("RV-1", 1, "failure"))
        p = subprocess.run([sys.executable, "-m", "ga", "--config", str(w.tmp / "ga.json"), "--ga-dir", str(w.ga), "review",
                            "--by", "baseline", "--repo", "alpha", "--sha", sha, "--class", "failure", "--why", "원인 없음"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 2)
        self.assertIn("cause", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
