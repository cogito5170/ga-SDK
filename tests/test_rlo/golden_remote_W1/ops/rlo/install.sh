#!/usr/bin/env bash
# Install rlo-sdk[sensor] (pinned) into a private venv for this repo's rlo guard. Idempotent.
# Written by ga-rlo init --profile remote. Owned by the hub: the worker session must not edit it.
set -u
PIN="3323f88741c198f453370936c481c00fbd26d398"
VENV="${GA_RLO_VENV:-$HOME/.cache/ga-rlo-venv}"
MARK="$VENV/.pinned-$PIN"
[ -f "$MARK" ] && exit 0
PY="$(command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3)"
"$PY" -m venv "$VENV" >/dev/null 2>&1 || exit 1
"$VENV/bin/pip" install -q "rlo-sdk[sensor] @ git+https://github.com/cogito5170/rlo-SDK@$PIN" >/dev/null 2>&1 || exit 1
"$VENV/bin/python" -c "import rlo.hooks" >/dev/null 2>&1 || exit 1
touch "$MARK"
