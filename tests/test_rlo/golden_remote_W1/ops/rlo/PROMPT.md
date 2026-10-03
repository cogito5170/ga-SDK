# Guard rules for the W1 session (paste into the session's start prompt)

Every tool call you make goes through the rlo guard (`ops/rlo/GUARD.md`). It can deny a call; it never allows more.

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
