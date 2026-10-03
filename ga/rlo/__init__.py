"""ga.rlo — the seam where rlo's Autonomy is wired into ga (GA_UNIFIED U2, BD-206).

Owned by GR: ga/rlo/* and tests/rlo/* are GR's; GA does not edit them. GA keeps only this empty seam and the
``ga rlo ...`` entry, which calls ``ga.rlo.cli.main(argv)``. Nothing here imports rlo: ga core must import without it.
"""
__version__ = "0.5.0"  # ga_rlo 0.4.0 moved in (CMD-GR5)
