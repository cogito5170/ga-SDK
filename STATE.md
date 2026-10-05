# STATE — CMD-FT1 rev 1, branch claude/ft1 (done, partial matrix)

Harness: bench/final_task/ (ft/ package, run.py, fixtures/). Tests: tests/test_ft1.py (27, offline, fake backend), tests/mutations_ft1.py kills 4/4.
Real runs: bench/final_task/results/ (runs.jsonl 58 rows, ledger.jsonl 107 `claude -p` calls incl. 2 probes, SUMMARY.md, bulk_files.json).
Stopped by the harness at the cap: 107 of 110 calls used, the next run could pass 110 ($7.50 of $35 spent). Only 58 of 100 runs: bulk control 10, A selective 10 (rep 1),
B 20 (rep 1-2 mostly), C 18. The 110-call cap, not the dollar cap, binds (A costs 3 calls per run).

Known problems (not fixed, runs are kept as they are):
- T2 is void: fixture test_textutil.py imports only the old names, so no answer's `test_slugify` could call `slugify` -> 0 correct everywhere. Fix for a rerun:
  `from textutil import *` in fixtures/t2/test_textutil.py. No calls were left to rerun. SUMMARY.md 1b shows the tables without T2.
- `usage` vs claude -p total_cost_usd disagree on 103/105 calls: total_cost_usd is priced from modelUsage, which adds an internal Haiku call per claude -p. Both are recorded.
- Sonnet calls with ~$0.0005 quota are prompt-cache hits (cache_read x0.1) on the repeated system prompt.
- C T5 has 1 run per model; B Haiku T5 once asked for a tool; the C vs A (H3) verdicts rest on 5 cells per model.
- Full `unittest discover -s tests` shows 35 errors / 1 failure (not looked into; tests.test_ga32_net and tests.test_cli pass).
