# ga ops tick (CMD-GA57)

`ga ops tick [--dry-run] --config ~/.ga/hub.json --ga-dir ~/.ga` closes operational anomalies with rlo's loop
instead of only recording them:

    observe (code) -> rule/1 -> action-spec/1 -> guard -> execute -> VERIFY (next tick) -> escalate | blocked alert

The VM's `ga-hub.service` runs it right after `ga hub tick --shadow`. `ga ops last` and the console's
Decisions page (`/api/ops`) show the newest decisions.

## Pieces

- **Observations** (`ga/ops/core.py observe`): the last `hub/shadow.jsonl` row per mail, with base ancestry
  (`git merge-base --is-ancestor <remote>/<base> <sha>` in the configured repo, cached by sha) and the surviving
  mutants parsed from the judge's `needs`. Code only, zero tokens.
- **Table** (`ga/ops/table.json`): `rule/1` rows `{id, kind, when, action, evidence}`; the first rule whose `when`
  holds wins. `action-spec/1` rows are built with `ga.rlo.remote`'s spec shape, plus `window_s` and `escalate`.
- **Guard**: `low` runs, `medium` runs and sends an alert, `high` never runs (alert only).
- **Actions v1**: `retry_next_rung` (O2), `alert_ops` (notify/1 kind `alert` to `baseline-ops`, at most once per kind
  per UTC day), `send_back_base_drift` (O7, SEND_BACK "re-merge <base ref>"), `send_back_survivors` (O4, one
  template ask per mutant: file:line, tests, expected behaviour), `push_integration` (high: never run), `wait`.
  On a shadow hub, a send-back goes to the shadow recipient (`baseline-shadow`), not to the worker.
- **VERIFY**: an executed action with a postcondition leaves `{postcondition, deadline}` in `ops/state.json`. A
  later tick checks it. A definite failure, or no result by the deadline, re-enters one rung higher (the spec's
  `escalate`: retry -> the next ladder rung; send-back -> alert). The same `(rule, subject, action)` failing twice
  sends one `blocked` alert, and ops leaves that subject alone. Nothing retries without bound.
- **Model** (S4): only for an anomaly that no rule matches. The evidence card has a fixed size (at most 1,500 tokens
  by `ga.ctxpack.tokens`, no history). It gets one turn per tick, on the cheapest `ga.act.route.LADDER` rung that a
  ledger or shadow row shows answering. The answer must be one action name from the table; anything else becomes an
  `unclassified` alert. Every turn is one row in `ops/ledger/<UTC day>.jsonl`, with tokens.

Files: `<ga dir>/ops/state.json`, `decisions.jsonl`, `ledger/`.

## O2: why a shadow decision can end ASK_HUMAN with no tokens, no served model and no error

`MailHub._one` (ga/hub.py) writes a shadow row *before* `_decide` on three exits:

1. no commit for a configured repo in the report,
2. no directive `<id>` on file (`directives_dir`, or the hub state),
3. `ga judge` raised.

Each exit sets `self._usage, self._error, self._served = None, "", None` and writes `ASK_HUMAN` with only an `asks`
line. No model is called, and no backend error label is set, because nothing reached the backend. The error field
therefore reads null, even though the decision failed. Only the asks line tells the three exits apart.

On the agv backend, a row that did go through `_decide` cannot be in this state. `check_served` raises when agy
names no served model, and `_decide` turns every raise into `error: backend:<reason>`. So a "judge class success"
next to null tokens, null served and null error comes from the person's own judgement of that report. The row
itself is one of the pre-decide exits (its `judge_class` is null). On the VM, the usual cause is a directive missing
from `~/baseline/directives`, or `ga judge` failing on the VM checkout.

ops handles both cases:

- rule `o2_pre_decide` (judge_class null, no tokens) sends an `alert_ops` with the asks line;
- rules `o2_no_call` (judge success, no tokens, no error) and `o2_backend_error` (`backend:*`) run
  `retry_next_rung`.
