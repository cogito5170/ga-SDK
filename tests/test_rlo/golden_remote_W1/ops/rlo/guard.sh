#!/usr/bin/env bash
# PreToolUse rlo guard for the W1 worker session (enforce). Written by ga-rlo init --profile remote.
# Fail closed: any problem (no venv, no model, rlo error, no recorded verdict, non-JSON output) -> deny.
# rlo allows by printing nothing; it never prints "allow" (it does not widen permissions).
# No clock override on purpose: a worker must not be able to make the guard read stale state.
set -u
DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
VENV="${GA_RLO_VENV:-$HOME/.cache/ga-rlo-venv}"
MODEL="$DIR/ops/rlo/model.json"
REC="$HOME/.rlo/W1.jsonl"
deny() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"rlo guard (fail closed): %s"}}\n' "$1"
  exit 0
}
INPUT="$(cat)"
[ -n "$INPUT" ] || deny "empty hook input"
if [ ! -x "$VENV/bin/python" ]; then
  bash "$DIR/ops/rlo/install.sh" || deny "rlo not installed and install failed"
fi
[ -f "$MODEL" ] || deny "model file missing"
mkdir -p "$(dirname "$REC")"
BEFORE=$(wc -l < "$REC" 2>/dev/null || echo 0)
OUT="$(printf '%s' "$INPUT" | "$VENV/bin/python" -m rlo.hooks --model "$MODEL" --mode enforce \
        --grant Bash --grant mcp__github__add_issue_comment --grant mcp__claude-code-remote__send_message --record "$REC" 2>/dev/null)"
RC=$?
[ "$RC" -eq 0 ] || deny "rlo exited $RC"
AFTER=$(wc -l < "$REC" 2>/dev/null || echo 0)
# rlo writes one record line per verdict; no new line means no verdict was made -> deny
[ "$AFTER" -gt "$BEFORE" ] || deny "rlo recorded no verdict"
# allow = empty output (rlo never prints "allow"); anything printed must be JSON
if [ -n "$OUT" ]; then
  printf '%s' "$OUT" | "$VENV/bin/python" -c 'import json,sys; json.loads(sys.stdin.read())' 2>/dev/null || deny "rlo output is not JSON"
  printf '%s\n' "$OUT"
fi
exit 0
