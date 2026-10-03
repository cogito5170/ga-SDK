"""CMD-GA11 (3): judge GA10's three rounds again with the fixed rules (METHOD rev 9), without any model run.

(A) records only: the recorded path-mode test counts and the recorded judge proposals go through the new
    machine floors (``Hub._reproduce`` with a bundle that returns the recorded counts) and ``_settle_verdict``.
(B) the same heads with a real Bundle (b) as well (clean venv, pip install from the rlo-SDK remote): what the hub
    would have seen had GA10 installed. Measurement only: pip and unittest, no model.

    python examples/verify/ga11_replay.py --rlo /path/to/rlo-sdk --sibs /path/to/sibs --out ga11_replay.json [--install]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ga import config as gacfg  # noqa: E402
from ga import rules  # noqa: E402
from ga.adapters.base import BundleResult, Counts, RepoRun  # noqa: E402
from ga.adapters.git import GitVcs  # noqa: E402
from ga.adapters.mailbox import FileMailbox  # noqa: E402
from ga.adapters.venv import VenvBundle  # noqa: E402
from ga.hub import Hub  # noqa: E402

# GA10 as recorded (examples/verify/ga10_results.json, .ga/verdicts): head tested, what was integrated, the path
# counts, the hard findings, and the judge's own proposal (the final class where the machine did not raise it)
ROUNDS = [
    {"n": 1, "head": "c6b2f95122f4828ff341b7d140579543936cc34c", "integrated": None, "counts": (63, 0, 2, 0), "r4": False,
     "judge": {"class": "partial", "cause": "measurement"}, "ga10": ("partial", "measurement", None),
     "baseline": ("insufficient", "hub_directive")},
    {"n": 2, "head": "af701a245d6dd9a50b0b4f028d6465a15e95b230", "integrated": "af701a2", "counts": (81, 0, 2, 0), "r4": True,
     "judge": {"class": "blocked", "cause": "dependency"}, "ga10": ("blocked", "dependency", None),
     "baseline": ("partial", "requirement")},
    {"n": 3, "head": "6c33b855766702f4df9cc292cf2b07c89a8d72ec", "integrated": "6c33b85", "counts": (93, 0, 2, 0), "r4": False,
     "judge": {"class": "success"}, "ga10": ("partial", "measurement", "skipped"),
     "baseline": ("partial", "implementation")},
]


class RecordedBundle:
    """Returns the counts GA10 recorded for the path mode."""

    def __init__(self, counts):
        self.counts = counts

    def run_path(self, heads):
        (repo, sha), = heads.items()
        p, f, s, e = self.counts
        return BundleResult("path", f == 0 and e == 0, [RepoRun(repo, sha, "path", f == 0 and e == 0, Counts(p, f, s, e))])


def config(rlo: str, sibs: str):
    raw = {
        "schema": "ga-config/1", "hub": {"name": "hub"}, "integration_branch": "claude/ga-trial-k9",
        "repos": {"rlo": {"path": rlo, "remote": "origin", "push": False, "package": "rlo-sdk", "extras": "sensor",
                          "test": ["{python}", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                          "env": {"PYTHONPATH": ".:" + sibs},
                          "expected_skipped": {"count": 2, "why": "설치 메타데이터 시험(PYTHONPATH 방식에서는 건너뜀)"}}},
        "sessions": {"W1": {"prefix": "WA", "branch": "w1-suggest-model", "repos": ["rlo"]},
                     "W2": {"prefix": "WB", "branch": "w2-parallel-calls", "repos": ["rlo"]}},
        "ownership": [], "bundle": {"timeout": 1800},
    }
    return gacfg.from_dict(raw, Path(rlo).parent)


def judge_again(cfg, ga: Path, r: dict, bundle, modes) -> dict:
    hub = Hub(cfg, ga_dir=ga, channel=FileMailbox(ga / "mailbox"), vcs=GitVcs(cfg, ga), judge=None, runner=None,
              bundle=bundle, modes=modes)
    findings = rules.r4_ff(cfg, "rlo", "W2", False) if r["r4"] else []
    integrated = {"rlo": r["head"]} if r["integrated"] else {}
    evidence, machine = hub._reproduce({"rlo": r["head"]}, integrated, [{"head": {"handled": []}}], findings, {"directives": {}})
    proposed = {"schema": "verdict/1", "claims_vs_evidence": [], "next": {"choice": "wait", "reason": "replay"}, **r["judge"]}
    v = hub._settle_verdict(proposed, machine, evidence, findings, r["n"])
    return {"machine": machine, "final": [v["class"], v.get("cause"), v.get("subclass")],
            "tests": evidence["tests"].get("rlo"), "notes": evidence["notes"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rlo", required=True)
    ap.add_argument("--sibs", required=True, help="os.pathsep-joined sibling checkouts for the path mode")
    ap.add_argument("--out", required=True)
    ap.add_argument("--install", action="store_true", help="(B) also run a real Bundle (b) on each head")
    a = ap.parse_args()
    cfg = config(a.rlo, a.sibs)
    out = []
    with tempfile.TemporaryDirectory(prefix="ga11-replay-") as tmp:
        for r in ROUNDS:
            row = {"n": r["n"], "head": r["head"][:7], "ga10": list(r["ga10"]), "baseline": list(r["baseline"])}
            row["A_records"] = judge_again(cfg, Path(tmp) / f"a{r['n']}", r, RecordedBundle(r["counts"]), ("path",))
            if a.install:
                ga = Path(tmp) / f"b{r['n']}"
                vcs = GitVcs(cfg, ga)
                row["B_install"] = judge_again(cfg, ga, r, VenvBundle(cfg, vcs, ga / "bundle"), ("path", "install"))
            out.append(row)
            print(json.dumps(row, ensure_ascii=False)[:600], flush=True)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
