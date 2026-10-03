"""CMD-GA6: real turns through the Agent SDK Runner, ordinary work inside the OS write sandbox (haiku).

Sessions A and B each commit a small file in their own clone and post a report; the hub pulls both branches
and integrates them. Sandbox required, guard on (the defaults), K7 isolation. Only numbers and labels are kept.

    <venv with claude-agent-sdk>/bin/python examples/verify/ga6_real.py --out examples/verify/ga6_results.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from ga.adapters import sandbox  # noqa: E402
from ga.adapters.git import git  # noqa: E402
from ga.adapters.agent_sdk import AgentSDKRunner, load_sdk  # noqa: E402

from world import GIT_ENV, World, directive, proposal  # noqa: E402


def body(w: World, session: str, repo: str, did: str, name: str) -> str:
    branch = w.cfg.sessions[session].branch_for(repo)
    head = json.dumps({"schema": "report/1", "from": session, "handled": [{"id": did, "rev_seen": 1, "status": "done"}],
                       "commits": [{"repo": repo, "branch": branch, "sha": "<SHA>"}]}, ensure_ascii=False)
    post = w.cfg.hub["post_command"].format(session=session).replace("<보고 파일>", "report.md")
    return (f"## 할 일\n1. `{repo}/notes/{name}.txt` 파일을 만들고 내용은 `{name} done` 한 줄.\n"
            f"2. `git -C {repo} add -A` 와 `git -C {repo} commit -m {name}` (push 는 하지 않는다: 허브가 가져간다).\n"
            f"3. `git -C {repo} rev-parse HEAD` 로 sha 를 얻는다.\n"
            f"4. Write 로 현재 디렉터리에 `report.md` 를 쓴다. 맨 앞은 아래 머리 그대로(<SHA> 만 바꿈), 뒤에 `## Result` 한 줄.\n"
            f"```ga\n{head}\n```\n5. 올린다: `{post}`\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--executable", default="claude")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = {"model": args.model, "executable": "claude" if args.executable == "claude" else "stub",
           "sandbox_available": sandbox.available(), "sdk_installed": load_sdk() is not None}
    w = World(remote=True, budget={"runs": 2})
    try:
        py = sys.executable
        w.cfg.hub["post_command"] = f"{py} -m ga --config {w.tmp / 'ga.json'} --ga-dir {w.ga} post --channel {{session}} --from {{session}} <보고 파일>"
        runner = AgentSDKRunner(
            w.ga / "headless" / "home", executable=args.executable, model=args.model, timeout=600, max_budget_usd=0.5,
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash(git -C:*)", "Bash(git status:*)", f"Bash({py} -m ga:*)"],
            extra_env={**GIT_ENV, "GIT_CONFIG_GLOBAL": os.environ["GIT_CONFIG_GLOBAL"], "PYTHONPATH": str(ROOT)},
            sandbox="require",
        )
        w.permit_runner(runner)  # METHOD rev 13 §4c
        integ_before = {r: git(w.tmp / "remotes" / f"{r}.git", "rev-parse", "integ") for r in ("alpha", "beta")}
        for sid, repo, did, name in (("A", "alpha", "CMD-A1", "a1"), ("B", "beta", "CMD-B1", "b1")):
            w.hub.send(directive(did, sid, goal=f"{repo} 에 {name} 기록을 남긴다"), body(w, sid, repo, did, name))
        w.proposals.append(proposal("success", "wait", "둘 다 통합됨"))
        res = w.hub.tick()
        st = w.hub.load_state()
        guard = {}
        for s in ("A", "B"):
            log = runner.guard_log(s)
            rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
            guard[s] = {"calls": len(rows), "denied": sorted(r["rule"] for r in rows if r["decision"] == "deny")}
        out["turns"] = [{k: t.get(k) for k in ("session", "directive", "runner", "error", "cost", "seconds", "sandboxed", "diag", "labels")} for t in st["turns"]]
        out["guard"] = guard
        out["reports_received"] = len(res.integrated) if res else 0
        out["integrated"] = sorted(res.integrated)
        out["verdict"] = (res.verdict or {}).get("class")
        for r, name in (("alpha", "a1"), ("beta", "b1")):
            bare = w.tmp / "remotes" / f"{r}.git"
            now = git(bare, "rev-parse", "integ")
            out[f"{r}_integ_moved"] = now != integ_before[r]
            out[f"{r}_file_ok"] = git(bare, "show", f"{now}:notes/{name}.txt", check=False).strip() == f"{name} done"
        # diagnostics (labels and counts only): did each session commit, did its report arrive and parse, what the round noted
        diag = {}
        for s, repo in (("A", "alpha"), ("B", "beta")):
            ws = w.ga / "worktrees" / s / repo
            diag[s] = {
                "commits_in_clone": int(git(ws, "rev-list", "--count", f"{integ_before[repo]}..HEAD", check=False) or 0),
                "hub_pulled_branch": w.vcs.session_head(repo, s) is not None,
                "session_posts": sum(1 for x in w.mail.read(s) if x.author == s),
                "report_file_left_in_dir": (w.ga / "worktrees" / s / "report.md").exists(),
            }
            rp = w.ga / "worktrees" / s / "report.md"
            if rp.exists():
                from ga.forms import FormError, parse_post
                try:
                    head, _, _ = parse_post(rp.read_text(encoding="utf-8"), "report/1")
                    diag[s]["report_file_parses"] = True
                    diag[s]["report_claims"] = len(head.get("commits", []))
                    claimed = head.get("commits", [{}])[0].get("sha", "")
                    diag[s]["claimed_sha_known_to_hub"] = bool(claimed) and w.vcs.resolve(repo, claimed) is not None
                except FormError as e:
                    diag[s]["report_file_parses"] = False
                    diag[s]["parse_problem"] = [p.message[:80] for p in e.problems][:3]
            log = runner.guard_log(s)
            rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
            diag[s]["guard_rules_seen"] = sorted({r["rule"] for r in rows if r["rule"]})
        out["diagnostics"] = diag
        rounds = w.hub.records.all("round/1")
        out["round_notices"] = [n[:160] for r in rounds for n in r.get("notices", [])]
        out["runs"] = len(st["turns"])
        out["cost_total_usd"] = round(sum(t["cost"] or 0 for t in st["turns"]), 6)
    finally:
        w.close()
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
