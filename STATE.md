# STATE — CMD-FT1 rev 2, branch claude/ft1r2 (done, full matrix)

Harness: bench/final_task/ (ft/ package, run.py, fixtures/). Offline tests: tests/test_ft1.py (30), tests/mutations_ft1.py kills 5/5 (incl. void T2 fixture).
Rev 2: T2 fixture fixed (`from textutil import *`), 12 rev-1 T2 rows marked `void` (kept in runs.jsonl, excluded from tables) and rerun; 42 missing runs done.
Result: 100 of 100 valid runs, 0 errors. rev-2 `claude -p` calls 106 of 130; cumulative claude -p cost $18.32 of $35 (rev 1 $14.49 + rev 2 $3.83); usage-based quota_usd $9.60.
Caps now: 130 calls counted per rev (ledger rows carry `rev`, none = rev 1), $35 cumulative by claude -p total_cost_usd.
Both quota columns (quota_usd, quota_cli_usd) in results/SUMMARY.md; section 6 names which measure bound each conclusion.

Known: claude -p cost vs usage disagree on nearly every call (internal Haiku call). Full-suite state: see reports/CMD-FT1.md.
