# W1 guard (rlo, enforce) -- owned by the hub

The middle verification line (METHOD §4c 7) for the `W1` remote worker session, written by
`ga-rlo init --profile remote` (ga_rlo, CMD-GR2). A human commits it; no AI session changes another session's guard.

- `.claude/settings.json`: SessionStart runs `ops/rlo/install.sh`; every tool call (PreToolUse, matcher `*`) runs
  `ops/rlo/guard.sh`.
- `install.sh`: installs pinned `rlo-sdk[sensor] @ 3323f88` into `$GA_RLO_VENV` (default `~/.cache/ga-rlo-venv`).
- `guard.sh`: runs `python -m rlo.hooks --mode enforce` with `model.json`. Records one line per verdict in
  `~/.rlo/W1.jsonl`.
  - Fail closed. It denies when:
    - the venv is missing and the install fails;
    - the model is missing;
    - rlo exits nonzero;
    - rlo records no new verdict line;
    - rlo prints non-JSON;
    - the hook input is empty.
  - rlo allows by printing nothing. There is no clock override.
- Grants (external tools allowed): `Bash`, `mcp__github__add_issue_comment`, `mcp__claude-code-remote__send_message`.
- Session plumbing always in the model: `ToolSearch`, `ReadNotifications`, `mcp__github__issue_read`, `mcp__github__add_issue_comment`, `mcp__claude-code-remote__send_message`.
- Not in the model, so denied with A1: e.g. `WebFetch`, `Agent`, `mcp__claude-code-remote__create_session`.
- rlo does not see Bash command content, file paths or argument values.

## For the worker session

- **When a deny ends with '-- react:', do exactly that alternative once. When escalate is true, post the deny verbatim on your channel and continue other work.**
- If a deny has no `-- react:` line (an older rlo), the alternative is: Post every guard deny verbatim on your channel (the issue your hub reads),
  then go on with other work. Do not ask only in your own chat: the hub does not see it.
- After an idle gap (over about 10 minutes) the first Bash can be denied with D: the guard no longer knows your run's
  health. Make one read-only call (for example `Read` of a file you need) and retry.
- Do not edit `.claude/` or `ops/rlo/`. Changes go through the hub.

