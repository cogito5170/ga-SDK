"""CMD-GA2 end criterion 2 and 3: a real hub round with the headless Runner (``claude -p``).

    python examples/verify/headless_real.py --out examples/verify/headless_results.json            # real, haiku
    python examples/verify/headless_real.py --executable tests-stub --out /tmp/x.json              # harness check, no cost

Two local sessions A (alpha) and B (beta) with a bare remote, one hub (the scripted judge plays the person's
part of judging). Turns: CMD-A1 → A, CMD-B1 → B (B also tries to push to A's branch: the pre-push hook must stop
it), tick, CMD-A2 → A resumed (it must recall a word given only in its first turn), tick. Three runs; the
config budget stops the hub at ``--max-runs`` (6 by default) before a turn would run.

Only numbers and verdicts are written to --out: no prompt, transcript, answer or report text.
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
sys.path.insert(0, str(ROOT / "tests"))

from ga.adapters.git import git  # noqa: E402
from ga.adapters.headless import DEFAULT_DISALLOWED, HeadlessRunner  # noqa: E402

from world import GIT_ENV, World, directive, proposal  # noqa: E402

WORD = "은행나무-17"


def child_traces(world_dir: Path) -> int:
    """Child-session traces in the person's real ~/.claude: project dirs named after the world's paths.

    (A fingerprint of the whole ~/.claude is the wrong measure: the parent session keeps writing its own
    transcript there during the run. The first real run reported a false alarm that way.)"""
    projects = Path(os.path.expanduser("~")) / ".claude" / "projects"
    if not projects.is_dir():
        return 0
    key = world_dir.name
    return sum(1 for p in projects.iterdir() if key in p.name)


def report_howto(w: World, session: str, repo: str) -> str:
    branch = w.cfg.sessions[session].branch_for(repo)
    return (
        "\n\n## 보고하는 법 (정확히 이 순서로)\n"
        f"1. `git -C {repo} rev-parse HEAD` 로 커밋 sha 를 얻는다.\n"
        "2. Write 도구로 현재 디렉터리에 `report.md` 를 쓴다. 맨 앞은 아래 머리 그대로, <SHA> 만 바꾼다. 그 뒤에 `## Result` 와 `## Evidence` 를 짧게 쓴다.\n"
        "```ga\n"
        + json.dumps({"schema": "report/1", "from": session, "handled": [{"id": "<ID>", "rev_seen": 1, "status": "done"}],
                      "commits": [{"repo": repo, "branch": branch, "sha": "<SHA>"}]}, ensure_ascii=False)
        + "\n```\n"
        f"3. 이 명령으로 올린다: `{w.cfg.hub['post_command'].format(session=session).replace('<보고 파일>', 'report.md')}`\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--executable", default="claude")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--max-runs", type=int, default=6)
    ap.add_argument("--turn-cap-usd", type=float, default=0.5)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    py = sys.executable
    holder: dict = {}
    w = World(remote=True, budget={"runs": args.max_runs}, runner=None)
    gitcfg = Path(os.environ["GIT_CONFIG_GLOBAL"])
    runner = HeadlessRunner(
        w.ga / "headless" / "home",
        executable=args.executable,
        model=args.model,
        timeout=600,
        max_budget_usd=args.turn_cap_usd,
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep",
                       "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git add:*)", "Bash(git commit:*)",
                       "Bash(git rev-parse:*)", "Bash(git push origin:*)", "Bash(git -C:*)", f"Bash({py} -m ga:*)"],
        disallowed_tools=list(DEFAULT_DISALLOWED) + ["Bash(git -C alpha config:*)", "Bash(git -C beta config:*)"],
        extra_env={**GIT_ENV, "GIT_CONFIG_GLOBAL": str(gitcfg), "PYTHONPATH": str(ROOT)},
    )
    w.runner = runner
    w.hub.runner = runner
    w.cfg.hub["post_command"] = f"{py} -m ga --config {w.tmp / 'ga.json'} --ga-dir {w.ga} post --channel {{session}} --from {{session}} <보고 파일>"

    def d(id_, to, repo, goal, extra=""):
        x = directive(id_, to, goal=goal, scope=f"현재 디렉터리의 ./{repo} 저장소만. 다른 파일은 건드리지 않는다",
                      done_when="커밋이 자기 브랜치에 push 되고 report/1 이 올라온다")
        body = f"## 할 일\n{goal}\n{extra}" + report_howto(w, to, repo).replace("<ID>", id_)
        return x, body

    t0 = time.monotonic()
    a1, a1b = d("CMD-A1", "A", "alpha", "`alpha/notes/a1.txt` 파일을 만들고 내용은 `A1 done` 한 줄. `git -C alpha add -A`, `git -C alpha commit -m a1`, `git -C alpha push origin sess-a`.",
                f"\n이번 일의 암호어는 `{WORD}` 다. 파일이나 보고에 쓰지 말고 기억만 해 둔다. 다음 지시에서 물을 것이다.\n")
    b1, b1b = d("CMD-B1", "B", "beta", "`beta/notes/b1.txt` 파일을 만들고 내용은 `B1 done` 한 줄. `git -C beta add -A`, `git -C beta commit -m b1`, `git -C beta push origin sess-b`.",
                "\n그다음 시험 삼아 `git -C beta push origin HEAD:refs/heads/sess-a` 를 한 번 돌린다. 이것은 실패해야 정상이다(남의 브랜치). "
                "실패했는지만 보고의 Evidence 에 한 줄로 적는다. 다시 시도하거나 우회하지 않는다.\n")
    a2, a2b = d("CMD-A2", "A", "alpha", "`alpha/notes/a2.txt` 파일을 만들고, 내용은 지난 지시(CMD-A1)에서 기억해 두라고 한 암호어 한 줄. 모르면 `unknown` 이라고 쓴다. 그다음 add · commit(-m a2) · `git -C alpha push origin sess-a`.")

    out: dict = {"model": args.model, "executable": "claude" if args.executable == "claude" else "stub", "max_runs": args.max_runs}
    steps = []
    for doc, body in ((a1, a1b), (b1, b1b)):
        post, findings, gates = w.hub.send(doc, body)
        steps.append({"send": doc["id"], "sent": post is not None, "gates": [g.number for g in gates], "findings": [p.rule or p.message[:40] for p in findings]})
    w.proposals.append(proposal("success", "continue", "A 이어서", directive_=a2, directive_body=a2b))
    r1 = w.hub.tick()
    steps.append({"tick": 1, "integrated": sorted(r1.integrated), "verdict": (r1.verdict or {}).get("class"), "sent": r1.sent,
                  "hard": sorted({p.rule for p in r1.findings if p.strength == "hard" and p.rule})})
    r2 = w.hub.tick()
    steps.append({"tick": 2, "integrated": sorted(r2.integrated), "verdict": (r2.verdict or {}).get("class"), "quiet": r2.quiet,
                  "hard": sorted({p.rule for p in r2.findings if p.strength == "hard" and p.rule})})
    out["steps"] = steps

    st = w.hub.load_state()
    turns = st.get("turns", [])
    out["runs"] = len(turns)
    out["cost_total_usd"] = round(sum(t["cost"] or 0 for t in turns), 6)
    out["turns"] = [{"session": t["session"], "directive": t["directive"], "error": t["error"], "cost_usd": t["cost"],
                     "seconds": t["seconds"], "resumed_previous": t["resumed"] is not None and t["resumed"] == next(
                         (u["session_id"] for u in turns[:i] if u["session"] == t["session"]), None)}
                    for i, t in enumerate(turns)]
    sids_a = [t["session_id"] for t in turns if t["session"] == "A"]
    out["session_id_reused_by_cli"] = len(sids_a) >= 2 and sids_a[0] == sids_a[1]
    bare_a = w.tmp / "remotes" / "alpha.git"
    bare_b = w.tmp / "remotes" / "beta.git"
    integ_a = git(bare_a, "rev-parse", "integ")
    a2_text = git(bare_a, "show", f"{integ_a}:notes/a2.txt", check=False)
    out["resume_recalled_word"] = WORD in a2_text
    out["a1_integrated"] = git(bare_a, "show", f"{integ_a}:notes/a1.txt", check=False).strip() == "A1 done"
    out["b1_integrated"] = git(bare_b, "show", "integ:notes/b1.txt", check=False).strip() == "B1 done"
    # pre-push in the real turn: sess-a on alpha's remote holds only A's commits; beta has no sess-a branch
    out["foreign_push_blocked"] = not git(bare_b, "rev-parse", "--verify", "--quiet", "refs/heads/sess-a", check=False)
    out["seconds_total"] = round(time.monotonic() - t0, 1)
    out["child_traces_in_real_claude_dir"] = child_traces(w.tmp)
    out["temp_home_used"] = (w.ga / "headless" / "home" / ".claude").is_dir()
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    w.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
