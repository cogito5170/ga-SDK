"""CMD-GA12 (4): replay the six operator interventions of GA10–GA11 against the fixed ga (METHOD rev 9 + rev 10),
without any model run. Each situation is rebuilt in the hermetic test world with the same shape (who committed,
what the reports claimed, what the Judge proposed); the Judge is the recorded proposal, scripted.

    python examples/verify/ga12_replay.py --out examples/verify/ga12_replay.json
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

from ga.adapters.git import GitVcs  # noqa: E402
from ga.hub import Hub  # noqa: E402

from world import World, directive, proposal  # noqa: E402


def scripted(*props):
    plan = list(props)
    return lambda ctx: plan.pop(0) if plan else proposal("success", "wait", "끝")


def i1_relative_ga_dir() -> dict:
    """GA10 #1: a relative --ga-dir broke the session clone (nothing ran)."""
    w = World()
    cwd = os.getcwd()
    try:
        os.chdir(w.tmp)
        hub = Hub(w.cfg, ga_dir=Path(".rel"), channel=w.mail, vcs=GitVcs(w.cfg, ".rel"), judge=w.hub.judge,
                  runner=w.runner, bundle=w.hub.bundle)
        post, _, _ = hub.send(directive("CMD-A1", "A"))
        ok = post is not None and (w.tmp / ".rel" / "worktrees" / "A" / "alpha" / ".git").is_dir()
    finally:
        os.chdir(cwd)
        w.close()
    return {"needed_now": not ok, "why": "ga_dir is resolved (F4, CMD-GA11)" if ok else "clone still fails"}


def i2_round1_unclaimed() -> dict:
    """GA10 #2: round 1 — both sessions committed, neither report claimed (W2 handled []); the Judge said
    partial · measurement · verify without a draft; the operator wrote WA1 rev 2 and WB1 rev 2."""
    w = World(judge_fn=scripted(proposal("partial", "verify", "증거가 모자란다", cause="measurement")))
    try:
        w.hub.send(directive("CMD-A1", "A"))
        w.hub.send(directive("CMD-B1", "B"))
        w.paste("A"), w.paste("B")
        w.work("A", "alpha", {"x.txt": "1\n"})
        w.work("B", "beta", {"y.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")])
        w.report("B", [])
        res = w.hub.tick()
        sent = sorted(res.sent)
    finally:
        w.close()
    return {"needed_now": sent != ["CMD-A1", "CMD-B1"], "hub_sent": sent,
            "why": "the hub drafts rev+1 for both sessions from the R1b notes (rev 10); the turn prompt now asks for "
                   "commits (F2), which would likely have avoided round 1 itself — not replayable without a model"}


def i3_i4_round2_not_ff(judge_next: str) -> dict:
    """GA10 #3 and #4: round 2 — W2's commit was not a fast-forward after W1's; the Judge said blocked · dependency
    · ask_user without a draft → gate 5 (#3, answered), then the operator wrote WB1 rev 3 (#4)."""
    p = proposal("blocked", "ask_user", "W2 가 통합 브랜치를 합쳐야 한다", cause="dependency") if judge_next == "ask_user" \
        else proposal("partial", "wait", "허브가 rev+1 을 보냈다", cause="requirement")
    w = World(judge_fn=scripted(p), shared_alpha=True)
    try:
        w.hub.send(directive("CMD-A1", "A"))
        w.hub.send(directive("CMD-B1", "B"))
        w.paste("A"), w.paste("B")
        a = w.work("A", "alpha", {"one.txt": "a\n"})
        b = w.work("B", "alpha", {"B_OWNS.txt": "b\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", a)])
        w.report("B", [("CMD-B1", 1, "done")], [("alpha", b)])
        res = w.hub.tick()
        out = {"judge": judge_next, "integrated": sorted(res.integrated), "hub_sent": res.sent, "gates": len(res.gates),
               "verdict": [res.verdict["class"], res.verdict.get("cause")]}
    finally:
        w.close()
    return out


def i6_outside_rejection(judge_next: str) -> dict:
    """GA11 #6: baseline rejected the integrated head (clean install lacked rlo.suggest_model); ga had no inlet, so
    the operator wrote WA1 rev 3. Now: `ga review`, then the next round."""
    p = proposal("success", judge_next, "바깥 판정을 본다")
    w = World(judge_fn=scripted(proposal("success", "wait", "좋다"), p))
    try:
        w.hub.send(directive("CMD-A1", "A"))
        w.paste("A")
        sha = w.work("A", "alpha", {"x.txt": "1\n"})
        w.report("A", [("CMD-A1", 1, "done")], [("alpha", sha)])
        w.hub.tick()
        rv = w.hub.review("baseline", "alpha", sha, "partial", "깨끗한 설치에 하위 패키지가 없다", cause="implementation")
        res = w.hub.tick()
        out = {"judge": judge_next, "review": rv["id"], "amends": rv.get("amends"), "hub_sent": res.sent,
               "verdict": [res.verdict["class"], res.verdict.get("cause")]}
    finally:
        w.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    r34_ask, r34_wait = i3_i4_round2_not_ff("ask_user"), i3_i4_round2_not_ff("wait")
    r6_refine, r6_wait = i6_outside_rejection("refine"), i6_outside_rejection("wait")
    rows = [
        {"n": 1, "kind": "cli_bug", **i1_relative_ga_dir()},
        {"n": 2, "kind": "directive_authoring", **i2_round1_unclaimed()},
        {"n": 3, "kind": "gate_answer", "needed_now": "conditional", "replay": [r34_ask, r34_wait],
         "why": "R4 itself raises no gate any more; a gate comes only if the Judge still says ask_user (as recorded)"},
        {"n": 4, "kind": "directive_authoring", "needed_now": r34_ask["hub_sent"] != ["CMD-B1"], "replay": r34_ask,
         "why": "R4 non-ff: the hub sends CMD-B1 rev 2 by itself (rev 9), even when the Judge asks the user"},
        {"n": 5, "kind": "config", "needed_now": True,
         "why": "turning Bundle (b) on and declaring the known skips is configuration; no rule can do it"},
        {"n": 6, "kind": "directive_authoring", "needed_now": "conditional", "replay": [r6_refine, r6_wait],
         "why": "`ga review` takes the rejection in (one command, not a directive); the hub drafts from it when the Judge "
                "says refine or verify without a draft; if the Judge says wait, the operator still writes the directive"},
    ]
    summary = {
        "interventions": 6,
        "not_needed": sum(1 for r in rows if r["needed_now"] is False),
        "conditional_on_judge": sum(1 for r in rows if r["needed_now"] == "conditional"),
        "still_needed": sum(1 for r in rows if r["needed_now"] is True),
    }
    out = {"summary": summary, "rows": rows}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    for r in rows:
        print(r["n"], r["kind"], r["needed_now"], json.dumps(r.get("replay", r.get("hub_sent", "")), ensure_ascii=False)[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
