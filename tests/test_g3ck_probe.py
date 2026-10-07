"""Post-deploy probe of vm/G3-INT (baseline): the deployed tree carries group 3."""
import unittest
from pathlib import Path


class Deployed(unittest.TestCase):
    def test_group3_files_are_present(self):
        for p in ("ga/vm/journal.py", "ga/vm/machine.py", "tests/test_vi10b_machine.py"):
            self.assertTrue(Path(p).is_file(), p)
