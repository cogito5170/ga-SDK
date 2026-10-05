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
import re
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


PRE_RECEIVE = """#!/bin/sh
# ga-SDK pre-receive hook (METHOD R3, receiving side) for repository {repo}. Installed by ga; edit the ga config instead.
# Unlike pre-push this runs on the remote, so `git push --no-verify` does not skip it.
# The pusher is named by GA_SESSION, which ga sets per session worktree (remote.<name>.receivepack) and for the hub's own push.
who="${{GA_SESSION:-}}"
integ='refs/heads/{integ}'
own_ref() {{
  case "$1" in
{own_cases}
    *) echo "" ;;
  esac
}}
managed() {{
  case "$1" in
{managed_cases}
  esac
  return 1
}}
status=0
refuse() {{
  echo "ga R3: $1" >&2
  # audit trail in the remote (one line per refusal; no content)
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) who=${{who:-?}} ref=$ref $1" >> ga-refused.log 2>/dev/null
  status=1
}}
while read old new ref; do
  if expr "$new" : '0*$' >/dev/null; then
    if [ -n "$who" ] || managed "$ref"; then
      refuse "deleting $ref is refused"; continue
    fi
  fi
  if [ "$who" = '{hub}' ]; then
    if [ "$ref" != "$integ" ]; then refuse "the hub pushes only $integ, not $ref"; continue; fi
  elif [ -n "$who" ]; then
    allowed=$(own_ref "$who")
    if [ -z "$allowed" ]; then refuse "unknown pusher $who"; continue; fi
    if [ "$ref" != "$allowed" ]; then refuse "session $who may push only to $allowed, not $ref"; continue; fi
  elif managed "$ref"; then
    refuse "$ref is managed by ga; push it from its session worktree"; continue
  fi
  if ! expr "$old" : '0*$' >/dev/null && ! expr "$new" : '0*$' >/dev/null; then
    if ! git merge-base --is-ancestor "$old" "$new" 2>/dev/null; then
      refuse "non-fast-forward (force) push to $ref is refused"
    fi
  fi
done
exit $status
"""

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _receivepack(who: str) -> str:
    return f"GA_SESSION={who} git-receive-pack"


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


def is_ancestor(rd: str | Path, a: str, b: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=str(rd), capture_output=True).returncode == 0


def changed_files(rd: str | Path, base: str, head: str) -> list[str]:
    return [l for l in git(rd, "diff", "--name-only", "--no-renames", base, head).splitlines() if l]


def checked_out_at(rd: str | Path, branch: str) -> Path | None:
    """The worktree that has ``branch`` checked out, else None."""
    path = None
    for line in git(rd, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(os.path.realpath(line[len("worktree "):]))  # GA41 S5: a real path, as callers compare it
        elif line == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None


def fast_forward_local(rd: str | Path, branch: str, new: str) -> str:
    """Move the local ``branch`` to ``new`` only as a fast-forward (R4): a checked-out branch by ``merge --ff-only``,
    else by a compare-and-swap ``update-ref``. Never forces."""
    local_old = git(rd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False) or None
    if local_old is not None and not is_ancestor(rd, local_old, new):
        raise GitError(f"{new[:7]} is not a fast-forward of {branch}@{local_old[:7]}")
    wt = checked_out_at(rd, branch)
    if wt is not None:
        git(wt, "merge", "--ff-only", "--quiet", new)
    else:
        git(rd, "update-ref", f"refs/heads/{branch}", new, local_old or "0" * 40)
    return new


def add_worktree(rd: str | Path, wt: Path, branch: str, start: str | None) -> Path:
    """A worktree at ``wt`` on ``branch``: the existing branch, or a new one from ``start``."""
    if wt.exists():
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)
    if git(rd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
        git(rd, "worktree", "add", "--quiet", str(wt), branch)
    else:
        if start is None:
            raise GitError(f"{rd}: no start for new branch {branch}")
        git(rd, "worktree", "add", "--quiet", "-b", branch, str(wt), start)
    return wt


def install_pre_push_hook(rd: str | Path, wt: Path, hooks: Path, who: str, repo: str, branch: str) -> Path:
    """The R3 pre-push hook in ``hooks``, made the worktree's own ``core.hooksPath`` (per-worktree config)."""
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(PRE_PUSH.format(session=who, repo=repo, ref=f"refs/heads/{branch}"), encoding="utf-8")
    hook.chmod(0o755)
    git(rd, "config", "extensions.worktreeConfig", "true")
    git(wt, "config", "--worktree", "core.hooksPath", str(hooks.resolve()))
    return hook


class GitVcs:
    def __init__(self, cfg: Config, ga_dir: str | Path):
        self.cfg = cfg
        # absolute: session clones are made with another working directory than this process's (GA10 F4)
        self.ga_dir = Path(os.path.realpath(ga_dir))  # GA41 S5

    # ------------------------------------------------------------------ basics

    def repo_dir(self, repo: str) -> Path:
        return self.cfg.resolve(self.cfg.repos[repo].path)

    def _remote(self, repo: str) -> str:
        return self.cfg.repos[repo].remote

    def remote_url(self, repo: str) -> str:
        return git(self.repo_dir(repo), "remote", "get-url", self._remote(repo))

    @property
    def cloned(self) -> bool:
        return self.cfg.isolation == "clone"

    def session_ref(self, repo: str, session: str) -> str:
        """Where the hub keeps what it fetched from a session's own clone (clone isolation)."""
        return f"refs/ga/sessions/{session}/{self.cfg.sessions[session].branch_for(repo)}"

    def fetch(self, repo: str) -> None:
        if self._remote(repo):
            git(self.repo_dir(repo), "fetch", "--quiet", "--prune", self._remote(repo))
        if self.cloned:
            # pull, never push: the hub reads each session's own branch from that session's own clone, so who
            # wrote a branch is where the hub fetched it from — nothing the session can claim or forge
            for s in self.cfg.sessions_of_repo(repo):
                ws = self.session_worktree(s.name, repo)
                if (ws / ".git").is_dir():
                    branch = s.branch_for(repo)
                    git(self.repo_dir(repo), "-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-tags",
                        "--no-write-fetch-head", str(ws), f"+refs/heads/{branch}:{self.session_ref(repo, s.name)}", check=False)

    def ref(self, repo: str, branch: str) -> str | None:
        """Head of a branch as the hub sees it (remote-tracking when there is a remote)."""
        name = f"refs/remotes/{self._remote(repo)}/{branch}" if self._remote(repo) else f"refs/heads/{branch}"
        out = git(self.repo_dir(repo), "rev-parse", "--verify", "--quiet", name + "^{commit}", check=False)
        return out or None

    def resolve(self, repo: str, rev: str) -> str | None:
        """Full sha of a (possibly short) commit id, or None if the repository does not have it."""
        return git(self.repo_dir(repo), "rev-parse", "--verify", "--quiet", rev + "^{commit}", check=False) or None

    def integration_head(self, repo: str) -> str | None:
        if self._remote(repo) and not self.cfg.repos[repo].push:  # kept local: the remote's copy is not the hub's
            return git(self.repo_dir(repo), "rev-parse", "--verify", "--quiet",
                       f"refs/heads/{self.cfg.integration_branch}^{{commit}}", check=False) or None
        return self.ref(repo, self.cfg.integration_branch)

    def session_head(self, repo: str, session: str) -> str | None:
        if self.cloned:
            return git(self.repo_dir(repo), "rev-parse", "--verify", "--quiet", self.session_ref(repo, session) + "^{commit}", check=False) or None
        return self.ref(repo, self.cfg.sessions[session].branch_for(repo))

    def is_ancestor(self, repo: str, a: str, b: str) -> bool:
        return is_ancestor(self.repo_dir(repo), a, b)

    def changed_files(self, repo: str, base: str, head: str) -> list[str]:
        return changed_files(self.repo_dir(repo), base, head)

    def added_lines(self, repo: str, base: str, head: str) -> str:
        """Lines added between base and head (what R6 scans)."""
        out = git(self.repo_dir(repo), "diff", "--unified=0", "--no-color", base, head)
        return "\n".join(l[1:] for l in out.splitlines() if l.startswith("+") and not l.startswith("+++"))

    def commit_subjects(self, repo: str, base: str, head: str) -> list[str]:
        out = git(self.repo_dir(repo), "log", "--format=%s", f"{base}..{head}")
        return [l for l in out.splitlines() if l]

    def packaged(self, repo: str, sha: str) -> bool:
        """Whether the tree at ``sha`` is an installable Python package (pyproject.toml or setup.py / setup.cfg)."""
        return any(subprocess.run(["git", "cat-file", "-e", f"{sha}:{name}"], cwd=str(self.repo_dir(repo)), capture_output=True).returncode == 0
                   for name in ("pyproject.toml", "setup.py", "setup.cfg"))

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
        return checked_out_at(self.repo_dir(repo), branch)

    def fast_forward(self, repo: str, new: str) -> str:
        """Move the integration branch to ``new`` (must be a fast-forward) and push it if there is a remote
        (unless the repository says ``push: false``)."""
        branch = self.cfg.integration_branch
        rd = self.repo_dir(repo)
        old = self.integration_head(repo)
        if old and not self.is_ancestor(repo, old, new):
            raise GitError(f"{repo}: {new[:7]} is not a fast-forward of {branch}@{old[:7]}")
        remote = self._remote(repo) if self.cfg.repos[repo].push else ""
        if remote:
            # no force: the remote refuses a non-fast-forward by itself; the hub names itself to the pre-receive hook
            git(rd, "-c", "push.negotiate=false", "push", "--quiet", f"--receive-pack={_receivepack(self.cfg.hub_name)}",
                remote, f"{new}:refs/heads/{branch}")
            git(rd, "fetch", "--quiet", remote)
        local_old = git(rd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False) or None
        if local_old is None or self.is_ancestor(repo, local_old, new):
            fast_forward_local(rd, branch, new)
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
        if self.cloned:
            return self._ensure_session_clone(session, repo, branch, wt)
        if not wt.exists():
            has_local = git(rd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
            start = None if has_local else (self.ref(repo, branch) or self.integration_head(repo))
            if not has_local and start is None:
                raise GitError(f"{repo}: neither {branch} nor {self.cfg.integration_branch} exists")
            add_worktree(rd, wt, branch, start)
        self.install_pre_push(session, repo)
        if self._remote(repo):
            # every push from this worktree names its session to the remote's pre-receive hook
            git(wt, "config", "--worktree", f"remote.{self._remote(repo)}.receivepack", _receivepack(session))
        return wt

    def _ensure_session_clone(self, session: str, repo: str, branch: str, ws: Path) -> Path:
        """An independent clone (no shared refs, no hardlinked objects) on the session's branch. Its origin is the
        hub's repository, for reading the integration branch; the session never needs to push."""
        if (ws / ".git").is_dir():
            return ws
        ws.parent.mkdir(parents=True, exist_ok=True)
        rd = self.repo_dir(repo)
        git(ws.parent, "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(rd), str(ws))
        start = self.session_head(repo, session)
        if start:  # a clone made again for a session the hub already knows: continue its branch
            git(ws, "fetch", "--quiet", "origin", f"+{self.session_ref(repo, session)}:refs/heads/{branch}")
            git(ws, "checkout", "--quiet", branch)
        else:
            ih = self.integration_head(repo)
            if ih is None:
                raise GitError(f"{repo}: {self.cfg.integration_branch} does not exist")
            git(ws, "checkout", "--quiet", "-b", branch, ih)
        return ws

    def local_remote_dir(self, repo: str) -> Path | None:
        """The remote's directory when the remote is a local (bare) repository, else None."""
        if not self._remote(repo):
            return None
        url = self.remote_url(repo)
        if url.startswith("file://"):
            url = url[len("file://"):]
            url = url[len("localhost"):] if url.startswith("localhost/") else url
        elif "://" in url or re.match(r"^[\w.-]+@[\w.-]+:", url):
            return None
        p = Path(url)
        p = p if p.is_absolute() else (self.repo_dir(repo) / p)
        return p.resolve() if p.exists() else None

    def install_pre_receive(self, repo: str) -> Path | None:
        """Install the R3 policy on a local bare remote (METHOD R3, receiving side). None if the remote is not local."""
        remote_dir = self.local_remote_dir(repo)
        if remote_dir is None:
            return None
        names = [self.cfg.hub_name, self.cfg.integration_branch]
        own = {s.name: s.branch_for(repo) for s in self.cfg.sessions_of_repo(repo)}
        for n in names + list(own) + list(own.values()):
            if not SAFE_NAME.match(n):
                raise GitError(f"name {n!r} is not safe to put in a hook")
        own_cases = "\n".join(f"    '{s}') echo 'refs/heads/{b}' ;;" for s, b in own.items())
        refs = [self.cfg.integration_branch, *own.values()]
        managed_cases = "\n".join(f"    'refs/heads/{b}') return 0 ;;" for b in dict.fromkeys(refs))
        hooks = (remote_dir / "hooks") if (remote_dir / "hooks").is_dir() or not (remote_dir / ".git").exists() else remote_dir / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "pre-receive"
        hook.write_text(PRE_RECEIVE.format(repo=repo, integ=self.cfg.integration_branch, hub=self.cfg.hub_name,
                                           own_cases=own_cases, managed_cases=managed_cases), encoding="utf-8")
        hook.chmod(0o755)
        return hook

    def install_pre_push(self, session: str, repo: str) -> Path:
        return install_pre_push_hook(self.repo_dir(repo), self.session_worktree(session, repo),
                                     self.ga_dir / "hooks" / session / repo, session, repo,
                                     self.cfg.sessions[session].branch_for(repo))
