# STATE — CMD-GA32 rev 3, branch claude/ga32r3-net (done)

ga 0.5.0 on rlo-sdk 0.11.1 (0d92a3d). S1: `ga-sdk[net]` pins the five NET packages by full sha (ga/_pins.py `NET_PINS`, pyproject
`net` extra); `ga.net.real` switches `ga/l0.py` peer events (l0-telemetry), `ga/net/pi.py` (ms.network), `ga/net/dc.py` purpose
(dc) and the node's purpose check onto the real packages when they import, and keeps the thin adapters otherwise
(`GA_NET=fallback` forces them). State rules stay ga's own; tests/test_ga32_net.py checks them against llmsensor.state.peer.
S2: rlo pin moved. Tests: tests/test_ga32_net.py reruns test_ga31 on both paths; tests/mutations_ga32.py kills 6/6.
Report: reports/CMD-GA32.md.
