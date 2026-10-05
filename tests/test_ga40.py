"""CMD-GA40: GA Planner in shadow. Fake runners only (0 network, 0 model runs); nothing is mailed or sent."""
import copy
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ga import mailbox
from ga.__main__ import main as ga_main
from ga.plan import card as cardmod
from ga.plan import cli, compare, draft, lessons

ROOT = Path(__file__).resolve().parents[1]

HEAD = {"schema": "directive/2", "id": "CMD-GA90", "rev": 1, "to": "GA", "after": [],
        "goal": "Add a small read-only report.", "why": "a test fixture",
        "refs": ["ga-sdk claude/con2 (5b2b194): ga/forms"],
        "scope": [{"id": "S1", "text": "ga/report.py: prints the report."}],
        "done_when": [{"id": "D1", "text": "a unittest prints the report."}], "budget": {"claude_p_runs": 0}}


def head(**kw):
    h = copy.deepcopy(HEAD)
    h.update(kw)
    return h


def scope(*texts):
    return [{"id": f"S{i}", "text": t} for i, t in enumerate(texts, 1)]


def dw(*texts):
    return [{"id": f"D{i}", "text": t} for i, t in enumerate(texts, 1)]


def dr(h=None, item=None, wp=draft.WORKER_RULES, heads=1):
    return {"head": h or head(), "item": item, "worker_prompt": wp, "heads": heads}


def ids(findings, lesson):
    return [f for f in findings if f["lesson"] == lesson]


class FakeRunner:
    """A runner with the ga backend shape: run_turn(text, resume) -> an object with .answer. No network."""

    def __init__(self, answers, before=None):
        self.answers, self.calls, self.before = list(answers), [], before

    def run_turn(self, text, resume, **kw):
        if self.before:
            self.before(self)
        self.calls.append(text)
        return type("T", (), {"answer": self.answers.pop(0) if self.answers else "no json"})()


def answer(h=None, item=None, **extra):
    doc = {"head": h or head(), **({"item": item} if item is not None else {}), **extra}
    return "```json\n" + json.dumps(doc, ensure_ascii=False) + "\n```"


def small_repo(tmp: Path) -> Path:
    r = tmp / "repo"
    (r / "web" / "src").mkdir(parents=True)
    (r / "ga").mkdir()
    (r / "README.md").write_text("# App\n\n## Install\n\n### Path B (Token venv)\n\n## Running\n\n## License\n\n## Notes\n")
    (r / ".ga-judge.json").write_text(json.dumps({"test": ["{python}", "-m", "unittest", "discover", "-s", "tests"]}))
    (r / "pyproject.toml").write_text('[project]\nname = "app-sdk"\nversion = "0.1.0"\n')
    (r / "web" / "package.json").write_text(json.dumps({"name": "app-web"}))
    return r


# ---- D1: the card ------------------------------------------------------------------------------------------------
class Card(unittest.TestCase):
    def test_card_holds_the_map_test_commands_run_heads_and_last_10_decisions(self):
        with tempfile.TemporaryDirectory() as t:
            r = small_repo(Path(t))
            log = Path(t) / "DECISION_LOG.md"
            log.write_text("".join(f"## BD-{i}: decision {i}\n\ntext\n\n" for i in range(1, 31)))
            c = cardmod.build("연간 요금 토글 추가", r, decision_log=log, to="GA")
            for want in ("연간 요금 토글 추가", "unittest discover", "app-sdk (pyproject.toml)", "app-web (web/package.json)",
                         "readme run head - Install", "Path B (Token venv)", "readme run head - Running", "dir - web/",
                         "BD-30: decision 30", "BD-21: decision 21", "directive/2", "L7 "):
                self.assertIn(want, c.text)
            self.assertNotIn("BD-20: decision 20", c.text)
            self.assertNotIn("License", c.text)
            self.assertNotIn("Last decisions", cardmod.build("x", r).text)  # no log configured: no decisions

    def test_card_stays_under_8kb_on_a_huge_repo(self):
        with tempfile.TemporaryDirectory() as t:
            r = Path(t) / "huge"
            r.mkdir()
            for i in range(3000):
                (r / f"pkg_{i:05d}_with_a_long_directory_name").mkdir()
            (r / "README.md").write_text("## Running\n" + "".join(f"### run step {i} " + "x" * 300 + "\n"
                                                                  for i in range(2000)))
            (r / ".ga-judge.json").write_text(json.dumps({"test": ["python"] + ["-k"] * 5000}))
            log = Path(t) / "DECISION_LOG.md"
            log.write_text("".join(f"## BD-{i}: " + "y" * 5000 + "\n" for i in range(50)))
            c = cardmod.build("요청 " * 40000, r, decision_log=log, to="GA")
            self.assertLessEqual(c.size, cardmod.CARD_MAX)
            self.assertLessEqual(len(c.text.encode("utf-8")), 8 * 1024)
            self.assertIn(lessons.CHECKLIST, c.text)  # fixed parts always whole
            self.assertIn(cardmod.TEMPLATE, c.text)
            self.assertTrue(c.dropped)


# ---- D1: one turn, one repair, ga check -------------------------------------------------------------------------
class Turn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = small_repo(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_good_answer_is_one_turn_and_passes_ga_check(self):
        run = FakeRunner([answer()])
        d = draft.plan("add a report", self.repo, run, to="GA")
        self.assertEqual((len(run.calls), d.turns, d.check, d.findings), (1, 1, [], []))
        self.assertLessEqual(len(run.calls[0].encode()), cardmod.CARD_MAX)
        p = draft.write(d, Path(self.tmp.name) / "home")
        self.assertEqual(p, Path(self.tmp.name) / "home" / "plan" / "drafts" / "CMD-GA90.md")
        with redirect_stdout(io.StringIO()) as out:
            code = ga_main(["--config", str(Path(self.tmp.name) / "none.json"), "check", str(p)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertNotIn("hard", out.getvalue())

    def test_bad_format_gets_one_repair(self):
        run = FakeRunner(["I think the plan is fine.", answer()])
        d = draft.plan("add a report", self.repo, run)
        self.assertEqual((len(run.calls), d.turns), (2, 2))
        self.assertIn("Your previous answer broke the answer form", run.calls[1])
        self.assertIn("not_json", run.calls[1])
        self.assertNotIn("Lessons checklist", run.calls[1])  # the repair carries no card again

    def test_second_bad_answer_fails_cleanly(self):
        for bad in (["nope", "still nope", answer()], ['{"head": 3}', '{"head": "x"}', answer()]):
            run = FakeRunner(bad)
            with self.assertRaises(draft.PlanFailed) as e:
                draft.plan("add a report", self.repo, run)
            self.assertEqual((len(run.calls), e.exception.turns), (2, 2))  # bounded: never a third turn
        home = Path(self.tmp.name) / "h2"
        out = []
        code = cli.main(["x", "--repo", str(self.repo), "--yes", "--home", str(home)],
                        runner=FakeRunner(["no", "no", answer()]), out=out.append)
        self.assertEqual(code, 1)
        self.assertIn("failed after 2 turn(s), no draft written", out[-1])
        self.assertFalse((home / "plan" / "drafts").exists() and any((home / "plan" / "drafts").iterdir()))

    def test_a_draft_carries_ga_check_errors(self):
        h = head()
        del h["why"]
        d = draft.plan("x", self.repo, FakeRunner([answer(h)]))
        self.assertTrue(any("$.why" in p for p in d.check))
        text = draft.render(d)
        self.assertIn("## ga check", text)
        self.assertIn("$.why", text)
        self.assertGreaterEqual(d.errors, 1)

    def test_several_heads_are_kept_and_l6_reports_them(self):
        a = "```json\n" + json.dumps({"heads": [head(), head(id="CMD-GA91")]}) + "\n```"
        d = draft.plan("x", self.repo, FakeRunner([a]))
        self.assertEqual(d.heads, 2)
        self.assertTrue(ids(d.findings, "L6"))


# ---- D1: the lessons, each on its real case and on the corrected version ---------------------------------------
class Lessons(unittest.TestCase):
    def assertFires(self, lesson, d, ctx=None):
        got = ids(lessons.check(d, ctx or {}), lesson)
        self.assertTrue(got, f"{lesson} did not fire")
        self.assertTrue(all(f["severity"] == "error" for f in got))
        return got

    def assertQuiet(self, lesson, d, ctx=None):
        self.assertEqual(ids(lessons.check(d, ctx or {}), lesson), [])

    def test_l1_aga2_goal_without_the_nested_shape(self):
        bad = {"goal": "Return the agy turn as a typed `turns` field with each turn's tool calls and their results.",
               "files": ["ga/backends/agy.py"]}
        good = dict(bad, goal="Return the agy turn as a typed `turns` field: turns: [{n: int, calls: [{name: str, "
                              "args: dict, result: str}]}].")
        self.assertFires("L1", dr(item=bad))
        self.assertQuiet("L1", dr(item=good))
        self.assertEqual(lessons.check(dr(item=good)), [])

    def test_l2_aga4_rev1_tsx_without_type_check(self):
        h = head(scope=scope("web/src/pages/Pricing.tsx: an annual / monthly toggle that switches the prices."),
                 done_when=dw("a vitest test switches the toggle and sees the annual prices."))
        item = {"goal": "the toggle", "files": ["web/src/pages/Pricing.tsx"], "done_when": ["vitest passes"]}
        self.assertFires("L2", dr(h, item))
        fixed = head(scope=h["scope"], done_when=dw(h["done_when"][0]["text"], "`tsc --noEmit` and `npm run build` pass."))
        self.assertQuiet("L2", dr(fixed, item))
        self.assertEqual(lessons.check(dr(fixed, item)), [])

    def test_l3_con1_rev1_planted_faults_miss_min_font_and_weight(self):
        h = head(scope=scope("ga/console/judge.py: the design judge; checks: contrast, min-font, weight, spacing."),
                 done_when=dw("planted faults: low contrast, tight spacing; each is caught."))
        got = self.assertFires("L3", dr(h))
        self.assertIn("minfont", got[0]["message"])
        self.assertIn("weight", got[0]["message"])
        self.assertNotIn("contrast", got[0]["message"].split("cover:")[1].split("(")[0])
        fixed = head(scope=h["scope"], done_when=dw("planted faults: low contrast, 9px text (min-font), font weight 300 "
                                                    "(weight), tight spacing; each is caught."))
        self.assertQuiet("L3", dr(fixed))
        self.assertEqual(lessons.check(dr(fixed)), [])
        none = head(scope=h["scope"], done_when=dw("the judge works."))
        self.assertFires("L3", dr(none))

    def test_l4_ga42_rev1_acting_scope_without_shadow(self):
        h = head(goal="ga integrate lands judged worker branches.",
                 scope=scope("ga integrate: merges each judged worker branch into the integration branch, pushes it "
                             "and mails the verdict to the hub."))
        got = self.assertFires("L4", dr(h))
        for verb in ("merges", "pushes", "mails"):
            self.assertIn(verb, got[0]["message"])
        fixed = head(goal=h["goal"], scope=scope(h["scope"][0]["text"] + " --shadow (the default) writes the merge "
                                                                         "plan only and touches nothing."))
        self.assertQuiet("L4", dr(fixed))
        self.assertEqual(lessons.check(dr(fixed)), [])
        # a scope that says it never acts is quiet (GA40's own S3)
        self.assertQuiet("L4", dr(head(scope=scope("ga plan writes the draft; never sends mail, never creates "
                                                   "sessions."))))
        for act in ("deletes the old drafts", "starts the Token dev services", "deploys the site"):
            self.assertFires("L4", dr(head(scope=scope(f"ga thing: {act}."))))

    def test_l5_con2_init_defaults_not_read_from_the_repo(self):
        with tempfile.TemporaryDirectory() as t:
            repo = small_repo(Path(t))
            h = head(scope=scope("`ga console init` writes ga-console.json with defaults: the Token venv at "
                                 "~/token/venv, integration branch main, port 8787."))
            got = self.assertFires("L5", dr(h), {"repo": repo})
            self.assertIn("read README.md", got[0]["message"])
            for lit in ("~/token/venv", "port 8787"):
                self.assertIn(lit, got[0]["message"])
            fixed = head(scope=scope("`ga console init` writes ga-console.json with defaults read from the repo's "
                                     "README.md run section (the Token venv per README path B) and .ga-judge.json."))
            self.assertQuiet("L5", dr(fixed), {"repo": repo})
            self.assertEqual(lessons.check(dr(fixed), {"repo": repo}), [])
            (repo / "README.md").unlink()
            self.assertIn("read pyproject.toml", self.assertFires("L5", dr(h), {"repo": repo})[0]["message"])

    def test_l6_one_directive_per_session_and_the_context_cap(self):
        self.assertFires("L6", dr(wp="Branch claude/ga90. No PRs. Report report/2."))
        two = draft.WORKER_RULES + '\n{"schema":"directive/2","id":"CMD-A"}\n{"schema":"directive/2","id":"CMD-B"}'
        got = self.assertFires("L6", dr(wp=two))
        self.assertIn("2 directives", got[0]["message"])
        self.assertFires("L6", dr(heads=3))
        self.assertQuiet("L6", dr())  # the code-written worker rules carry the cap
        self.assertQuiet("L6", dr(wp="One directive. Context budget: past ~150k tokens, commit and push."))

    def test_l7_version_bump_while_others_are_in_flight(self):
        h = head(done_when=dw("tests pass; bump the version to 0.41.0."))
        ctx = {"in_flight": ["CMD-GA39", "CMD-CON3"]}
        got = self.assertFires("L7", dr(h), ctx)
        self.assertIn("CMD-GA39", got[0]["message"])
        fixed = head(done_when=dw("tests pass; do not bump the version (baseline sets it at landing)."))
        self.assertQuiet("L7", dr(fixed), ctx)
        self.assertEqual(lessons.check(dr(fixed), ctx), [])
        self.assertQuiet("L7", dr(h), {"in_flight": []})                  # nothing in flight
        self.assertQuiet("L7", dr(h), dict(ctx, l7=False))                # configured off
        self.assertQuiet("L7", dr(h), {"in_flight": ["CMD-GA90"]})        # only itself

    def test_findings_are_attached_to_the_written_draft(self):
        with tempfile.TemporaryDirectory() as t:
            repo = small_repo(Path(t))
            h = head(scope=scope("ga integrate: merges and pushes the branch."))
            d = draft.plan("x", repo, FakeRunner([answer(h)]))
            p = draft.write(d, Path(t) / "home")
            text = p.read_text()
            self.assertIn("- L4 error $.scope[0] (S1)", text)
            self.assertIn("Status: draft (never sent) - 1 error(s)", text)


# ---- D1: compare --------------------------------------------------------------------------------------------------
class Compare(unittest.TestCase):
    def test_compare_lists_changed_fields_and_a_score(self):
        with tempfile.TemporaryDirectory() as t:
            d = draft.plan("x", small_repo(Path(t)), FakeRunner([answer()]))
            dp = draft.write(d, Path(t) / "home")
            final = head(goal="Add a small read-only report, in shadow.",
                         scope=HEAD["scope"] + [{"id": "S2", "text": "a --dry-run flag."}],
                         done_when=dw("a unittest prints the report and checks every field."),
                         refs=HEAD["refs"] + ["ga-sdk claude/con2: ga/intake"])
            fp = Path(t) / "final.md"
            fp.write_text("```ga\n" + json.dumps(final) + "\n```\n")
            out = []
            self.assertEqual(cli.main(["compare", str(dp), str(fp)], out=out.append), 0)
            r = json.loads(out[-1])
            got = {(c["field"], c.get("id") or c.get("ref"), c["change"]) for c in r["changes"]}
            self.assertEqual(got, {("goal", None, "changed"), ("scope", "S2", "added"),
                                   ("done_when", "D1", "changed"), ("refs", "ga-sdk claude/con2: ga/intake", "added")})
            self.assertEqual((r["units"], r["unchanged"], r["score"]), (6, 2, 0.333))
            self.assertEqual(compare.compare_files(dp, dp)["score"], 1.0)
            self.assertEqual(compare.compare(final, HEAD)["changes"][1]["change"], "removed")


# ---- D1: cost first, y/N, nothing mailed or sent ----------------------------------------------------------------
class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.repo = small_repo(self.t)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cost_is_shown_before_the_turn_and_no_means_zero_turns(self):
        out = []
        run = FakeRunner([answer()])
        code = cli.main(["가격", "--repo", str(self.repo), "--home", str(self.t / "h")], runner=run,
                        input_fn=lambda q: "n", out=out.append)
        self.assertEqual((code, run.calls), (2, []))
        self.assertIn("1 model turn on agv gpt-oss-120b-medium", out[0])
        self.assertIn("0 tokens", out[-1])

        seen = []
        run = FakeRunner([answer()], before=lambda r: seen.append(list(out)))
        out.clear()
        code = cli.main(["가격", "--repo", str(self.repo), "--home", str(self.t / "h"), "--to", "GA"], runner=run,
                        input_fn=lambda q: "y", out=out.append)
        self.assertEqual(code, 0)
        self.assertTrue(any("estimated input" in x for x in seen[0]))  # printed before the model turn
        self.assertTrue((self.t / "h" / "plan" / "drafts" / "CMD-GA90.md").exists())

    def test_yes_skips_the_question_and_in_flight_feeds_l7(self):
        def no_input(q):
            raise AssertionError("asked y/N under --yes")
        h = head(done_when=dw("bump the version to 0.41.0."))
        out = []
        code = cli.main(["x", "--repo", str(self.repo), "--yes", "--home", str(self.t / "h"), "--in-flight",
                         "CMD-GA39"], runner=FakeRunner([answer(h)]), input_fn=no_input, out=out.append)
        self.assertEqual(code, 1)
        self.assertIn("L7", (self.t / "h" / "plan" / "drafts" / "CMD-GA90.md").read_text())

    def test_ga_main_routes_plan_and_lists_it(self):
        with redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                ga_main(["--help"])
        self.assertIn("plan", out.getvalue())
        with mock.patch("ga.plan.cli.main", return_value=7) as m:
            self.assertEqual(ga_main(["plan", "compare", "a", "b"]), 7)
        m.assert_called_once_with(["compare", "a", "b"])

    def test_nothing_is_mailed_or_sent(self):
        def boom(*a, **k):
            raise AssertionError("the planner mailed something")
        home = self.t / "h"
        with mock.patch.object(mailbox.Mailbox, "send", boom), \
                mock.patch("subprocess.run", boom), mock.patch("subprocess.Popen", boom):
            h = head(scope=scope("ga integrate: merges and pushes the branch."))
            code = cli.main(["x", "--repo", str(self.repo), "--yes", "--home", str(home)],
                            runner=FakeRunner([answer(h)]), out=lambda s: None)
        self.assertEqual(code, 1)
        written = sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file())
        self.assertEqual(written, ["plan/drafts/CMD-GA90.md"])
        src = "\n".join(p.read_text() for p in (ROOT / "ga" / "plan").glob("*.py"))
        for word in ("mailbox", "Mailbox", "send_message", "create_session", ".send(", "notify", "smtplib", "urllib"):
            self.assertNotIn(word, re.sub(r'""".*?"""', "", src, flags=re.S), word)


if __name__ == "__main__":
    unittest.main()
