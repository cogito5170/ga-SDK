"""CMD-GA26 (POL-2 T2): ga gemini's prompts come from one prompt-spec/1 file (ga/specs/gemini-plan.pspec, rlo.pspec).

D1 verbatim is byte-identical to ga-sdk 438a34a's protocol(), first turn, --resume turn and agy turn (208 captured
cases, four tool tables), through the runtime path; a one-byte change to the spec is caught.
D2 compact on follow-up turns, the first turn verbatim; the token report on the labelled 8-turn conversation is no worse
than baseline's prototype (447 Gemini, 1574 agy); a follow-up that resends the once-section is caught.
S4 the spec's check agrees with check_plan on baseline ops/pspec/run.py's 17 cases at 7dc9d13: 16/17 on rlo-sdk 0.9.0
(6bc76c7) — the self-reference case, K16 P1 — and 17/17 on any later rlo. check_plan stays the runtime authority.
"""
import contextlib
import io
import json
import unittest
from importlib import metadata
from pathlib import Path

from ga import _pins
from ga import gemini as G
from ga.__main__ import main as ga_main

try:
    from rlo import pspec as P
    HAS_PSPEC = True
except ImportError:
    HAS_PSPEC = False
needs_pspec = unittest.skipUnless(HAS_PSPEC, "needs rlo-sdk >= 0.9.0 (rlo.pspec, CMD-K15) — installed with ga-sdk")

FIX = Path(__file__).resolve().parent / "fixtures" / "pspec"
GA_438 = "438a34a26d203ee7616409b50811d227f6297499"
PROTOTYPE = {"gemini_cli": (646, 447), "agy": (2263, 1574)}  # baseline PROMPT_SPEC.md §4, 8 turns, verbatim -> compact

# baseline ops/pspec/run.py at 7dc9d13, "2. checker agrees with ga's check_plan", in its order
RUN_TOOLS = {"read_file": {"about": "read a file from the workspace"},
             "search": {"about": "search the web; returns titles and urls"},
             "list_dir": {"about": "list a directory"}, "write_note": {"about": "append a note to the user's notes"},
             "fetch_url": {"about": "fetch a url as text"}, "noop": {}}
S = "ga-gemini-plan/1"
CASES_17 = [
    {"schema": S, "steps": [{"id": "a", "tool": "search", "args": {"q": "x"}}], "next": {"prompt": "go", "after": ["a"]}, "say": "hi"},
    {"schema": S, "steps": [], "next": None},
    {"schema": S, "steps": [{"id": "a", "tool": "nope"}]},
    {"schema": "ga-gemini-plan/2", "steps": []},
    {"schema": S, "steps": [{"id": "a", "tool": "search", "after": ["b"]}]},
    {"schema": S, "steps": [{"id": "a", "tool": "search"}, {"id": "a", "tool": "noop"}]},
    {"schema": S, "steps": [{"id": "this-id-is-too-long", "tool": "noop"}]},
    {"schema": S, "steps": [{"id": "a", "tool": "noop", "args": []}]},
    {"schema": S, "next": {"prompt": "  "}},
    {"schema": S, "next": {"prompt": "x", "after": ["z"]}},
    {"schema": S, "extra": 1},
    {"schema": S, "say": 3},
    {"schema": S, "steps": [{"id": "s%d" % i, "tool": "noop"} for i in range(17)]},
    {"schema": S, "steps": [{"id": "s%d" % i, "tool": "noop"} for i in range(16)]},
    {"schema": S, "steps": [{"id": "a", "tool": "noop", "zz": 1}]},
    "not an object",
    {"schema": S, "steps": [{"id": "a", "tool": "noop", "after": ["a"]}]},
]
# boundaries the 17 do not pin (a spec mutation such as {1,13} must be caught); both checks agree on all of them
BOUNDARY = [{"schema": S, "steps": [{"id": i, "tool": "noop"}]}
            for i in ("a" * 12, "a" * 13, "", "A-_9", "a.b", "a b", "\u00e9", 7, "M", "m", "A", "Z", "a", "z", "0", "5",
                      "9", "_", "-", "a:", "a/", "a@", "a[", "a`", "a{", "^", "$", "a\\", "a]")] + \
           [{"schema": S, "steps": [{"id": "a", "tool": "noop"}, {"id": "b", "tool": "noop", "after": ["a"]}]},
            {"schema": S, "next": {"prompt": "x"}}, {"schema": S, "next": None, "say": ""}, {"schema": S}]
SELF_REFERENCE = 16  # {"id": "a", "after": ["a"]}: check_plan rejects; rlo 0.9.0 counts a step's own id as seen (K16 P1)


def rlo_version():
    try:
        return metadata.version("rlo-sdk")
    except metadata.PackageNotFoundError:
        return _pins.VERSIONS["rlo-sdk"]


def expected_disagreements():
    """On rlo-sdk 0.9.0 (6bc76c7) the self-reference case; after the K16 pin, none."""
    return [SELF_REFERENCE] if rlo_version() == "0.9.0" else []


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def inputs(d, c):
    tools = d["tools"][c["tools"]]
    task = d["tasks"].get(c.get("task"), "")
    res = [{"id": a, "tool": b, "text": t} for a, b, t in d["results"].get(c.get("results"), [])]
    ask = d["asks"][c["ask"]] if "ask" in c else ""
    return tools, task, res, ask


class Stub:
    """What Supervisor._prompt reads (as in fixtures/pspec/capture_prompts.py), with a prompt_mode."""

    def __init__(self, tools, task, results, resumes, mode="verbatim"):
        self.cfg = G.GeminiConfig(root=Path("."), tools=tools, prompt_mode=mode)
        self.cli = type("Cli", (), {"resumes": resumes})()
        self.texts = {f"T1.r1.{r['id']}": r["text"] for r in results}
        self.st = {"steps": [{"id": "T1.m1", "kind": "model", "first": True, "prompt": task, "after": []}]
                   + [{"id": f"T1.r1.{r['id']}", "kind": "tool", "plan_id": r["id"], "tool": r["tool"], "args": {},
                       "after": []} for r in results]}

    _rec = G.Supervisor._rec

    def _result_text(self, sid):
        return self.texts[sid]


def runtime_prompt(d, c, mode="verbatim"):
    """The prompt ga sends for a captured case, through Supervisor._prompt (protocol() for the protocol case)."""
    tools, task, res, ask = inputs(d, c)
    if c["kind"] == "protocol":
        return G.protocol(G.GeminiConfig(root=Path("."), tools=tools))
    stub = Stub(tools, task, res, c["kind"] != "turn_noresume", mode)
    if c["kind"] == "first":
        return G.Supervisor._prompt(stub, stub.st["steps"][0])
    rec = {"id": "T1.m2", "kind": "model", "prompt": ask, "after": [], "needs": [f"T1.r1.{r['id']}" for r in res]}
    return G.Supervisor._prompt(stub, rec)


def spec_kills(spec_text, d, golden):
    """Problems of a spec text against the verbatim fixtures, the compact golden, the 17 check results and the header:
    an empty list means a mutant of the spec survives."""
    try:
        spec = P.load(spec_text)
    except P.SpecError:
        return ["load"]
    out = ["stray_tag"] if G.stray_tags(spec) else []  # what plan_spec() refuses
    if (spec.name, sorted(spec.inputs), spec.out_schema) != ("gemini-plan/1", ["ask", "results", "task", "tools"], S):
        out.append("header")
    want = {name: (i.term, i.basis, i.when) for name, i in G.plan_spec().inputs.items()}
    if {name: (i.term, i.basis, i.when) for name, i in spec.inputs.items()} != want or spec.goal != G.plan_spec().goal:
        out.append("inputs")
    try:
        for c, g in zip(d["cases"], golden["cases"]):
            tools, task, res, ask = inputs(d, c)
            vals = {"tools": G.spec_tools(tools), "task": task, "results": res, "ask": ask}
            sec = "once" if c["kind"] == "protocol" else c["kind"]
            if P.compile(spec, sec, vals, "verbatim") != c["text"] or P.compile(spec, sec, vals, "compact") != g["text"]:
                out.append(f"text:{c['kind']}")
                break
        base = [bool(P.check(G.plan_spec(), p, {"tools": RUN_TOOLS})) for p in CASES_17 + BOUNDARY]
        if [bool(P.check(spec, p, {"tools": RUN_TOOLS})) for p in CASES_17 + BOUNDARY] != base:
            out.append("check")
    except (P.SpecError, KeyError, TypeError, AttributeError, ValueError):
        out.append("compile")
    return out


@needs_pspec
class VerbatimTest(unittest.TestCase):
    """D1."""

    @classmethod
    def setUpClass(cls):
        cls.d = load("ga_438a34a.json")

    def test_fixtures_are_from_438a34a_with_at_least_three_tool_tables(self):
        self.assertEqual(self.d["ga_sdk"], GA_438)
        kinds = {c["kind"] for c in self.d["cases"]}
        self.assertEqual(kinds, {"protocol", "first", "turn", "turn_noresume"})
        for k in kinds:
            self.assertGreaterEqual(len({c["tools"] for c in self.d["cases"] if c["kind"] == k}), 3, k)
        self.assertEqual(len(self.d["cases"]), 208)

    def test_runtime_prompts_are_byte_identical_in_verbatim(self):
        for c in self.d["cases"]:
            with self.subTest(**{k: v for k, v in c.items() if k != "text"}):
                self.assertEqual(runtime_prompt(self.d, c, "verbatim"), c["text"])

    def test_the_first_turn_is_verbatim_in_compact_mode_too(self):
        for c in (c for c in self.d["cases"] if c["kind"] == "first"):
            self.assertEqual(runtime_prompt(self.d, c, "compact"), c["text"])

    def test_a_stray_tag_is_refused(self):
        self.assertEqual(G.stray_tags(G.plan_spec()), [])
        text = G.SPEC_FILE.read_text(encoding="utf-8")
        for mutant in (text[:-3] + "}x\n", text.replace("{{ ask }}\n--- turn_noresume", "{{ ask x}\n--- turn_noresume")):
            spec = P.load(mutant)  # rlo 0.9.0 loads it, and renders the broken tag as the whole one
            self.assertEqual(G.stray_tags(spec) != [], True)

    def test_about_is_handed_in_as_protocol_wrote_it(self):
        # without spec_tools the spec's {% if t.about %} row differs for a blank or colon-ended about
        tools = self.d["tools"]["edges"]
        raw = P.compile(G.plan_spec(), "once", {"tools": tools, "task": "", "results": [], "ask": ""})
        self.assertNotEqual(raw, G.protocol(G.GeminiConfig(root=Path("."), tools=tools)))
        self.assertEqual(G.spec_tools({"a": {"about": "x: "}, "b": {"python": "m:f"}, "c": {"about": 7}}),
                         {"a": {"about": "x"}, "b": {"about": ""}, "c": {"about": "7"}})

    def test_a_one_byte_change_in_the_spec_is_caught(self):
        text = G.SPEC_FILE.read_text(encoding="utf-8")
        golden = load("compact_golden.json")
        self.assertEqual(spec_kills(text, self.d, golden), [])
        body = [i for i, ch in enumerate(text) if not text[text.rfind("\n", 0, i) + 1:].startswith("#")]
        survived = []
        for i in body:  # every byte outside the comment lines
            ch = text[i]
            new = ("a" if ch == "z" else chr(ord(ch) + 1)) if ch.isalnum() else ("y" if ch == "x" else "x")
            if not spec_kills(text[:i] + new + text[i + 1:], self.d, golden):
                survived.append((i, ch))
        self.assertEqual(survived, [])


@needs_pspec
class CompactTest(unittest.TestCase):
    """D2 and S3."""

    @classmethod
    def setUpClass(cls):
        cls.d = load("ga_438a34a.json")
        cls.golden = load("compact_golden.json")
        cls.conv = load("conversation_hero8.json")

    def test_compact_follow_ups_match_the_golden(self):
        self.assertEqual(self.golden["spec_digest"], G.plan_spec().digest)
        for c, g in zip(self.d["cases"], self.golden["cases"]):
            if c["kind"] in ("turn", "turn_noresume"):
                with self.subTest(**{k: v for k, v in c.items() if k != "text"}):
                    self.assertEqual(runtime_prompt(self.d, c, "compact"), g["text"])

    def test_a_resume_follow_up_does_not_resend_the_once_section(self):
        self.assertEqual(once_resent(G.plan_spec(), self.d), [])
        mutant = G.SPEC_FILE.read_text(encoding="utf-8").replace("--- turn\n", "--- turn\n{% use once %}\n", 1)
        self.assertTrue(once_resent(P.load(mutant), self.d))  # the check bites

    def test_token_report_no_worse_than_the_prototype(self):
        r = G.token_report(self.conv)
        self.assertEqual(r["turns"], 8)
        for host, (verbatim, compact) in PROTOTYPE.items():
            h = r["hosts"][host]
            self.assertEqual(len(h["per_turn"]), 8)
            self.assertEqual(h["verbatim"], verbatim, host)
            self.assertLessEqual(h["compact"], compact, host)
            first = h["per_turn"][0]
            # what ga sends: the first turn verbatim (S3), every follow-up compact
            self.assertEqual(h["ga"], h["compact"] + first["verbatim"] - first["compact"], host)
            self.assertTrue(all(t["ga"] == t["compact"] for t in h["per_turn"][1:]))
        self.assertEqual(r["answers"], {"n": 8, "check_plan_ok": 8, "spec_ok": 8, "agree": 8})

    def test_token_report_runs_both_checks_on_each_answer(self):
        from unittest import mock
        with mock.patch.object(G, "spec_check", return_value=["$.steps"]):
            self.assertEqual(G.token_report(self.conv)["answers"], {"n": 8, "check_plan_ok": 8, "spec_ok": 0, "agree": 0})
        with mock.patch.object(G, "check_plan", return_value=["steps"]):
            self.assertEqual(G.token_report(self.conv)["answers"], {"n": 8, "check_plan_ok": 0, "spec_ok": 8, "agree": 0})

    def test_the_labelled_conversation_is_consistent(self):
        turns = self.conv["turns"]
        for prev, cur in zip(turns, turns[1:]):
            steps = {s["id"]: s["tool"] for s in prev["answer"]["steps"]}
            self.assertEqual({r["id"]: r["tool"] for r in cur["results"]}, steps)
            self.assertEqual(prev["answer"]["next"]["after"], [r["id"] for r in cur["results"]])
        self.assertIsNone(turns[-1]["answer"]["next"])

    def test_token_report_cli_is_offline_and_prints_no_text(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ga_main(["gemini", "--token-report", str(FIX / "conversation_hero8.json")])
        self.assertEqual(code, 0, err.getvalue())
        r = json.loads(out.getvalue())
        self.assertEqual(r["hosts"]["agy"]["verbatim"], 2263)
        for text in ("hero banner", "3 hits", "notes/:", "compare them"):  # numbers and labels only
            self.assertNotIn(text, out.getvalue())

    def test_prompt_mode_config_and_flag(self):
        self.assertEqual(G.GeminiConfig(root=Path(".")).prompt_mode, "compact")
        base = {"schema": G.CONFIG_SCHEMA}
        self.assertEqual(G.config_problems({**base, "prompt_mode": "verbatim"}), [])
        self.assertTrue(G.config_problems({**base, "prompt_mode": "tiny"}))


@needs_pspec
class SupervisorTest(unittest.TestCase):
    """The runtime path with the fake Gemini CLI: the mode per turn, the prompt sizes, and the beside-check."""

    def run_task(self, script, **cfg):
        from test_gemini import Box, FakeClock
        box = Box(self, script, **cfg)
        clock = FakeClock()
        sup = G.Supervisor(G.load_config(box.cfg_path), clock=clock, sleep=clock.sleep, out=io.StringIO())
        return box, sup.start("add two and three")

    def test_compact_follow_up_is_shorter_and_the_first_turn_is_the_same(self):
        from test_gemini import plan
        script = [{"plan": plan([{"id": "a", "tool": "add", "args": {"a": 2, "b": 3}}], {"prompt": "report", "after": ["a"]})},
                  {"plan": plan(say="5")}]
        lens = {}
        for mode in ("compact", "verbatim"):
            box, ok = self.run_task(script, prompt_mode=mode)
            self.assertTrue(ok)
            lens[mode] = [c["prompt_len"] for c in box.calls()]
            turns = [r for r in box.log() if r["event"] == "turn"]
            self.assertEqual([r["prompt_mode"] for r in turns], ["verbatim", mode])
            self.assertTrue(all(isinstance(r["prompt_est"], int) and r["prompt_est"] > 0 for r in turns))
            self.assertEqual([r.get("prompt_mode") for r in box.log() if r["event"] == "task"], [mode])
        self.assertEqual(lens["compact"][0], lens["verbatim"][0])
        self.assertLess(lens["compact"][1], lens["verbatim"][1])

    def test_check_plan_decides_and_a_disagreement_is_logged(self):
        script = [{"plan": {"schema": S, "steps": [{"id": "a", "tool": "add", "args": {"a": 1, "b": 1}, "after": ["a"]}],
                            "next": None}}]
        box, ok = self.run_task(script)
        self.assertFalse(ok)  # check_plan rejects the self-reference, whatever the spec's check says
        self.assertEqual([r["ok"] for r in box.log() if r["event"] == "plan"], [False])
        rows = [r for r in box.log() if r["event"] == "check_disagree"]
        if expected_disagreements():
            self.assertEqual(rows, [{**rows[0], "check_plan_ok": False, "spec_ok": True, "step": "T1.m1"}])
        else:
            self.assertEqual(rows, [])


def once_resent(spec, d):
    """Cases where a compact follow-up on a host that resumes carries a line of the compact once-section."""
    bad = []
    for c in d["cases"]:
        if c["kind"] != "turn":
            continue
        tools, task, res, ask = inputs(d, c)
        vals = {"tools": G.spec_tools(tools), "task": task, "results": res, "ask": ask}
        once = [ln for ln in P.compile(spec, "once", vals, "compact").splitlines() if ln.strip()][:2]
        text = P.compile(spec, "turn", vals, "compact")
        if any(ln in text for ln in once) or (task and task.strip() and task in text):
            bad.append(c["tools"])
    return bad


@needs_pspec
class CheckAgreementTest(unittest.TestCase):
    """S4: check_plan is the authority; the same-spec check runs beside it."""

    def test_the_17_cases(self):
        self.assertEqual(len(CASES_17), 17)
        agree = [bool(G.check_plan(p, RUN_TOOLS)) == bool(G.spec_check(p, RUN_TOOLS)) for p in CASES_17]
        disagree = [i for i, a in enumerate(agree) if not a]
        self.assertEqual(disagree, expected_disagreements(),
                         f"rlo-sdk {rlo_version()}: {17 - len(disagree)}/17 agree")

    def test_boundaries_agree(self):
        for p in BOUNDARY:
            with self.subTest(plan=p):
                self.assertEqual(bool(G.check_plan(p, RUN_TOOLS)), bool(G.spec_check(p, RUN_TOOLS)))

    def test_the_self_reference_case_is_the_one_on_0_9_0(self):
        p = CASES_17[SELF_REFERENCE]
        self.assertEqual(G.check_plan(p, RUN_TOOLS), ["steps[0].after"])  # the authority rejects it
        self.assertEqual(G.spec_check(p, RUN_TOOLS) == [], rlo_version() == "0.9.0")


if __name__ == "__main__":
    unittest.main()
