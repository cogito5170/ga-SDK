"""CMD-GA7: fewer quiet give-ups — harmless commands allowed, the hub's report command allowed, and the turn
prompt says how to work and to report even when stuck. Guard and sandbox are not weakened."""
import json
import unittest

from ga.adapters import bash_guard
from ga.adapters.agent_sdk import AgentSDKRunner, fake_sdk
from ga.adapters.base import TurnRequest
from ga.adapters.headless import DEFAULT_ALLOWED, HeadlessRunner
from ga.prompts import post_allow, turn_prompt

from test_headless import Stub
from world import World, directive


class AllowListTest(unittest.TestCase):
    def test_harmless_commands_added_and_nothing_broad(self):
        for rule in ("Bash(ls:*)", "Bash(mkdir:*)", "Bash(cd:*)", "Bash(git -C:*)", "Bash(git rev-parse:*)", "Bash(cat:*)", "Bash(pwd)"):
            self.assertIn(rule, DEFAULT_ALLOWED)
        self.assertNotIn("Bash", DEFAULT_ALLOWED)  # never "every command"
        self.assertFalse([r for r in DEFAULT_ALLOWED if r.startswith(("Bash(rm", "Bash(sh", "Bash(bash", "Bash(python", "Bash(curl"))])

    def test_guard_still_stops_bypasses_through_the_new_rules(self):
        for cmd in ("git -C beta push --no-verify origin sess-a", "git -C beta -c core.hooksPath=/x push", "cd beta && git --no-verif push"):
            self.assertEqual(bash_guard.decide({"tool_name": "Bash", "tool_input": {"command": cmd}})[0], "deny", cmd)


class PostAllowTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.w.cfg.hub["post_command"] = "/usr/bin/python3 -m ga --config /w/ga.json --ga-dir /w/.ga post --channel {session} --from {session} <보고 파일>"

    def test_hub_passes_its_own_report_command_only(self):
        rule = post_allow(self.w.cfg, "A")
        self.assertEqual(rule, ["Bash(/usr/bin/python3 -m ga --config /w/ga.json --ga-dir /w/.ga post --channel A --from A:*)"])
        self.assertEqual(self.w.hub.sandbox_paths("A")["allow"], rule)

    def test_both_runners_add_it(self):
        stub = Stub([{"do": "ok"}])
        self.addCleanup(stub.close)
        perms = {"allow": post_allow(self.w.cfg, "A")}
        argv = HeadlessRunner(stub.dir / "home", executable=stub.exe).argv(TurnRequest("A", "x", stub.dir, permissions=perms))
        self.assertIn(perms["allow"][0], argv[argv.index("--allowedTools") + 1])
        rec = []
        r = AgentSDKRunner(stub.dir / "home2", executable=stub.exe, sdk=fake_sdk([], rec), sandbox="off")
        r.run_turn(TurnRequest("A", "x", stub.dir, permissions=perms))
        self.assertIn(perms["allow"][0], rec[0]["options"].allowed_tools)
        self.assertIn("Bash(ls:*)", rec[0]["options"].allowed_tools)


class TurnPromptTest(unittest.TestCase):
    def test_how_to_work_and_always_report(self):
        w = World()
        self.addCleanup(w.close)
        text = turn_prompt(w.cfg, "A", "```ga\n{}\n```\n")
        for s in ("## 일하는 방법 (ga)", "Write", "git -C <저장소>", "push 하지 않는다", "./alpha (브랜치 sess-a)",
                  "보고는 반드시 올린다", "## Blocker", "같은 명령을 되풀이하지 않는다"):
            self.assertIn(s, text)

    def test_worktree_mode_says_push_own_branch(self):
        w = World(isolation="worktree")
        self.addCleanup(w.close)
        self.assertIn("자기 브랜치만 push 한다", turn_prompt(w.cfg, "A", "x"))


if __name__ == "__main__":
    unittest.main()
