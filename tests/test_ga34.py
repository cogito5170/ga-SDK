"""CMD-GA34: pool nodes do real repository work (worktrees, tools, after/files, judge + fast-forward integration, judge
beyond pip, turn.started / turn.progress). Fake backends and temporary git repos only: 0 network, 0 model calls."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import net_world as nw  # noqa: E402
from test_ga33 import PoolCase, item, network  # noqa: E402
from ga import judge as J  # noqa: E402
from ga.adapters import git as G  # noqa: E402
from ga.backends import builtin as B  # noqa: E402
from ga.backends.base import ConfigError  # noqa: E402
from ga.net import pool as P  # noqa: E402

PASS_TEST = "import unittest\nfrom {mod} import {fn}\nclass T(unittest.TestCase):\n    def test_{fn}(self): self.assertEqual({fn}(), 1)\n"
JUDGE_CFG = {"setup": [], "test": ["{python}", "-m", "unittest", "discover"]}


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


class Deps(PoolCase):
    """S3: after."""

    def test_item_keys_and_id_forms(self):
        self.world()
        pl = self.pool()
        self.assertEqual(pl.add(item("W-FE-01"))[0], True)
        self.assertEqual(pl.add(item("CMD-B1", after=["W-FE-01"], files=["web/src/**"]))[0], True)
        for bad, word in ((item("CMD-C1", after="W-FE-01"), "after"), (item("CMD-C2", after=["CMD-C2"]), "itself"),
                          (item("CMD-C3", files=["/etc/x"]), "files"), (item("CMD-C4", files=["a/../b"]), "files"),
                          (item("CMD-C5", files=[]), "files"), (item("w-fe-1"), "id")):
            ok, why = pl.add(bad)
            self.assertFalse(ok)
            self.assertIn(word, why)

    def test_unknown_and_failed_dependencies_are_refused_queued_and_done_are_not(self):
        self.world()
        self.add(item("CMD-A1"))
        ok, why = self.pool().add(item("CMD-B1", after=["CMD-Z9"]))
        self.assertFalse(ok)
        self.assertIn("CMD-Z9", why)
        self.add(item("CMD-B1", after=["CMD-A1"]))  # queued
        for _ in range(4):
            self.round()
        self.add(item("CMD-C1", after=["CMD-A1"]))  # done
        (self.w.ga / "queue" / "failed").mkdir(parents=True, exist_ok=True)
        (self.w.ga / "queue" / "failed" / "000099-CMD-F1.json").write_text(json.dumps(item("CMD-F1")))
        ok, why = self.pool().add(item("CMD-D1", after=["CMD-F1"]))
        self.assertFalse(ok)

    def test_cycle_is_refused(self):
        self.world()
        q = self.w.ga / "queue"
        q.mkdir(parents=True)
        (q / "000001-CMD-A1.json").write_text(json.dumps({"schema": "work/1", **item("CMD-A1", after=["CMD-B1"])}))
        ok, why = self.pool().add(item("CMD-B1", after=["CMD-A1"]))
        self.assertFalse(ok)
        self.assertIn("cycle", why)

    def test_dependent_item_starts_only_after_its_dependency_is_done(self):
        self.world()
        self.add(item("CMD-A1"), item("CMD-B1", goal="implement against the contract of CMD-A1", after=["CMD-A1"]))
        first = self.round()["started"]
        self.assertEqual([s["item"] for s in first], ["CMD-A1"])
        starts = {}
        for r in range(2, 8):
            ev = self.round()
            for s in ev["started"]:
                starts[s["item"]] = r
            for x in ev["retired"]:
                starts.setdefault("done:" + x["item"], r)
        self.assertIn("CMD-B1", starts)
        self.assertGreaterEqual(starts["CMD-B1"], starts["done:CMD-A1"])
        self.assertEqual(self.pool().status()["done"], ["CMD-A1", "CMD-B1"])
        blocked = [e for e in self.pl0() if e["type"] == "work.blocked"]
        self.assertEqual([(e["data"]["item"], e["data"]["waiting_on"]) for e in blocked], [("CMD-B1", ["CMD-A1"])])

    def test_failed_dependency_fails_the_dependent_with_reason(self):
        self.world()
        self.add(item("CMD-A1", check=["false"]), item("CMD-B1", after=["CMD-A1"]))
        for _ in range(6):
            self.round()
        st = self.pool().status()
        self.assertEqual(st["failed"], ["CMD-A1", "CMD-B1"])
        why = [e["data"]["reason"] for e in self.pl0() if e["type"] == "work.failed" and e["data"]["item"] == "CMD-B1"]
        self.assertEqual(why, ["dependency failed: CMD-A1"])


class Ownership(PoolCase):
    """S4: files."""

    def test_globs(self):
        self.assertTrue(P.owned("web/src/a/b.ts", ["web/src/**"]))
        self.assertTrue(P.owned("web/x.ts", ["web/**/*.ts"]))
        self.assertFalse(P.owned("web/src2/a.ts", ["web/src/**"]))
        self.assertFalse(P.owned("api/a.py", ["web/**", "*.md"]))
        self.assertTrue(P.owned("README.md", ["*.md"]))
        self.assertFalse(P.owned("docs/a.md", ["*.md"]))
        self.assertTrue(P.overlap(["web/src/**"], ["web/src/a.ts"]))
        self.assertTrue(P.overlap(["web/**"], ["web/src/**"]))
        self.assertFalse(P.overlap(["web/**"], ["api/**"]))
        self.assertFalse(P.overlap(["web/a*.ts"], ["web/b.ts"]))

    def test_overlapping_items_never_live_together(self):
        self.world(network(max_live=3))
        self.add(item("CMD-A1", files=["web/src/**"]), item("CMD-B1", files=["web/src/app.ts"]),
                 item("CMD-C1", files=["api/**"]), item("CMD-D1"))
        for _ in range(10):
            self.round()
            live = [v["item"] for v in self.reg()["live"].values()]
            ids = {x["id"] for x in live}
            self.assertFalse({"CMD-A1", "CMD-B1"} <= ids)
        first = self.pl0()
        self.assertEqual(self.pool().status()["done"], ["CMD-A1", "CMD-B1", "CMD-C1", "CMD-D1"])
        started_1 = [e["data"]["item"] for e in first if e["type"] == "node.started"][:3]
        self.assertEqual(started_1, ["CMD-A1", "CMD-C1", "CMD-D1"])  # B waits for A; C, D (no overlap) do not
        self.assertIn(("CMD-B1", ["CMD-A1"]), [(e["data"]["item"], e["data"]["waiting_on"]) for e in first
                                               if e["type"] == "work.blocked"])

    def test_without_files_nothing_waits(self):
        self.world()
        self.add(item("CMD-A1"), item("CMD-B1"))
        self.assertEqual(len(self.round()["started"]), 2)


class RepoCase(PoolCase):
    """A target repository (main, a passing test, a setup-mode judge config) and a pool with ``repo``."""

    def repo_world(self, writes=None, judge=None, **pool):
        net = network(repo={"path": "app", "branch": "main"}, **pool)
        w = self.world(net)
        app = w.tmp / "app"
        app.mkdir()
        sh(app, "init", "-q", "-b", "main")
        (app / "base.py").write_text("def one():\n    return 1\n")
        (app / "test_base.py").write_text(PASS_TEST.format(mod="base", fn="one"))
        (app / ".ga-judge.json").write_text(json.dumps(JUDGE_CFG))
        sh(app, "add", "-A")
        sh(app, "commit", "-q", "-m", "init")
        self.app, self.seen, self.judge = app, [], judge
        self.writes = writes or {}
        fb = w.backends["fake_text"]
        orig = fb.create

        def create(m, o, ctx):
            self.seen.append(dict(ctx))
            r = orig(m, o, ctx)
            run = r.run_turn

            def run_turn(prompt, *a, **k):
                iid = next((i for i in self.writes if i in prompt), None)
                for rel, text in (self.writes.get(iid) or {}).items():
                    p = Path(ctx["cwd"]) / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(text)
                return run(prompt, *a, **k)
            r.run_turn = run_turn
            return r
        fb.create = create
        return w

    def pool(self):
        return P.Pool(self.w.cfg(), self.w.ga, get_backend=self.w.get, clock=self.w.clock,
                      judge=getattr(self, "judge", None))

    def head(self, ref="main"):
        return sh(self.app, "rev-parse", ref)

    def run_until_idle(self, n=8):
        for _ in range(n):
            self.round()


class Workspace(RepoCase):
    """S1 + S5."""

    def test_turn_runs_in_its_own_worktree_and_a_passing_branch_fast_forwards_main(self):
        self.repo_world({"CMD-A1": {"two.py": "def two():\n    return 1\n",
                                    "test_two.py": PASS_TEST.format(mod="two", fn="two")}})
        old = self.head()
        self.add(item("CMD-A1", files=["two.py", "test_two.py"]))
        self.round()
        wt = self.w.ga / "worktrees" / "writer_1"
        self.assertTrue(wt.is_dir())
        self.assertEqual(sh(wt, "rev-parse", "--abbrev-ref", "HEAD"), "ga/writer_1")
        self.assertEqual(sh(wt, "config", "--worktree", "core.hooksPath"), str((self.w.ga / "hooks" / "writer_1").resolve()))
        self.run_until_idle()
        self.assertEqual(Path(self.seen[0]["cwd"]).resolve(), wt.resolve())
        self.assertNotIn("/nodes/", self.seen[0]["cwd"])
        new = self.head()
        self.assertNotEqual(new, old)
        self.assertTrue(G.is_ancestor(self.app, old, new))
        self.assertEqual(new, self.head("ga/writer_1"))  # integrated = the node branch head, fast-forward
        self.assertEqual(sorted(G.changed_files(self.app, old, new)), ["test_two.py", "two.py"])
        self.assertFalse(wt.exists())  # retire removes the worktree, keeps the branch
        self.assertEqual(self.pool().status()["done"], ["CMD-A1"])
        ev = [e for e in self.pl0() if e["type"] == "work.integrated"]
        self.assertEqual([e["data"]["sha"] for e in ev], [new])
        self.assertTrue((self.app / "two.py").exists())  # main's checkout moved with it (merge --ff-only)

    def test_pre_push_hook_refuses_other_branches(self):
        self.repo_world()
        self.add(item("CMD-A1"))
        self.round()
        hook = self.w.ga / "hooks" / "writer_1" / "pre-push"
        sha = self.head()
        for ref, code in (("refs/heads/ga/writer_1", 0), ("refs/heads/main", 1)):
            p = subprocess.run([str(hook)], input=f"refs/heads/x {sha} {ref} {'0' * 40}\n", text=True,
                               capture_output=True, cwd=str(self.app))
            self.assertEqual(p.returncode, code, p.stderr)

    def test_two_nodes_never_share_a_worktree(self):
        self.repo_world({"CMD-A1": {"a.py": "A = 1\n"}, "CMD-B1": {"b.py": "B = 1\n"}})
        self.add(item("CMD-A1", files=["a.py"]), item("CMD-B1", files=["b.py"]))
        self.round()
        ws = [v["ws"] for v in self.reg()["live"].values()]
        self.assertEqual(len(ws), 2)
        self.assertNotEqual(ws[0]["path"], ws[1]["path"])
        self.assertNotEqual(ws[0]["branch"], ws[1]["branch"])
        self.run_until_idle()
        cwds = {c["cwd"] for c in self.seen}
        self.assertEqual(len(cwds), 2)
        self.assertTrue((self.app / "a.py").exists() and (self.app / "b.py").exists())
        self.assertEqual(self.pool().status()["done"], ["CMD-A1", "CMD-B1"])

    def test_moved_integration_head_is_merged_then_judged_and_main_only_fast_forwards(self):
        self.repo_world({"CMD-A1": {"a.py": "A = 1\n"}, "CMD-B1": {"b.py": "B = 1\n"}})
        self.add(item("CMD-A1", files=["a.py"]), item("CMD-B1", files=["b.py"]))
        heads = [self.head()]
        self.run_until_idle()
        heads += [e["data"]["sha"] for e in self.pl0() if e["type"] == "work.integrated"]
        self.assertEqual(heads[-1], self.head())
        for a, b in zip(heads, heads[1:]):
            self.assertTrue(G.is_ancestor(self.app, a, b), "main moved by a non-fast-forward")
        self.assertEqual(len(heads), 3)
        log = sh(self.app, "log", "--format=%s", "main")
        self.assertIn("Merge commit", log)  # the second node branch merged the moved main (never rebased)

    def test_hand_in_outside_files_fails_with_the_paths(self):
        self.repo_world({"CMD-A1": {"a.py": "A = 1\n", "web/x.ts": "x\n"}})
        old = self.head()
        self.add(item("CMD-A1", files=["a.py"]))
        self.run_until_idle()
        self.assertEqual(self.head(), old)
        self.assertEqual(self.pool().status()["failed"], ["CMD-A1"])
        rej = [e["data"] for e in self.pl0() if e["type"] == "work.rejected"]
        self.assertEqual(len(rej), 1)
        self.assertIn("web/x.ts", rej[0]["reason"])
        self.assertNotIn("a.py", rej[0]["reason"])
        self.assertTrue(sh(self.app, "rev-parse", "ga/writer_1"))  # the branch is kept

    def test_failing_judge_is_not_integrated(self):
        bad = "import unittest\nclass T(unittest.TestCase):\n    def test_red(self): self.assertEqual(1, 2)\n"
        self.repo_world({"CMD-A1": {"test_red.py": bad}})
        old = self.head()
        self.add(item("CMD-A1"))
        self.run_until_idle()
        self.assertEqual(self.head(), old)
        self.assertEqual(self.pool().status()["failed"], ["CMD-A1"])
        rej = [e["data"] for e in self.pl0() if e["type"] == "work.rejected"]
        self.assertEqual(rej[0]["failing"], ["test_red.T.test_red"])
        self.assertIn("test_red.T.test_red", [e for e in self.pl0() if e["type"] == "work.failed"][0]["data"]["reason"])

    def test_injected_failing_judge_blocks_integration(self):
        verdict = J.Judgement(base="main")
        verdict.worse("failure", "implementation")
        self.repo_world({"CMD-A1": {"a.py": "A = 1\n"}}, judge=lambda *a, **k: verdict)
        old = self.head()
        self.add(item("CMD-A1"))
        self.run_until_idle()
        self.assertEqual(self.head(), old)

    def test_ga_dir_paths_in_a_hand_in_are_refused(self):
        self.repo_world({"CMD-A1": {".ga/x.json": "{}\n"}})
        self.add(item("CMD-A1"))
        self.run_until_idle()
        self.assertEqual(self.pool().status()["failed"], ["CMD-A1"])

    def test_fast_forward_local_refuses_non_fast_forward(self):
        self.repo_world()
        sh(self.app, "checkout", "-q", "-b", "side", "HEAD~0")
        (self.app / "s.py").write_text("s\n")
        sh(self.app, "add", "-A")
        sh(self.app, "commit", "-q", "-m", "side")
        side = self.head("side")
        sh(self.app, "checkout", "-q", "main")
        (self.app / "m.py").write_text("m\n")
        sh(self.app, "add", "-A")
        sh(self.app, "commit", "-q", "-m", "main")
        m = self.head()
        with self.assertRaises(G.GitError):  # checked out: merge --ff-only
            G.fast_forward_local(self.app, "main", side)
        sh(self.app, "checkout", "-q", "side")
        with self.assertRaises(G.GitError):  # not checked out: the ancestry check before update-ref
            G.fast_forward_local(self.app, "main", side)
        self.assertEqual(self.head("main"), m)

    def test_without_repo_nothing_changes(self):
        self.world()
        self.add(item("CMD-A1"))
        self.round()
        self.assertFalse((self.w.ga / "worktrees").exists())
        self.assertNotIn("ws", next(iter(self.reg()["live"].values())))
        self.assertNotIn("blocked", self.pool().status())


class Turns(PoolCase):
    """S7: turn.started, turn.progress; S2 ctx."""

    def node_l0(self):
        out = []
        for f in sorted(self.w.ga.glob("nodes/**/telemetry.jsonl")):
            out += [json.loads(x) for x in f.read_text().splitlines()]
        return out

    def test_turn_started_precedes_run_end_for_every_turn(self):
        self.world()
        self.add(item("CMD-A1"), item("CMD-B1"))
        for _ in range(4):
            self.round()
        ev = [e for e in self.node_l0() if e["type"] in ("turn.started", "run.end")]
        self.assertTrue(ev)
        ends = [e for e in ev if e["type"] == "run.end"]
        for e in ends:
            starts = [s for s in ev if s["type"] == "turn.started" and s["run_id"] == e["run_id"]]
            self.assertEqual(len(starts), 1)
            self.assertLess(ev.index(starts[0]), ev.index(e))
            self.assertEqual(set(starts[0]["data"]) >= {"node", "item", "backend", "model"}, True)

    def test_progress_relays_tool_and_path_only_capped(self):
        roles = {"writer": {"backends": ["fake_text"], "pack_max_tokens": 3000, "progress": True}}
        w = self.world(network(roles=roles))
        fb = w.backends["fake_text"]
        orig = fb.create
        secret = "FILE CONTENT 42"

        def create(m, o, ctx):
            r = orig(m, o, ctx)
            run = r.run_turn

            def run_turn(*a, **k):
                line = json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "src/a.py", "content": secret}},
                    {"type": "text", "text": secret}]}})
                for _ in range(60):
                    for row in B.progress_of(line):
                        ctx["on_progress"](row)
                return run(*a, **k)
            r.run_turn = run_turn
            return r
        fb.create = create
        self.add(item("CMD-A1"))
        for _ in range(3):
            self.round()
        pr = [e for e in self.node_l0() if e["type"] == "turn.progress"]
        self.assertEqual(len(pr), 50)
        self.assertEqual(pr[0]["data"], {"node": "writer_1", "item": "CMD-A1", "tool": "Write", "path": "src/a.py"})
        self.assertNotIn(secret, json.dumps(pr))

    def test_tools_reach_the_ctx_only_when_the_role_sets_them(self):
        roles = {"writer": {"backends": ["fake_text"], "pack_max_tokens": 3000,
                            "tools": {"allow": ["Read", "Edit"], "permission_mode": "acceptEdits"}},
                 "seer": {"backends": ["fake_text"], "pack_max_tokens": 3000}}
        with mock.patch.object(P, "_takes_tools", lambda b: True):
            w = self.world(network(roles=roles))
            seen = []
            fb = w.backends["fake_text"]
            orig = fb.create
            fb.create = lambda m, o, ctx: (seen.append(ctx), orig(m, o, ctx))[1]
            self.add(item("CMD-A1"), item("CMD-B1", role="seer"))
            for _ in range(3):
                self.round()
        by = {c["cwd"].rstrip("/").rsplit("/", 1)[-1]: c for c in seen}
        self.assertEqual(by["writer_1"]["tools"], {"allow": ["Read", "Edit"], "permission_mode": "acceptEdits"})
        self.assertNotIn("tools", by["seer_1"])
        self.assertNotIn("on_progress", by["seer_1"])


class Tools(unittest.TestCase):
    """S2: claude_cli argv and the config check."""

    def test_default_argv_is_unchanged(self):
        a = B.ClaudeRunner(["claude"], "claude-haiku-4-5").argv("p", "sys")
        self.assertEqual(a[a.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", a)
        self.assertNotIn("--permission-mode", a)
        self.assertEqual(a[a.index("--output-format") + 1], "json")
        b = B.ClaudeRunner(["claude"], "claude-haiku-4-5", bare=False).argv("p")
        self.assertFalse(any(x in ("--allowedTools", "--permission-mode", "--tools") for x in b))

    def test_tools_argv(self):
        r = B.ClaudeRunner(["claude"], "claude-haiku-4-5", tools={"allow": ["Read", "Edit", "Bash(python3 -m unittest:*)"]})
        self.assertFalse(r.bare)
        a = r.argv("p")
        self.assertEqual(a[a.index("--allowedTools") + 1], "Read,Edit,Bash(python3 -m unittest:*)")
        self.assertEqual(a[a.index("--tools") + 1], "Read,Edit,Bash")
        self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--dangerously-skip-permissions", a)
        r = B.ClaudeRunner(["claude"], "m", tools={"allow": ["Read"], "permission_mode": "acceptEdits"}, on_progress=print)
        a = r.argv("p")
        self.assertEqual(a[a.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(a[a.index("--output-format") + 1: a.index("--output-format") + 3], ["stream-json", "--verbose"])

    def test_refused_tools_and_modes(self):
        for t in ({"allow": ["Read"], "permission_mode": "bypassPermissions"}, {"allow": ["Read"], "permission_mode": "auto"},
                  {"allow": ["WebFetch"]}, {"allow": ["Bash"]}, {"allow": ["Bash(*)"]}, {"allow": ["Bash(git push:*)"]},
                  {"allow": ["Bash(curl x)"]}, {"allow": []}, {"allow": ["Read"], "x": 1}):
            self.assertTrue(B.tools_problems(t), t)
            with self.assertRaises(ConfigError):
                B.ClaudeRunner(["claude"], "m", tools=t)
        self.assertEqual(B.tools_problems({"allow": ["Read", "Edit", "Write", "Glob", "Grep", "Bash(npm test:*)"]}), [])

    def test_config_check(self):
        def probs(role):
            net = network(roles={"writer": {"backends": ["claude_cli"], **role}})
            return [str(p) for p in P.problems_of(net)]
        self.assertEqual(probs({"tools": {"allow": ["Read", "Edit", "Write"]}, "progress": True}), [])
        self.assertTrue(any("bypassPermissions" in p for p in probs({"tools": {"allow": ["Read"],
                                                                               "permission_mode": "bypassPermissions"}})))
        net = network(roles={"writer": {"backends": ["gemini_cli"], "tools": {"allow": ["Read"]}}})
        self.assertTrue(any("cannot run a turn with tools" in str(p) for p in P.problems_of(net)))
        self.assertTrue(P.problems_of(network(repo={"path": 3})))
        self.assertEqual(P.problems_of(network(repo={"path": "app", "branch": "main"})), [])

    def test_progress_of_strips_everything_but_tool_and_path(self):
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py", "old_string": "x", "new_string": "SECRET"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "cat SECRET"}}]}})
        self.assertEqual(B.progress_of(line), [{"tool": "Edit", "path": "a.py"}, {"tool": "Bash"}])
        self.assertEqual(B.progress_of('{"type": "user", "message": {}}'), [])
        self.assertEqual(B.progress_of("not json"), [])

    def test_streamed_run_relays_lines_and_parses_the_result(self):
        script = ("import json\nprint(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read',"
                  "'input':{'file_path':'x.py'}}]}}))\nprint(json.dumps({'type':'result','subtype':'success','result':'ok',"
                  "'modelUsage':{'claude-haiku-4-5':{}},'usage':{'input_tokens':1,'output_tokens':1}}))\n")
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        f = d / "fake_claude.py"
        f.write_text(script)
        got = []
        r = B.ClaudeRunner([sys.executable, str(f)], "claude-haiku-4-5", tools={"allow": ["Read"]}, on_progress=got.append,
                           env={})
        t = r.run_turn("p")
        self.assertEqual(t.answer, "ok")
        self.assertEqual(got, [{"tool": "Read", "path": "x.py"}])


class JudgeBeyondPip(unittest.TestCase):
    """S6."""

    def test_js_summaries(self):
        vitest = " Test Files  3 passed (3)\n      Tests  12 passed (12)\n   Duration  1.2s"
        vitest_red = "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m | \x1b[1m\x1b[32m11 passed\x1b[39m\x1b[22m (12)"
        jest = "Tests:       1 failed, 2 skipped, 11 passed, 14 total\nTime:        2.3 s"
        pw = "  1 failed\n    [chromium] › a.spec.ts:3:5 › logs in\n  1 flaky\n  12 passed (4.1s)"
        self.assertEqual(J.parse_counts(vitest), {"passed": 12, "failed": 0, "skipped": 0})
        self.assertEqual(J.parse_counts(vitest_red), {"passed": 11, "failed": 1, "skipped": 0})
        self.assertEqual(J.parse_counts(jest), {"passed": 11, "failed": 1, "skipped": 2})
        self.assertEqual(J.parse_counts(pw), {"passed": 13, "failed": 1, "skipped": 0})
        self.assertEqual(J.parse_counts("Ran 3 tests in 0.1s\n\nOK"), {"passed": 3, "failed": 0, "skipped": 0})
        self.assertIsNone(J.parse_counts("nothing here"))

    def test_js_failing_names(self):
        out = " FAIL  src/a.test.ts > math > adds\n  1) [chromium] › e2e/login.spec.ts:3:5 › logs in \n"
        self.assertEqual(J.failing_tests(out), ["[chromium] › e2e/login.spec.ts:3:5 › logs in", "src/a.test.ts > math > adds"])

    def test_junit(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        f = d / "junit.xml"
        f.write_text('<?xml version="1.0"?><testsuites><testsuite name="s">'
                     '<testcase classname="a.test.ts" name="adds"/>'
                     '<testcase classname="a.test.ts" name="subs"><failure message="x"/></testcase>'
                     '<testcase classname="b" name="err"><error/></testcase>'
                     '<testcase classname="b" name="skip"><skipped/></testcase></testsuite></testsuites>')
        self.assertEqual(J.parse_junit(f), ({"passed": 1, "failed": 2, "skipped": 1}, ["a.test.ts.subs", "b.err"]))
        (d / "bad.xml").write_text("<html/>")
        self.assertIsNone(J.parse_junit(d / "bad.xml"))
        self.assertIsNone(J.parse_junit(d / "none.xml"))

    def test_config_setup_instead_of_dist(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        for cfg, ok in (({"setup": [["npm", "ci"]], "test": ["npm", "test"]}, True),
                        ({"setup": [], "test": ["x"], "junit": "r.xml"}, True), ({"test": ["npm", "test"]}, False),
                        ({"setup": ["npm ci"], "test": ["x"]}, False), ({"setup": [], "test": ["x"], "junit": 1}, False),
                        ({"dist": "p", "test": ["x"]}, True)):
            (d / ".ga-judge.json").write_text(json.dumps(cfg))
            if ok:
                J.load_config(d, None)
            else:
                with self.assertRaises(J.JudgeError):
                    J.load_config(d, None)

    def repo(self, cfg, files):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        p = mock.patch.dict("os.environ", env)
        p.start()
        self.addCleanup(p.stop)
        sh(d, "init", "-q", "-b", "main")
        (d / ".ga-judge.json").write_text(json.dumps(cfg))
        for k, v in files.items():
            (d / k).write_text(v)
        sh(d, "add", "-A")
        sh(d, "commit", "-q", "-m", "init")
        return d

    def test_judge_commit_runs_setup_and_reads_junit(self):
        # a "runner" that writes JUnit XML: two passing cases, one failing; setup writes a marker the runner needs
        runner = ("import pathlib,sys\nassert pathlib.Path('ready').exists(), 'setup did not run'\n"
                  "pathlib.Path('out').mkdir(exist_ok=True)\n"
                  "pathlib.Path('out/junit.xml').write_text('<testsuite><testcase classname=\"c\" name=\"a\"/>"
                  "<testcase classname=\"c\" name=\"b\"/><testcase classname=\"c\" name=\"red\"><failure/></testcase>"
                  "</testsuite>')\nsys.exit(1)\n")
        cfg = {"setup": [["{python}", "-c", "open('ready','w').write('1')"]], "test": ["{python}", "run.py"],
               "junit": "out/junit.xml"}
        d = self.repo(cfg, {"run.py": runner})
        j = J.judge_commit(d, "main", "main")
        self.assertEqual(j.tests[d.name], {"passed": 2, "failed": 1, "skipped": 0})
        self.assertIn("c.red", j.preexisting)  # red on base too: told apart by test id, so not a new failure
        self.assertEqual((j.failing, j.cls), ([], "success"))

    def test_junit_failures_are_never_counted_as_passes(self):
        # the output claims green; the JUnit file the config names says red, and the JUnit file wins
        runner = ("import pathlib\npathlib.Path('j.xml').write_text('<testsuite><testcase classname=\"c\" name=\"red\">"
                  "<failure/></testcase></testsuite>')\nprint('Ran 1 test in 0.0s')\nprint('OK')\n")
        d = self.repo({"setup": [], "test": ["{python}", "run.py"], "junit": "j.xml"}, {"run.py": runner})
        j = J.judge_commit(d, "main", "main")
        self.assertEqual(j.tests[d.name], {"passed": 0, "failed": 1, "skipped": 0})
        self.assertEqual(j.preexisting, ["c.red"])

    def test_a_committed_junit_file_is_not_read(self):
        stale = '<testsuite><testcase classname="c" name="ok"/></testsuite>'
        d = self.repo({"setup": [], "test": ["{python}", "-c", "pass"], "junit": "j.xml"}, {"j.xml": stale})
        j = J.judge_commit(d, "main", "main")
        self.assertEqual((j.cls, j.cause), ("insufficient", "measurement"))

    def test_failing_setup_is_a_dependency_failure(self):
        d = self.repo({"setup": [["{python}", "-c", "raise SystemExit(3)"]], "test": ["{python}", "-c", "pass"]}, {})
        j = J.judge_commit(d, "main", "main")
        self.assertEqual((j.cls, j.cause), ("failure", "dependency"))

    def test_non_ff_commit_is_refused(self):
        d = self.repo(JUDGE_CFG, {})
        sh(d, "checkout", "-q", "-b", "f")
        (d / "a").write_text("a")
        sh(d, "add", "-A")
        sh(d, "commit", "-q", "-m", "a")
        sh(d, "checkout", "-q", "main")
        (d / "b").write_text("b")
        sh(d, "add", "-A")
        sh(d, "commit", "-q", "-m", "b")
        j = J.judge_commit(d, "f", "main")
        self.assertFalse(j.ff)
        self.assertNotEqual(j.cls, "success")


if __name__ == "__main__":
    unittest.main()
