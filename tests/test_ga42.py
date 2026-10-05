"""CMD-GA42 S1 / D1: ``ga hub`` — inbox -> ga judge -> fixed-size verdict card -> one small-model decision -> code acts.

Offline: a temp world (git repos with bare remotes, a baseline-shaped folder), a fake mailbox that runs ``ga check`` on
every form it is given, fake judge results and a scripted fake backend. 0 model runs.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ga.backends.base import BackendTurn
from ga.forms import apply_changes, dump_wire, hard, parse_text, validate
from ga.hub import CARD_MAX, MailHub, parse_decision, verdict_card
from ga.judge import Judgement
from ga.mailbox import Message

REPO = "o/proj"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


class FakeMailbox:
    """Mail in memory; ``send`` refuses a form with a hard ``ga check`` problem, like the real one."""

    def __init__(self):
        self.box, self.read, self.sent, self.n = {}, set(), [], 0

    def put(self, to, text, sender):
        self.n += 1
        head, _ = parse_text(text)
        path = f"to/{to}/2026100{self.n:04d}-{sender}-{head.get('schema', '?').replace('/', '')}.md"
        probs = [str(p) for p in hard(validate(head))]
        self.box.setdefault(to, []).append(Message(path, to, sender, "", "", text, head.get("schema"), probs))
        return path

    def send(self, to, text, sender=None):
        head, _ = parse_text(text)
        probs = hard(validate(head))
        if probs:
            raise AssertionError(f"ga check refused a hub form: {probs}")
        self.sent.append((to, head, text))
        return self.put(to, text, sender)

    def unread(self, name):
        return [m for m in self.box.get(name, []) if m.path not in self.read]

    def mark_read(self, name, path):
        self.read.add(path)


class Fake:
    def __init__(self, answers):
        self.answers, self.calls, self.bare = list(answers), [], True

    def run_turn(self, prompt, session_id=None, *, system=None, **k):
        self.calls.append({"prompt": prompt, "system": system})
        a = self.answers[min(len(self.calls), len(self.answers)) - 1]
        return BackendTurn(a, ["fake-small"], {"input_tokens": len(prompt) // 4, "output_tokens": 5}, "anthropic", None, 0.01, 1)


DIRECTIVE = {"schema": "directive/2", "id": "CMD-T1", "rev": 1, "to": "GA", "goal": "make calc add",
             "why": "user asked (BD-433, BD-416)", "scope": [{"id": "S1", "text": "fix add"}],
             "done_when": [{"id": "D1", "text": "tests green"}]}

DLOG = ("# DECISION_LOG\n\n| id | text | related |\n|---|---|---|\n| BD-433 | newest | BD-416 |\n"
        "| BD-432 | older | - |\n| BD-60 | the old era starts | - |\n| BD-59 | old | - |\n")
BASE = "# BASELINE\n\n## 13\n- 40 회차: a (BD-430).\n- 41 회차: b (BD-433).\n\n## 14\nend\n"


class World:
    def __init__(self, case):
        self.tmp = Path(tempfile.mkdtemp(prefix="ga42-"))
        case.addCleanup(shutil.rmtree, self.tmp, True)
        t = self.tmp
        # the integration repo: bare remote + a clone on main; the session's commit on branch s
        git(t, "init", "-q", "--bare", "-b", "main", "proj.git")
        git(t, "clone", "-q", str(t / "proj.git"), "proj")
        self.proj = t / "proj"
        (self.proj / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        git(self.proj, "add", "-A"); git(self.proj, "commit", "-qm", "base"); git(self.proj, "push", "-q", "origin", "main")
        git(self.proj, "checkout", "-qb", "s")
        (self.proj / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (self.proj / "other.py").write_text("x = 1\n")
        git(self.proj, "add", "-A"); git(self.proj, "commit", "-qm", "fix")
        self.sha = git(self.proj, "rev-parse", "HEAD")
        git(self.proj, "checkout", "-q", "main")
        self.main0 = git(self.proj, "rev-parse", "main")
        # the baseline repo
        git(t, "init", "-q", "--bare", "-b", "main", "base.git")
        git(t, "clone", "-q", str(t / "base.git"), "baseline")
        self.base = t / "baseline"
        (self.base / "DECISION_LOG.md").write_text(DLOG)
        (self.base / "BASELINE.md").write_text(BASE)
        (self.base / "ops" / "tokmon").mkdir(parents=True)
        (self.base / "ops" / "tokmon" / "sessions.txt").write_text("session_01old AGY CMD-AG1\n")
        (self.base / "directives").mkdir()
        (self.base / "directives" / "CMD-T1.md").write_text(dump_wire(DIRECTIVE))
        git(self.base, "add", "-A"); git(self.base, "commit", "-qm", "records"); git(self.base, "push", "-q", "origin", "main")
        self.mail = FakeMailbox()
        self.conf = {"name": "baseline", "human": "human", "baseline_repo": str(self.base),
                     "directives_dir": str(self.base / "directives"), "backend": "fake", "model": "fake-small",
                     "repos": {REPO: {"path": str(self.proj), "base": "main"}},
                     "owned": {"CMD-T1": ["calc.py", "tests/*"]}, "sessions": {"GA": "session_01ga42"}}
        self.judged = []

    def judgement(self, cls="success", **kw):
        def fn(report, repo, base, **k):
            self.judged.append(report)
            j = Judgement(cls=cls, sha=self.sha, repo=REPO, ff=True, base=base, directive="CMD-T1",
                          tests={REPO: {"passed": 3, "failed": 0 if cls == "success" else 1, "skipped": 0}}, **kw)
            if cls != "success":
                j.cause = "implementation"
                j.failing = ["test_calc.T.test_add"]
            return j
        return fn

    def hub(self, answers, cls="success", **kw):
        self.runner = Fake(answers)
        return MailHub(self.conf, ga_dir=self.tmp / ".ga", mailbox=self.mail, runner=self.runner,
                       judge_fn=self.judgement(cls, **kw), today=lambda: "2026-10-05")

    def report(self, items=None, did="CMD-T1"):
        head = {"schema": "report/2", "from": "GA", "handled": [{"id": did, "rev_seen": 1, "status": "done"}],
                "commits": [{"repo": REPO, "branch": "s", "sha": self.sha}],
                "tests": {"passed": 3, "failed": 0, "skipped": 0}, "change_size": "interface",
                "items": items or [{"id": "D1", "state": "met", "evidence": ["unittest 3 ok"]}]}
        return self.mail.put("baseline", dump_wire(head), "GA")

    def remote_main(self, repo="proj.git"):
        return git(self.tmp / repo, "rev-parse", "main")

    def snapshot(self):
        out = {}
        for p in sorted(self.tmp.rglob("*")):
            if p.is_file():
                out[str(p)] = (p.stat().st_mtime_ns, p.read_bytes())
        return out


def to(w, who):
    return [(h, t) for (r, h, t) in w.mail.sent if r == who]


class Accept(unittest.TestCase):
    def test_accept_on_success_integrates_by_ff_and_writes_one_row_one_round_line_and_sessions(self):
        w = World(self)
        w.report()
        res = w.hub(["ACCEPT"]).tick()
        self.assertEqual(res.sent, ["ACCEPT CMD-T1"])
        self.assertEqual(w.remote_main(), w.sha)  # fast-forwarded and pushed
        self.assertEqual(git(w.proj, "rev-list", "--count", f"{w.main0}..{w.sha}"), "1")
        dl = (w.base / "DECISION_LOG.md").read_text()
        rows = [ln for ln in dl.splitlines() if ln.startswith("| BD-434 |")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(dl.splitlines()), len(DLOG.splitlines()) + 1)
        lines = dl.splitlines()
        self.assertTrue(lines[lines.index(rows[0]) + 1].startswith("| BD-60 |"))  # inserted before BD-60
        self.assertTrue(rows[0].endswith("| BD-416, BD-433 |"), rows[0])
        self.assertIn(f"{REPO}@{w.sha[:7]}", rows[0])
        bl = (w.base / "BASELINE.md").read_text().splitlines()
        new = [ln for ln in bl if ln.startswith("- 42 회차:")]
        self.assertEqual(len(new), 1)
        self.assertEqual(bl.index(new[0]), bl.index("- 41 회차: b (BD-433).") + 1)
        self.assertTrue(new[0].endswith("(BD-434)."))
        self.assertEqual(len(bl), len(BASE.splitlines()) + 1)
        self.assertEqual((w.base / "ops/tokmon/sessions.txt").read_text(),
                         "session_01old AGY CMD-AG1\nsession_01ga42 GA CMD-T1\n")
        # committed and pushed in the baseline repo
        self.assertEqual(w.remote_main("base.git"), git(w.base, "rev-parse", "HEAD"))
        self.assertEqual(git(w.base, "status", "--porcelain"), "")
        # one model turn, the verdict mailed to the session, nothing to the person
        self.assertEqual(len(w.runner.calls), 1)
        (v, _), = to(w, "GA")
        self.assertEqual((v["schema"], v["class"], v["next"]["choice"]), ("verdict/1", "success", "continue"))
        self.assertEqual(to(w, "human"), [])

    def test_accept_when_judge_failed_does_not_integrate_and_asks_the_human(self):
        w = World(self)
        w.report()
        res = w.hub(["ACCEPT"], cls="failure").tick()
        self.assertEqual(res.sent, ["ASK_HUMAN CMD-T1"])
        self.assertEqual(w.remote_main(), w.main0)
        self.assertEqual(git(w.proj, "rev-parse", "main"), w.main0)
        self.assertEqual((w.base / "DECISION_LOG.md").read_text(), DLOG)
        self.assertEqual((w.base / "BASELINE.md").read_text(), BASE)
        (q, _), = to(w, "human")
        self.assertEqual(q["next"]["choice"], "ask_user")
        self.assertIn("judge class is failure", q["next"]["reason"])

    def test_accept_with_items_left_for_judgement_is_not_integrated(self):
        w = World(self)
        w.report()
        w.hub(["ACCEPT"], needs=["D1 claim not measured"]).tick()
        self.assertEqual(w.remote_main(), w.main0)
        self.assertEqual(len(to(w, "human")), 1)

    def test_push_is_never_forced_a_moved_remote_is_left_alone(self):
        w = World(self)
        other = w.tmp / "other"
        git(w.tmp, "clone", "-q", str(w.tmp / "proj.git"), "other")
        (other / "z.txt").write_text("someone else\n")
        git(other, "add", "-A"); git(other, "commit", "-qm", "other"); git(other, "push", "-q", "origin", "main")
        moved = w.remote_main()
        w.report()
        res = w.hub(["ACCEPT"]).tick()
        self.assertEqual(w.remote_main(), moved)  # not overwritten
        self.assertEqual(res.sent, ["ASK_HUMAN CMD-T1"])
        self.assertIn("integration refused", to(w, "human")[0][0]["next"]["reason"])
        self.assertEqual((w.base / "DECISION_LOG.md").read_text(), DLOG)

    def test_baseline_push_is_never_forced(self):
        w = World(self)
        other = w.tmp / "bother"
        git(w.tmp, "clone", "-q", str(w.tmp / "base.git"), "bother")
        (other / "x.md").write_text("other hub\n")
        git(other, "add", "-A"); git(other, "commit", "-qm", "other"); git(other, "push", "-q", "origin", "main")
        moved = w.remote_main("base.git")
        w.report()
        hub = w.hub(["ACCEPT"])
        hub.tick()
        self.assertEqual(w.remote_main("base.git"), moved)
        st = json.loads((w.tmp / ".ga/hub/state.json").read_text())
        self.assertFalse(st["verdicts"]["CMD-T1"][-1]["baseline_pushed"])


class SendBack(unittest.TestCase):
    def test_send_back_mails_verdict_and_a_directive_rev_plus_one_that_passes_ga_check(self):
        w = World(self)
        w.report()
        res = w.hub(["SEND_BACK\n- add a test for negative numbers\n- keep other.py out of the change"], cls="partial").tick()
        self.assertEqual(res.sent, ["SEND_BACK CMD-T1 rev 2"])
        (v, _), (d, text) = to(w, "GA")
        self.assertEqual((v["schema"], v["next"]["choice"]), ("verdict/1", "refine"))
        self.assertIn("ask: add a test for negative numbers", v["claims_vs_evidence"])
        self.assertEqual((d["schema"], d["id"], d["rev"]), ("directive/2", "CMD-T1", 2))
        self.assertEqual(hard(validate(d)), [])
        self.assertTrue(d["changes"])
        full = apply_changes(DIRECTIVE, d)
        self.assertIn("negative numbers", json.dumps(full["scope"]))
        self.assertEqual(w.remote_main(), w.main0)
        self.assertEqual(to(w, "human"), [])

    def test_the_fourth_send_back_on_one_id_becomes_ask_human(self):
        w = World(self)
        hub = w.hub(["SEND_BACK\n- more"], cls="partial")
        revs = []
        for _ in range(4):
            w.report()
            revs += hub.tick().sent
        self.assertEqual(revs, ["SEND_BACK CMD-T1 rev 2", "SEND_BACK CMD-T1 rev 3", "SEND_BACK CMD-T1 rev 4",
                                "ASK_HUMAN CMD-T1"])
        directives = [h for h, _ in to(w, "GA") if h["schema"] == "directive/2"]
        self.assertEqual([d["rev"] for d in directives], [2, 3, 4])
        prev = DIRECTIVE
        for d in directives:
            self.assertEqual(hard(validate(d)), [])
            prev = apply_changes(prev, d)  # each revision applies to the one before
        (q, _), = to(w, "human")
        self.assertIn("send-back cap 3", q["next"]["reason"])

    def test_unparseable_answer_is_ask_human(self):
        w = World(self)
        w.report()
        self.assertEqual(w.hub(["I think it looks fine overall."]).tick().sent, ["ASK_HUMAN CMD-T1"])
        self.assertEqual(w.remote_main(), w.main0)


class Quiet(unittest.TestCase):
    def test_a_tick_with_nothing_new_changes_no_file_and_makes_no_model_call(self):
        w = World(self)
        hub = w.hub(["ACCEPT"])
        before = w.snapshot()
        res = hub.tick()
        self.assertTrue(res.quiet)
        self.assertEqual(w.snapshot(), before)
        self.assertEqual(w.runner.calls, [])
        w.report()
        hub.tick()
        self.assertEqual(len(w.runner.calls), 1)
        before = w.snapshot()
        res = hub.tick()
        self.assertTrue(res.quiet)
        self.assertEqual(res.writes, 0)
        self.assertEqual(w.snapshot(), before)
        self.assertEqual(len(w.runner.calls), 1)
        self.assertEqual(len(w.judged), 1)

    def test_daily_turn_cap_stops_model_calls(self):
        w = World(self)
        w.conf["daily_turns"] = 1
        hub = w.hub(["SEND_BACK\n- x"], cls="partial")
        w.report(); w.report()
        res = hub.tick()
        self.assertEqual(len(w.runner.calls), 1)
        self.assertTrue(any("daily turn cap" in p for p in res.plan))
        self.assertEqual(len(w.mail.unread("baseline")), 1)  # waits for tomorrow, unread

    def test_every_turn_goes_to_the_ledger_and_l0(self):
        w = World(self)
        w.report()
        w.hub(["ACCEPT"]).tick()
        (row,) = [json.loads(x) for x in (w.tmp / ".ga/hub/ledger/2026-10-05.jsonl").read_text().splitlines()]
        self.assertEqual((row["id"], row["decision"], row["kind"]), ("CMD-T1", "ACCEPT", "hub"))
        self.assertLessEqual(row["card_bytes"], CARD_MAX)
        (ev,) = [json.loads(x) for x in (w.tmp / ".ga/telemetry/hub.jsonl").read_text().splitlines()]
        self.assertEqual(ev["type"] if "type" in ev else ev.get("event"), "run.end")


class Card(unittest.TestCase):
    def test_the_card_stays_within_6_kb_on_a_huge_report(self):
        w = World(self)
        huge = [{"id": f"D{i}", "state": "met", "evidence": ["x" * 5000, "y" * 5000]} for i in range(1, 60)]
        w.report(items=huge)
        hub = w.hub(["ACCEPT"], notes=["mutant survived: " + "m" * 3000] * 50)
        hub.tick()
        (call,) = w.runner.calls
        self.assertLessEqual(len(call["prompt"].encode("utf-8")), CARD_MAX)
        self.assertIn("## judge", call["prompt"])
        self.assertIn("## previous verdicts on this id", call["prompt"])

    def test_card_sections(self):
        j = Judgement(cls="failure", cause="implementation", sha="a" * 40, repo=REPO, ff=True, base="main",
                      tests={REPO: {"passed": 1, "failed": 2, "skipped": 0}}, failing=["t1", "t2"],
                      notes=["mutation m3 survived"])
        c = verdict_card(DIRECTIVE, {"items": [{"id": "D1", "state": "unmet", "evidence": ["red"]}]}, j,
                         ["calc.py +1 -1"], ["other.py"], [{"decision": "SEND_BACK", "class": "partial"}])
        for s in ("make calc add", "D1: tests green", "D1 unmet: red", "class failure", "t1, t2", "mutation m3 survived",
                  "calc.py +1 -1", "other.py", "SEND_BACK (partial)"):
            self.assertIn(s, c)
        big = verdict_card({**DIRECTIVE, "goal": "한" * 50000}, {"items": []}, j, ["f"] * 1000, ["o"] * 1000, [])
        self.assertLessEqual(len(big.encode("utf-8")), CARD_MAX)

    def test_files_outside_owned_paths_reach_the_card(self):
        w = World(self)
        w.report()
        w.hub(["ACCEPT"]).tick()
        card = w.runner.calls[0]["prompt"]
        part = card.split("## files outside the directive's owned paths\n")[1].split("\n\n")[0]
        self.assertEqual(part, "other.py")


class Decision(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_decision("ACCEPT"), ("ACCEPT", []))
        self.assertEqual(parse_decision("SEND_BACK\n- a\n- b\n" + "- c\n" * 10)[1][:2], ["a", "b"])
        self.assertEqual(len(parse_decision("SEND_BACK\n" + "- c\n" * 10)[1]), 6)
        self.assertEqual(parse_decision("ASK_HUMAN is this right?"), ("ASK_HUMAN", ["is this right?"]))
        self.assertEqual(parse_decision("SEND_BACK")[0], "ASK_HUMAN")
        self.assertEqual(parse_decision("")[0], "ASK_HUMAN")


if __name__ == "__main__":
    unittest.main()
