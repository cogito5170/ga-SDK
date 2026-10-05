"""CMD-GA37 D3 smoke: `ga do` on claude_cli bare (Haiku 4.5) against a small temp repo, one Korean and one English
request. Budget: at most 2 real claude -p runs in total (a third run, e.g. a repair turn, is refused, not made)."""
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
    runs[0] += 1
    if runs[0] > 2:
        raise BackendError("smoke budget: 2 claude -p runs")
    assert self.bare and "--dangerously-skip-permissions" not in self.argv("x", "y")
    return orig_run(self, *a, **kw)
B.ClaudeRunner.run_turn = run_turn
orig_parse = B.parse_claude
def parse(stdout, code, model, seconds=0.0):
    try:
        d = json.loads(stdout.strip().splitlines()[-1])
        raws.append({k: d.get(k) for k in ("usage", "total_cost_usd", "duration_ms", "modelUsage")})
    except Exception:
        raws.append({"unparsed": True})
    return orig_parse(stdout, code, model, seconds)
B.parse_claude = parse

repo = Path(tempfile.mkdtemp(prefix="ga37-smoke-"))
(repo / "package.json").write_text(json.dumps({"name": "todo", "scripts": {"test": "vitest run", "build": "vite build"}}))
(repo / "README.md").write_text("# Todo\n\nA small React todo app.\n")
(repo / "src").mkdir(); (repo / "src/App.tsx").write_text("export const App = () => null\n")
(repo / "src/TodoList.tsx").write_text("export const TodoList = () => null\n")
(repo / "tests").mkdir(); (repo / "tests/app.test.tsx").write_text("test('x', () => {})\n")

out = []
for req in ("할 일 목록에 완료 항목 숨기기 토글을 추가해줘", "add a due date field to each todo item"):
    o, e = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
        code = ga_main.main(["do", "--repo", str(repo), "--backend", "claude_cli", "--model", MODEL, req])
    rep = json.loads(o.getvalue().strip().splitlines()[-1])
    task = repo / ".ga" / "tasks" / rep["task"] / "task.json"
    spec = json.loads(task.read_text()) if task.exists() else None
    out.append({"request": req, "exit": code, "status": rep["status"], "turns": rep["turns"], "tokens": rep["tokens"],
                "summary_tokens": rep["summary_tokens"], "valid_task1": spec is not None and not hard(validate(spec)),
                "notes": rep["notes"], "spec": spec, "stderr": e.getvalue().splitlines()})
    ledger = sorted((repo / ".ga" / "ledger").iterdir())
res = {"model": MODEL, "backend": "claude_cli (bare)", "claude_p_runs": runs[0], "runs": out, "raw_usage": raws,
       "ledger_rows": sum(len(p.read_text().splitlines()) for p in ledger)}
Path(__file__).with_name("smoke_result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n")
print(json.dumps({"runs": runs[0], "out": [{k: r[k] for k in ("status", "turns", "tokens", "valid_task1")} for r in out]},
                 ensure_ascii=False))
