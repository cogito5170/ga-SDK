# W1 guard (rlo, enforce) -- owned by the hub

The middle verification line (METHOD §4c 7) for the `W1` remote worker session, written by
`ga rlo init --profile remote` (ga-SDK `ga.rlo`, CMD-GR2 · GR7). A human commits it; no AI session changes another
session's guard.

- `.claude/settings.json`: SessionStart runs `ops/rlo/install.sh`; every tool call (PreToolUse, matcher `*`) runs
  `ops/rlo/guard.sh`, which runs `ops/rlo/guard.py` with python3.
- `install.sh`: installs pinned `rlo-sdk[sensor] @ @RLO_PIN7@` into `$GA_RLO_VENV` (default `~/.cache/ga-rlo-venv`) and marks
  the venv with the PIN. `guard.py` runs it again whenever the marker of the current PIN is missing, so a PIN move
  lands at the next tool call, without a new session.
- Model and PIN: this working tree (`ops/rlo/model.json`, `ops/rlo/install.sh`): no guard ref is configured.
- `guard.py`: runs `python -m rlo.hooks --mode enforce` with the model. Records one line per verdict, and a state line
  naming the source and its sha, in `~/.rlo/W1.jsonl`.
  - Fail closed. It denies when:
    - python3 is missing, or the hook input is empty or not JSON;
    - the guard ref is unreadable, the model is missing, or install.sh has no PIN;
    - rlo is not installed for the PIN and the install fails;
    - rlo exits nonzero, records no verdict, or prints non-JSON.
  - Even then the channel stays open: `ToolSearch`, `ReadNotifications`, `mcp__github__issue_read`, `mcp__github__add_issue_comment`, `mcp__claude-code-remote__send_message`, and the Bash channel commands below, are not denied
    (python3 missing is the one exception).
  - rlo allows by printing nothing. There is no clock override.
- Grants (external actions allowed): `Bash`, `mcp__github__add_issue_comment`, `mcp__claude-code-remote__send_message`, `ga.mail.send`. `ga.mail.send` is the Bash command below, not a tool.
- Session plumbing always in the model: `ToolSearch`, `ReadNotifications`, `mcp__github__issue_read`, `mcp__github__add_issue_comment`, `mcp__claude-code-remote__send_message`. The channel commands are in it as `ga.mail.read`, `ga.mail.scan`,
  `ga.mail.send`, so a deny's react line can name them.
- Not in the model, so denied with A1: e.g. `WebFetch`, `Agent`, `mcp__claude-code-remote__create_session`.
- rlo does not see Bash command content, file paths or argument values. guard.py reads one thing in a Bash command:
  a `ga mail` command must be written as declared (repo `.`, remote `origin`), or it is denied.
- Every deny is saved as rlo record lines in `.ga/mailbox/outbox/` (git-ignored), one file per deny.

## For the worker session

- **When a deny ends with '-- react:', do exactly that alternative once. When escalate is true, post the deny verbatim on your channel and continue other work.**
- Channel commands (Bash). A react `tool` of `ga.mail.read`, `ga.mail.send` or `ga.mail.scan` means:
  - `ga.mail.read`: `ga mail read --repo . --as W1`
  - `ga.mail.send`: `ga mail send --repo . --to hub --from W1 FILE` (FILE holds one ga form)
  - `ga.mail.scan`: `ga mail scan --repo .`
  Write them as shown: plain words, no quotes and no other shell syntax. Written so, they pass even when the guard
  fails closed.
- **At your next allowed Bash, run 'ga mail send' for each pending guard event in .ga/mailbox/outbox/, then continue other work.** For each file F there:
  `ga mail send --repo . --to hub --from W1 --guard-event F --re <the CMD id you work on> && rm -f F`
- If a deny has no `-- react:` line (an older rlo), the alternative is: Post every guard deny verbatim on your channel (the issue your hub reads),
  then go on with other work. Do not ask only in your own chat: the hub does not see it.
- After an idle gap (over about 10 minutes) the first Bash can be denied with D: the guard no longer knows your run's
  health. Make one read-only call (for example `Read` of a file you need) and retry.
- Do not edit `.claude/` or `ops/rlo/`. Changes go through the hub.

