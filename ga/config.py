"""Hub configuration (JSON). Nothing repository-, session- or branch-specific lives in code; it all comes from here.

Shape (``schema: ga-config/1``)::

    {
      "schema": "ga-config/1",
      "hub": {"name", "repo", "session_id", "guidance", "session_guidance",
              "worker_head", "worker_tail", "wake"},
      "integration_branch": "...",
      "repos": {"<name>": {"path", "remote", "src", "test": [...], "env", "package", "extras", "url", "slug"}},
      "sessions": {"<name>": {"prefix", "tag", "branch", "branches", "repos", "channel",
                              "session_id", "first_directive"}},
      "ownership": [{"repo", "path", "session"}],        # first match wins
      "budget": {"llm_runs": 6, ...},
      "rules": {"raise": ["R7", ...]},
      "secret_patterns": ["..."],                         # added to the built-in ones
      "bundle": {"pip_args": ["--no-index"], "timeout": 1800}
    }
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .forms import HARD, FormError, Problem

DEFAULT_WORKER_HEAD = (
    "넌 이제부터 {name} 세션이야. commit된 결과는 <{tag}> 태그를 붙여서 <{hub_repo}> 레포로 보내. "
    "{hub_name}에서 판단할거야."
)
DEFAULT_WORKER_TAIL = "{hub_name}이랑 통신 시작해라"
DEFAULT_WAKE = "[{tag}] 보고 {link}"

RULE_IDS = ("R1", "R1b", *(f"R{i}" for i in range(2, 14)))
HARD_RULES = ("R1b", "R2", "R3", "R4", "R5", "R6", "R9", "R12")


@dataclass
class Repo:
    name: str
    path: str = ""  # local git repository, relative to the config file (METHOD §4.3)
    remote: str = ""  # e.g. "origin"; empty = no remote, worktrees share the repository's refs
    src: str = "."  # what goes on PYTHONPATH, relative to a checkout
    test: list[str] = field(default_factory=list)  # argv; "{python}" is replaced by the bundle's interpreter
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    slug: str = ""  # owner/name, for user-facing commands
    package: str = ""  # distribution name for Bundle (b); empty = not installable
    extras: str = ""


@dataclass
class Session:
    name: str
    prefix: str
    branch: str
    repos: list[str]
    tag: str = ""
    branches: dict[str, str] = field(default_factory=dict)
    channel: str = ""
    session_id: str = ""
    first_directive: str = ""

    def branch_for(self, repo: str) -> str:
        return self.branches.get(repo, self.branch)


@dataclass
class Config:
    hub: dict[str, Any]
    integration_branch: str
    repos: dict[str, Repo]
    sessions: dict[str, Session]
    ownership: list[dict[str, str]]
    budget: dict[str, float] = field(default_factory=dict)
    raised_rules: tuple[str, ...] = ()
    secret_patterns: list[str] = field(default_factory=list)
    bundle: dict[str, Any] = field(default_factory=dict)  # {"pip_args": [...], "timeout": seconds}
    judge: dict[str, Any] = field(default_factory=dict)  # {"kind": "file" | "llm", "model", "max_runs", ...}
    runner: dict[str, Any] = field(default_factory=dict)  # {"kind": "manual" | "headless", "model", "timeout", "max_budget_usd", ...}
    base_dir: Path = Path(".")

    # ------------------------------------------------------------------ lookups

    @property
    def hub_name(self) -> str:
        return self.hub["name"]

    def strength(self, rule: str) -> str:
        return HARD if rule in HARD_RULES or rule in self.raised_rules else "soft"

    def owner_of(self, repo: str, path: str) -> str | None:
        """Owning session of a file: first matching ownership row wins."""
        for row in self.ownership:
            if row["repo"] == repo and fnmatch.fnmatchcase(path, row["path"]):
                return row["session"]
        return None

    def ownership_rows(self, session: str) -> list[dict[str, str]]:
        return [r for r in self.ownership if r["session"] == session]

    def session_by_prefix(self, prefix: str) -> Session | None:
        return next((s for s in self.sessions.values() if s.prefix == prefix), None)

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    def read_text(self, rel: str) -> str:
        return self.resolve(rel).read_text(encoding="utf-8")

    def sessions_of_repo(self, repo: str) -> list[Session]:
        return [s for s in self.sessions.values() if repo in s.repos]


def problems_of(raw: Any) -> list[Problem]:
    """Validate a raw config dict. Every problem here is hard."""
    probs: list[Problem] = []

    def bad(path: str, msg: str) -> None:
        probs.append(Problem(path, msg, HARD))

    if not isinstance(raw, dict) or raw.get("schema") != "ga-config/1":
        return [Problem("$.schema", "must be ga-config/1")]
    hub = raw.get("hub")
    if not isinstance(hub, dict) or not isinstance(hub.get("name"), str) or not hub.get("name"):
        bad("$.hub.name", "is required")
        hub = {}
    if not isinstance(raw.get("integration_branch"), str) or not raw.get("integration_branch"):
        bad("$.integration_branch", "is required")
    repos = raw.get("repos") or {}
    sessions = raw.get("sessions") or {}
    if not isinstance(repos, dict):
        bad("$.repos", "must be an object")
        repos = {}
    if not isinstance(sessions, dict):
        bad("$.sessions", "must be an object")
        sessions = {}
    for name, r in repos.items():
        if not isinstance(r, dict):
            bad(f"$.repos.{name}", "must be an object")
        elif "test" in r and not (isinstance(r["test"], list) and all(isinstance(x, str) for x in r["test"])):
            bad(f"$.repos.{name}.test", "must be an argv list")
    prefixes: dict[str, str] = {}
    for name, s in sessions.items():
        if not isinstance(s, dict):
            bad(f"$.sessions.{name}", "must be an object")
            continue
        for k in ("prefix", "branch"):
            if not isinstance(s.get(k), str) or not s.get(k):
                bad(f"$.sessions.{name}.{k}", "is required")
        pre = s.get("prefix")
        if isinstance(pre, str) and pre:
            if not pre.isalpha() or not pre.isupper():
                bad(f"$.sessions.{name}.prefix", "must be upper-case letters")
            if pre in prefixes:
                bad(f"$.sessions.{name}.prefix", f"duplicates the prefix of session {prefixes[pre]}")
            prefixes[pre] = name
        for rn in s.get("repos", []):
            if rn not in repos:
                bad(f"$.sessions.{name}.repos", f"unknown repo {rn}")
        if name == hub.get("name"):
            bad(f"$.sessions.{name}", "a worker session cannot share the hub's name")
    owners = set(sessions) | {hub.get("name")}
    seen: dict[tuple[str, str], str] = {}
    for i, row in enumerate(raw.get("ownership", [])):
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ("repo", "path", "session")):
            bad(f"$.ownership[{i}]", "needs repo, path, session")
            continue
        if row["repo"] not in repos:
            bad(f"$.ownership[{i}].repo", f"unknown repo {row['repo']}")
        if row["session"] not in owners:
            bad(f"$.ownership[{i}].session", f"unknown session {row['session']}")
        key = (row["repo"], row["path"])
        if key in seen and seen[key] != row["session"]:
            bad(f"$.ownership[{i}]", f"{row['repo']} {row['path']} is already owned by {seen[key]} (one file, one owner)")
        seen.setdefault(key, row["session"])
    rules = raw.get("rules", {})
    if not isinstance(rules, dict):
        bad("$.rules", "must be an object")
        rules = {}
    for k in rules:
        if k != "raise":
            bad(f"$.rules.{k}", "only 'raise' is allowed; rules can be raised to hard, never lowered")
    for rid in rules.get("raise", []):
        if rid not in RULE_IDS:
            bad("$.rules.raise", f"unknown rule {rid}")
    known = {"schema", "hub", "integration_branch", "repos", "sessions", "ownership", "budget", "rules", "secret_patterns", "bundle", "runner", "judge"}
    for k in raw:
        if k not in known:
            bad(f"$.{k}", "unknown key")
    budget = raw.get("budget", {})
    if not isinstance(budget, dict) or any(isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 for v in budget.values()):
        bad("$.budget", "must map names to non-negative numbers")
    return probs


def from_dict(raw: dict[str, Any], base_dir: str | Path = ".") -> Config:
    probs = problems_of(raw)
    if probs:
        raise FormError(probs)
    repos = {n: Repo(name=n, **r) for n, r in raw["repos"].items()}
    sessions = {}
    for n, s in raw["sessions"].items():
        s = dict(s)
        s.setdefault("tag", s["prefix"])
        s.setdefault("repos", [])
        sessions[n] = Session(name=n, **s)
    hub = dict(raw["hub"])
    hub.setdefault("worker_head", DEFAULT_WORKER_HEAD)
    hub.setdefault("worker_tail", DEFAULT_WORKER_TAIL)
    hub.setdefault("wake", DEFAULT_WAKE)
    hub.setdefault("repo", hub["name"])
    return Config(
        hub=hub,
        integration_branch=raw["integration_branch"],
        repos=repos,
        sessions=sessions,
        ownership=list(raw.get("ownership", [])),
        budget=dict(raw.get("budget", {})),
        raised_rules=tuple(raw.get("rules", {}).get("raise", [])),
        secret_patterns=list(raw.get("secret_patterns", [])),
        bundle=dict(raw.get("bundle", {})),
        runner=dict(raw.get("runner", {})),
        judge=dict(raw.get("judge", {})),
        base_dir=Path(base_dir),
    )


def load(path: str | Path) -> Config:
    p = Path(path)
    return from_dict(json.loads(p.read_text(encoding="utf-8")), p.parent)
