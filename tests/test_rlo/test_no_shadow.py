"""BD-218: the moved tests live in tests/test_rlo/, not tests/rlo/. Under `unittest discover -s tests` a tests/rlo/
package would be the top-level `rlo` and shadow rlo-sdk, so every ga.rlo path importing rlo.hooks would break."""
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent


class NoShadowTest(unittest.TestCase):
    def test_import_rlo_is_the_installed_rlo_sdk(self):
        import rlo
        import rlo.hooks

        where = Path(rlo.__file__).resolve()
        self.assertFalse(where.is_relative_to(TESTS), f"rlo resolves to {where}, under {TESTS}")
        self.assertTrue(hasattr(rlo.hooks, "HookAdapter"))
        self.assertTrue(rlo.versions()["sdk"].startswith("rlo-sdk/"))

    def test_no_tests_rlo_package(self):
        self.assertFalse((TESTS / "rlo").exists(), "tests/rlo/ would shadow rlo-sdk under discover -s tests (BD-218)")


if __name__ == "__main__":
    unittest.main()
