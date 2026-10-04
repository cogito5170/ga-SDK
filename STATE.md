# STATE — CMD-GA28 rev 2 (ga backends), branch claude/ga28-backends

Code: 1866121 (on 6e15f57). Report: reports/CMD-GA28.md (report/2 head).

| item | state | where |
|---|---|---|
| S1 backend protocol | done | ga/backends/base.py (BackendTurn, check_served, RateLimited); loop in ga/gemini.py is backend-free |
| S2 plugins | done | ga/backends/__init__.py (group ga.backends, rlo.plugins load rules); builtin.py (agv, gemini_cli, claude_cli, codex_cli), http.py (openai_http, anthropic_http, extra `http`, key from options.key_env only) |
| S3 agv multi-family | done, CLI name unconfirmed | agy not installed: command agy, backend agv, alias agy; slug_family gpt/claude/gemini else ConfigError; usage format + quota family per family |
| S4 CLI and forms | done | `ga supervise`, ga-supervise/1, ga-plan/1 (+ ga-gemini-plan/1 alias), ga/specs/supervisor-plan.pspec; Gemini verbatim 208/208, 17/17; no default budget |
| S5 bare plan call | done where supported | claude_cli + HTTP bare; agv, gemini_cli, codex_cli not bare (overhead table: `ga supervise --list-backends`) |
| BD-304 a/b | done | first turn compact (245 -> 159); check_plan fullmatch |
| D1 | met | tests/test_ga28.py D1SameTask, fixtures tests/fixtures/backends |
| D2 | met | D2Bare, D2Mutations; 11 mutations applied and killed |
| D3 | met | fresh clone, empty venv pip check clean, suite under unshare -n (loopback only) |

Not done / for the integrating side: measure agv and codex fixed overhead with one 'OK' turn; read `agy --help` for a
system-prompt or tools flag; confirm agv usage and codex served-model shapes (A/U in the fixtures).
Note: compact golden regenerated (compact prompts name ga-plan/1).
