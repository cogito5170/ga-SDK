"""METHOD §9 verification 1 and 3: replay baseline's recorded stages through ga's Bundle.

    python examples/verify/replay_stages.py --stages <baseline>/STAGES.md --repos <dir with clones> [--stage stage-2 ...]
    python examples/verify/replay_stages.py --repos <dir> --pin-conflict

Verification 1: for each stage table in STAGES.md, put every repository at its recorded sha side by side
(Bundle (a), as baseline did) and compare the test counts with the recorded ones.
Verification 3: Bundle (b) — a clean pinned install — of action@2f4791e with guard (which pins
action@3995fdb) must fail as pin_conflict; the same set with action@3995fdb must install.

Clones are looked up as <repos>/<name lower-cased>. Nothing here is specific to ga's code paths:
it only builds a ga config and calls VenvBundle.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ga import config as gacfg  # noqa: E402
from ga.adapters.git import GitVcs  # noqa: E402
from ga.adapters.venv import VenvBundle  # noqa: E402

ROW = re.compile(r"^\|\s*([A-Za-z][\w-]*)\s*\|\s*`([0-9a-f]{40})`[^|]*\|\s*(\d+)\s*\|\s*$")
HEAD = re.compile(r"^## (stage-\d+)\b")


def read_stages(path: Path) -> dict[str, dict[str, tuple[str, int]]]:
    stages: dict[str, dict[str, tuple[str, int]]] = {}
    cur = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = HEAD.match(line)
        if m:
            cur = m.group(1)
            stages[cur] = {}
            continue
        m = ROW.match(line)
        if m and cur:
            stages[cur][m.group(1)] = (m.group(2), int(m.group(3)))
    return stages


def dist_name(repo_dir: Path, sha: str) -> str:
    text = subprocess.run(["git", "show", f"{sha}:pyproject.toml"], cwd=repo_dir, capture_output=True, text=True).stdout
    m = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else ""


def test_command(repo_dir: Path, sha: str) -> list[str]:
    """unittest discovery; a tests/ package (relative imports, e.g. DC) needs the top-level dir set."""
    cmd = ["{python}", "-m", "unittest", "discover", "-s", "tests"]
    is_pkg = subprocess.run(["git", "cat-file", "-e", f"{sha}:tests/__init__.py"], cwd=repo_dir, capture_output=True).returncode == 0
    return cmd + ["-t", "."] if is_pkg else cmd


def make(repos_dir: Path, names: list[str], heads: dict[str, str], work: Path, pip_args: list[str]) -> VenvBundle:
    raw = {
        "schema": "ga-config/1",
        "hub": {"name": "replay"},
        "integration_branch": "claude/gracious-meitner-vp49xe",
        "repos": {
            n: {"path": str(repos_dir / n.lower()), "test": test_command(repos_dir / n.lower(), heads[n]),
                "package": dist_name(repos_dir / n.lower(), heads[n])}
            for n in names
        },
        "sessions": {},
        "ownership": [],
        "bundle": {"pip_args": pip_args, "timeout": 3600},
    }
    cfg = gacfg.from_dict(raw, repos_dir)
    return VenvBundle(cfg, GitVcs(cfg, work), work / "bundle")


def verify_stages(args) -> list[dict]:
    stages = read_stages(Path(args.stages))
    out = []
    for name, table in stages.items():
        if args.stage and name not in args.stage:
            continue
        heads = {r: sha for r, (sha, _) in table.items()}
        bundle = make(Path(args.repos), list(table), heads, Path(args.work), [])
        res = bundle.run_path(heads)
        for run in res.runs:
            want = table[run.repo][1]
            got = run.counts.as_dict() if run.counts else None
            total = (got["passed"] + got["failed"] + got["skipped"] + got.get("errors", 0)) if got else None
            out.append({"stage": name, "repo": run.repo, "sha": heads[run.repo][:7], "recorded": want, "reproduced": got,
                        "total": total, "match": total == want or (got and got["passed"] == want), "ok": run.ok,
                        "problem": run.problem})
            print(json.dumps(out[-1], ensure_ascii=False), flush=True)
    return out


def verify_pin_conflict(args) -> list[dict]:
    repos = Path(args.repos)
    guard = subprocess.run(["git", "rev-parse", "be871b9"], cwd=repos / "guard", capture_output=True, text=True).stdout.strip()
    out = []
    for action_sha in ("2f4791e5c33df6cf19d41f139d95d74e4b86b42e", "3995fdb3ba487f31d841d3e11b710e64f0d523db"):
        heads = {"action": action_sha, "guard": guard}
        bundle = make(repos, ["action", "guard"], heads, Path(args.work), [])
        # pip clones from GitHub (the pins inside guard name GitHub URLs); point action there too
        bundle.source_url = lambda repo, _b=bundle: f"https://github.com/cogito5170/{repo}"  # type: ignore[method-assign]
        res = bundle.run_install(heads)
        out.append({"action": action_sha[:7], "guard": guard[:7], "ok": res.ok, "problem": res.problem,
                    "tests": {r.repo: r.counts.as_dict() if r.counts else None for r in res.runs}})
        print(json.dumps(out[-1], ensure_ascii=False), flush=True)
        if res.problem and res.problem != "pin_conflict":
            print(res.output[-1500:], file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages")
    ap.add_argument("--repos", required=True)
    ap.add_argument("--stage", action="append")
    ap.add_argument("--work", default=".ga-replay")
    ap.add_argument("--pin-conflict", action="store_true")
    args = ap.parse_args()
    rows = verify_pin_conflict(args) if args.pin_conflict else verify_stages(args)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
