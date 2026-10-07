"""VI-03 (baseline acceptance test): every gated model turn under ga/ has a row in ga/llm/sites.json saying its
purpose, its ladder step and why a rule cannot decide it. A new L.run_turn( site without a row fails this test."""
import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "ga"
TABLE = ROOT / "llm" / "sites.json"
STEPS = {"cheap", "strong", "either"}


def gated_sites(root=ROOT):
    """{'<path under ga/>::<enclosing function qualname>': [literal purpose kwargs]} for L.run_turn / llm.run_turn calls."""
    found = {}
    for f in sorted(root.rglob("*.py")):
        rel = f.relative_to(root)
        if rel.parts[0] == "llm":
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))

        def walk(node, names):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    walk(child, names + [child.name])
                    continue
                if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "run_turn" and isinstance(child.func.value, ast.Name)
                        and child.func.value.id in ("L", "llm")):
                    key = f"{rel.as_posix()}::{'.'.join(names) or '<module>'}"
                    purposes = found.setdefault(key, [])
                    for kw in child.keywords:
                        if kw.arg == "purpose" and isinstance(kw.value, ast.Constant):
                            purposes.append(kw.value.value)
                walk(child, names)
        walk(tree, [])
    return found


class SitesTableTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(TABLE.exists(), "ga/llm/sites.json is missing")
        self.rows = json.loads(TABLE.read_text(encoding="utf-8"))

    def test_shape(self):
        self.assertIsInstance(self.rows, list)
        for r in self.rows:
            self.assertEqual(set(r), {"site", "purpose", "ladder_step", "why_no_rule"}, r)
            self.assertIn(r["ladder_step"], STEPS, r)
            self.assertIsInstance(r["purpose"], str)
            self.assertTrue(r["purpose"].strip(), r)
            self.assertGreaterEqual(len(r["why_no_rule"].strip()), 20, r)
        sites = [r["site"] for r in self.rows]
        self.assertEqual(len(sites), len(set(sites)), "one row per site")

    def test_every_gated_turn_has_a_row_and_no_row_is_stale(self):
        code = gated_sites()
        self.assertTrue(code, "the scan found no gated turn: the scan or the tree is wrong")
        self.assertEqual(sorted(r["site"] for r in self.rows), sorted(code))

    def test_a_literal_purpose_in_code_matches_the_row(self):
        code = gated_sites()
        for r in self.rows:
            literal = set(code.get(r["site"], []))
            if literal:
                self.assertIn(r["purpose"], literal, r)


if __name__ == "__main__":
    unittest.main()
