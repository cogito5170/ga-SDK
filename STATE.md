# STATE — CMD-GA29 (GA2, branch claude/ga29-fresh-context, base 438a34a)

Directive: fresh claude -p per turn + ctxpack/1 + state file + cursor + usage/L0 + rlo 0.10.0 budget hook + S5 measurement.

## Design (decided)
- config: `sessions.<s>.context: fresh|resume` (default resume), `sessions.<s>.pack_max_tokens` (required in fresh),
  `runner.context_budget: {soft, hard, mode}` (budget hook, fresh only).
- `ga/ctxpack.py`: build_pack(head, state, inbox headers, retrieved) -> Pack(text, parts, drops); units in priority order,
  dropped from the end; head never dropped (head over cap = CtxPackError). tokens = ceil(utf8 bytes/4) (= rlo.pspec.tokens).
- `TurnRequest.fresh`; runners ignore resume when fresh; hub keeps no resume id in fresh mode.
- answer = report/2 (```ga) + ```state block; hub checks, writes `<ga>/sessions/<s>/state.md`, posts report as session,
  then sets `st["ctx"][s]["cursor"]`. Failed check -> turn error `answer:<why>`, nothing posted, no retry.
- TurnResult: usage, model, answer. L0 run.end record per turn -> `<ga>/telemetry/<s>.jsonl` (stdlib envelope).
- budget hook: `ga/adapters/budget_hook.py` (uses rlo.ctxbudget), in runner's own ga-settings.json only.
- tokens tools: runner options `tools` (--tools) and `system_prompt`.
- S5: scratch repo, examples/ga29_measure.py, results/ga29/.

## Progress
- [x] pin d190d95 / version 0.2.0 (481 old tests OK in venv /home/user/venv29)
- [x] ctxpack
- [x] runner + hub fresh mode + tests/test_ga29.py (15 tests, 10/10 mutations killed: results/ga29/mutations.json)
- [x] S5 runs: 15/30 claude -p used; results/ga29/table.md (resume, fresh, fresh-small all pass; attempts 1-3 kept)
- [x] D3: empty venv install at 0e414e4 pip check clean; fresh clone 496 tests OK (skip 1) with proxies pointed at 127.0.0.1:9
- [x] report/2 on baseline#12 + notify/1 to baseline (after this commit; this commit is the one the report claims)

## Results (S5, haiku, results/ga29/table.md)
resume 1,138,767 read / max ctx 40,053 / $0.405 · fresh 1,118,889 / 32,566 / $0.238 · fresh-small 820,561 / 22,160 / $0.190; all pass.
Resume's first-call context grows per turn (23.3k -> 29.2k -> 34.6k); fresh stays ~23-24k; fresh-small ~14k.
Open: fresh totals are close to resume here because only 3 short turns and fresh made more calls (41 vs 36); the
fixed CLI system prompt + tool schemas (~14-23k per call) dominate, and they also accumulate *within* one claude -p.
