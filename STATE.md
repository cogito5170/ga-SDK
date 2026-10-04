# STATE — CMD-GA32 rev 2, branch claude/ga32r2-net-real (PAUSED: S1 blocked by a pip resolution conflict)

| item | state | where |
|---|---|---|
| Step 0 | done | add_repo (read) for rlo-SDK, Telemetry, Sensor, DC, MS, action; cloned at the directive shas to /home/user/cogito5170/*; git+https install of rlo-sdk 063b861 works |
| S2 rlo 0.11.0 | done | ga/_pins.py, pyproject.toml, docstrings; core install clean (pip check ok); full suite 623 passed, 1 skipped on rlo-sdk 0.11.0 |
| S1 ga-sdk[net] | BLOCKED | see below |
| D2 0.5.0 bump | not done | waits for S1 |

## S1 blocker (exact)
Installing rlo-sdk[sensor] 063b861 together with the five directive shas:
`ERROR: Cannot install l0-telemetry 0.2.0 (from git+https://github.com/cogito5170/Telemetry@f6c7ae26...) and rlo-sdk because these package versions have conflicting dependencies. ResolutionImpossible`
rlo-sdk 063b861 pins its own older shas (Telemetry 35e8119, DC 526f2fb, MS 19d850e, action 3995fdb, Sensor f1e45b5, +guard, health), installed as dc 0.1.0, ms 0.2.0, l0-telemetry 0.1.0. The NET APIs (dc.peer, ms.network) exist only in the newer shas, so the extra cannot use rlo's. Needs a decision: rlo-sdk must move its pins to the directive shas (a NET6-era rlo), or the extra gets `--no-deps`-style handling. Not worked around.
