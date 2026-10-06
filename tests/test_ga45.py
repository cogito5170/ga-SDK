"""CMD-GA45 D1: the VM shadow hub's config and unit, shadow rows mailed to baseline-shadow, shadow-compare from the
mailbox, and the model ladder in ga act. Offline: a temp HOME, fake systemctl, fake and local-git mailboxes, scripted
fake backends. 0 model runs."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ga import __main__ as M  # noqa: E402
from ga.act import loop as A  # noqa: E402
from ga.act import commands as AK  # noqa: E402
from ga.forms import hard, parse_text, validate  # noqa: E402
from ga.hub import MailHub, read_jsonl, shadow_rows_from_mailbox  # noqa: E402
from ga.mailbox import Mailbox  # noqa: E402
from ga.vm import core  # noqa: E402
from tests.test_ga38 import CALC, FIX, Fake, py_repo  # noqa: E402
from tests.test_ga42 import World  # noqa: E402
from tests.test_ga42_shadow import hub  # noqa: E402
from tests.test_ops2 import Full  # noqa: E402


def sh(*a, cwd=None):
    return subprocess.run(list(a), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


# ------------------------------------------------------------------------------------------------ S1: VM hub config
class HubUnit(unittest.TestCase):
    def test_hub_unit_has_config_and_ga_dir(self):
        home = Path("/home/u")
        unit = core.unit_files(home)[core.HUB_UNIT]
        (line,) = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
        self.assertTrue(line.endswith("-m ga hub tick --shadow --config /home/u/.ga/hub.json --ga-dir /home/u/.ga"), line)

    def test_hub_conf_paths_are_absolute(self):
        c = core.hub_conf(Path("/home/u"))
        for p in [c["mailbox_repo"], c["baseline_repo"], c["directives_dir"]] + [r["path"] for r in c["repos"].values()]:
            self.assertTrue(p.startswith("/home/u/"), p)
            self.assertNotIn("~", p)
        self.assertEqual(set(c["repos"]), {"cogito5170/ga-sdk", "cogito5170/Token"})
        self.assertEqual(c["repos"]["cogito5170/Token"]["path"], "/home/u/token")
        self.assertEqual({r["base"] for r in c["repos"].values()}, {"claude/gracious-meitner-vp49xe"})
        self.assertEqual((c["backend"], c["model"], c["options"], c["shadow"], c["daily_turns"]),
                         ("agv", "gemini-3.1-pro-high", {"agent": "ga-plan"}, True, 40))


class HubJson(Full):
    def test_full_writes_hub_json_once_and_never_overwrites(self):
        with self.agy():
            rc, _ = self.full()
        self.assertEqual(rc, 0, self.out)
        f = self.home / ".ga/hub.json"
        conf = json.loads(f.read_text())
        self.assertEqual(conf, core.hub_conf(self.home))
        self.assertTrue(conf["mailbox_repo"].startswith("/"))
        unit = (self.home / ".config/systemd/user" / core.HUB_UNIT).read_text()
        self.assertIn(f"--config {self.home}/.ga/hub.json --ga-dir {self.home}/.ga", unit)
        f.write_text('{"mine": true}\n')
        os.utime(f, ns=(1, 1))
        with self.agy():
            self.full()
        self.assertEqual(f.read_text(), '{"mine": true}\n')
        self.assertEqual(f.stat().st_mtime_ns, 1)


class MissingConfig(unittest.TestCase):
    def test_one_clear_line_and_nonzero_exit(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = M.main(["hub", "tick", "--shadow", "--config", str(d / "hub.json"), "--ga-dir", str(d / ".ga")])
        self.assertNotEqual(rc, 0)
        lines = err.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("no config at", lines[0])
        self.assertNotIn("Traceback", err.getvalue())


# ------------------------------------------------------------------------------------- S2: shadow rows to baseline
class ShadowMail(unittest.TestCase):
    def test_one_baseline_shadow_form_then_nothing(self):
        w = World(self)
        w.report()
        h = hub(w, ["ACCEPT"], "success", shadow=True)
        h.tick()
        (sent,) = w.mail.sent
        to, head, text = sent
        self.assertEqual(to, "baseline-shadow")
        self.assertNotEqual(to, w.conf["name"])
        self.assertEqual(hard(validate(parse_text(text)[0])), [])  # ga check: 0 hard
        self.assertEqual((head["schema"], head["kind"]), ("notify/1", "shadow"))
        sh_ = head["shadow"]
        self.assertEqual((sh_["id"], sh_["rev"], sh_["sha"], sh_["decision"], sh_["judge_class"]),
                         ("CMD-T1", 1, w.sha, "ACCEPT", "success"))
        self.assertIsInstance(sh_["input"], int)
        self.assertEqual(w.mail.box.get("baseline-shadow", [])[0].path, w.mail.box["baseline-shadow"][0].path)
        st = json.loads((w.tmp / ".ga/hub/state.json").read_text())
        (row,) = read_jsonl(w.tmp / ".ga/hub/shadow.jsonl")
        self.assertEqual(st["shadow_mailed"], [row["mail"]])
        res = h.tick()
        self.assertEqual(len(w.mail.sent), 1)  # not mailed again
        self.assertTrue(res.quiet)
        self.assertEqual(len(w.mail.unread("baseline")), 1)  # the hub's own inbox: only the report, unread

    def test_shadow_to_own_name_is_refused(self):
        w = World(self)
        w.report()
        w.conf["shadow_to"] = "baseline"
        h = hub(w, ["ACCEPT"], "success", shadow=True)
        res = h.tick()
        self.assertEqual(w.mail.sent, [])
        self.assertTrue(any("shadow mail failed" in p for p in res.plan), res.plan)


class CompareFromMailbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        sh("git", "init", "-q", "--bare", str(self.tmp / "origin.git"))
        sh("git", "clone", "-q", str(self.tmp / "origin.git"), str(self.tmp / "box"))
        self.ga = self.tmp / ".ga"
        rows = [{"at": "2026-10-06T00:00:00Z", "id": "A", "rev": 1, "sha": "a" * 40, "judge_class": "success",
                 "decision": "ACCEPT", "tokens": {"input": 10, "output": 2}, "mail": "to/baseline/1-GA-A.md"},
                {"at": "2026-10-06T00:00:01Z", "id": "B", "rev": 2, "sha": "b" * 40, "judge_class": "partial",
                 "decision": "ACCEPT", "tokens": {"input": 11, "output": 3}, "mail": "to/baseline/2-GA-B.md"},
                {"at": "2026-10-06T00:00:02Z", "id": "C", "rev": 1, "sha": None, "judge_class": None,
                 "decision": "SEND_BACK", "tokens": {"input": None, "output": None}, "mail": "to/baseline/3-GA-C.md"}]
        (self.ga / "hub").mkdir(parents=True)
        (self.ga / "hub/shadow.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.box = Mailbox(self.tmp / "box", sleep=lambda s: None)
        conf = {"name": "baseline", "mailbox_repo": str(self.tmp / "box"), "repos": {}}
        res = MailHub(conf, ga_dir=self.ga, mailbox=self.box, shadow=True).tick()
        self.assertEqual(len(res.sent), 3, res.plan)
        self.base = self.tmp / "baseline.json"
        self.base.write_text(json.dumps([{"id": "A", "rev": 1, "decision": "ACCEPT"},
                                         {"id": "B", "rev": 2, "decision": "SEND_BACK"},
                                         {"id": "C", "rev": 1, "decision": "SEND_BACK"}]))

    def cli(self, *a):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = M.main(["hub", "shadow-compare", str(self.base), "--ga-dir", str(self.ga), *a])
        return rc, json.loads(out.getvalue().splitlines()[-1])

    def test_mailbox_gives_the_same_numbers_as_shadow_jsonl(self):
        rc1, a = self.cli()
        rc2, b = self.cli("--mailbox", str(self.tmp / "box"), "--name", "baseline-shadow")
        self.assertEqual((rc1, a), (rc2, b))
        self.assertEqual((a["compared"], a["agree"], a["false_accepts"]), (3, 2, ["B rev 2"]))
        self.assertEqual(len(shadow_rows_from_mailbox(self.box, "baseline-shadow")), 3)
        self.assertEqual(list(self.box.unread("baseline")), [])  # nothing landed in the hub's own inbox

    def test_second_tick_mails_nothing(self):
        before = sh("git", "-C", str(self.tmp / "origin.git"), "rev-parse", "ga-mailbox")
        res = MailHub({"name": "baseline", "mailbox_repo": str(self.tmp / "box"), "repos": {}}, ga_dir=self.ga,
                      mailbox=self.box, shadow=True).tick()
        self.assertTrue(res.quiet)
        self.assertEqual(sh("git", "-C", str(self.tmp / "origin.git"), "rev-parse", "ga-mailbox"), before)


# ---------------------------------------------------------------------------------------------- S3: model ladder
CFG = {"commands": {"test": ["{python}", "-m", "unittest", "-v"]}, "done_when": "test", "timeout_s": 60,
       "models": ["cheap", "mid", "strong"]}
DIRTY = ("EDIT calc.py\n<<<<<<< SEARCH\n    return a * b\n=======\n    return a * b  # rung 1 was here\n>>>>>>> REPLACE\n"
         "NEW junk.py\nprint('rung 1')\n")


class Ladder(unittest.TestCase):
    def repo(self, cfg=CFG):
        root = py_repo(self, cfg)
        sh("git", "init", "-q", cwd=root)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@x", "add", ".", cwd=root)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "c1", cwd=root)
        state = root.parent / (root.name + "-state")
        self.addCleanup(shutil.rmtree, state, True)
        return root, state

    def ladder(self, root, state, fakes, ladder="cheap,mid,strong", **kw):
        made = []

        def make(m):
            made.append(m)
            return fakes[m]
        item = {"id": "CMD-T1", "goal": "make the tests pass", "files": ["calc.py", "junk.py"]}
        res = A.run_item(root, item, backend="fake", model="cheap", ladder=ladder, make_runner=make,
                         state_dir=state, **kw)
        return res, made

    def test_rung1_blocked_rung2_done_from_a_clean_tree(self):
        root, state = self.repo()
        seen = {}

        def rung2(prompt, system):
            seen["calc"] = (root / "calc.py").read_text()
            seen["junk"] = (root / "junk.py").exists()
            return FIX
        res, made = self.ladder(root, state, {"cheap": Fake([DIRTY]), "mid": Fake([rung2])}, max_turns=1)
        self.assertEqual(res.status, "done", res.reason)
        self.assertEqual(made, ["cheap", "mid"])
        self.assertEqual(seen, {"calc": CALC, "junk": False})  # rung 2 started from the clean tree
        self.assertEqual([(g["model"], g["status"]) for g in res.rungs], [("cheap", "blocked"), ("mid", "done")])
        self.assertTrue(res.rungs[0]["reason"].startswith("turn cap"))
        for g in res.rungs:
            self.assertGreater(g["tokens"]["total"], 0)
        self.assertEqual(res.tokens["total"], sum(g["tokens"]["total"] for g in res.rungs))
        d = res.to_dict()
        self.assertEqual(d["rungs"], res.rungs)
        self.assertEqual(set(d["rungs"][0]), {"model", "status", "reason", "turns", "tokens"})

    def test_a_tree_that_starts_with_a_removed_file_restarts_each_rung_from_that_state(self):
        # the agy bridge removes an item's `rewrite` files before ga act (BD-450); a ladder must keep that start
        root, state = self.repo()
        (root / "calc.py").unlink()
        seen = {}

        def rung2(prompt, system):
            seen["calc_exists"] = (root / "calc.py").exists()
            seen["junk"] = (root / "junk.py").exists()
            return "NEW calc.py\n" + CALC
        rung1 = "NEW calc.py\nbroken(\nNEW junk.py\nprint('rung 1')\n"
        res, made = self.ladder(root, state, {"cheap": Fake([rung1]), "mid": Fake([rung2])}, ladder="cheap,mid",
                                max_turns=1)
        self.assertEqual(made, ["cheap", "mid"], res.rungs)
        self.assertEqual(seen, {"calc_exists": False, "junk": False})  # rung 2 starts with calc.py removed again

    def test_rung1_done_never_runs_rung2(self):
        root, state = self.repo()
        res, made = self.ladder(root, state, {"cheap": Fake([FIX]), "mid": Fake(["BLOCKED x\n"])})
        self.assertEqual((res.status, made, len(res.rungs)), ("done", ["cheap"], 1))

    def test_refused_stops_the_ladder(self):
        root, state = self.repo()
        res, made = self.ladder(root, state, {"cheap": Fake(["BLOCKED refusing: unsafe\n"]), "mid": Fake([FIX])})
        self.assertEqual((res.status, made), ("blocked", ["cheap"]))
        self.assertTrue(res.reason.startswith("model:"))

    def test_no_progress_and_token_cap_escalate(self):
        root, state = self.repo()
        need = "NEED symbol calc.add\n"
        res, made = self.ladder(root, state, {"cheap": Fake([need]), "mid": Fake([need])}, max_tokens=1,
                                ladder="cheap,mid")
        self.assertEqual(made, ["cheap", "mid"])  # the token cap is a climb, not a stop
        self.assertTrue(all(g["reason"].startswith("token cap") for g in res.rungs), res.rungs)
        root, state = self.repo()
        again = "EDIT calc.py\n<<<<<<< SEARCH\n    return a * b\n=======\n    return b * a\n>>>>>>> REPLACE\n"
        back = "EDIT calc.py\n<<<<<<< SEARCH\n    return b * a\n=======\n    return a * b\n>>>>>>> REPLACE\n"
        res, made = self.ladder(root, state, {"cheap": Fake([again, back]), "mid": Fake([FIX])}, ladder="cheap,mid")
        self.assertTrue(res.rungs[0]["reason"].startswith("no progress"), res.rungs)
        self.assertEqual((made, res.status), (["cheap", "mid"], "done"))

    def test_unknown_model_rejected_before_any_run(self):
        root, state = self.repo()
        for bad in ("cheap,evil; rm -rf /", "cheap,gpt-9"):
            with self.assertRaises(AK.ActConfigError):
                self.ladder(root, state, {"cheap": Fake([FIX])}, ladder=bad)
        self.assertFalse(state.exists())  # nothing ran
        self.assertEqual((root / "calc.py").read_text(), CALC)

    def test_default_models_are_the_agy_list(self):
        self.assertEqual(len(AK.MODELS), 18)
        self.assertIn("gemini-3.1-pro-high", AK.MODELS)
        self.assertIn("claude-opus-5-5-high", AK.MODELS)
        self.assertEqual(AK.load(Path("/nonexistent"))["models"], list(AK.MODELS))

    def test_ladder_from_options_and_no_ladder_unchanged(self):
        root, state = self.repo()
        item = {"id": "CMD-T1", "goal": "g", "files": ["calc.py"]}
        made = []
        res = A.run_item(root, item, backend="fake", model="cheap", options={"ladder": ["cheap", "mid"]},
                         make_runner=lambda m: made.append(m) or Fake([FIX]), state_dir=state)
        self.assertEqual((res.status, made), ("done", ["cheap"]))
        res2 = A.run_item(root, item, backend="fake", model="m", runner=Fake([FIX]), state_dir=state)
        self.assertNotIn("rungs", res2.to_dict())

    def test_cli_rejects_unknown_model(self):
        root, state = self.repo()
        (root.parent / "item.json").write_text(json.dumps({"id": "CMD-T1", "goal": "g", "files": ["calc.py"]}))
        self.addCleanup(lambda: (root.parent / "item.json").unlink(missing_ok=True))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = M.main(["act", "--item", str(root.parent / "item.json"), "--repo", str(root), "--backend", "agv",
                         "--ladder", "cheap,nope", "--state", str(state)])
        self.assertEqual(rc, 2)
        self.assertIn("unknown model", err.getvalue())


class Version(unittest.TestCase):
    def test_version_0_14_0(self):
        import ga
        self.assertEqual(ga.__version__, "0.18.0")
        self.assertIn('version = "0.18.0"', (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())


if __name__ == "__main__":
    unittest.main()
