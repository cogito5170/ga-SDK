# Guard rules for the W1 session (paste into the session's start prompt)

Every tool call you make goes through the rlo guard (`ops/rlo/GUARD.md`). It can deny a call; it never allows more.

- **When a deny ends with '-- react:', do exactly that alternative once. When escalate is true, post the deny verbatim on your channel and continue other work.**
- If a deny has no `-- react:` line (an older rlo), the alternative is: Post every guard deny verbatim on your channel (the issue your hub reads),
  then go on with other work. Do not ask only in your own chat: the hub does not see it.
- After an idle gap (over about 10 minutes) the first Bash can be denied with D: the guard no longer knows your run's
  health. Make one read-only call (for example `Read` of a file you need) and retry.
- Do not edit `.claude/` or `ops/rlo/`. Changes go through the hub.
