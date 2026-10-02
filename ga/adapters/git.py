"""git Vcs (METHOD §4.3) with local placement (§4c, G9).

- Repositories are local paths from the config. With a ``remote`` the sessions push their branches
  there and the hub fetches; without one, worktrees share the repository's refs, so a session's
  commit on its branch *is* the hand-in.
- Each session gets one worktree per repo under ``<ga>/worktrees/<session>/<repo>`` with a
  pre-push hook (R3) installed through a per-worktree ``core.hooksPath``.
- The hub only fast-forwards the integration branch (R4) and never force-pushes (R3).
"""
from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path

from ..config import Config

PRE_PUSH = """#!/bin/sh
# ga-SDK pre-push hook (METHOD R3) for session {session}, repository {repo}.
# Allowed: fast-forward pushes to {ref} only. Installed by ga; edit the ga config instead.
allowed="{ref}"
status=0
while read local_ref local_sha remote_ref remote_sha; do
  if [ "$remote_ref" != "$allowed" ]; then
    echo "ga R3: session {session} may push only to $allowed, not $remote_ref" >&2
    status=1
    continue
  fi
  if expr "$local_sha" : '0*$' >/dev/null; then
    echo "ga R3: deleting $remote_ref is refused" >&2
    status=1
    continue
  fi
  if ! expr "$remote_sha" : '0*$' >/dev/null; then
    if ! git merge-base --is-ancestor "$remote_sha" "$local_sha" 2>/dev/null; then
      echo "ga R3: non-fast-forward (force) push to $remote_ref is refused" >&2
      status=1
    fi
  fi
done
exit $status
"""


class GitError(RuntimeError):
    pass


def git(cwd: str | Path, *args: str, check: bool = True, input: bytes | None = None, env: dict[str, str] | None = None) -> str:
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, input=input, env=full_env)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} (in {cwd}) failed: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout.decode(errors="replace").strip()


class GitVcs:
    def __init__(self, cfg: Config, ga_dir: str | Path):
        self.cfg = cfg
        self.ga_dir = Path(ga_dir)

    # ------------------------------------------------------------------ basics

    def repo_dir(self, repo: str) -> Path:
        return self.cfg.resolve(self.cfg.repos[repo].path)

    def _remote(self, repo: str) -> str:
        return self.cfg.repos[repo].remote

    def remote_url(self, repo: str) -> str:
        return git(self.repo_dir(repo), "remote", "get-url", self._remote(repo))

    def fetch(self, repo: str) -> None:
        if self._remote(repo):
            git(self.repo_dir(repo), "fetch", "--quiet", "--prune", self._remote(repo))

    def ref(self, repo: str, branch: str) -> str | None:
        """Head of a branch as the hub sees it (remote-tracking when there is a remote)."""
        name = f"refs/remotes/{self._remote(repo)}/{branch}" if self._remote(repo) else f"refs/heads/{branch}"
        out = git(self.repo_dir(repo), "rev-parse", "--verify", "--quiet", name + "^{commit}", check=False)
        return out or None

    def resolve(self, repo: str, rev: str) -> str | None:
        """Full sha of a (possibly short) commit id, or None if the repository does not have it."""
        return git(self.repo_dir(repo), "rev-parse", "--verify", "--quiet", rev + "^{commit}", check=False) or None

    def integration_head(self, repo: str) -> str | None:
        return self.ref(repo, self.cfg.integration_branch)

    def session_head(self, repo: str, session: str) -> str | None:
        return self.ref(repo, self.cfg.sessions[session].branch_for(repo))

    def is_ancestor(self, repo: str, a: str, b: str) -> bool:
        p = subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=str(self.repo_dir(repo)), capture_output=True)
        return p.returncode == 0

    def changed_files(self, repo: str, base: str, head: str) -> list[str]:
        out = git(self.repo_dir(repo), "diff", "--name-only", "--no-renames", base, head)
        return [l for l in out.splitlines() if l]

    def added_lines(self, repo: str, base: str, head: str) -> str:
        """Lines added between base and head (what R6 scans)."""
        out = git(self.repo_dir(repo), "diff", "--unified=0", "--no-color", base, head)
        return "\n".join(l[1:] for l in out.splitlines() if l.startswith("+") and not l.startswith("+++"))

    def commit_subjects(self, repo: str, base: str, head: str) -> list[str]:
        out = git(self.repo_dir(repo), "log", "--format=%s", f"{base}..{head}")
        return [l for l in out.splitlines() if l]

    def export(self, repo: str, sha: str, dest: str | Path) -> Path:
        """Extract the tree of ``sha`` into ``dest`` (no worktree bookkeeping)."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        data = subprocess.run(["git", "archive", "--format=tar", sha], cwd=str(self.repo_dir(repo)), capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            if hasattr(tarfile, "data_filter"):
                tf.extractall(dest, filter="data")
            else:  # Python without extraction filters; the tree is our own repository's
                tf.extractall(dest)
        return dest

    # ------------------------------------------------------------------ hub side

    def _checked_out_at(self, repo: str, branch: str) -> Path | None:
        out = git(self.repo_dir(repo), "worktree", "list", "--porcelain")
        path = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                path = Path(line[len("worktree "):])
            elif line == f"branch refs/heads/{branch}" and path is not None:
                return path
        return None

    def fast_forward(self, repo: str, new: str) -> str:
        """Move the integration branch to ``new`` (must be a fast-forward) and push it if there is a remote."""
        branch = self.cfg.integration_branch
        rd = self.repo_dir(repo)
        old = self.integration_head(repo)
        if old and not self.is_ancestor(repo, old, new):
            raise GitError(f"{repo}: {new[:7]} is not a fast-forward of {branch}@{old[:7]}")
        remote = self._remote(repo)
        if remote:
            # no force: the remote refuses a non-fast-forward by itself
            git(rd, "-c", "push.negotiate=false", "push", "--quiet", remote, f"{new}:refs/heads/{branch}")
            git(rd, "fetch", "--quiet", remote)
        local_old = git(rd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False) or None
        if local_old is None or self.is_ancestor(repo, local_old, new):
            wt = self._checked_out_at(repo, branch)
            if wt is not None:
                git(wt, "merge", "--ff-only", "--quiet", new)
            else:
                args = ["update-ref", f"refs/heads/{branch}", new] + ([local_old] if local_old else ["0" * 40])
                git(rd, *args)
        return new

    # ------------------------------------------------------------------ session side (G9)

    def session_worktree(self, session: str, repo: str) -> Path:
        return self.ga_dir / "worktrees" / session / repo

    def ensure_session_worktree(self, session: str, repo: str) -> Path:
        """Create the session's worktree on its branch (from the integration head if new) and install the R3 hook."""
        s = self.cfg.sessions[session]
        branch = s.branch_for(repo)
        rd = self.repo_dir(repo)
        wt = self.session_worktree(session, repo)
        if not wt.exists():
            wt.parent.mkdir(parents=True, exist_ok=True)
            has_local = git(rd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
            if has_local:
                git(rd, "worktree", "add", "--quiet", str(wt), branch)
            else:
                start = self.ref(repo, branch) or self.integration_head(repo)
                if start is None:
                    raise GitError(f"{repo}: neither {branch} nor {self.cfg.integration_branch} exists")
                git(rd, "worktree", "add", "--quiet", "-b", branch, str(wt), start)
        self.install_pre_push(session, repo)
        return wt

    def install_pre_push(self, session: str, repo: str) -> Path:
        branch = self.cfg.sessions[session].branch_for(repo)
        hooks = self.ga_dir / "hooks" / session / repo
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "pre-push"
        hook.write_text(PRE_PUSH.format(session=session, repo=repo, ref=f"refs/heads/{branch}"), encoding="utf-8")
        hook.chmod(0o755)
        rd = self.repo_dir(repo)
        git(rd, "config", "extensions.worktreeConfig", "true")
        git(self.session_worktree(session, repo), "config", "--worktree", "core.hooksPath", str(hooks.resolve()))
        return hook
