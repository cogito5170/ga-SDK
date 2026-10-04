# STATE — CMD-GA32 rev 1, branch claude/ga32-net-real (PAUSED: two actions refused)

On integration head e24d3e6 (ga 0.4.0). Version still 0.4.0: the 0.5.0 bump waits for S1/S2.

| item | state | where |
|---|---|---|
| Step 0 hook (shadow) | refused | the install command was denied by the permission classifier; no .ga/rlo-hook.jsonl, so no reports/CMD-GA32.budget.json |
| S3 judge | done | ga/judge.py `failing_tests`, `split_failures`, `_base_failures`: names red tests, reruns the suite on the base head, pre-existing failures are noted and do not decide the class; `ga judge --template CMD` (`judge.template`) |
| S4 catalog | done | ga/backends/catalog.py: Claude prices and `effort` knob (anthropic_http `options.effort` -> `output_config.effort`); gpt/gemini/agv stay null (no documented value in this session) |
| tests | done | tests/test_ga32.py (12); full suite 611+12 passed on rlo d190d95; mutations killed: pre-existing counted new, new hidden as pre-existing, unnamed failures excused, null price cheapest |
| S2 rlo 0.11.0 | BLOCKED | `pip install ... rlo-SDK@063b861` denied ("Code from External"); rlo-SDK is outside the session's repo scope, so its source cannot be read either |
| S1 ga-sdk[net] | BLOCKED | needs the five NET packages (full shas + repo URLs, MS 9371151 / NET6 sha) installed and read; same denial class; the pin cannot resolve until NET6 anyway |

## Next
1. Someone allows installing rlo-SDK@063b861 (or adds the repo to scope): run `pytest tests` in a venv with it, list the 7 ga.rlo failures, decide update-ga.rlo (move `ga/_pins.py` + pyproject + tests/test_pins.py to 0.11.0) vs rlo regression (repro in the report).
2. S1: pyproject `[project.optional-dependencies] net = [five pins]`, `_pins.py`, ga/net uses the real packages when importable (adapters stay the fallback), behaviour tests run against both; mutation: fallback used while the extra is installed.
3. D2: bump to 0.5.0, empty-venv installs of ga-sdk and ga-sdk[net] with clean pip check, fresh-clone full suite, final report, notify.
