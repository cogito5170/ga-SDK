# STATE — CMD-GA31 rev 1 (peer runtime), branch claude/ga31-peer-runtime

On claude/ga28-backends e124bc3. ga 0.3.0. Report: reports/CMD-GA31.md (report/2 head).

| item | state | where |
|---|---|---|
| S6 switch, legacy | done | ga/config.py `network` (mode hub default), ga/net/__init__.py; FileMailbox and `ga post` refuse a ```peer form |
| S1 node step, runner | done | ga/net/node.py (`ga node step`, `ga run --every`), ga/net/msg.py (exchange/1 + one ```peer block), ctxpack `3p peer`, ga/l0.py `peer.message.*` |
| S2 interaction | done, thin adapters | ga/net/state.py (NET2 rules F1-F5), ga/net/dc.py (NET3 peer_interaction, ContextPolicy KEEP/DROP), ga/net/pi.py (NET4: ms.network when importable, else a port), rlo Governor per edge |
| S3 router | done | ga/net/router.py, ga/backends/catalog.py (every built-in declares a catalog; effort knobs none until plugins take the option) |
| S4 budget checkpoint | done | ga/net/checkpoint.py; TurnResult.stop (headless runner reads ga-budget.jsonl); node and hub (`Hub._checkpoint`, `_continue_checkpoints`) |
| S5 no CCR | done | `ga usage` (ga/net/usage.py); test runs T1 in a clean env with remote/mcp imports blocked |
| D1 | met | tests/test_ga31.py D1T1Scenario (3 fake nodes, T1 + T4) |
| D2 | met | tests/test_ga31.py; `python tests/mutations_ga31.py`: 10 mutations, all killed |
| D3 | see report | 0.3.0; fresh clone + empty venv + pip check; suite under unshare -n; live smoke not run |

Thin adapters to swap when baseline sends the NET1-3 shas: L0 peer events (ga/l0.py, NET1 field list), State rules
(state.py vs Sensor NET2 helper), peer_interaction + export source (dc.py vs DC NET3), peer_context (MS ContextPolicy).
Report/2 head uses `paused` for the checkpoint: report/2 has no `partial` status.
