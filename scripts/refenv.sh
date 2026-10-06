#!/usr/bin/env bash
# The reference environment for ga-sdk verdicts (DEV-R0c; docs/REFENV.md). One shot, from nothing:
#
#   scripts/refenv.sh [--src DIR] [--venv DIR] [--test] [-- PYTEST_ARGS...]
#
#   --src DIR    the ga-sdk checkout to install and test (default: this script's repo)
#   --venv DIR   the venv to (re)create (default: ~/.cache/ga-refenv; it is wiped first)
#   --test       run the full suite after building; the exit code is pytest's
#   --no-browser skip playwright (the 52 browser/design tests then skip: not a reference run)
#
# Pins: Python >= 3.10 (reference 3.13); rlo-sdk and the NET packages by commit sha (pyproject.toml / ga/_pins.py);
# every other package by scripts/refenv-constraints.txt; pip 26.2.1. Needs git and HTTPS to github.com and PyPI.
# Prints one last line: refenv: python X pip Y rlo-sdk Z ga-sdk V playwright W venv DIR
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV=""
TEST=0
BROWSER=1
PYTEST_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --src) SRC="$(cd "$2" && pwd)"; shift 2 ;;
    --venv) VENV="$2"; shift 2 ;;
    --test) TEST=1; shift ;;
    --no-browser) BROWSER=0; shift ;;
    --) shift; PYTEST_ARGS=("$@"); break ;;
    *) echo "refenv: unknown argument $1" >&2; exit 2 ;;
  esac
done
VENV="${VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/ga-refenv}"
PY="${PYTHON:-python3}"
PIP_VERSION="26.2.1"
CONSTRAINTS="$SRC/scripts/refenv-constraints.txt"

"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "refenv: $PY is $("$PY" -V 2>&1); Python >= 3.10 is required (reference 3.13)" >&2; exit 2; }
command -v git >/dev/null || { echo "refenv: git is required (rlo-sdk and the NET packages are git pins)" >&2; exit 2; }

if [ -e "$VENV" ] && [ ! -f "$VENV/pyvenv.cfg" ]; then  # never wipe a directory that is not a venv
  echo "refenv: $VENV exists and is not a venv; give another --venv" >&2; exit 2
fi
rm -rf "$VENV"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install -q --disable-pip-version-check "pip==$PIP_VERSION"
"$VENV/bin/python" -m pip install -q -c "$CONSTRAINTS" pytest
"$VENV/bin/python" -m pip install -q -c "$CONSTRAINTS" -e "$SRC[http,agent]"
if [ "$BROWSER" = 1 ]; then
  # playwright 1.56.0 drives Chromium build 1194. A preinstalled one (PLAYWRIGHT_BROWSERS_PATH) is used as is;
  # only when it is missing is it downloaded (python -m playwright install chromium).
  "$VENV/bin/python" -m pip install -q -c "$CONSTRAINTS" playwright
  if ! "$VENV/bin/python" -c 'import os, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    sys.exit(0 if os.path.exists(p.chromium.executable_path) else 1)'; then
    "$VENV/bin/python" -m playwright install chromium
  fi
fi
# the suite reads the installed metadata and entry points (tests/test_pins.py, tests/test_ga28.py): check they are there
"$VENV/bin/python" - <<'EOF'
import importlib.metadata as md, sys
import ga, rlo  # noqa: F401
from ga import _pins
want = _pins.VERSIONS["rlo-sdk"]
got = md.version("rlo-sdk")
if got != want:
    sys.exit(f"refenv: rlo-sdk {got} installed, the pin says {want}")
eps = {e.name for e in md.entry_points(group="ga.backends")}
if "agv" not in eps:
    sys.exit("refenv: ga.backends entry points missing (the editable install did not register them)")
try:
    pw = md.version("playwright")
except md.PackageNotFoundError:
    pw = "none"
print(f"refenv: python {sys.version.split()[0]} pip {md.version('pip')} rlo-sdk {got} ga-sdk {md.version('ga-sdk')} "
      f"playwright {pw} venv {sys.prefix}")
EOF

if [ "$TEST" = 1 ]; then
  cd "$SRC"
  exec "$VENV/bin/python" -m pytest -q -p no:cacheprovider -rs tests "${PYTEST_ARGS[@]}"
fi
