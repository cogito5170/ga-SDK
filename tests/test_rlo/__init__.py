"""ga.rlo tests (moved from ga_rlo 0.4.0, CMD-GR5). A package so `unittest discover -s tests` finds them.

They need rlo-sdk (a dependency of ga-sdk). Without it -- e.g. a checkout run with `pip install --no-deps` -- the whole
package is skipped, with the reason, instead of every test erroring (GA's note on CMD-GR7)."""
import importlib.util
import unittest

if importlib.util.find_spec("rlo") is None:
    raise unittest.SkipTest("rlo-sdk is not installed: the ga.rlo tests need it (pip install ga-sdk brings it)")
