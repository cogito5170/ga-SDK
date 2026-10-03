"""ga.rlo — rlo's Autonomy wired into ga (GA_UNIFIED U2, BD-206): the ``ga rlo ...`` commands (moved from ga_rlo 0.4.0).

Owned by GR: ga/rlo/* and tests/test_rlo/* are GR's (BD-218); GA does not edit them. GA keeps the ``ga rlo ...`` entry,
which calls ``ga.rlo.cli.main(argv)``. This file imports nothing, and the modules below import rlo only when a command
runs: ga core must import without rlo.
"""
__version__ = "0.5.1"  # 0.5.0: ga_rlo 0.4.0 moved in (CMD-GR5); 0.5.1: one pin source, turn-like doctor replay (CMD-GR6)
