"""CMD-GA8: one hub round with a real GitHub issue and a real remote (cloud) session.

The library may not call GitHub or create remote sessions from this environment, so the agent running this
script relays: the hub's Channel is a ``CallbackChannel`` over files, its Runner a ``RemoteSessionRunner`` with an
``OutboxCallback``. Steps (each a separate process; the hub's state is in ``<work>/.ga``):

    ga8_real.py init --work W --issue N --branch BR --base SHA   # local clone, local-only integration branch
    ga8_real.py send --work W        # hub posts CMD-A1 -> W/relay/out/A/*.md, turn -> W/outbox/A/*.json
      (agent: post the .md as a comment on issue N; create the remote session with the .json prompt and
       write its id to W/outbox/A/session_id; when it has reported, write the issue's comments to
       W/relay/in/A.json as [{"id", "body", "login", "created_at"}])
    ga8_real.py tick --work W --out results.json   # hub reads, fetches the remote, integrates locally

Only numbers and labels go into the results file. The integration branch is never pushed (``push: false``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ga import config as gacfg  # noqa: E402
from ga.adapters.base import Post  # noqa: E402
from ga.adapters.git import GitVcs, git  # noqa: E402
from ga.adapters.github import CallbackChannel, split_author, with_author  # noqa: E402
from ga.adapters.human import CallableJudge  # noqa: E402
from ga.adapters.runner import OutboxCallback, RemoteSessionRunner  # noqa: E402
from ga.adapters.venv import VenvBundle  # noqa: E402
from ga.hub import Hub  # noqa: E402

REPO_URL = "https://github.com/cogito5170/ga-SDK"
INTEG = "ga8-integ"


def relay_channel(work: Path) -> CallbackChannel:
    out, inn = work / "relay" / "out", work / "relay" / "in"

    def post(channel: str, author: str, text: str) -> Post:
        d = out / channel
        d.mkdir(parents=True, exist_ok=True)
        pid = f"{time.time_ns():020d}"
        (d / f"{pid}.md").write_text(with_author(text, author), encoding="utf-8")
        return Post(pid, channel, author, "", text)

    def read(channel: str, after: str | None) -> list[Post]:
        f = inn / f"{channel}.json"
        rows = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
        posts = []
        for c in rows:
            pid = f"{int(c['id']):020d}"
            if after is not None and pid <= after:
                continue
            author, text = split_author(c["body"], c.get("login", "?"))
            posts.append(Post(pid, channel, author, c.get("created_at", ""), text))
        return sorted(posts, key=lambda p: p.id)

    return CallbackChannel(post, read)


def hub(work: Path) -> Hub:
    cfg = gacfg.load(work / "ga.json")
    vcs = GitVcs(cfg, work / ".ga")

    def judge(ctx):  # scripted: the machine class is the floor either way; no model turn is spent on judging
        return {"verdict": {"schema": "verdict/1", "class": "success", "evidence": {"heads": {}, "tests": {}},
                            "claims_vs_evidence": [], "next": {"choice": "wait", "reason": "GA8 시연 한 바퀴"}},
                "summary": "GA8 시연", "directive": None}

    return Hub(cfg, ga_dir=work / ".ga", channel=relay_channel(work), vcs=vcs, judge=CallableJudge(judge),
               runner=RemoteSessionRunner(OutboxCallback(work / "outbox")), bundle=VenvBundle(cfg, vcs, work / ".ga" / "bundle"),
               modes=("path",), today=lambda: time.strftime("%Y-%m-%d"))


def cmd_init(a) -> None:
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    sdk = work / "sdk"
    if not sdk.exists():
        git(work, "clone", "--quiet", REPO_URL, str(sdk))
    git(sdk, "fetch", "--quiet", "origin")
    git(sdk, "branch", "-f", INTEG, a.base)
    (work / "G.md").write_text("GA8 시연 허브\n", encoding="utf-8")
    (work / "SG.md").write_text("GA8 시연 세션\n", encoding="utf-8")
    raw = {
        "schema": "ga-config/1",
        "hub": {"name": "hub", "repo": "cogito5170/ga-SDK", "guidance": "G.md", "session_guidance": "SG.md"},
        "integration_branch": INTEG,
        "isolation": "remote",
        "repos": {"sdk": {"path": "sdk", "remote": "origin", "push": False, "slug": "cogito5170/ga-SDK",
                          "test": ["{python}", "-m", "unittest", "discover", "-s", "tests"]}},
        "sessions": {"A": {"prefix": "A", "branch": a.branch, "repos": ["sdk"],
                           "channel": f"cogito5170/ga-SDK#{a.issue} (GitHub 이슈 댓글)"}},
        "ownership": [{"repo": "sdk", "path": "examples/remote/*", "session": "A"}],
        "budget": {"runs": 1},
    }
    (work / "ga.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"integ": git(sdk, "rev-parse", INTEG)}))


def cmd_send(a) -> None:
    work = Path(a.work)
    h = hub(work)
    d = {"schema": "directive/1", "id": "CMD-A1", "rev": 1, "to": "A",
         "goal": "원격 세션이 ga-SDK 에 examples/remote/ga8_hello.txt 한 줄을 남기고 보고한다",
         "why": "GA8 원격 Runner 시연 (짧은 정상 일)", "scope": "examples/remote/ga8_hello.txt 하나",
         "done_when": "파일이 자기 브랜치에 커밋 · push 되고 report/1 이 통로에 올라온다"}
    branch = h.cfg.sessions["A"].branch_for("sdk")
    body = ("## 할 일\n"
            "1. `examples/remote/ga8_hello.txt` 파일을 만들고 내용은 `ga8 remote ok` 한 줄.\n"
            f"2. 커밋하고 브랜치 `{branch}` 로 push 한다.\n"
            "3. 통로 이슈에 댓글 하나로 보고한다. 맨 앞은 아래 머리(<SHA> 는 네 커밋), 뒤에 `## Result` 한 줄, "
            "마지막 줄은 `<!-- ga-author: A -->`.\n"
            "```ga\n" + json.dumps({"schema": "report/1", "from": "A",
                                    "handled": [{"id": "CMD-A1", "rev_seen": 1, "status": "done"}],
                                    "commits": [{"repo": "sdk", "branch": branch, "sha": "<SHA>"}]}, ensure_ascii=False)
            + "\n```\n")
    post, findings, gates = h.send(d, body)
    print(json.dumps({"posted": post is not None, "findings": [f"{p.rule}:{p.strength}" for p in findings],
                      "gates": [g.number for g in gates]}, ensure_ascii=False))


def cmd_tick(a) -> None:
    work = Path(a.work)
    h = hub(work)
    res = h.tick()
    st = h.load_state()
    out = {
        "turns": [{k: t.get(k) for k in ("session", "directive", "runner", "ended", "error", "cost", "diag")}
                  | {"session_id_recorded": bool(t.get("session_id"))} for t in st["turns"]],
        "spent": st["spent"],
        "integrated": sorted(res.integrated),
        "integ_moved": git(work / "sdk", "rev-parse", INTEG) != a.base if a.base else None,
        "remote_integ_untouched": True,  # push: false — nothing is pushed by the hub (checked by the agent too)
        "verdict": (res.verdict or {}).get("class"),
        "cause": (res.verdict or {}).get("cause"),
        "findings": sorted({f"{p.rule}:{p.strength}" for p in res.findings if p.rule}),
        "gates": [g.get("number", g.get("gate", "?")) for g in res.gates],
        "quiet": res.quiet,
    }
    sha = git(work / "sdk", "rev-parse", INTEG)
    out["file_ok"] = git(work / "sdk", "show", f"{sha}:examples/remote/ga8_hello.txt", check=False).strip() == "ga8 remote ok"
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--work", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--base", required=True)
    p = sub.add_parser("send")
    p.add_argument("--work", required=True)
    p = sub.add_parser("tick")
    p.add_argument("--work", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--base", default="")
    a = ap.parse_args()
    {"init": cmd_init, "send": cmd_send, "tick": cmd_tick}[a.cmd](a)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
    sys.exit(main())
