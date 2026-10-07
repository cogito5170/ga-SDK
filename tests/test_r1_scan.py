"""R1: no model call outside the gateway. backends.create( · .run_turn( · an agent-SDK query( · a model CLI subprocess,
anywhere under ga/ except ga/llm (the gateway) and ga/backends + ga/adapters (the backends' own internals)."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "ga"
INTERNAL = ("llm", "backends", "adapters")
MARK = "llm-scan:"  # a line that carries it is a runner wrapper inside an already-gated turn


def scan(root=ROOT, internal=INTERNAL):
    hits = []
    for f in sorted(root.rglob("*.py")):
        rel = f.relative_to(root)
        if rel.parts[0] in internal:
            continue
        src = f.read_text(encoding="utf-8")
        lines = src.splitlines()
        tree = ast.parse(src)
        parents = {}
        for n in ast.walk(tree):
            for c in ast.iter_child_nodes(n):
                parents[c] = n
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                continue
            attr, base = n.func.attr, n.func.value
            bname = base.id if isinstance(base, ast.Name) else ""
            what = None
            if attr == "create" and bname == "backends":
                what = "backends.create("
            elif attr == "run_turn" and bname not in ("L", "llm"):
                if n.args and isinstance(n.args[0], ast.Call) and getattr(n.args[0].func, "id", "") == "TurnRequest":
                    continue  # the session Runner API (adapters), not a backend runner
                if MARK in lines[n.lineno - 1]:
                    continue
                what = ".run_turn("
            elif attr == "query" and "sdk" in bname.lower():
                what = "agent_sdk query("
            elif attr in ("run", "Popen", "check_output", "call") and bname == "subprocess":
                fn = n
                while fn in parents and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = parents[fn]
                seg = ast.get_source_segment(src, fn) or ""
                if ('"--model"' in seg or "'--model'" in seg) and ('"-p"' in seg or "'-p'" in seg):
                    what = "model CLI subprocess"
            if what:
                hits.append(f"{rel}:{n.lineno} {what}")
    return hits


class NoModelCallOutsideTheGateway(unittest.TestCase):
    def test_ga_tree_is_clean(self):
        self.assertEqual(scan(), [])

    def test_the_scan_catches_each_kind(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            r = Path(d)
            (r / "bad.py").write_text(
                "import subprocess\nfrom ga import backends\n"
                "def a():\n    backends.create('x', 'm', {}, {})\n"
                "def b(r):\n    r.run_turn('p', None)\n"
                "def c(sdk):\n    sdk.query(prompt='p')\n"
                "def d():\n    subprocess.run(['claude', '-p', 'x', '--model', 'm'])\n")
            (r / "llm").mkdir()
            (r / "llm" / "ok.py").write_text("def a(r):\n    r.run_turn('p', None)\n")
            kinds = sorted(h.split(" ", 1)[1] for h in scan(r))
            self.assertEqual(kinds, sorted(["backends.create(", ".run_turn(", "agent_sdk query(", "model CLI subprocess"]))
