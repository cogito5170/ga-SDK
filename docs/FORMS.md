# Forms

Generated from ga/forms/registry.json. Do not edit.

## Enums

### NOTIFY_KINDS

- `directive`
- `report`
- `verdict`
- `question`
- `ack`
- `shadow`
- `alert`

### HANDLED_STATUS

- `done`
- `paused`
- `declined`

### BLOCKER_KINDS

- `env`
- `permission`
- `credential`
- `budget`
- `dependency`
- `design`

### CHANGE_SIZES

- `implementation`
- `component`
- `interface`
- `architecture`
- `baseline`

### NEEDS

- `credential`
- `budget`
- `new_repo`
- `new_session`

### VERDICT_CLASSES

- `success`
- `partial`
- `failure`
- `blocked`
- `insufficient`

### CAUSES

- `implementation`
- `requirement`
- `dependency`
- `measurement`
- `environment`
- `hub_directive`

### NEXT_CHOICES

- `continue`
- `refine`
- `verify`
- `handoff`
- `change_direction`
- `wait`
- `ask_user`

### JOURNAL_STATES

- `PLANNED`
- `DISPATCHED`
- `ACTING`
- `REPORTED`
- `VERDICT`
- `SEND_BACK`
- `SHADOW`
- `INTEGRATED`
- `DEPLOYED`
- `OBSERVED`
- `BLOCKED`
- `CANCELLED`

### JOURNAL_EVENTS

- `T1`
- `T2`
- `T3`
- `T4`
- `T5`
- `T6`
- `T6'`
- `T7`
- `T8`
- `T9`
- `T10`
- `T11`

## Forms

### directive/1

- `id` (required)
- `rev` (required)
- `supersedes` (optional)
- `to` (required)
- `goal` (required)
- `why` (required)
- `scope` (required)
- `done_when` (required)
- `after` (optional)
- `budget` (optional)
- `change_size` (optional)
- `contradicts` (optional)

### report/1

- `from` (required)
- `handled` (required)
- `commits` (optional)
- `tests` (optional)
- `change_size` (optional)
- `needs` (optional)
- `exchanges` (optional)

### verdict/1

- `class` (required)
- `subclass` (optional)
- `cause` (optional)
- `evidence` (required)
- `claims_vs_evidence` (required)
- `next` (required)
- `report_ref` (optional)
- `round` (optional)
- `note` (optional)

### round/1

- `n` (required)
- `date` (required)
- `repos` (required)
- `directives` (required)
- `verdict` (required)
- `summary` (required)
- `next` (required)
- `notices` (optional)
- `decisions` (optional)

### decision/1

- `id` (required)
- `date` (required)
- `decision` (required)
- `basis` (required)
- `by` (required)
- `supersedes` (optional)
- `scope` (optional)

### stage/1

- `name` (required)
- `date` (required)
- `repos` (required)
- `established` (required)
- `deferred` (required)
- `decision` (optional)

### question/1

- `gate` (required)
- `about` (required)
- `A` (required)
- `B` (required)
- `C` (required)
- `options` (required)
- `recommendation` (required)
- `refs` (optional)

### exchange/1

- `from` (required)
- `to` (required)
- `why` (required)
- `asked` (required)
- `got` (required)
- `proposal` (optional)

### review/1

- `id` (required)
- `date` (required)
- `by` (required)
- `repo` (required)
- `sha` (required)
- `class` (required)
- `cause` (optional)
- `why` (required)
- `round` (optional)
- `amends` (optional)

### directive/2

- `id` (required)
- `rev` (required)
- `supersedes` (optional)
- `to` (required)
- `goal` (required)
- `why` (required)
- `scope` (optional)
- `done_when` (optional)
- `refs` (optional)
- `changes` (optional)
- `after` (optional)
- `budget` (optional)
- `change_size` (optional)
- `contradicts` (optional)
- `note` (optional)
- `model` (optional)

### report/2

- `from` (required)
- `handled` (required)
- `commits` (optional)
- `tests` (optional)
- `change_size` (optional)
- `needs` (optional)
- `exchanges` (optional)
- `items` (required)
- `results` (optional)
- `blockers` (optional)
- `deviations` (optional)
- `proposals` (optional)
- `note` (optional)

### notify/1

- `to` (required)
- `kind` (required)
- `ref` (required)
- `id` (optional)
- `shadow` (optional)
- `note` (optional)

### task/1

- `id` (required)
- `request` (required)
- `kind` (required)
- `goal` (required)
- `answer` (optional)
- `decision` (optional)
- `options` (optional)
- `resolution` (optional)
- `deliverables` (required)
- `constraints` (required)
- `acceptance` (required)
- `non_goals` (required)
- `assumptions` (required)
- `questions` (required)
- `risks` (required)
- `repo` (required)

### journal/1

- `id` (required)
- `at` (required)
- `item` (required)
- `from_state` (required)
- `to_state` (required)
- `event` (required)
- `guard` (required)
- `inputs_hash` (required)
- `record` (required)

