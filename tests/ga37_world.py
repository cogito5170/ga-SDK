"""CMD-GA37 test world: a small web-app repo fixture and the five built-in backends behind fake hosts (0 network)."""
import json
import sys
import tempfile
from pathlib import Path

from ga import backends

HERE = Path(__file__).resolve().parent
FAKE = str(HERE / "fake_hosts_ga37.py")
GOLDEN = HERE / "fixtures" / "ga37" / "golden.json"
FIVE = ("claude_cli", "agv", "codex_cli", "openai_http", "anthropic_http")
MODELS = {"claude_cli": "claude-haiku-4-5-20251001", "agv": "gpt-5.1", "codex_cli": "gpt-5.1-codex",
          "openai_http": "gpt-5.1", "anthropic_http": "claude-haiku-4-5-20251001"}
USAGE = {"input": 100, "output": 50, "cache_read": 10}


def make_repo(root: Path) -> Path:
    files = {
        "package.json": json.dumps({"name": "pricing-app", "scripts": {"test": "vitest run", "build": "vite build",
                                                                         "lint": "eslint ."}}),
        "README.md": "# Pricing App\n\nA small React app with a pricing page, settings and search.\n",
        "src/pages/Pricing.tsx": "export const Pricing = () => null\n",
        "src/pages/Settings.tsx": "export const Settings = () => null\n",
        "src/api/search.ts": "export function search() {}\n",
        "src/api/auth.ts": "export function login() {}\n",
        "tests/login.test.ts": "test('login', () => {})\n",
        "tsconfig.json": "{}\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


class Hosts:
    """The five built-ins, each a real ga runner on a fake host. ``answers``: the strings the host answers with."""

    def __init__(self, answers: list[str], sleep: float | None = None):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
        if sleep:
            (self.dir / "sleep").write_text(str(sleep))
        self.http_calls: list[dict] = []

    def calls(self) -> list[dict]:
        p = self.dir / "calls.jsonl"
        cli = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
        return cli + self.http_calls

    def transport(self, kind: str):
        answers = json.loads((self.dir / "answers.json").read_text())

        def send(url, headers, body, timeout_s):
            req = json.loads(body)
            n = len(self.http_calls)
            ans = answers[min(n, len(answers) - 1)]
            if kind == "openai":
                system, prompt = req["messages"][0]["content"], req["messages"][1]["content"]
                data = {"model": req["model"], "choices": [{"message": {"content": ans}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                  "prompt_tokens_details": {"cached_tokens": 10}}}
            else:
                system, prompt = req["system"], req["messages"][0]["content"]
                data = {"model": req["model"], "content": [{"type": "text", "text": ans}],
                        "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10}}
            self.http_calls.append({"mode": kind, "argv": [url], "prompt": prompt, "system": system,
                                    "tools": "tools" in req})
            return 200, {}, json.dumps(data).encode()
        return send

    def runner(self, name: str, cwd: str):
        ctx = {"cwd": cwd, "timeout_s": 60}
        py = [sys.executable, FAKE]
        if name == "claude_cli":
            return backends.create(name, MODELS[name], {"cli": py + ["claude", str(self.dir)], "bare": True}, ctx)
        if name == "codex_cli":
            return backends.create(name, MODELS[name], {"cli": py + ["codex", str(self.dir)]}, ctx)
        if name == "agv":
            return backends.create(name, MODELS[name], {"cli": py + ["agy", str(self.dir)]}, ctx)
        kind = "openai" if name == "openai_http" else "anthropic"
        return backends.create(name, MODELS[name], {"base_url": "http://fake.invalid", "key_env": "GA37_FAKE_KEY"},
                               dict(ctx, transport=self.transport(kind)))


def golden() -> list[dict]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
