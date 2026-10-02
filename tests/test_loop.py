"""G6 · G9 · G5 · G7 and METHOD §9 verification 2, on a hermetic local world (no remote, no network).

G6/G9: file mailbox + manual runner + git worktrees: hub round trip with two worker sessions, to the end.
G5:    a second tick with nothing new writes nothing.
G7:    each of the seven gates stops the hub (no directive, no turn, a question).
§9-2:  the crossings of baseline rounds 51 (A4), 53 (A5) and K1 are caught via rev_seen / supersedes.
"""
import unittest

from ga.adapters.git import GitError, git
from ga.forms import dump_text, parse_text
from ga.records import RecordStore

from world import FakeHeadlessRunner, FakeRemoteRunner, World, directive, proposal


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.close()

    def test_full_loop_two_sessions_manual_runner(self):
        w = self.w
        start = {r: w.integ(r) for r in ("alpha", "beta")}
        # hub sends the first directives; the manual runner leaves prompts for the person
        for d in (directive("CMD-A1", "A"), directive("CMD-B1", "B")):
            post, findings, gates = w.hub.send(d)
            self.assertIsNotNone(post, findings)
        self.assertIn("세션 A 의 터미널", w.out.getvalue())
        # the person pastes; sessions work in their own worktrees and report
        self.assertIn("CMD-A1", w.paste("A"))
        self.assertIn("CMD-B1", w.paste("B"))
        a1 = w.work("A", "alpha", {"alphapkg/__init__.py": "V = 2\n"})
        b1 = w.work("B", "beta", {"betapkg/__init__.py": "V = 2\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", a1)], tests={"passed": 1, "failed": 0, "skipped": 0})
        w.report("B", [("CMD-B1", 1, "done")], [("beta", b1)])
        # round 1: integrate both, reproduce, judge proposes the next directive for A
        w.proposals.append(proposal("success", "continue", "A 다음 일", directive_=directive("CMD-A2", "A")))
        r1 = w.hub.tick()
        self.assertEqual(r1.integrated, {"alpha": a1, "beta": b1})
        self.assertEqual((w.integ("alpha"), w.integ("beta")), (a1, b1))
        self.assertNotEqual(start["alpha"], a1)
        self.assertEqual(r1.verdict["class"], "success")
        self.assertEqual(r1.verdict["evidence"]["tests"]["alpha"], {"passed": 1, "failed": 0, "skipped": 0})
        self.assertEqual(r1.sent, ["CMD-A2"])
        ctx = w.contexts[-1]
        self.assertEqual(sorted(r["head"]["from"] for r in ctx.reports), ["A", "B"])
        # round 2: A does CMD-A2, the judge chooses wait
        self.assertIn("CMD-A2", w.paste("A"))
        a2 = w.work("A", "alpha", {"notes.txt": "x\n"})
        w.report("A", [("CMD-A2", 1, "done")], [("alpha", a2)])
        r2 = w.hub.tick()
        self.assertEqual(r2.integrated, {"alpha": a2})
        self.assertEqual(r2.sent, [])
        st = w.hub.load_state()
        self.assertEqual({k: v["status"] for k, v in st["directives"].items()}, {"CMD-A1": "done", "CMD-B1": "done", "CMD-A2": "done"})
        rounds = RecordStore(w.ga / "records").all("round/1")
        self.assertEqual([(r["n"], r["verdict"], r["next"]) for r in rounds], [(1, "success", "continue"), (2, "success", "wait")])
        self.assertIn("- 2 회차 (2026-10-02): alpha", (w.ga / "records" / "ROUNDS.md").read_text(encoding="utf-8"))
        # G5: nothing new -> writes 0, posts 0, and not one byte changes
        before = w.snapshot()
        r3 = w.hub.tick()
        self.assertTrue(r3.quiet)
        self.assertEqual((r3.writes, r3.posts, r3.turns), (0, 0, 0))
        r4 = w.hub.tick()
        self.assertEqual((r4.quiet, r4.writes), (True, 0))
        self.assertEqual(before, w.snapshot())
        self.assertEqual(st["spent"]["runs"], 3)
        self.assertEqual(st["spent"]["cost_unknown_runs"], 3)  # manual runner cannot know the cost

    def test_pre_push_hook_blocks_other_branches(self):
        w = World(remote=True)
        try:
            w.hub.send(directive("CMD-A1", "A"))
            wt = w.vcs.session_worktree("A", "alpha")
            w.work("A", "alpha", {"x.txt": "1\n"})  # own branch: allowed
            with self.assertRaises(GitError) as e:
                git(wt, "push", "origin", "HEAD:refs/heads/integ")
            self.assertIn("ga R3", str(e.exception))
            with self.assertRaises(GitError):
                git(wt, "push", "origin", "HEAD:refs/heads/sess-b")
            git(wt, "reset", "--quiet", "--hard", "HEAD~1")
            git(wt, "commit", "--quiet", "--allow-empty", "-m", "rewrite")
            with self.assertRaises(GitError) as e:
                git(wt, "push", "--force", "origin", "sess-a")
            self.assertIn("non-fast-forward", str(e.exception))
            with self.assertRaises(GitError):
                git(wt, "push", "origin", ":sess-a")
            # the hook is per worktree: the person's own checkout is not restricted
            git(w.repos["alpha"], "push", "--quiet", "origin", "main")
        finally:
            w.close()

    def test_pre_receive_holds_even_with_no_verify(self):
        w = World(remote=True)
        try:
            self.assertEqual(len(w.hooks), 2)
            w.hub.send(directive("CMD-A1", "A"))
            wt = w.vcs.session_worktree("A", "alpha")
            w.work("A", "alpha", {"x.txt": "1\n"})
            for argv, why in (
                (["push", "--no-verify", "origin", "HEAD:refs/heads/sess-b"], "may push only to refs/heads/sess-a"),
                (["push", "--no-verify", "origin", "HEAD:refs/heads/integ"], "may push only to refs/heads/sess-a"),
                (["push", "--no-verify", "origin", "HEAD:refs/heads/new-branch"], "may push only to refs/heads/sess-a"),
                (["push", "--no-verify", "origin", ":sess-a"], "deleting"),
            ):
                with self.subTest(argv=argv):
                    with self.assertRaises(GitError) as e:
                        git(wt, *argv)
                    self.assertIn(why, str(e.exception))
            git(wt, "reset", "--quiet", "--hard", "HEAD~1")
            git(wt, "commit", "--quiet", "--allow-empty", "-m", "rewrite")
            with self.assertRaises(GitError) as e:
                git(wt, "push", "--no-verify", "--force", "origin", "sess-a")
            self.assertIn("non-fast-forward", str(e.exception))
            log = (w.tmp / "remotes" / "alpha.git" / "ga-refused.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(log), 5)
            self.assertIn("who=A ref=refs/heads/sess-b", log[0])
            # an unnamed pusher (the person's own checkout) may not touch managed refs, but may push others
            main = w.repos["alpha"]
            git(main, "commit", "--quiet", "--allow-empty", "-m", "person's own commit")  # so the push is not a no-op
            with self.assertRaises(GitError) as e:
                git(main, "push", "--no-verify", "origin", "main:refs/heads/integ")
            self.assertIn("managed by ga", str(e.exception))
            git(main, "push", "--quiet", "--no-verify", "origin", "main:refs/heads/scratch")
            # the hub's own fast-forward goes through (it names itself)
            git(wt, "reset", "--quiet", "--hard", "origin/sess-a")
            sha = git(wt, "rev-parse", "HEAD")
            w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
            self.assertEqual(w.hub.tick().integrated, {"alpha": sha})
            self.assertEqual(git(w.tmp / "remotes" / "alpha.git", "rev-parse", "integ"), sha)
        finally:
            w.close()

    def test_runner_interface_takes_headless_and_remote_fakes(self):
        # headless: knows the end of the turn and the cost; the session works inside run_turn
        holder = {}

        def act(req):
            w = holder["w"]
            sha = w.work(req.session, "alpha", {"h.txt": "h\n"})
            w.report(req.session, [("CMD-A1", 1, "done")], [("alpha", sha)])

        w = World(runner=FakeHeadlessRunner(act))
        holder["w"] = w
        try:
            w.hub.send(directive("CMD-A1", "A"))
            res = w.hub.tick()
            self.assertEqual(list(res.integrated), ["alpha"])
            st = w.hub.load_state()
            self.assertEqual(st["spent"]["cost"], 0.25)
            self.assertEqual(st["resume"]["A"], "sess-A")
            self.assertTrue(w.runner.calls[0].workdir.exists())
        finally:
            w.close()
        # remote: fire-and-forget; same hub code
        remote = FakeRemoteRunner()
        w = World(runner=remote)
        try:
            w.hub.send(directive("CMD-B1", "B"))
            self.assertEqual(remote.sent[0].session, "B")
            self.assertIn("CMD-B1", remote.sent[0].prompt)
            self.assertTrue(w.hub.tick().quiet)  # no report yet: the safety-net tick stays silent
        finally:
            w.close()


class ExchangeR1bTest(unittest.TestCase):
    """METHOD rev 4 (BD-133): an exchange is not an action; only changes claimed under a directive are integrated."""

    def setUp(self):
        self.w = World()
        self.w.hub.send(directive("CMD-A1", "A"))
        self.w.hub.send(directive("CMD-B1", "B"))
        self.start = self.w.integ("alpha")

    def tearDown(self):
        self.w.close()

    def test_unreported_commit_is_not_new_and_not_integrated(self):
        w = self.w
        w.work("A", "alpha", {"x.txt": "1\n"})
        res = w.hub.tick()
        self.assertTrue(res.quiet)
        self.assertEqual(w.integ("alpha"), self.start)

    def test_commit_under_no_directive_is_blocked(self):
        w = self.w
        sha = w.work("A", "alpha", {"from_exchange.txt": "B told me\n"})
        w.report("A", [], [("alpha", sha)], body="## Result\nB 와 교신하고 바꿨다\n")
        res = w.hub.tick()
        self.assertEqual(res.integrated, {})
        self.assertEqual(w.integ("alpha"), self.start)
        self.assertEqual((res.verdict["class"], res.verdict["cause"]), ("blocked", "requirement"))
        self.assertIn("R1b", [p.rule for p in res.findings if p.strength == "hard"])

    def test_directive_of_another_session_does_not_cover(self):
        w = self.w
        sha = w.work("A", "alpha", {"y.txt": "1\n"})
        w.report("A", [("CMD-B1", 1, "done")], [("alpha", sha)])
        res = w.hub.tick()
        self.assertEqual(res.integrated, {})
        self.assertIn("R1b", [p.rule for p in res.findings])

    def test_only_the_claimed_commit_is_integrated(self):
        w = self.w
        claimed = w.work("A", "alpha", {"a.txt": "1\n"})
        w.work("A", "alpha", {"b.txt": "2\n"}, msg="after the report")
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", claimed[:7])])
        res = w.hub.tick()
        self.assertEqual(res.integrated, {"alpha": claimed})
        self.assertEqual(w.integ("alpha"), claimed)
        rounds = RecordStore(w.ga / "records").all("round/1")
        self.assertTrue(any("1 commit(s) after the claimed" in n for n in rounds[-1]["notices"]))

    def test_exchange_is_information_not_action(self):
        w = self.w
        ex = {"schema": "exchange/1", "from": "A", "to": "B", "why": "급했다", "asked": "beta 의 V 값", "got": "2",
              "proposal": "alpha 도 2 로 맞춘다"}
        w.mail.post("A", "A", dump_text(ex))
        res = w.hub.tick()
        self.assertFalse(res.quiet)  # the hub judges it (§3.5)
        self.assertEqual(res.integrated, {})
        self.assertEqual(w.contexts[-1].exchanges, [ex])
        rounds = RecordStore(w.ga / "records").all("round/1")
        self.assertTrue(any(n.startswith("exchange A→B (not an action") for n in rounds[-1]["notices"]))

    def test_exchange_written_into_another_channel_is_flagged(self):
        w = self.w
        w.mail.post("B", "A", "B 야, 너 V 몇이야?")
        res = w.hub.tick()
        self.assertIn("R1", [p.rule for p in res.findings])


class GateStopTest(unittest.TestCase):
    """G7: every gate stops — the proposed directive is not sent and no turn is run."""

    def run_case(self, setup, expect_gate):
        w = World()
        try:
            w.hub.send(directive("CMD-A1", "A"))
            w.paste("A")
            proposed = setup(w)
            w.proposals.append(proposed or proposal("success", "continue", "다음", directive_=directive("CMD-A2", "A")))
            res = w.hub.tick()
            self.assertIn(expect_gate, [q["gate"] for q in res.gates], res.findings)
            self.assertEqual(res.sent, [])
            self.assertEqual(w.runner.pending("A"), [])  # no new prompt for the person
            qfiles = sorted((w.ga / "questions").glob("*.md"))
            self.assertTrue(qfiles)
            head, _ = parse_text(qfiles[0].read_text(encoding="utf-8"))
            self.assertEqual(head["schema"], "question/1")
            last = RecordStore(w.ga / "records").all("round/1")[-1]
            self.assertEqual(last["next"], "ask_user")
            return w, res
        except Exception:
            w.close()
            raise

    def test_gate1_stage_close(self):
        def setup(w):
            sha = w.work("A", "alpha", {"a.txt": "1\n"})
            w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
            return proposal("success", "continue", "단계를 닫자", directive_=directive("CMD-A2", "A"), action="stage_close")
        w, res = self.run_case(setup, 1)
        # the user approves; the next tick may go ahead when the judge cites the decision
        qid = next(iter(w.hub.load_state()["questions"]))
        dec = w.hub.answer(qid, "진행")
        self.assertEqual(dec["by"], "user")
        w.proposals.append(proposal("success", "continue", "사용자 결정에 따라", directive_=directive("CMD-A2", "A"),
                                    action="stage_close", approved_by=[dec["id"]]))
        res2 = w.hub.tick()
        self.assertEqual(res2.sent, ["CMD-A2"])
        self.assertEqual(res2.gates, [])
        w.close()

    def test_gate1_refused_answer_keeps_stopping(self):
        def setup(w):
            w.report("A", [("CMD-A1", 1, "done")])
            return proposal("success", "continue", "태그", action="tag")
        w, _ = self.run_case(setup, 1)
        qid = next(iter(w.hub.load_state()["questions"]))
        dec = w.hub.answer(qid, "보류")
        w.proposals.append(proposal("success", "continue", "태그", action="tag", approved_by=[dec["id"]]))
        self.assertEqual([q["gate"] for q in w.hub.tick().gates], [1])
        w.close()

    def test_gate2_architecture(self):
        def setup(w):
            w.report("A", [("CMD-A1", 1, "done")], change_size="architecture")
        self.run_case(setup, 2)[0].close()

    def test_gate3_user_decision_clash(self):
        def setup(w):
            w.hub.records.put({"schema": "decision/1", "id": "BD-7", "date": "2026-10-01", "decision": "X 는 하지 않는다",
                               "basis": "사용자 결정", "by": "user", "supersedes": []})
            w.report("A", [("CMD-A1", 1, "paused")], body="## Deviation\nBD-7 과 부딪힌다. 멈추고 묻는다\n")
        self.run_case(setup, 3)[0].close()

    def test_gate4_unowned_file(self):
        def setup(w):
            sha = w.work("A", "alpha", {"docs/new.md": "x\n"})
            w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        w, res = self.run_case(setup, 4)
        self.assertEqual(res.integrated, {})
        self.assertEqual(res.verdict["class"], "blocked")
        w.close()

    def test_gate5_open_question(self):
        def setup(w):
            w.report("A", [("CMD-A1", 1, "paused")], body="## Request\nOQ-19 의 값을 정해 달라\n")
        self.run_case(setup, 5)[0].close()

    def test_gate6_secret(self):
        def setup(w):
            secret = "".join(["gh", "p_", "Q" * 36])
            sha = w.work("A", "alpha", {"leak.txt": f"token={secret}\n"})
            w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        w, res = self.run_case(setup, 6)
        self.assertEqual(res.integrated, {})
        self.assertNotEqual(w.integ("alpha"), w.vcs.session_head("alpha", "A"))
        w.close()

    def test_gate6_budget(self):
        w = World(budget={"runs": 1})
        try:
            w.hub.send(directive("CMD-A1", "A"))  # spends the one run
            w.paste("A")
            w.report("A", [("CMD-A1", 1, "done")])
            w.proposals.append(proposal("success", "continue", "다음", directive_=directive("CMD-A2", "A")))
            res = w.hub.tick()
            self.assertEqual([q["gate"] for q in res.gates], [6])
            self.assertEqual(res.sent, [])
        finally:
            w.close()

    def test_gate7_new_repo(self):
        def setup(w):
            w.report("A", [("CMD-A1", 1, "paused")], needs=["new_repo"])
        self.run_case(setup, 7)[0].close()


class CrossingTest(unittest.TestCase):
    """§9 verification 2: the crossings baseline met (A4 round 51, A5 round 53, K1) are caught."""

    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.close()

    def test_a4_addendum_not_read(self):
        w = self.w
        w.hub.send(directive("CMD-A4", "A"))
        w.hub.send(directive("CMD-A4", "A", rev=2, supersedes={"id": "CMD-A4", "rev": 1}, scope="덧붙임: 범위가 넓어짐"))
        sha = w.work("A", "alpha", {"a4.txt": "1\n"})
        w.report("A", [("CMD-A4", [1, 1], "done")], [("alpha", sha)])
        res = w.hub.tick()
        self.assertEqual((res.verdict["class"], res.verdict["subclass"], res.verdict["cause"]), ("partial", "crossed", "hub_directive"))
        self.assertEqual(w.hub.load_state()["directives"]["CMD-A4"]["status"], "open")

    def test_a5_ran_after_being_superseded(self):
        w = self.w
        w.hub.send(directive("CMD-A5", "A"))
        w.hub.send(directive("CMD-A6", "A", supersedes={"id": "CMD-A5", "rev": 1}))
        w.report("A", [("CMD-A5", 1, "done")])
        res = w.hub.tick()
        self.assertEqual(res.verdict["subclass"], "crossed")
        self.assertTrue(any("superseded by CMD-A6" in n for n in res.verdict["evidence"]["notes"]))

    def test_k1_source_sha_changed_in_rev2(self):
        w = self.w
        w.hub.send(directive("CMD-K1", "A", scope="옮길 원본 sha 1111111"))
        w.hub.send(directive("CMD-K1", "A", rev=2, supersedes={"id": "CMD-K1", "rev": 1}, scope="옮길 원본 sha 2222222"))
        w.report("A", [("CMD-K1", 1, "done")])
        self.assertEqual(w.hub.tick().verdict["subclass"], "crossed")

    def test_read_addendum_before_finishing_is_not_crossed(self):
        w = self.w
        w.hub.send(directive("CMD-A7", "A"))
        w.hub.send(directive("CMD-A7", "A", rev=2, supersedes={"id": "CMD-A7", "rev": 1}))
        w.report("A", [("CMD-A7", [1, 2], "done")])
        res = w.hub.tick()
        self.assertEqual(res.verdict["class"], "success")
        self.assertEqual(w.hub.load_state()["directives"]["CMD-A7"]["status"], "done")

    def test_resending_an_old_rev_is_refused(self):
        w = self.w
        w.hub.send(directive("CMD-A8", "A", rev=2))
        with self.assertRaises(Exception):
            w.hub.send(directive("CMD-A8", "A", rev=1))


if __name__ == "__main__":
    unittest.main()
