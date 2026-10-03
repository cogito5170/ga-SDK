"""A hermetic hub for ga_rlo tests: one git repository, one session, `ga-rlo init`'s config, the fake ``claude``.

No model call, no network: the Runner is ga's real headless Runner pointed at ``fake_claude.py``.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from ga.rlo import cli

HERE = Path(__file__).resolve().parent
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
TEST_OK = "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def run_cli(*argv: str) -> tuple[int, str, str]:
    """`ga rlo <argv>` -- or `ga <argv>` when the command is ga's own (tick, send, permit ...), as a person would type."""
    from ga.__main__ import main as ga_main

    opts, cmd, _ = cli._split(list(argv))
    main = ga_main if cmd in cli.GA_COMMANDS else cli.main
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(list(argv))
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
    return rc, out.getvalue(), err.getvalue()


class World:
    def __init__(self, model: str = "haiku"):
        self.tmp = Path(tempfile.mkdtemp(prefix="ga-rlo-world-"))
        self._env = {k: os.environ.get(k) for k in [*GIT_ENV, "GIT_CONFIG_GLOBAL", "HOME", "CLAUDE_CONFIG_DIR"]}
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
        os.environ.update(GIT_ENV, GIT_CONFIG_GLOBAL=str(gitconfig))
        self.person = self.tmp / "person"  # the person's HOME: ga_rlo must not touch it
        (self.person / ".claude").mkdir(parents=True)
        os.environ["HOME"] = str(self.person)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        repo = self.tmp / "repos" / "work"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests" / "test_x.py").write_text(TEST_OK, encoding="utf-8")
        (repo / "README.md").write_text("# work\n", encoding="utf-8")
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "init")
        git(repo, "branch", "integration")
        self.repo = repo
        self.hub = self.tmp / "hub"
        self.hub.mkdir()
        self.config = self.hub / "ga.json"
        self.ga = self.hub / ".ga"
        (self.hub / "G.md").write_text("허브 안내\n", encoding="utf-8")
        (self.hub / "SG.md").write_text("세션 안내\n", encoding="utf-8")
        self.init_out = run_cli("--config", str(self.config), "init", "--repo", "work=../repos/work",
                                "--session", "W:W:sess-w:work", "--model", model,
                                "--guidance", "G.md", "--session-guidance", "SG.md")
        fake = self.tmp / "bin" / "claude"
        fake.parent.mkdir()
        fake.write_text(f"#!/bin/sh\nexec {sys.executable} {HERE / 'fake_claude.py'} \"$@\"\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.plan_path = self.tmp / "plan.json"
        # CMD-GR6 S4: the turn imports the same ga as this test process (a source checkout need not be installed:
        # without this, the fake claude and `python -m ga.rlo.hook` cannot import ga in the turn's clean environment)
        import ga

        extra_env = {"GA_RLO_FAKE": str(self.plan_path), "PYTHONPATH": str(Path(ga.__file__).resolve().parent.parent)}
        self.edit(lambda raw: (raw["runner"].update(executable=str(fake), extra_env=extra_env),
                               raw["repos"]["work"].update(test=["{python}", "-m", "unittest", "discover", "-s", "tests"]),
                               raw.update(bundle={"pip_args": ["--no-index"], "timeout": 300})))

    # ------------------------------------------------------------------ config

    def raw(self) -> dict:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def edit(self, fn) -> None:
        raw = self.raw()
        fn(raw)
        self.config.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    def permit(self) -> str:
        """The person's part: `ga permit` with exactly the printed scope, then the id into runner.permission."""
        r = self.raw()["runner"]
        rc, out, err = run_cli("--config", str(self.config), "permit", "--runner", r["kind"], "--model", r["model"],
                               "--sandbox", r["sandbox"], "--budget", "runs=6", "--note", "test: the person said yes")
        assert rc == 0, err
        bd = out.strip().splitlines()[-1]
        self.edit(lambda raw: raw["runner"].update(permission=bd))
        return bd

    def plan(self, tools: list[dict], directive: str = "CMD-W1") -> None:
        self.plan_path.write_text(json.dumps({
            "tools": tools, "repo": "work", "file": "x.txt", "text": "work\n", "mailbox": str(self.ga / "mailbox"),
            "session": "W", "directive": directive, "branch": "sess-w"}), encoding="utf-8")

    def directive_file(self, did: str = "CMD-W1") -> Path:
        from ga.forms import dump_text

        d = {"schema": "directive/1", "id": did, "rev": 1, "to": "W", "goal": "x.txt 를 더한다", "why": "시험",
             "scope": "work 저장소", "done_when": "시험이 초록"}
        p = self.tmp / f"{did}.md"
        p.write_text(dump_text(d, "## 할 일\nx.txt\n"), encoding="utf-8")
        return p

    def judge(self, n: int, cls: str = "success") -> None:
        """The person's judgement for round n (FileJudge)."""
        v = {"schema": "verdict/1", "class": cls, "evidence": {"heads": {}, "tests": {}}, "claims_vs_evidence": [],
             "next": {"choice": "wait", "reason": "시험"}}
        p = self.ga / "judge" / f"round-{n}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"verdict": v, "summary": "가짜 턴 한 바퀴", "directive": None}), encoding="utf-8")

    def state(self) -> dict:
        return json.loads((self.ga / "state.json").read_text(encoding="utf-8"))

    def close(self) -> None:
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)
