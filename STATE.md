# STATE — CMD-GA36 rev 2, branch claude/ga36 (done)

ga 0.8.0: GA CLI `ga ask` / `ga bridge`, GA UI `ga ui` (GA Engine naming, research/NAMING.md).
- `ga/ask/intents.py`: 8 intents (help status next run report usage doctor stop), ko+en regex, verbs weigh 3; unmatched -> 3 closest, never a guess. No model in routing.
- `ga/ask/__init__.py` Engine: cost classes free|mail|model; `execute` refuses model/mail without confirmation (`--yes` = mail only); daily agy cap (`DayTurns`, default 10); `--model` one agy turn (`--mode plan --disable-slash-commands`, prompt <= 1500 tokens); doctor's five kinds; ledger `~/.ga-ask/ledger.jsonl`.
- `ga/ask/solve.py`: `--solve` / ui Solve box: one Supervisor in-process with ga's tool table only, turn cap (8) and the daily cap, y/N with the estimate (cap x measured overhead), per-turn lines from log.jsonl, totals = sums, overhead share, one ledger row; non-table tools and TOOL_NEEDED listed.
- `ga/bridge`: baseline's ops/agy_bridge absorbed (tools `ga.bridge.tools`), capacity 503 -> one retry -> dependency blocker `capacity:`; agy adapter reason `capacity`.
- `ga/ui`: 127.0.0.1, token, Host/Origin/Sec-Fetch-Site, POST+X-GA-Token, CSP, SSE with Last-Event-ID; `ga/runlog.py` (Tail, TurnMeter, follow, RunLog, redact).

Tests: tests/test_ga36.py (41), tests/test_ga36_bridge.py (24, 9 ported as-is), tests/mutations_ga36.py 12/12 killed with a green control.

# STATE — CMD-GA34 rev 1, branch claude/ga34-realwork (done)

ga 0.7.0: pool nodes do real repository work.
- `network.pool.repo` → per-node worktree `.ga/worktrees/<node>/` on `ga/<node>` (R3 pre-push hook reused from ga/adapters/git.py), turn cwd = worktree; hand-in = runtime commit, ownership check, merge of a moved integration head, `judge.judge_commit`, fast-forward only (`git.fast_forward_local`). L0 work.integrated / work.rejected.
- role `tools` {allow, permission_mode} → claude_cli non-bare `--tools/--allowedTools/--permission-mode`; bypassPermissions/auto, network tools, unscoped Bash and git/curl/... Bash prefixes refused; other backends refuse tools.
- work/1 `after` (deps, cycles, work.blocked once) and `files` (overlap never live together); ids `<PREFIX>-<LETTERS>-<n>`.
- judge `setup` argv lists instead of `dist`, `junit` path, vitest/jest/playwright summary parsers.
- L0 `turn.started` for every pool-node turn (static nodes only with `progress`), `turn.progress` (tool + path, cap 50).

Tests: tests/test_ga34.py (36), tests/mutations_ga34.py kills 10/10. Live smoke: results/ga34/ (1 claude -p run, $0.0303).
Baseline probe: original fails R3 R6 R8 R9; R1 R2 R4 R5 R7 assert no-new-key defaults (kept); with the new keys all 9 fail.
