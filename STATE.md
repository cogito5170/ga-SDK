# STATE — CMD-GA34 rev 1, branch claude/ga34-realwork (done)

ga 0.7.0: pool nodes do real repository work.
- `network.pool.repo` → per-node worktree `.ga/worktrees/<node>/` on `ga/<node>` (R3 pre-push hook reused from ga/adapters/git.py), turn cwd = worktree; hand-in = runtime commit, ownership check, merge of a moved integration head, `judge.judge_commit`, fast-forward only (`git.fast_forward_local`). L0 work.integrated / work.rejected.
- role `tools` {allow, permission_mode} → claude_cli non-bare `--tools/--allowedTools/--permission-mode`; bypassPermissions/auto, network tools, unscoped Bash and git/curl/... Bash prefixes refused; other backends refuse tools.
- work/1 `after` (deps, cycles, work.blocked once) and `files` (overlap never live together); ids `<PREFIX>-<LETTERS>-<n>`.
- judge `setup` argv lists instead of `dist`, `junit` path, vitest/jest/playwright summary parsers.
- L0 `turn.started` for every pool-node turn (static nodes only with `progress`), `turn.progress` (tool + path, cap 50).

Tests: tests/test_ga34.py (36), tests/mutations_ga34.py kills 10/10. Live smoke: results/ga34/ (1 claude -p run, $0.0303).
Baseline probe: original fails R3 R6 R8 R9; R1 R2 R4 R5 R7 assert no-new-key defaults (kept); with the new keys all 9 fail.
