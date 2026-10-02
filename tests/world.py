"""A hermetic local world for loop tests: git repos, a hub, two worker sessions, no network.

The person's part (pasting a prompt into a session, the session doing the work and reporting)
is played by functions here.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from ga import config as gacfg
from ga.adapters.base import JudgeContext, TurnRequest, TurnResult
from ga.adapters.git import GitVcs, git
from ga.adapters.human import CallableJudge
from ga.adapters.mailbox import FileMailbox
from ga.adapters.runner import ManualRunner
from ga.adapters.venv import VenvBundle, file_url
from ga.forms import dump_text
from ga.hub import Hub

GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "ga-test", "GIT_AUTHOR_EMAIL": "ga@test",
    "GIT_COMMITTER_NAME": "ga-test", "GIT_COMMITTER_EMAIL": "ga@test",
}


def isolate_git(tmp: Path) -> None:
    """Point git at an empty global config (no signing, no push negotiation) for this process and children."""
    empty = tmp / "gitconfig"
    empty.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    os.environ.update(GIT_ENV)
    os.environ["GIT_CONFIG_GLOBAL"] = str(empty)


TEST_OK = "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
TEST_SKIP = TEST_OK + "\n    @unittest.skip('needs install metadata')\n    def test_skip(self):\n        pass\n"
TEST_FAIL = TEST_OK + "\n    def test_bad(self):\n        self.assertEqual(1, 2)\n"


def pyproject(name: str, deps: list[str] | None = None) -> str:
    dep = ", ".join(f'"{d}"' for d in deps or [])
    return (
        f'[project]\nname = "{name}"\nversion = "0.1"\ndependencies = [{dep}]\n'
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
        f'[tool.setuptools]\npackages = ["{name}"]\n'
    )


class World:
    INTEG = "integ"

    def __init__(self, judge_fn: Callable[[JudgeContext], dict[str, Any]] | None = None, remote: bool = False,
                 modes: tuple[str, ...] = ("path",), runner=None, budget: dict | None = None, shared_alpha: bool = False,
                 beta_deps: list[str] | None = None):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="ga-world-")
        self.tmp = Path(self.tmp_obj.name)
        isolate_git(self.tmp)
        self.remote = remote
        self.repos = {}
        self.repos["alpha"] = self._make_repo("alpha", "alphapkg")
        self.repos["beta"] = self._make_repo("beta", "betapkg", beta_deps(self) if callable(beta_deps) else beta_deps)
        raw = {
            "schema": "ga-config/1",
            "hub": {"name": "hub", "repo": "local/hub", "guidance": "G.md", "session_guidance": "SG.md"},
            "integration_branch": self.INTEG,
            "repos": {
                n: {"path": f"repos/{n}", "remote": "origin" if remote else "", "test": ["{python}", "-m", "unittest", "discover", "-s", "tests"],
                    "package": f"{n}pkg"}
                for n in self.repos
            },
            "sessions": {
                "A": {"prefix": "A", "branch": "sess-a", "repos": ["alpha"], "channel": "mailbox:A"},
                "B": {"prefix": "B", "branch": "sess-b", "repos": ["beta", "alpha"] if shared_alpha else ["beta"], "channel": "mailbox:B"},
            },
            "ownership": [
                {"repo": "alpha", "path": "B_OWNS.txt", "session": "B"},
                {"repo": "alpha", "path": "README.md", "session": "A"},
                {"repo": "alpha", "path": "pyproject.toml", "session": "A"},
                {"repo": "alpha", "path": "alphapkg/*", "session": "A"},
                {"repo": "alpha", "path": "tests/*", "session": "A"},
                {"repo": "alpha", "path": "*.txt", "session": "A"},
                {"repo": "beta", "path": "*", "session": "B"},
            ],
            "budget": budget or {"runs": 50},
            "bundle": {"pip_args": ["--no-index"], "timeout": 600},
        }
        (self.tmp / "G.md").write_text("허브 안내\n", encoding="utf-8")
        (self.tmp / "SG.md").write_text("세션 안내\n", encoding="utf-8")
        self.cfg = gacfg.from_dict(raw, self.tmp)
        self.ga = self.tmp / ".ga"
        self.mail = FileMailbox(self.ga / "mailbox")
        self.vcs = GitVcs(self.cfg, self.ga)
        self.out = io.StringIO()
        self.runner = runner or ManualRunner(self.ga / "outbox", out=self.out)
        self.proposals: list[dict[str, Any]] = []
        self.contexts: list[JudgeContext] = []
        self.judge_fn = judge_fn or self._scripted
        self.hub = Hub(self.cfg, ga_dir=self.ga, channel=self.mail, vcs=self.vcs, judge=CallableJudge(self._judge),
                       runner=self.runner, bundle=VenvBundle(self.cfg, self.vcs, self.ga / "bundle"), modes=modes,
                       today=lambda: "2026-10-02")

    # ------------------------------------------------------------------ setup

    def _make_repo(self, name: str, pkg: str, deps: list[str] | None = None) -> Path:
        d = self.tmp / "repos" / name
        d.mkdir(parents=True)
        git(d, "init", "--quiet", "-b", "main")
        (d / pkg).mkdir()
        (d / pkg / "__init__.py").write_text("V = 1\n", encoding="utf-8")
        (d / "tests").mkdir()
        (d / "tests" / "test_x.py").write_text(TEST_OK, encoding="utf-8")
        (d / "pyproject.toml").write_text(pyproject(pkg, deps), encoding="utf-8")
        (d / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        git(d, "add", "-A")
        git(d, "commit", "--quiet", "-m", "init")
        git(d, "branch", self.INTEG)
        if self.remote:
            bare = self.tmp / "remotes" / f"{name}.git"
            bare.parent.mkdir(parents=True, exist_ok=True)
            git(self.tmp, "init", "--quiet", "--bare", str(bare))
            git(d, "remote", "add", "origin", str(bare))
            git(d, "push", "--quiet", "origin", "main", self.INTEG)
            git(d, "fetch", "--quiet", "origin")
        return d

    def close(self):
        self.tmp_obj.cleanup()

    # ------------------------------------------------------------------ judge

    def _scripted(self, ctx: JudgeContext) -> dict[str, Any]:
        if self.proposals:
            return self.proposals.pop(0)
        return proposal("success", "wait", "할 일 없음")

    def _judge(self, ctx: JudgeContext) -> dict[str, Any]:
        self.contexts.append(ctx)
        return self.judge_fn(ctx)

    # ------------------------------------------------------------------ the person's part

    def paste(self, session: str) -> str:
        """The person takes the oldest prompt for a session from the outbox (manual runner)."""
        pending = self.runner.pending(session)
        assert pending, f"no prompt for {session}"
        text = pending[0].read_text(encoding="utf-8")
        pending[0].unlink()
        return text

    def work(self, session: str, repo: str, files: dict[str, str], msg: str = "work") -> str:
        """The session commits in its worktree (and pushes its own branch when there is a remote)."""
        wt = self.vcs.ensure_session_worktree(session, repo)
        for rel, text in files.items():
            p = wt / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "--quiet", "-m", msg)
        if self.remote:
            git(wt, "push", "--quiet", "origin", self.cfg.sessions[session].branch_for(repo))
        return git(wt, "rev-parse", "HEAD")

    def report(self, session: str, handled: list[tuple[str, Any, str]], commits: list[tuple[str, str]] = (),
               body: str = "## Result\n했다\n", **head_extra) -> None:
        head = {
            "schema": "report/1",
            "from": session,
            "handled": [{"id": i, "rev_seen": r, "status": s} for i, r, s in handled],
            **({"commits": [{"repo": r, "branch": self.cfg.sessions[session].branch_for(r), "sha": sha} for r, sha in commits]} if commits else {}),
            **head_extra,
        }
        self.mail.post(session, session, dump_text(head, body))

    # ------------------------------------------------------------------ inspection

    def integ(self, repo: str) -> str:
        return self.vcs.integration_head(repo)

    def snapshot(self) -> dict[str, bytes]:
        out = {}
        for p in sorted(self.tmp.rglob("*")):
            if p.is_file():
                out[str(p.relative_to(self.tmp))] = p.read_bytes()
        return out


def directive(id_: str, to: str, rev: int = 1, **extra) -> dict[str, Any]:
    d = {"schema": "directive/1", "id": id_, "rev": rev, "to": to, "goal": f"{id_} 의 목표", "why": "시험",
         "scope": "자기 저장소", "done_when": "시험이 초록"}
    d.update(extra)
    return d


def proposal(cls: str, next_: str, reason: str, *, directive_=None, cause=None, **extra) -> dict[str, Any]:
    v = {"schema": "verdict/1", "class": cls, "evidence": {"heads": {}, "tests": {}}, "claims_vs_evidence": [],
         "next": {"choice": next_, "reason": reason}}
    if cause:
        v["cause"] = cause
    return {"verdict": v, "summary": reason, "directive": directive_, **extra}


class FakeHeadlessRunner:
    """Stands in for a 2nd-edition headless runner: knows the turn ended, returns a session id and a cost."""

    kind = "headless"

    def __init__(self, act: Callable[[TurnRequest], None]):
        self.act = act
        self.calls: list[TurnRequest] = []

    def run_turn(self, req: TurnRequest) -> TurnResult:
        self.calls.append(req)
        self.act(req)
        return TurnResult(ended=True, session_id=f"sess-{req.session}", cost=0.25)


class FakeRemoteRunner:
    """Stands in for a remote-session runner: sends a message, cannot know when the turn ends, no cost."""

    kind = "remote"

    def __init__(self):
        self.sent: list[TurnRequest] = []

    def run_turn(self, req: TurnRequest) -> TurnResult:
        self.sent.append(req)
        return TurnResult(ended=None)
