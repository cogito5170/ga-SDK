"""Run matrix (FINAL_TASK 5.2), order, cap, results. The order keeps comparisons if a stop comes early:
phase 1 the bulk control (arm A, one rep) each next to its selective pair, then rep 1..3 of B, C (and A) by task and model."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import quota
from .arms import MAX_CALLS, Run, execute
from .bulk import MIN_TOKENS, bulk_context
from .budget import Budget, CapReached
from .world import make_tasks

HAIKU, SONNET = "claude-haiku-4-5-20251001", "claude-sonnet-5-5"
MODELS = (HAIKU, SONNET)
TASKS = ("T1", "T2", "T3", "T4", "T5")
RESULTS = Path(__file__).resolve().parents[1] / "results"


def plan(reps: int = 3) -> list[dict[str, Any]]:
    """The 100 runs in the order they are started."""
    out: list[dict[str, Any]] = []
    for model in MODELS:
        for t in TASKS:
            out.append({"arm": "A", "model": model, "task": t, "rep": 1, "mode": "bulk"})
            out.append({"arm": "A", "model": model, "task": t, "rep": 1, "mode": "selective"})
    for rep in range(1, reps + 1):
        for arm in ("B", "C", "A"):
            if arm == "A" and rep == 1:
                continue
            for t in TASKS:
                for model in MODELS:
                    out.append({"arm": arm, "model": model, "task": t, "rep": rep, "mode": "selective"})
    return out


def key(r: dict[str, Any]) -> str:
    return f"{r['arm']}|{r['model']}|{r['mode']}|{r['task']}|{r['rep']}"


def estimate_usd(arm: str, model: str, mode: str, bulk_tokens: int) -> float:
    """Worst case for one run: every allowed call at a 1.4x-estimated prompt and 2000 output tokens, uncached."""
    p_in, p_out = quota.prices(model)
    per = ((bulk_tokens if mode == "bulk" else 0) + 6000) * 1.4 * p_in + 2000 * p_out
    return MAX_CALLS[arm] * per


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def run_matrix(backend, budget: Budget, runs_path: Path, *, items: list[dict[str, Any]] | None = None,
               max_runs: int | None = None, bulk: dict | None = None, log=print) -> dict[str, Any]:
    """Start runs in order until done, ``max_runs``, or the cap: a run that could pass either cap (worst case) is not
    started and the matrix stops there. -> {"started", "stopped": None | reason}."""
    tasks = make_tasks()
    done = {key(r) for r in load_rows(runs_path)}
    items = plan() if items is None else items
    bulk = bulk if bulk is not None else (bulk_context() if any(i["mode"] == "bulk" for i in items) else None)
    started, stopped = 0, None
    for it in items:
        if key(it) in done:
            continue
        if max_runs is not None and started >= max_runs:
            stopped = "max_runs"
            break
        bt = bulk["tokens"] if (it["mode"] == "bulk" and bulk) else 0
        if it["mode"] == "bulk" and bt < MIN_TOKENS:
            raise ValueError("bulk context is under the 50k minimum")
        if not budget.can_start(MAX_CALLS[it["arm"]], estimate_usd(it["arm"], it["model"], it["mode"], bt)):
            stopped = f"cap: {budget.calls} calls, ${budget.usd:.4f} used; the next run could pass {budget.max_calls} calls or ${budget.usd_cap}"
            break
        run_id = key(it)
        run = Run(tasks[it["task"]], it["model"], it["mode"], backend, budget, run_id, bulk if it["mode"] == "bulk" else None)
        try:
            m = execute(run, it["arm"])
        except CapReached as e:
            stopped = f"cap: {e}"
            break
        row = {"schema": "final-task-metrics/1", "run_id": run_id, **it, **m}
        if it["mode"] == "bulk":
            row["bulk"] = {"files_ref": "bulk_files.json", "tokens_est": bulk["tokens"], "n_files": len(bulk["files"])}
        with runs_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")
        started += 1
        log(f"{run_id}: correct={m['result_correct']} calls={m['llm_calls']} quota=${m['quota_usd']:.4f} "
            f"(total {budget.calls} calls, ${budget.usd:.4f})" + (f" ERROR {m['error']}" if m["error"] else ""))
    return {"started": started, "stopped": stopped}


def main(argv: list[str] | None = None) -> int:
    import argparse
    from .backend import ClaudeBackend
    from .world import ensure_image
    ap = argparse.ArgumentParser(description="CMD-FT1 real runs (bare claude -p)")
    ap.add_argument("--max-runs", type=int)
    ap.add_argument("--results", default=str(RESULTS))
    a = ap.parse_args(argv)
    res = Path(a.results)
    res.mkdir(parents=True, exist_ok=True)
    ensure_image()
    bulk = bulk_context()
    (res / "bulk_files.json").write_text(json.dumps({"tokens_est": bulk["tokens"], "files": bulk["files"]}, indent=1) + "\n")
    out = run_matrix(ClaudeBackend(), Budget(res / "ledger.jsonl"), res / "runs.jsonl", max_runs=a.max_runs, bulk=bulk)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
