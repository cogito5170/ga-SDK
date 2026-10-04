"""CMD-GA27 (BD-297/298): semantic, compact messages between sessions and tools.

S1 the wire form: one ```ga block of minified JSON plus the footer; `ga check` flags wire:prose and wire:pretty
(soft); a minified head-only message has no problem; parse(minified) == parse(pretty) for every historical head.
S5 the offline token report: (a) the 279 comments of BD-297 at least 38% smaller in the S1 form; (b) ga inbox
delivers each message once, so its read volume does not depend on the read schedule; (c) the writer's bytes.
S2/S3 the four forms as prompt-spec/1 (ga/specs/forms): `ga render` is deterministic in and across processes for every
form kind; rlo.pspec's check agrees with ga.forms on every historical head, and each disagreement has its reason.
The history is tests/fixtures/wire/channels.json: the ga heads (no prose) of baseline #11 #12 #16 #18.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ga import forms as F
from ga.__main__ import main

try:
    import rlo.pspec  # noqa: F401  (K15/K16)
    HAS_PSPEC = True
except ImportError:
    HAS_PSPEC = False
needs_pspec = unittest.skipUnless(HAS_PSPEC, "needs rlo-sdk >= 0.9 (rlo.pspec) — installed with ga-sdk")

FIX = Path(__file__).resolve().parent / "fixtures" / "wire"
ROOT = Path(__file__).resolve().parents[1]


def history():
    return json.loads((FIX / "channels.json").read_text(encoding="utf-8"))


def heads(only_json=True):
    """(comment, parsed head) for every historical comment with a ga head that is JSON."""
    out = []
    for c in history()["comments"]:
        if c["head_text"] is None:
            continue
        try:
            out.append((c, json.loads(c["head_text"])))
        except ValueError:
            if not only_json:
                out.append((c, None))
    return out


def run(*argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def write(text):
    d = Path(tempfile.mkdtemp())
    f = d / "m.md"
    f.write_text(text, encoding="utf-8")
    return f


REPORT = {"schema": "report/2", "from": "GA", "handled": [{"id": "CMD-GA27", "rev_seen": 1, "status": "done"}],
          "items": [{"id": "D1", "state": "met", "evidence": ["verified: the wire tests"]}], "note": "short, for people"}


class WireTest(unittest.TestCase):
    def check(self, text):
        return run("--config", "/nonexistent/ga.json", "check", str(write(text)))

    def test_a_minified_head_only_message_has_no_problem(self):
        for head in (REPORT, {"schema": "notify/1", "to": "baseline", "kind": "report",
                              "ref": "https://github.com/o/r/issues/12#issuecomment-1"}):
            code, out, _ = self.check(F.dump_wire(head))
            self.assertEqual((code, out), (0, ""))
            self.assertEqual(F.wire_problems(F.dump_wire(head)), [])

    def test_prose_and_pretty_are_soft_problems(self):
        pretty = "```ga\n" + json.dumps(REPORT, indent=1) + "\n```\n## Result\nWhat the head already says.\n\n---\n" \
                 + F.FOOTER + "\n"
        code, out, _ = self.check(pretty)
        self.assertEqual(code, 0)  # soft only
        self.assertIn("[soft] wire:prose", out)
        self.assertIn("[soft] wire:pretty", out)
        self.assertEqual([p.rule for p in F.wire_problems(F.dump_text(REPORT, ""))], ["wire:pretty"])  # spaces
        self.assertEqual([p.rule for p in F.wire_problems(F.dump_wire(REPORT).replace("\n---", "\nmore\n---"))],
                         ["wire:prose"])

    def test_the_footer_alone_is_not_prose_and_other_forms_are_not_checked(self):
        mini = "```ga\n" + F.minify(REPORT) + "\n```\n"
        self.assertEqual(F.wire_problems(mini), [])
        self.assertEqual(F.wire_problems(mini + "\n---\n" + F.FOOTER), [])
        self.assertEqual(F.wire_problems(mini + F.FOOTER + "\n"), [])
        stage = F.dump_text({"schema": "stage/1", "name": "x"}, "## Notes\nprose is fine here\n")
        self.assertEqual(F.wire_problems(stage), [])

    def test_note_is_bounded(self):
        for schema_doc in (REPORT, {"schema": "notify/1", "to": "A", "kind": "ack", "ref": "https://x/y"}):
            self.assertEqual(F.validate({**schema_doc, "note": "n" * 280}), [])
            self.assertEqual([p.path for p in F.validate({**schema_doc, "note": "n" * 281})], ["$.note"])

    def test_round_trip_on_every_historical_head(self):
        hs = heads()
        self.assertGreaterEqual(len(hs), 180)
        for c, h in hs:
            with self.subTest(id=c["id"]):
                wire = F.dump_wire(h)
                again, body = F.parse_text(wire)
                self.assertEqual(again, h)  # parse(minified) == parse(pretty)
                self.assertEqual(F.strip_footer(body).strip(), "")
                self.assertNotIn("wire:pretty", [p.rule for p in F.wire_problems(wire)])

    def test_ga_notify_wire(self):
        code, out, _ = run("notify", "--to", "baseline", "--kind", "report", "--ref", "https://x/y#1", "--id",
                           "CMD-GA27", "--wire")
        self.assertEqual(code, 0)
        self.assertEqual(F.wire_problems(out), [])
        self.assertEqual(F.parse_text(out)[0]["kind"], "report")


VALID_HEADS = 120  # historical heads of the four kinds that ga.forms accepts


def classify(forms_probs, spec_probs):
    """Why ga.forms and the spec check disagree: each reason is a type prompt-spec/1 does not have."""
    if forms_probs and not spec_probs:
        msgs = " ".join(str(p) for p in forms_probs)
        if all(("must be one of" in str(p)) or ("must be a URL" in str(p)) or ("must be D<number>" in str(p))
               or ("must be S<number>" in str(p)) or ("must be CMD-" in str(p)) for p in forms_probs):
            return "pspec has no enum or pattern type" if "must be one of" in msgs else "pspec has no pattern type"
        return "unexplained: ga.forms rejects"
    if spec_probs and not forms_probs:
        if all(p.endswith((".value", "].value")) or ".ci" in p for p in spec_probs):
            return "pspec has no number or union type (results value / ci)"
        return "unexplained: the spec rejects"
    return None


@needs_pspec
class FormSpecTest(unittest.TestCase):
    def setUp(self):
        from ga import wire
        self.wire = wire

    def kinds(self):
        return [(c, h) for c, h in heads() if h.get("schema") in self.wire.FORM_SPECS]

    def test_agreement_with_ga_forms_on_every_historical_head(self):
        rows = self.kinds()
        self.assertEqual(len(rows), 141)
        reasons = {}
        for c, h in rows:
            forms_probs = F.hard(F.validate(h))
            spec_probs = self.wire.spec_check(h)
            why = classify(forms_probs, spec_probs)
            if why:
                reasons.setdefault(why, []).append(c["id"])
        self.assertFalse([k for k in reasons if k.startswith("unexplained")], reasons)
        self.assertEqual({k: len(v) for k, v in reasons.items()},
                         {"pspec has no pattern type": 10, "pspec has no enum or pattern type": 5,
                          "pspec has no number or union type (results value / ci)": 18})
        self.assertEqual(len(rows) - sum(map(len, reasons.values())), 108)

    def test_render_is_deterministic_and_matches_the_golden(self):
        golden = json.loads((FIX / "render_golden.json").read_text(encoding="utf-8"))
        byid = {c["id"]: h for c, h in heads()}
        for name, g in golden.items():
            h = g.get("head") or byid[g["id"]]
            for lang in ("en", "ko"):
                with self.subTest(name=name, lang=lang):
                    once = self.wire.render(h, lang)
                    self.assertEqual(once, self.wire.render(h, lang))
                    self.assertEqual(once, g[lang])
        kinds = {(g.get("head") or byid[g["id"]])["schema"] for g in golden.values()}
        self.assertEqual(kinds, {"directive/2", "report/2", "verdict/1", "notify/1"})  # every form kind

    def test_every_valid_historical_head_renders_in_both_languages(self):
        n = 0
        for c, h in self.kinds():
            if F.hard(F.validate(h)):
                continue
            for lang in ("en", "ko"):
                self.assertEqual(self.wire.render(h, lang), self.wire.render(h, lang))
                n += 1
        self.assertEqual(n, 2 * VALID_HEADS)

    def test_ga_render_in_a_fresh_process(self):
        golden = json.loads((FIX / "render_golden.json").read_text(encoding="utf-8"))
        env = dict(os.environ, PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        for name in ("full_report", "full_directive", "full_notify"):
            f = write(F.dump_wire(golden[name]["head"]))
            for lang in ("en", "ko"):
                p = subprocess.run([sys.executable, "-m", "ga", "render", str(f), "--lang", lang], capture_output=True,
                                   env=env)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(p.stdout.decode("utf-8"), golden[name][lang])

    def test_render_refuses_an_invalid_head_and_reads_stdin(self):
        bad = {"schema": "notify/1", "to": "A", "kind": "shout", "ref": "nowhere"}
        code, _, err = run("render", str(write(F.dump_wire(bad))))
        self.assertEqual(code, 2)
        self.assertIn("$.kind", err)
        golden = json.loads((FIX / "render_golden.json").read_text(encoding="utf-8"))
        old = sys.stdin
        sys.stdin = io.StringIO(F.dump_wire(golden["full_notify"]["head"]))
        try:
            code, out, _ = run("render", "-", "--lang", "ko")
        finally:
            sys.stdin = old
        self.assertEqual((code, out), (0, golden["full_notify"]["ko"]))


class TokenReportTest(unittest.TestCase):
    def setUp(self):
        from ga import wire
        self.wire, self.corpus = wire, history()

    def test_a_the_279_comments_are_at_least_38_percent_smaller(self):
        a = self.wire.token_report(self.corpus)["a_wire"]
        self.assertEqual((a["comments"], a["with_head"]), (279, 177))
        self.assertGreaterEqual(a["saved_pct"], 38.0)  # baseline's floor (BD-298)
        self.assertEqual(a["bytes_now"], sum(c["bytes"] for c in sorted(self.corpus["comments"],
                                                                        key=lambda c: c["created_at"])[:279]))

    def test_b_inbox_volume_does_not_depend_on_the_schedule(self):
        log = json.loads((FIX / "ao_reads_model.json").read_text(encoding="utf-8"))
        cs = sorted(self.corpus["comments"], key=lambda c: c["created_at"])[:279]
        b = self.wire.token_report(self.corpus, reads=log["reads"], notify_now_bytes=log["notify_now_bytes"])["b_reads"]
        self.assertEqual((b["reads"], b["notifies"]), (143, 63))
        once = sum(self.wire.inbox_line_bytes(c) for c in cs)
        one_read = [{"at": "2026-10-04T15:35:45Z", "issue": i, "kind": "read"} for i in (11, 12, 16, 18)]
        self.assertEqual(self.wire.replay(cs, one_read, notify_now_bytes=0, notify_wire_bytes=0)["bytes_inbox"], once)
        notify = len(F.dump_wire({"schema": "notify/1", "to": "GA", "kind": "report",
                                  "ref": "https://github.com/cogito5170/baseline/issues/12#issuecomment-5982260506"})
                     .encode())
        self.assertEqual(b["bytes_inbox"], once + 63 * notify)  # every message once, plus the 63 notify lines
        self.assertLess(b["bytes_inbox"], b["bytes_now"])

    def test_c_the_writer_emits_fewer_bytes(self):
        c = self.wire.token_report(self.corpus)["c_writer"]
        self.assertEqual(sorted(c), ["directive", "report"])
        for k in c.values():
            self.assertLess(k["bytes_s1_mean"], k["bytes_now_mean"])

    def test_cli(self):
        code, out, err = run("wire-report", str(FIX / "channels.json"), "--reads", str(FIX / "ao_reads_model.json"))
        self.assertEqual(code, 0, err)
        r = json.loads(out)
        self.assertEqual(sorted(r), ["a_wire", "b_reads", "c_writer", "estimate", "schema"])


if __name__ == "__main__":
    unittest.main()
