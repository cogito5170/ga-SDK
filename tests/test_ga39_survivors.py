"""CMD-GA43 S4: tests for the six mutants GA39's own auto-mutation run left alive (items.py goal-shape warnings,
LIMIT_NAME filtering, the --force-with-lease operator, node_modules skipped when scanning schema files)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ga.verify import items as I
from ga.verify import mutate as M

TYPES = "export interface ActionTurn {\n  name: string\n  exitCode: number\n}\n"


def doc(goal: str, tests: dict, files=("src/lib/status.ts",)) -> dict:
    return {"item": {"id": "S", "goal": goal, "files": list(files), "done_when": "t"},
            "tests": tests, "commands": {"commands": {"t": ["npx", "tsc", "--noEmit"]}}}


def shapes(d: dict, repo: Path) -> list[str]:
    return [m for lv, c, m in I.lint(d, repo) if c == "shapes" and lv == "warning"]


class GoalShapes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = r = Path(self.tmp.name)
        (r / "src/types").mkdir(parents=True)
        (r / "src/types/index.ts").write_text(TYPES)
        (r / "src/lib").mkdir(parents=True)
        (r / "src/lib/status.ts").write_text("import { ActionTurn } from '../types'\nexport const f = (t: ActionTurn) => t\n")
        (r / "src/lib/other.ts").write_text("export const g = 1\n")
        self.tests = {"src/lib/status.test.ts": "import { f } from './status'\n"}  # names no type

    def test_type_named_without_fields_warns_once_naming_its_fields(self):
        w = shapes(doc("Show each ActionTurn on the card.", self.tests), self.repo)
        self.assertEqual(len(w), 1)
        self.assertIn("ActionTurn", w[0])
        self.assertIn("exitCode, name", w[0])

    def test_type_named_with_a_field_does_not_warn(self):
        self.assertEqual(shapes(doc("Show each ActionTurn's exitCode on the card.", self.tests), self.repo), [])

    def test_type_absent_from_the_tests_but_used_by_the_files_warns(self):
        self.assertNotIn("ActionTurn", "".join(self.tests.values()))
        self.assertEqual(len(shapes(doc("Show each ActionTurn.", self.tests), self.repo)), 1)

    def test_type_named_only_in_the_tests_warns(self):
        tests = {"src/lib/other.test.ts": "import { g } from './other'\nconst t: ActionTurn = null\n"}
        self.assertEqual(len(shapes(doc("Show each ActionTurn.", tests, files=("src/lib/other.ts",)), self.repo)), 1)

    def test_type_used_by_neither_tests_nor_files_does_not_warn(self):
        tests = {"src/lib/other.test.ts": "import { g } from './other'\n"}
        self.assertEqual(shapes(doc("Show each ActionTurn.", tests, files=("src/lib/other.ts",)), self.repo), [])

    def test_type_not_in_the_goal_does_not_warn(self):
        self.assertEqual(shapes(doc("Show the card.", self.tests), self.repo), [])

    def test_a_word_containing_the_type_name_is_not_the_type(self):
        self.assertEqual(shapes(doc("Show each ActionTurnout.", self.tests), self.repo), [])

    def test_no_repo_or_no_goal_means_no_warning(self):
        self.assertEqual([c for _, c, _ in I.lint(doc("Show each ActionTurn.", self.tests))], [])
        self.assertEqual(shapes(doc("", self.tests), self.repo), [])


class SchemaScan(unittest.TestCase):
    def test_node_modules_and_dot_dirs_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            for d, name in (("src/types", "Kept"), ("node_modules/pkg/types", "Vendored"),
                            ("web/node_modules/x/api", "Nested"), (".cache/types", "Hidden")):
                (r / d).mkdir(parents=True)
                (r / d / "index.ts").write_text(TYPES.replace("ActionTurn", name))
            self.assertEqual(sorted(I.schema_types(r)), ["Kept"])
            self.assertEqual(I.schema_types(r)["Kept"], {"name", "exitCode"})

    def test_python_schema_types(self):
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            (r / "app/models").mkdir(parents=True)
            (r / "app/models/turn.py").write_text("from typing import TypedDict\nclass Turn(TypedDict):\n    name: str\n"
                                                  "    exit_code: int\n")
            self.assertEqual(I.schema_types(r), {"Turn": {"name", "exit_code"}})


def muts(text: str) -> list[dict]:
    return M.file_mutations("ga/x.py", text, set(range(1, text.count("\n") + 2)))


class LimitName(unittest.TestCase):
    def test_upper_limit_names_are_widened(self):
        got = {m["find"].strip(): m["replace"].strip() for m in muts("MAX_BYTES = 512 * 1024\nREQUEST_CAP = 10\n"
                                                                       "MIN_FONT = 12\nLIMIT = 3\n") if m["op"] == "limit"}
        self.assertEqual(got, {"MAX_BYTES = 512 * 1024": "MAX_BYTES = (512 * 1024) * 1000", "REQUEST_CAP = 10":
                               "REQUEST_CAP = (10) * 1000", "MIN_FONT = 12": "MIN_FONT = 1", "LIMIT = 3": "LIMIT = (3) * 1000"})

    def test_other_names_are_left_alone(self):
        for src in ("WIDTH = 5\n", "max_bytes = 5\n", "MAX_NAME = 'x'\n", "Max_Bytes = 5\n", "PORTS = 8000\n"):
            with self.subTest(src=src):
                self.assertEqual([m for m in muts(src) if m["op"] == "limit"], [])


class ForceWithLease(unittest.TestCase):
    def test_force_with_lease_becomes_force(self):
        src = 'import subprocess\n\ndef push(b):\n    subprocess.run(["git", "push", "--force-with-lease", "origin", b], check=True)\n'
        flags = [m for m in muts(src) if m["op"] == "flag"]
        lease = [m for m in flags if "--force-with-lease" in m["find"]]
        self.assertTrue(any('"--force"' in m["replace"] and "--force-with-lease" not in m["replace"] for m in lease), flags)

    def test_other_strings_are_not_flags(self):
        self.assertEqual([m for m in muts('A = "--force"\nB = "--force-with-lease=main"\n') if m["op"] == "flag"], [])


if __name__ == "__main__":
    unittest.main()
