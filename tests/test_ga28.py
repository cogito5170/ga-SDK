"""CMD-GA28: the supervisor loop on backend plugins. No network, no model call: CLI hosts are tests/fake_host.py replaying
tests/fixtures/backends (make_fixtures.py), HTTP hosts a recorded transport.

D1  the same scripted task (add 2 and 3, then report) ends the same through a fake backend registered by entry point
    and through every built-in (agv with a gpt, a claude and a gemini slug); old ga-gemini/1 configs, `ga gemini` and
    ga-gemini-plan/1 answers still work.
D2  bare plan calls (no host tools, the spec's once section as the system prompt) and the mutations: a served-model
    mismatch accepted for a non-Gemini family; an unknown agv family guessed; host tools left on in a bare call; a
    broken plugin breaking the registry; a key read from anywhere but the named env var, or written to a log or the
    state; a default quota applied.
"""
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from ga import backends  # noqa: E402
from ga import gemini as G  # noqa: E402
from ga.__main__ import main as ga_main  # noqa: E402
from ga.backends.base import BackendError, ModelMismatch  # noqa: E402
from test_gemini import Box, FakeClock, plan  # noqa: E402

FIX = TESTS / "fixtures" / "backends"
HOST = [sys.executable, str(TESTS / "fake_host.py")]
TOOLS = {"add": {"python": "gemini_tools:add", "about": "a + b"}}
CLI_CASES = {  # name -> (backend, model, fixture dir)
    "agv_gpt": ("agv", "gpt-5.1", "agv_gpt"),
    "agv_claude": ("agv", "claude-sonnet-4-5", "agv_claude"),
    "agv_gemini": ("agv", "gemini-3.8-flash-high", "agv_gemini"),
    "gemini_cli": ("gemini_cli", "gemini-3-flash-preview", "gemini_cli"),
    "claude_cli": ("claude_cli", "claude-haiku-4-5-20251001", "claude_cli"),
    "codex_cli": ("codex_cli", "gpt-5.1-codex", "codex_cli"),
}
HTTP_CASES = {"openai_http": "gpt-5.1", "anthropic_http": "claude-haiku-4-5-20251001"}
KEY_ENV, SECRET, OTHER = "GA28_TEST_KEY", "sk-ga28-SECRET-named-var", "sk-ga28-OTHER-default-var"
WANT = ("done", "5", {"sum": 5}, 2)  # status, say, the add step's result, model turns


def once(mode="compact"):
    return G.prompt_text("once", TOOLS, mode=mode, form=G.PLAN_SCHEMA)


class Recorded:
    """An HTTP transport replaying tests/fixtures/backends/<name>/<n>.out; it keeps every request."""

    def __init__(self, name, status=200, headers=None):
        self.name, self.status, self.headers, self.requests = name, status, headers or {}, []

    def __call__(self, url, headers, body, timeout_s):
        self.requests.append({"url": url, "headers": dict(headers), "body": json.loads(body)})
        raw = (FIX / self.name / f"{len(self.requests)}.out").read_bytes()
        return self.status, dict(self.headers), raw


class Run(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.dir)

    def env(self, **kv):
        p = mock.patch.dict(os.environ, kv)
        p.start()
        self.addCleanup(p.stop)

    def config(self, backend, model, options=None, **extra):
        path = self.dir / "ga-supervise.json"
        path.write_text(json.dumps({"schema": G.SUPERVISE_SCHEMA, "backend": backend, "model": model,
                                    "options": options or {}, "tools": TOOLS, **extra}))
        return path

    def supervise(self, backend, model, options=None, cli=None, fixture=None):
        if fixture:
            self.env(FAKE_HOST_FIXTURE=str(FIX / fixture), FAKE_HOST_DIR=str(self.dir))
            options = {"cli": HOST, **(options or {})}
        cfg = G.load_config(self.config(backend, model, options))
        clock = FakeClock()
        self.out = io.StringIO()
        sup = G.Supervisor(cfg, cli=cli, clock=clock, sleep=clock.sleep, out=self.out)
        ok = sup.start("add two and three")
        return sup, ok

    def outcome(self, sup):
        st = json.loads(sup.state_file.read_text())
        result = json.loads((sup.dir / "results" / "T1.r1.a.json").read_text())
        turns = sum(1 for s in st["steps"] if s["kind"] == "model" and s["id"] in st["done"])
        return st["status"], st["say"], result, turns

    def log(self, sup):
        return [json.loads(x) for x in sup.log_file.read_text().splitlines()]

    def argvs(self):
        return [json.loads(x) for x in (self.dir / "argv.jsonl").read_text().splitlines()]


# ---- a fake backend, registered by entry point ----------------------------------------------------------------------

PLUGIN = '''
import json
from ga.backends.base import API_VERSION, BackendTurn, check_served

ANSWERS = [{"schema": "ga-plan/1", "steps": [{"id": "a", "tool": "add", "args": {"a": 2, "b": 3}}],
            "next": {"prompt": "report the sum", "after": ["a"]}},
           {"schema": "ga-plan/1", "steps": [], "next": None, "say": "5"}]
CALLS = []


class Runner:
    resumes, bare, usage_format = False, True, "anthropic"

    def __init__(self, model):
        self.model = model

    def run_turn(self, prompt, session=None, *, system=None, on_wait=None, wait_every_s=None):
        CALLS.append({"prompt": prompt, "system": system})
        check_served([self.model], self.model)
        return BackendTurn("```json\\n" + json.dumps(ANSWERS[len(CALLS) - 1]) + "\\n```", [self.model],
                           {"input_tokens": 100, "output_tokens": 10}, "anthropic")


class Fake:
    name, version, api = "ga28_fake", "0.1", API_VERSION
    overhead = {"bare": True, "tokens": 0, "source": "test", "closest": "bare"}

    def create(self, model, options, ctx):
        return Runner(model)


BACKEND = Fake()


class OldApi(Fake):
    name, api = "ga28_old", 0


class NoCreate:
    name, version, api, overhead = "ga28_nocreate", "1", API_VERSION, {}


class Named(Fake):
    pass


class Dup(Fake):
    name = "agv"
'''
ENTRY_POINTS = """[ga.backends]
ga28_fake = ga28_plugin:BACKEND
ga28_old = ga28_plugin:OldApi
ga28_nocreate = ga28_plugin:NoCreate
ga28_named = ga28_plugin:Named
ga28_missing = ga28_no_such_module:X
agv = ga28_plugin:Dup
"""


class PluginSite:
    """A temp site dir with ga28_plugin and its dist-info, put first on sys.path; the registry is reloaded."""

    def __init__(self, test):
        self.root = Path(tempfile.mkdtemp())
        test.addCleanup(__import__("shutil").rmtree, self.root)
        (self.root / "ga28_plugin.py").write_text(PLUGIN)
        di = self.root / "ga28_plugin-0.1.dist-info"
        di.mkdir()
        (di / "METADATA").write_text("Metadata-Version: 2.1\nName: ga28-plugin\nVersion: 0.1\n")
        (di / "entry_points.txt").write_text(ENTRY_POINTS)
        sys.path.insert(0, str(self.root))
        importlib.invalidate_caches()
        test.addCleanup(self.close)
        self.reg = backends.registry(reload=True)
        self.mod = importlib.import_module("ga28_plugin")

    def close(self):
        sys.path.remove(str(self.root))
        sys.modules.pop("ga28_plugin", None)
        importlib.invalidate_caches()
        backends.registry(reload=True)


class D1SameTask(Run):
    def test_a_fake_backend_registered_by_entry_point(self):
        site = PluginSite(self)
        sup, ok = self.supervise("ga28_fake", "fake-model-1")
        self.assertTrue(ok, self.out.getvalue())
        self.assertEqual(self.outcome(sup), WANT)
        self.assertEqual([c["system"] for c in site.mod.CALLS], [once(), once()])  # bare: the protocol is the system
        self.assertNotIn(once().splitlines()[0], site.mod.CALLS[0]["prompt"])
        self.assertIn("Task:\nadd two and three", site.mod.CALLS[1]["prompt"])  # a bare host keeps nothing: whole
        self.assertIn("Results:", site.mod.CALLS[1]["prompt"])

    def test_every_builtin_cli_backend_from_a_recorded_fixture(self):
        for name, (backend, model, fixture) in CLI_CASES.items():
            with self.subTest(name):
                self.setUp()
                sup, ok = self.supervise(backend, model, fixture=fixture)
                self.assertTrue(ok, (name, self.out.getvalue(), [r for r in self.log(sup) if r["event"] == "turn"]))
                self.assertEqual(self.outcome(sup), WANT)
                turns = [r for r in self.log(sup) if r["event"] == "turn"]
                self.assertTrue(all(isinstance(r["provider_prompt_tokens"], int) for r in turns), turns)
                self.assertEqual(len(self.argvs()), 2)

    def test_every_builtin_http_backend_from_a_recorded_fixture(self):
        self.env(**{KEY_ENV: SECRET})
        for name, model in HTTP_CASES.items():
            with self.subTest(name):
                self.setUp()
                rec = Recorded(name)
                cli = backends.create(name, model, {"key_env": KEY_ENV}, {"transport": rec})
                sup, ok = self.supervise(name, model, {"key_env": KEY_ENV}, cli=cli)
                self.assertTrue(ok, self.out.getvalue())
                self.assertEqual(self.outcome(sup), WANT)
                self.assertEqual(len(rec.requests), 2)

    def test_agv_reads_usage_and_quota_per_family(self):
        for fam, (model, fmt, qf) in {"gpt": ("gpt-5.1", "openai", "claude_gpt"),
                                      "claude": ("claude-sonnet-4-5", "anthropic", "claude_gpt"),
                                      "gemini": ("gemini-3.8-flash-high", "gemini", "gemini")}.items():
            with self.subTest(fam):
                self.setUp()
                sup, ok = self.supervise("agv", model, fixture=f"agv_{fam}")
                self.assertTrue(ok)
                self.assertEqual((sup.cli.family, sup.cli.usage_format, sup.cli.quota_family), (fam, fmt, qf))
                self.assertEqual(sup.agy_quota.family, qf)
                self.assertEqual(sup.agy_quota.info["remaining_pct"], 100.0 if qf == "claude_gpt" else 96.0)
                self.assertEqual({r["usage_format"] for r in self.log(sup) if r["event"] == "turn"}, {fmt})

    def test_agy_is_an_alias_of_agv(self):
        self.assertIs(backends.get("agy"), backends.get("agv"))
        self.assertEqual(list(backends.registry().plugins)[:1], ["agv"])  # agv first

    def test_old_configs_and_ga_gemini_still_work(self):
        script = [{"plan": plan([{"id": "a", "tool": "add", "args": {"a": 2, "b": 3}}], {"prompt": "report", "after": ["a"]})},
                  {"plan": plan(say="5")}]
        for argv in (["gemini", "--config"], ["supervise", "--config"]):
            with self.subTest(argv[0]):
                box = Box(self, script)
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = ga_main(argv + [str(box.cfg_path), "add two and three"])
                self.assertEqual(code, 0, out.getvalue())
                self.assertEqual(box.state()["status"], "done")
                self.assertTrue(out.getvalue().startswith(f"[ga {argv[0]}]"))
        box = Box(self, script)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ga_main(["supervise", "--config", str(box.cfg_path), "--backend", "claude_cli", "x"]), 2)
        self.assertIn("ga-supervise/1", err.getvalue())

    def test_old_plan_answers_still_check(self):
        old = {"schema": G.LEGACY_PLAN_SCHEMA, "steps": [{"id": "a", "tool": "add", "args": {}}], "next": None}
        self.assertEqual(G.check_plan(old, TOOLS), [])
        self.assertEqual(G.spec_check(old, TOOLS), [])
        self.assertEqual(G.check_plan({**old, "schema": G.PLAN_SCHEMA}, TOOLS), [])
        self.assertEqual(G.check_plan({**old, "schema": "ga-plan/2"}, TOOLS), ["schema"])
        # ga gemini still names its old form in the prompt, byte for byte
        self.assertIn('"schema": "ga-gemini-plan/1"', G.protocol(G.GeminiConfig(root=Path("."), tools=TOOLS)))


class D2Bare(Run):
    def test_claude_cli_plan_call_is_bare(self):
        sup, ok = self.supervise("claude_cli", "claude-haiku-4-5-20251001", fixture="claude_cli")
        self.assertTrue(ok)
        for argv in self.argvs():
            self.assertEqual(argv[argv.index("--tools") + 1], "")  # mutation: host tools left on
            self.assertIn("--strict-mcp-config", argv)
            self.assertEqual(argv[argv.index("--system-prompt") + 1], once())
            self.assertNotIn(once().splitlines()[0], argv[argv.index("-p") + 1])
        self.assertTrue(all(r["bare"] for r in self.log(sup) if r["event"] == "turn"))

    def test_claude_cli_argv_without_tools_off_is_refused_by_this_test(self):
        r = backends.create("claude_cli", "m", {"bare": False}, {})
        a = r.argv("hi")
        self.assertNotIn("--tools", a)  # the non-bare form exists only when asked for
        with self.assertRaises(BackendError):
            backends.create("claude_cli", "m", {}, {}).argv("hi")  # bare needs the system text

    def test_http_plan_call_is_bare(self):
        self.env(**{KEY_ENV: SECRET})
        for name, model in HTTP_CASES.items():
            with self.subTest(name):
                self.setUp()
                rec = Recorded(name)
                cli = backends.create(name, model, {"key_env": KEY_ENV}, {"transport": rec})
                self.supervise(name, model, {"key_env": KEY_ENV}, cli=cli)
                for q in rec.requests:
                    self.assertFalse({"tools", "tool_choice", "functions"} & set(q["body"]))
                    if name == "anthropic_http":
                        self.assertEqual(q["body"]["system"], once())
                        self.assertEqual([m["role"] for m in q["body"]["messages"]], ["user"])
                    else:
                        self.assertEqual(q["body"]["messages"][0], {"role": "system", "content": once()})
                        self.assertEqual([m["role"] for m in q["body"]["messages"]], ["system", "user"])

    def test_hosts_that_cannot_go_bare_get_the_whole_prompt(self):
        for name in ("agv", "gemini_cli", "codex_cli"):
            plug = backends.get(name)
            self.assertFalse(plug.overhead["bare"])
            self.assertTrue(plug.overhead["source"] and plug.overhead["closest"])
        sup, ok = self.supervise("agv", "gpt-5.1", fixture="agv_gpt")
        first = self.argvs()[0]
        self.assertTrue(first[first.index("-p") + 1].startswith(once().splitlines()[0]))
        with self.assertRaises(BackendError):
            sup.cli.run_turn("x", system="y")

    def test_token_report_table_per_backend(self):
        conv = json.loads((TESTS / "fixtures" / "pspec" / "conversation_hero8.json").read_text())
        r = G.token_report(conv)["backends"]
        self.assertEqual(list(r)[:6], list(backends.BUILTINS))
        for name, row in r.items():
            self.assertIn("fixed_overhead_source", row)
            self.assertEqual(len(row["per_turn"]), 8)
            self.assertEqual(row["pspec_prompt_tokens"], sum(row["per_turn"]))
        self.assertEqual(r["gemini_cli"]["fixed_overhead"], 11822)
        self.assertEqual(r["claude_cli"]["fixed_overhead"], {"haiku": 945, "sonnet": 1197})
        self.assertTrue(r["claude_cli"]["bare"] and not r["agv"]["bare"])
        conv["usage"] = {"claude_cli": {"format": "anthropic", "turns": [{"input_tokens": 1000, "output_tokens": 9}] * 8}}
        self.assertEqual(G.backend_report(conv)["claude_cli"]["provider_prompt_tokens"], 8000)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ga_main(["supervise", "--token-report",
                                      str(TESTS / "fixtures" / "pspec" / "conversation_hero8.json")]), 0)
        self.assertIn("claude_cli", json.loads(out.getvalue())["backends"])


class D2Mutations(Run):
    def test_served_model_mismatch_fails_every_family(self):
        for name, (backend, model, fixture) in CLI_CASES.items():
            with self.subTest(name):
                self.setUp()
                other = model[:-1] + ("x" if model[-1] != "x" else "y")
                if backend == "agv":
                    other = {"gpt": "gpt-5", "claude": "claude-opus-4-5", "gemini": "gemini-3-pro"}[name[4:]]
                sup, ok = self.supervise(backend, other, fixture=fixture)
                self.assertFalse(ok)
                self.assertEqual([r["reason"] for r in self.log(sup) if r["event"] == "turn"], ["served_model_mismatch"])
        self.env(**{KEY_ENV: SECRET})
        for name, model in HTTP_CASES.items():
            with self.subTest(name):
                cli = backends.create(name, model + "-x", {"key_env": KEY_ENV}, {"transport": Recorded(name)})
                with self.assertRaises(ModelMismatch):
                    cli.run_turn("p", system="s")

    def test_an_unknown_agv_family_is_a_config_error(self):
        for slug in ("llama-4-70b", "gpt5", "o3-mini", "Mistral-large", ""):
            with self.subTest(slug):
                with self.assertRaises(backends.ConfigError):
                    backends.create("agv", slug, {}, {})
                with self.assertRaises(G.FormError):
                    G.load_config(self.config("agv", slug))
        for slug in ("gpt-5.1", "claude-sonnet-4-5", "gemini-3.8-flash-high", "GPT-5"):
            backends.create("agv", slug, {}, {})

    def test_a_broken_plugin_does_not_break_the_registry(self):
        site = PluginSite(self)
        reg = site.reg
        self.assertEqual(list(reg.plugins)[:6], list(backends.BUILTINS))
        self.assertIn("ga28_fake", reg.plugins)
        errs = {e["name"]: e["error"] for e in reg.errors}
        self.assertEqual(set(errs), {"ga28_old", "ga28_nocreate", "ga28_named", "ga28_missing", "agv"})
        self.assertIn("backend API 0", errs["ga28_old"])
        self.assertIn("missing 'create'", errs["ga28_nocreate"])
        self.assertIn("differs from the entry point name", errs["ga28_named"])
        self.assertIn("ModuleNotFoundError", errs["ga28_missing"])
        self.assertIn("duplicate", errs["agv"])
        self.assertIs(reg.get("agv"), backends.builtin.AGV)  # the built-in stays
        with self.assertRaisesRegex(KeyError, "backend API 0"):
            reg.get("ga28_old")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(backends.main(["list"]), 0)
        self.assertEqual(len(json.loads(out.getvalue())["errors"]), 5)

    def test_the_key_comes_only_from_the_named_env_var_and_is_never_written(self):
        self.env(**{KEY_ENV: SECRET, "OPENAI_API_KEY": OTHER, "ANTHROPIC_API_KEY": OTHER})
        for name, model in HTTP_CASES.items():
            with self.subTest(name):
                self.setUp()
                rec = Recorded(name)
                cli = backends.create(name, model, {"key_env": KEY_ENV}, {"transport": rec})
                sup, ok = self.supervise(name, model, {"key_env": KEY_ENV}, cli=cli)
                self.assertTrue(ok)
                self.assertNotIn(SECRET, json.dumps(vars(cli), default=str))  # not kept on the runner
                for q in rec.requests:
                    auth = q["headers"].get("authorization", q["headers"].get("x-api-key"))
                    self.assertIn(SECRET, auth)
                    self.assertNotIn(OTHER, json.dumps(q))
                written = "".join(p.read_text(errors="replace") for p in self.dir.rglob("*") if p.is_file())
                for s in (SECRET, OTHER, "SECRET", "OTHER-default"):
                    self.assertNotIn(s, written + self.out.getvalue())
        for status, reason in ((500, "transient:500"), (401, "http_401"), (429, "rate_limited_minute")):  # failed turns (GA41: 500 is transient)
            with self.subTest(status=status):
                self.setUp()
                rec = Recorded("openai_http", status=status, headers={"retry-after": "7"})
                cli = backends.create("openai_http", "gpt-5.1", {"key_env": KEY_ENV}, {"transport": rec})
                sup, ok = self.supervise("openai_http", "gpt-5.1", {"key_env": KEY_ENV}, cli=cli)
                self.assertFalse(ok)
                self.assertEqual({r.get("reason") for r in self.log(sup) if r["event"] == "turn"}, {reason})
                written = "".join(p.read_text(errors="replace") for p in self.dir.rglob("*") if p.is_file())
                self.assertNotIn(SECRET, written + self.out.getvalue())
                self.assertNotIn("SECRET", written + self.out.getvalue())
        with mock.patch.dict(os.environ, {KEY_ENV: ""}):  # unset: a failed turn, never another variable
            cli = backends.create("openai_http", "gpt-5.1", {"key_env": KEY_ENV}, {"transport": Recorded("openai_http")})
            with self.assertRaisesRegex(BackendError, "key_env_unset"):
                cli.run_turn("p", system="s")

    def test_a_config_holds_no_key(self):
        for opts in ({"api_key": "x"}, {"token": "x"}, {"base_url": "sk-abc"}, {"key_env": "lower case"}):
            with self.subTest(opts):
                with self.assertRaises(G.FormError):
                    G.load_config(self.config("openai_http", "gpt-5.1", opts))
        self.assertEqual(G.load_config(self.config("openai_http", "gpt-5.1", {"key_env": "OPENAI_API_KEY"})).options,
                         {"key_env": "OPENAI_API_KEY"})

    def test_no_default_quota(self):
        cfg = G.load_config(self.config("claude_cli", "claude-haiku-4-5-20251001"))
        self.assertEqual(cfg.budget, {})
        self.assertIsNone(cfg.daily["requests"])
        clock = FakeClock()
        day = G.DayCount(self.dir / "day.json", cfg.daily)
        gov = G.daily_governor(cfg, clock, day, cfg.model)
        for _ in range(500):  # no window, no cap: nothing ever waits
            self.assertTrue(gov.try_acquire(cfg.est_tokens, cfg.model).ok)
        self.assertEqual(gov.wait_s(cfg.est_tokens, cfg.model), 0)
        self.assertIsNone(day.left(clock()))
        # a configured budget is the integrating side's, and it binds
        cfg = G.load_config(self.config("claude_cli", "claude-haiku-4-5-20251001", budget={"rpm": 2}))
        gov = G.daily_governor(cfg, clock, G.DayCount(self.dir / "day2.json", cfg.daily), cfg.model)
        self.assertEqual([gov.try_acquire(0, cfg.model).ok for _ in range(3)], [True, True, False])

    def test_cli_backend_flag_and_listing(self):
        path = self.config("nope", "m")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ga_main(["supervise", "--config", str(path), "x"]), 2)
        self.assertIn("$.backend", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ga_main(["supervise", "--list-backends"]), 0)
        self.assertEqual(list(json.loads(out.getvalue())["backends"])[:6], list(backends.BUILTINS))


class CompactFirstTurn(unittest.TestCase):
    def test_the_first_turn_is_compact_in_compact_mode(self):
        # BD-304 (a): baseline measured 245 -> 161 est. tokens on its task; here: shorter, the same task
        from rlo.pspec import tokens
        v = G.prompt_text("first", TOOLS, "add two and three", mode="verbatim", form=G.PLAN_SCHEMA)
        c = G.prompt_text("first", TOOLS, "add two and three", mode="compact", form=G.PLAN_SCHEMA)
        self.assertLess(tokens(c), tokens(v))
        self.assertTrue(c.endswith("Task:\nadd two and three"))


if __name__ == "__main__":
    unittest.main()
