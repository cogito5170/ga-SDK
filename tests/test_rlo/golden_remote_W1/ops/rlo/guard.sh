#!/usr/bin/env bash
# PreToolUse rlo guard for the W1 worker session (enforce). Written by ga rlo init --profile remote.
# The guard is ops/rlo/guard.py (standard library only); this wrapper runs it with python3 and fails closed around it.
# rlo allows by printing nothing; nothing here prints "allow".
set -u
DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
deny() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"rlo guard (fail closed): %s"}}\n' "$1"
  exit 0
}
PY="$(command -v python3 || true)"
[ -n "$PY" ] || deny "no python3 to run ops/rlo/guard.py"
OUT="$("$PY" "$DIR/ops/rlo/guard.py")" || deny "ops/rlo/guard.py failed"
case "$OUT" in
  "") ;;
  "{"*) printf '%s\n' "$OUT" ;;
  *) deny "guard output is not JSON" ;;
esac
exit 0
