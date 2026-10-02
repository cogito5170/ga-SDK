"""Adapter interfaces (METHOD §4, §4c). Implementations are swappable; 1st edition ships local ones only."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class Post:
    id: str  # sortable, unique within a channel
    channel: str  # the session whose channel this is
    author: str
    at: str  # ISO-8601 UTC
    text: str


class Channel(Protocol):
    """4.1 — one channel per session; one report = one post."""

    def post(self, channel: str, author: str, text: str) -> Post: ...

    def read(self, channel: str, after: str | None = None) -> list[Post]: ...


@dataclass
class TurnRequest:
    session: str
    prompt: str
    workdir: Path
    resume_id: str | None = None
    permissions: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, float] = field(default_factory=dict)


@dataclass
class TurnResult:
    ended: bool | None  # None = the runner cannot know (manual · remote); the next report tells
    session_id: str | None = None
    cost: float | None = None  # None = unknown
    note: str = ""
    error: str = ""  # "" = the turn ran; else e.g. "timeout", "exit 1", "bad_json", "is_error:<subtype>"
    seconds: float | None = None  # wall time of the turn when the runner measured it
    sandboxed: bool | None = None  # True: ran in the OS write sandbox; False: did not; None: the runner cannot say


class Runner(Protocol):
    """4.2 / §4c — runs one turn of a session (this is also how a session is woken)."""

    kind: str

    def run_turn(self, req: TurnRequest) -> TurnResult: ...


@dataclass
class Counts:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        d = {"passed": self.passed, "failed": self.failed, "skipped": self.skipped}
        if self.errors:
            d["errors"] = self.errors
        return d


@dataclass
class RepoRun:
    repo: str
    sha: str
    mode: str  # "path" | "install"
    ok: bool
    counts: Counts | None
    output: str = ""
    problem: str = ""  # e.g. "pin_conflict", "install_failed", "no_test_command", "unparsed_output"


@dataclass
class BundleResult:
    mode: str
    ok: bool
    runs: list[RepoRun] = field(default_factory=list)
    problem: str = ""
    output: str = ""


class Bundle(Protocol):
    """4.4 — run the repos together at given heads: (a) side by side on a path, (b) clean pinned install."""

    def run_path(self, heads: dict[str, str]) -> BundleResult: ...

    def run_install(self, heads: dict[str, str]) -> BundleResult: ...


@dataclass
class JudgeContext:
    round: int
    reports: list[dict[str, Any]]  # [{"post": Post, "head": report/1, "body": str, "notes": [...]}]
    evidence: dict[str, Any]  # machine-made: heads, tests, notes
    machine_class: str | None  # set when rules force a class (blocked, partial-crossed)
    findings: list[Any]  # rule Problems
    open_directives: list[dict[str, Any]]
    answers: list[dict[str, Any]] = field(default_factory=list)  # gate questions the user answered since last round
    exchanges: list[dict[str, Any]] = field(default_factory=list)  # exchange/1 heads: information, never actions (§3.5)


class Judge(Protocol):
    """4.6 — proposes a verdict and the next step with reasons. It never decides gated matters.

    Returns {"verdict": verdict/1, "summary": str, "directive": directive/1 | None,
             "decision": decision/1 | None, "action": str | None}."""

    def propose(self, ctx: JudgeContext) -> dict[str, Any]: ...
