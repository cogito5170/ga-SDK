"""Record tests/fixtures/ga50/events.jsonl (CMD-GA50): one hub directive through the real code paths — a local
mailbox (NET fetch / push), ``ga bridge`` one pass (QUEUE, TASK, AGENT), and inside it a ``ga act`` item run with a
scripted fake model (LLM, TOOL, CODE with a failing lint, FILE, the green done_when). 0 network, 0 models.

    python tests/fixtures/ga50/record.py       (prints the cut index the live-screen test uses)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "events.jsonl"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ga50-rec-"))
    if OUT.exists():
        OUT.unlink()
    os.environ["GA_EVENTS"] = str(OUT)
    os.environ.pop("GA_EVENT_PARENT", None)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x"}
    os.environ.update({k: v for k, v in env.items() if k.startswith("GIT_")})
    try:
        from ga import bridge as B
        from ga.act import loop as A
        from ga.forms import dump_wire
        from ga.mailbox import Mailbox
        from test_ga36_bridge import DIRECTIVE
        import test_ga38 as T
        bare, work = tmp / "mail.git", tmp / "mail"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=env)
        subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, env=env, capture_output=True)
        Mailbox(work).send("AGY", dump_wire(DIRECTIVE), "baseline")
        os.remove(OUT)  # the hub's send is not part of the story: start at the bridge
        repo = tmp / "repo"
        repo.mkdir()
        (repo / "calc.py").write_text(T.CALC)
        (repo / "test_calc.py").write_text(T.TEST_CALC)
        (repo / "lint.py").write_text(T.LINT)
        (repo / ".ga-act.json").write_text(json.dumps(T.ACT_CFG))
        (tmp / "ga-supervise.json").write_text("{}")
        cfg = {"name": "AGY", "hub": "baseline", "workdir": str(tmp), "supervise_config": "ga-supervise.json",
               "max_answer_chars": 4000, "capacity_backoff_s": 0, "mailbox_repo": str(work)}

        def runner(c, conf, task):
            item = {"id": "CMD-T1", "goal": "calc.py 의 add 를 고쳐 test_calc.py 를 통과시키기", "files": ["calc.py"]}
            res = A.run_item(repo, item, backend="fake", model="gemini-2.5-flash", runner=T.Fake(["RUN lint\n", T.FIX]),
                             state_dir=tmp / "state")
            return {"code": 0 if res.status == "done" else 1, "out": res.reason,
                    "events": [{"event": "turn", "tokens": 100}] * res.turns + [{"event": "end", "status": res.status}]}
        B.one_pass(cfg, box=Mailbox(work), runner=runner, log=lambda s: None)
    finally:
        shutil.rmtree(tmp, True)
    rows = [json.loads(x) for x in OUT.read_text(encoding="utf-8").splitlines()]
    # rewrite the temp paths out (the stream is a fixture, not a record of this machine)
    text = OUT.read_text(encoding="utf-8").replace(str(tmp), "/home/vm/work")
    OUT.write_text(text, encoding="utf-8")
    lint_fail = next(i for i, r in enumerate(rows) if r["type"] == "TOOL" and r["action"] == "RUN" and r["status"] == "FAILED")
    cut = next(i for i, r in enumerate(rows) if i > lint_fail and r["type"] == "LLM" and r["action"] == "PROCESSING") + 1
    print(f"{len(rows)} events; cut {cut}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
