"""Builds results/ga29/table.md from the committed summary.json / l0_calls.jsonl / l0_turns.jsonl of each variant."""
import json
from pathlib import Path

D = Path(__file__).resolve().parent
rows, turns = [], []
for v in ("resume", "fresh", "fresh-small"):
    s = json.loads((D / v / "summary.json").read_text())
    ev = [json.loads(x) for x in (D / v / "l0_calls.jsonl").read_text().splitlines()]
    l0t = [json.loads(x) for x in (D / v / "l0_turns.jsonl").read_text().splitlines()]
    k = ("reported_input_tokens", "reported_cache_read_input_tokens", "reported_cache_creation_input_tokens")
    assert sum(sum(e["data"][x] for x in k) for e in l0t) == s["tokens_read_reported"] == s["tokens_read_calls"]
    rows.append(f"| {v} | {s['turns']} | {s['claude_p_runs']} | {s['model_calls']} | {s['tool_calls']} | {s['tokens_read_calls']:,} | "
                f"{s['output_tokens_calls']:,} | {s['cost_usd']:.3f} | {s['max_context_per_call']:,} | "
                f"{'yes' if s['passed_checks'] else 'no'} |")
    by, turn = {}, {}
    for e in sorted(ev, key=lambda e: (e["run_id"], e["seq"])):
        if e["type"] == "turn.start":
            turn[e["run_id"]] = turn.get(e["run_id"], 0) + 1
        if e["type"] == "llm.response":
            d = e["data"]
            by.setdefault(f"{e['run_id']} turn {turn.get(e['run_id'], 0)}", []).append(sum(d[x] or 0 for x in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
    for i, (rid, c) in enumerate(sorted(by.items())):
        turns.append(f"| {v} | {rid} | {len(c)} | {c[0]:,} | {max(c):,} | {sum(c):,} |")
out = ["| mode | turns | claude -p runs | model calls | tool calls | tokens read (input+cache_read+cache_creation) | output tokens | cost USD | max context per call | passed checks |",
       "|---|---|---|---|---|---|---|---|---|---|", *rows, "",
       "Per hub turn (transcript file tN in the runner's own config dir, split at its turn.start events):", "",
       "| mode | transcript, turn | model calls | first-call context | max context | tokens read |", "|---|---|---|---|---|---|", *turns]
(D / "table.md").write_text("\n".join(out) + "\n")
print("\n".join(out))
