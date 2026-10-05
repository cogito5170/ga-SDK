"""CMD-GA37 D3, third run (budget raised to 3 by baseline): the English request once, on claude_cli bare (Haiku 4.5),
same temp repo shape as smoke.py. At most 1 real claude -p run here (a repair turn would be refused, not made).
Records the per-turn problem labels from the ledger."""
import contextlib, io, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ga import __main__ as ga_main
from ga.backends import builtin as B
from ga.backends.base import BackendError
from ga.forms import hard, validate

MODEL = "claude-haiku-4-5-20251001"
runs, raws = [0], []
orig_run = B.ClaudeRunner.run_turn
def run_turn(self, *a, **kw):
    if runs[0] >= 1:
        runs.append("refused")
        raise BackendError("smoke budget: 1 claude -p run here (3 in total for GA37)")
    runs[0] += 1
    assert self.bare and "--dangerously-skip-permissions" not in self.argv("x", "y")
    return orig_run(self, *a, **kw)
B.ClaudeRunner.run_turn = run_turn
orig_parse = B.parse_claude
def parse(stdout, code, model, seconds=0.0):
    try:
        d = json.loads(stdout.strip().splitlines()[-1])
        raws.append({k: d.get(k) for k in ("usage", "total_cost_usd", "duration_ms")})
    except Exception:
        raws.append({"unparsed": True})
    return orig_parse(stdout, code, model, seconds)
B.parse_claude = parse

repo = Path(tempfile.mkdtemp(prefix="ga37-smoke-en-"))
(repo / "package.json").write_text(json.dumps({"name": "todo", "scripts": {"test": "vitest run", "build": "vite build"}}))
(repo / "README.md").write_text("# Todo\n\nA small React todo app.\n")
(repo / "src").mkdir(); (repo / "src/App.tsx").write_text("export const App = () => null\n")
(repo / "src/TodoList.tsx").write_text("export const TodoList = () => null\n")
(repo / "tests").mkdir(); (repo / "tests/app.test.tsx").write_text("test('x', () => {})\n")

req = "add a due date field to each todo item"
o, e = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
    code = ga_main.main(["do", "--repo", str(repo), "--backend", "claude_cli", "--model", MODEL, req])
rep = json.loads(o.getvalue().strip().splitlines()[-1])
task = repo / ".ga" / "tasks" / rep["task"] / "task.json"
spec = json.loads(task.read_text()) if task.exists() else None
turn_rows = [json.loads(x) for p in sorted((repo / ".ga" / "ledger").glob("2*.jsonl")) for x in p.read_text().splitlines()]
req_rows = [json.loads(x) for p in sorted((repo / ".ga" / "ledger").glob("requests-*.jsonl")) for x in p.read_text().splitlines()]
res = {"model": MODEL, "backend": "claude_cli (bare)", "claude_p_runs": runs[0], "refused": runs.count("refused"),
       "request": req, "exit": code, "kind": rep.get("kind"), "outcome": rep.get("outcome"), "status": rep["status"],
       "turns": rep["turns"], "tokens": rep["tokens"], "summary_tokens": rep["summary_tokens"],
       "valid_task1": spec is not None and not hard(validate(spec)),
       "turn_problems": [{"n": r["n"], "kind": r["kind"], "problems": r["problems"], "input": r["input"],
                          "output": r["output"], "seconds": r["seconds"], "error": r["error"]} for r in turn_rows],
       "request_row": req_rows, "raw_usage": raws, "notes": rep["notes"], "spec": spec,
       "stderr": e.getvalue().splitlines()}
Path(__file__).with_name("smoke_result_en.json").write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n")
print(json.dumps({k: res[k] for k in ("claude_p_runs", "refused", "kind", "outcome", "turns", "tokens", "valid_task1",
                                      "turn_problems")}, ensure_ascii=False))
