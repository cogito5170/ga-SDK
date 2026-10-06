# ga project (CMD-GA54)

One file per project, `~/.ga/projects/<name>.json` (mode 0600; `$GA_HOME` moves `~/.ga`), schema `project/1`:

```json
{"schema": "project/1", "name": "ga",
 "repos": [{"name": "baseline", "url": "https://github.com/o/baseline.git", "branch": "main", "path": "~/baseline"}],
 "environment": {"kind": "vm", "python": "~/ga-venv/bin/python", "venv": "~/ga-venv", "services": ["console", "hub"]},
 "instructions": {"file": "~/baseline/CLAUDE.md"},
 "routines": [{"name": "lint", "every": "30min", "action": "lint-a"}],
 "mailbox": {"repo": "~/baseline", "branch": "ga-mailbox"},
 "models": {"ladder": "~/.ga/hub.json"}}
```

Refused: unknown keys, secret-shaped keys or values (rule R6's patterns), URLs with credentials (use a credential
helper), instructions over 8 KB, and a routine whose `action` is not an approved GA Action id — a routine never
carries a shell string. `every` is a systemd `OnCalendar` expression or an interval such as `30min`.

| command | does |
|---|---|
| `ga project init [--name N] [--from-session-file F] [--json] [--yes]` | reads what exists (console config, git remotes and branches, `~/.ga`, enabled ga user units and timers, the mailbox, the file you name) and prints the proposal with a source per field. Writes only with `--yes` at a terminal. Re-run: a diff, and what is set is kept. |
| `ga project show [N]` / `list` | the saved file / the saved projects |
| `ga project status [N] [--fetch] [--json]` | repo heads, threads (open directives, bridge items, `agv/<id>-r<n>` branches) as queued / running / report / verdict, routines with last and next run. No network unless `--fetch` (at most every 5 min). |
| `ga project apply [N] [--dry-run] [--yes]` | clone a missing repo, fast-forward an existing one (never reset or force; dirty, diverged or on another branch: one line, skipped), write and enable `ga-project-<name>--<routine>.timer` user units that run `python -m ga actions run <id>`. Dry run unless `--yes` at a terminal. No sudo, no credentials. |

Approval is a human act only: `--yes` at a TTY, or the console's 프로젝트 screen (`#/project`), whose button POSTs
`/api/project/approve {sha256}` with the console token; the sha256 must match the proposal on screen. Approval words in
mail, reports or a model's text are never read.

`ga actions run <id> [--root DIR]` (new) runs an approved GA Action in DIR; it is what a routine's timer calls.
