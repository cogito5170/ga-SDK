# Reference environment (DEV-R0c)

Every verdict on a ga-sdk commit runs the suite in this environment, built from nothing by one script:

    scripts/refenv.sh --src <ga-sdk checkout> --venv <dir> --test

It wipes and recreates the venv. If `<dir>` exists and is not a venv, the script refuses to touch it. It installs
the pinned set, checks the install, and then runs `pytest -q -rs tests` in `<src>`. The exit code is pytest's. The
last line before the tests names what was built:

    refenv: python 3.13.x pip 26.2.1 rlo-sdk 0.11.1 ga-sdk <v> playwright 1.56.0 venv <dir>

The integrator and baseline `ops/verdict.py` call the script exactly like this. Run without `--test`, it only
builds the venv.

## Pins

| what | pin | source |
|---|---|---|
| Python | >= 3.10; reference **3.13** (3.13.16 measured) | `pyproject.toml` requires-python |
| pip | 26.2.1 | `scripts/refenv.sh` |
| rlo-sdk[sensor] | 0.11.1 at rlo-SDK `0d92a3d`; it brings the NET packages (Telemetry, Sensor, DC, MS, action) and guard/health, all by commit sha | `pyproject.toml` = `ga/_pins.py` |
| extras | `ga-sdk[http,agent]` (httpx, claude-agent-sdk) | `pyproject.toml` |
| playwright | 1.56.0, which drives Chromium build 1194 | `scripts/refenv-constraints.txt` |
| everything else | exact versions | `scripts/refenv-constraints.txt` (a `pip freeze` of a passing run) |

`ga-sdk` is installed in editable mode (`-e`). The suite reads its installed metadata and the `ga.backends` entry
points (`tests/test_pins.py`, `tests/test_ga28.py`), and the script checks both before testing. Without that
install, `test_ga28` fails, an env failure and not a real one.

The script needs `git`, plus HTTPS to github.com and PyPI. Chromium: if `PLAYWRIGHT_BROWSERS_PATH` already holds
build 1194 (as in the Claude Code cloud container: `/opt/pw-browsers`), it is used as it is. Only when that build is
missing does the script run `python -m playwright install chromium`. With `--no-browser`, playwright is left out and
the 52 browser/design tests skip. That run is not a reference run.

## Skips and failures, classified (integration branch f9671da)

Without playwright: 1348 passed, 0 failed, 53 skipped.

- 52 env skips, all "playwright is not installed": 23 `no browser` (test_console_frontend, test_ga44_console,
  test_ga50) and 14 `judge unavailable` (test_console_design, test_ga43), some of them counted per subtest. The
  recipe fixes them by installing playwright.
- 1 by-design skip: `tests/test_unified.py:202` "ga.rlo.cli is there (GR moved it in)". The test covers the
  pre-move layout and skips once GR's `ga.rlo` exists. That is correct; it is not env.
- The "~62 env-only failures" that workers see are rlo-sdk not installed and ga-sdk not installed (no metadata, no
  entry points). The recipe installs both.

No real failures were found.

## Proof (from nothing)

A fresh clone of `claude/DEV-R0c` at 03616c3 (the integration branch f9671da plus this recipe), with a fresh venv:
`scripts/refenv.sh --venv <new dir> --test`. Result: **1400 passed, 0 failed, 0 errors, 1 skipped** (the
by-design `test_unified` skip), exit 0, in 20 min. Built: python 3.13.16, pip 26.2.1, rlo-sdk 0.11.1,
ga-sdk 0.18.1, playwright 1.56.0.
