"""CMD-GA4: the PreToolUse guard that keeps a headless turn from bypassing the git hooks or forging the push identity."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from ga.adapters import bash_guard
from ga.adapters.base import TurnRequest

from test_headless import Stub

DENY = [
    ("git push --no-verify origin HEAD:refs/heads/sess-a", "no_verify"),
    ("git -C beta push origin HEAD:refs/heads/sess-a --no-verify", "no_verify"),
    ("git -C beta push --receive-pack='GA_SESSION=hub git-receive-pack' origin HEAD:refs/heads/integ", "receive_pack"),
    ("git -C beta push origin HEAD:integ --receive-pack=/bin/sh", "receive_pack"),
    ("git -C beta push --exec=x origin sess-b", "receive_pack"),
    ("git -C beta -c core.hooksPath=/dev/null push origin HEAD:refs/heads/sess-a", "git_c"),
    ("git -c remote.origin.receivepack=x push", "receive_pack"),
    ("git --config-env=core.hooksPath=X push", "git_c"),
    ("git -C beta config remote.origin.url /tmp/x", "git_config"),
    ("git config --worktree core.hooksPath /tmp", "git_config"),
    ("GA_SESSION=hub git -C beta push origin integ", "identity"),
    ("GIT_DIR=/tmp/other git push", "git_env"),
    ("cd beta && GIT_CONFIG_GLOBAL=/x git push", "git_env"),
    ("env -i git push origin sess-a", "git_env"),
    ("git --git-dir=/tmp/other.git push", "other_repo"),
    ("cp evil ../.git/hooks/pre-push", "hook_files"),
    ("a=--no; git push ${a}-verify origin sess-a", "shell_indirection"),
    ("git push $FLAGS origin sess-a", "shell_indirection"),
    ("git push `echo --no``-verify` origin x", "shell_indirection"),
    ("eval 'git push origin sess-a'", "shell_indirection"),
    ("echo Z2l0 | base64 -d | sh", "shell_indirection"),
    ("sh -c 'git push origin x'", "shell_indirection"),
    ("python3 -c 'import os'", "shell_indirection"),
    (". ./evil.sh", "shell_indirection"),
    # baseline's probes (BD-139): quote splitting, abbreviated long options, scripts
    ('g""it -c remote.origin.receivepack="GA_SESSION=hub git-receive-pack" push', "receive_pack"),
    ("'git' -c x=y push", "git_c"),
    ("git push --no-verif origin HEAD:refs/heads/sess-a", "no_verify"),
    ("git push --receive-pac=x origin HEAD:integ", "receive_pack"),
    ("bash x.sh", "script_exec"),
    ("./x.sh", "script_exec"),
    ("make push", "script_exec"),
    ("python3 tool.py", "script_exec"),
]
ALLOW = [
    "git -C beta add -A",
    'git -C beta commit -m "update env docs"',
    "git -C beta push origin sess-b",
    "git -C beta push origin HEAD:refs/heads/sess-b",
    "git -C beta status",
    "git -C beta rev-parse HEAD",
    "python3 -m unittest discover -s tests",
    "/usr/local/bin/python3 -m ga --config /w/ga.json post --channel B --from B report.md",
]


def run_hook(event, log=None):
    argv = [sys.executable, bash_guard.__file__] + (["--log", str(log)] if log else [])
    p = subprocess.run(argv, input=event if isinstance(event, str) else json.dumps(event), capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout)


class GuardRulesTest(unittest.TestCase):
    def test_denied_commands(self):
        for cmd, rule in DENY:
            with self.subTest(cmd=cmd):
                self.assertEqual(bash_guard.decide({"tool_name": "Bash", "tool_input": {"command": cmd}}), ("deny", rule))

    def test_allowed_commands(self):
        for cmd in ALLOW:
            with self.subTest(cmd=cmd):
                self.assertEqual(bash_guard.decide({"tool_name": "Bash", "tool_input": {"command": cmd}}), ("allow", ""))

    def test_file_tools(self):
        for path, want in (("/w/A/alpha/notes/a.txt", "allow"), ("/w/A/report.md", "allow"),
                           ("/repo/.git/config", "deny"), ("/repo/.git/worktrees/alpha/config.worktree", "deny"),
                           ("/remotes/beta.git/hooks/pre-receive", "deny"), ("/w/A/alpha/.git", "deny")):
            with self.subTest(path=path):
                self.assertEqual(bash_guard.decide({"tool_name": "Write", "tool_input": {"file_path": path}})[0], want)
        self.assertEqual(bash_guard.decide({"tool_name": "Read", "tool_input": {"file_path": "/repo/.git/config"}})[0], "allow")


class GuardProtocolTest(unittest.TestCase):
    def test_hook_protocol_and_log(self):
        log = Path(self.id().replace(".", "_") + ".jsonl")
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "guard.jsonl"
            rc, out = run_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                "tool_input": {"command": "git push --no-verify origin x"}}, log)
            self.assertEqual(rc, 0)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(run_hook({"tool_name": "Bash", "tool_input": {"command": "git status"}}, log), (0, {}))
            self.assertEqual(run_hook("not json", log)[1]["hookSpecificOutput"]["permissionDecision"], "deny")  # closing side
            rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(r["decision"], r["rule"]) for r in rows], [("deny", "no_verify"), ("allow", ""), ("deny", "unreadable")])
            self.assertNotIn("origin x", log.read_text(encoding="utf-8"))  # rule names only, never the command

    def test_runner_installs_the_guard_through_settings(self):
        stub = Stub([{"do": "ok"}])
        self.addCleanup(stub.close)
        r = stub.runner()
        r.run_turn(TurnRequest("A", "x", stub.dir))
        argv = stub.calls()[0]["argv"]
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))
        hook = settings["hooks"]["PreToolUse"][0]
        self.assertIn("Bash", hook["matcher"])
        self.assertIn("bash_guard.py", hook["hooks"][0]["command"])
        self.assertTrue(str(Path(argv[argv.index("--settings") + 1])).startswith(str(stub.dir / "home")))  # never the real ~/.claude
        # the installed command really runs and denies
        p = subprocess.run(hook["hooks"][0]["command"], shell=True, capture_output=True, text=True,
                           input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git -c core.hooksPath=/x push"}}))
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        no_guard = stub.runner(guard=False)
        self.assertNotIn("--settings", no_guard.argv(TurnRequest("A", "x", stub.dir)))


if __name__ == "__main__":
    unittest.main()
