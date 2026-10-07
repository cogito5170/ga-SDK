"""VI-10a (baseline acceptance test, VMAUTO; research/VM_INTERIOR_DESIGN.md §1 journal channel, §2, §10 state/):
the journal/1 form in the forms registry, and the work-item states as fold(journal). No model, no session.

Registry (ga/forms/registry.json, given by baseline): enums JOURNAL_STATES, JOURNAL_EVENTS; form journal/1 with the
fields id, at, item, from_state, to_state, event, guard, inputs_hash, record (all required). docs/FORMS.md is
regenerated from it (tests/test_vi06_registry.py).

ga/forms/kinds.py:
  JOURNAL_STATES = tuple(_REG['enums']['JOURNAL_STATES']); JOURNAL_EVENTS likewise
  JOURNAL_TRANSITIONS = {"T1": ("NONE", "PLANNED"), "T2": ("PLANNED", "DISPATCHED"), "T3": ("DISPATCHED", "ACTING"),
      "T4": ("ACTING", "REPORTED"), "T5": ("REPORTED", "VERDICT"), "T6": ("VERDICT", "SEND_BACK"),
      "T6'": ("SEND_BACK", "DISPATCHED"), "T7": ("VERDICT", "SHADOW"), "T8": ("VERDICT", "INTEGRATED"),
      "T9": ("INTEGRATED", "DEPLOYED"), "T10": ("DEPLOYED", "OBSERVED"), "T11": (None, "CANCELLED")}
  SCHEMAS["journal/1"] = (JOURNAL, _journal_cross), field checks:
    id          "J-<n>", n >= 1 (^J-[1-9]\\d*$)
    at          ^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z$
    item        a work/1 item id: ^(?:CMD-[A-Z]+\\d+|[A-Z][A-Z0-9]*-[A-Z]+-\\d+)$ (= ga.net.pool.ITEM_ID)
    from_state  "NONE" (an item the journal has not seen) or one of JOURNAL_STATES
    to_state    "NONE" or one of JOURNAL_STATES
    event       one of JOURNAL_EVENTS
    guard       exactly {ok: bool, why: non-empty str}
    inputs_hash 64 lowercase hex
    record      an object
  _journal_cross: guard.ok false -> to_state must equal from_state (a refused transition keeps the state);
    guard.ok true -> (from_state, to_state) == JOURNAL_TRANSITIONS[event]; for T11 from_state is any state except
    NONE and CANCELLED, to_state CANCELLED. Otherwise a hard problem at $.event (or $.to_state for a refusal).

ga/vm/journal.py (imports only hashlib, json, pathlib, typing and ga.forms):
  class JournalError(ValueError)
  inputs_hash(inputs) = sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                               .encode("utf-8")).hexdigest()
  fold(rows) -> {item: state}: rows in order; each row must validate as journal/1 with 0 hard problems, its id must be
    J-<its 1-based position>, and its from_state must equal the item's current state ("NONE" when unseen), else
    JournalError; the item's state becomes to_state. Items whose state is "NONE" are left out; keys sorted.
  dumps(row) = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\\n"
  append(path, row): JournalError on a hard problem; makes parent dirs; appends dumps(row)
  read(path) -> rows (json per non-blank line); a missing file is []
"""
import ast
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ga.forms import hard, validate
from ga.forms import kinds as K
from ga.forms import registry as REG

ROOT = Path(__file__).resolve().parent.parent
STATES = ["PLANNED", "DISPATCHED", "ACTING", "REPORTED", "VERDICT", "SEND_BACK", "SHADOW", "INTEGRATED", "DEPLOYED",
          "OBSERVED", "BLOCKED", "CANCELLED"]
EVENTS = ["T1", "T2", "T3", "T4", "T5", "T6", "T6'", "T7", "T8", "T9", "T10", "T11"]
FIELDS = ["id", "at", "item", "from_state", "to_state", "event", "guard", "inputs_hash", "record"]
H = "0" * 64


def row(n, item, f, t, ev, ok=True, **kw):
    r = {"schema": "journal/1", "id": f"J-{n}", "at": "2026-10-07T03:00:00Z", "item": item, "from_state": f,
         "to_state": t, "event": ev, "guard": {"ok": ok, "why": "test"}, "inputs_hash": H, "record": {}}
    r.update(kw)
    return r


class Form(unittest.TestCase):
    def test_registry_has_the_form_and_enums(self):
        reg = REG.load()
        self.assertEqual(reg["enums"]["JOURNAL_STATES"], STATES)
        self.assertEqual(reg["enums"]["JOURNAL_EVENTS"], EVENTS)
        self.assertEqual(list(reg["forms"]["journal/1"]["fields"]), FIELDS)
        self.assertTrue(all(v["required"] for v in reg["forms"]["journal/1"]["fields"].values()))
        self.assertEqual(tuple(K.JOURNAL_STATES), tuple(STATES))
        self.assertEqual(tuple(K.JOURNAL_EVENTS), tuple(EVENTS))
        self.assertIn("journal/1", K.SCHEMAS)

    def test_enums_are_read_from_the_registry(self):
        src = (ROOT / "ga" / "forms" / "kinds.py").read_text(encoding="utf-8")
        for name in ("JOURNAL_STATES", "JOURNAL_EVENTS"):
            self.assertRegex(src, rf"(?m)^{name}\s*=\s*tuple\(_REG\['enums'\]\['{name}'\]\)")

    def test_transitions_table(self):
        self.assertEqual(K.JOURNAL_TRANSITIONS, {
            "T1": ("NONE", "PLANNED"), "T2": ("PLANNED", "DISPATCHED"), "T3": ("DISPATCHED", "ACTING"),
            "T4": ("ACTING", "REPORTED"), "T5": ("REPORTED", "VERDICT"), "T6": ("VERDICT", "SEND_BACK"),
            "T6'": ("SEND_BACK", "DISPATCHED"), "T7": ("VERDICT", "SHADOW"), "T8": ("VERDICT", "INTEGRATED"),
            "T9": ("INTEGRATED", "DEPLOYED"), "T10": ("DEPLOYED", "OBSERVED"), "T11": (None, "CANCELLED")})

    def test_every_legal_transition_is_valid(self):
        for ev, (f, t) in K.JOURNAL_TRANSITIONS.items():
            for src in ([f] if f else [s for s in STATES if s != "CANCELLED"]):
                with self.subTest(ev=ev, src=src):
                    self.assertEqual(hard(validate(row(1, "CMD-X1", src, t, ev))), [])

    def test_refused_rows_keep_the_state(self):
        self.assertEqual(hard(validate(row(1, "CMD-X1", "PLANNED", "PLANNED", "T2", ok=False))), [])
        self.assertEqual(hard(validate(row(1, "CMD-X1", "NONE", "NONE", "T1", ok=False))), [])
        self.assertTrue(hard(validate(row(1, "CMD-X1", "PLANNED", "DISPATCHED", "T2", ok=False))))

    def test_bad_rows_are_hard(self):
        bad = [row(1, "CMD-X1", "PLANNED", "ACTING", "T2"),           # not T2's target
               row(1, "CMD-X1", "NONE", "DISPATCHED", "T2"),          # unseen item cannot be dispatched
               row(1, "CMD-X1", "CANCELLED", "CANCELLED", "T11"),     # T11 is not from CANCELLED
               row(1, "CMD-X1", "NONE", "CANCELLED", "T11"),          # nor from NONE
               row(1, "CMD-X1", "NONE", "PLANNED", "T12"),
               row(0, "CMD-X1", "NONE", "PLANNED", "T1"),
               row(1, "cmd-x1", "NONE", "PLANNED", "T1"),
               row(1, "CMD-X1", "NONE", "PLANNED", "T1", at="2026-10-07 03:00"),
               row(1, "CMD-X1", "NONE", "PLANNED", "T1", inputs_hash="ABC"),
               row(1, "CMD-X1", "NONE", "PLANNED", "T1", guard={"ok": True}),
               row(1, "CMD-X1", "NONE", "PLANNED", "T1", guard={"ok": 1, "why": "x"}),
               row(1, "CMD-X1", "NONE", "PLANNED", "T1", record=[]),
               row(1, "CMD-X1", "NONE", "PLANNED", "T1", extra=1),
               row(1, "CMD-X1", "NONE", "WAITING", "T1")]
        for r in bad:
            with self.subTest(r=r):
                self.assertTrue(hard(validate(r)), r)
        r = row(1, "CMD-X1", "NONE", "PLANNED", "T1")
        del r["record"]
        self.assertTrue(hard(validate(r)))

    def test_item_ids_follow_work_1(self):
        from ga.net.pool import ITEM_ID
        for i in ("CMD-VIB4", "W-FE-01", "CMD-X1"):
            self.assertTrue(ITEM_ID.match(i))
            self.assertEqual(hard(validate(row(1, i, "NONE", "PLANNED", "T1"))), [])


class Fold(unittest.TestCase):
    def setUp(self):
        from ga.vm import journal as J
        self.J = J

    def test_inputs_hash(self):
        x = {"b": [1, "é"], "a": None}
        want = hashlib.sha256(json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(self.J.inputs_hash(x), want)
        self.assertEqual(self.J.inputs_hash({"a": None, "b": [1, "é"]}), want)
        self.assertNotEqual(self.J.inputs_hash({}), want)

    def rows(self):
        return [row(1, "CMD-A1", "NONE", "PLANNED", "T1"), row(2, "CMD-B1", "NONE", "PLANNED", "T1"),
                row(3, "CMD-A1", "PLANNED", "DISPATCHED", "T2"), row(4, "CMD-B1", "PLANNED", "PLANNED", "T2", ok=False),
                row(5, "CMD-C1", "NONE", "NONE", "T1", ok=False), row(6, "CMD-B1", "PLANNED", "CANCELLED", "T11")]

    def test_fold(self):
        self.assertEqual(self.J.fold([]), {})
        st = self.J.fold(self.rows())
        self.assertEqual(st, {"CMD-A1": "DISPATCHED", "CMD-B1": "CANCELLED"})
        self.assertEqual(list(st), sorted(st))
        self.assertEqual(self.J.fold(self.rows()[:3]), {"CMD-A1": "DISPATCHED", "CMD-B1": "PLANNED"})

    def test_fold_refuses_a_broken_journal(self):
        rs = self.rows()
        with self.assertRaises(self.J.JournalError):  # from_state does not match the fold
            self.J.fold(rs[:2] + [row(3, "CMD-A1", "DISPATCHED", "ACTING", "T3")])
        with self.assertRaises(self.J.JournalError):  # a row missing (id gap)
            self.J.fold(rs[:2] + [dict(rs[3], id="J-4")])
        with self.assertRaises(self.J.JournalError):  # rows reordered
            self.J.fold([rs[1], rs[0]] + rs[2:])
        with self.assertRaises(self.J.JournalError):  # a hard form problem
            self.J.fold([row(1, "CMD-A1", "NONE", "DISPATCHED", "T1")])
        self.assertTrue(issubclass(self.J.JournalError, ValueError))

    def test_append_read_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state" / "journal" / "2026-10-07.jsonl"
            self.assertEqual(self.J.read(p), [])
            for r in self.rows():
                self.J.append(p, r)
            self.assertEqual(self.J.read(p), self.rows())
            self.assertEqual(p.read_text(encoding="utf-8"), "".join(self.J.dumps(r) for r in self.rows()))
            self.assertEqual(self.J.dumps(self.rows()[0]),
                             json.dumps(self.rows()[0], sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.assertEqual(self.J.fold(self.J.read(p)), self.J.fold(self.rows()))
            with self.assertRaises(self.J.JournalError):
                self.J.append(p, row(7, "CMD-A1", "NONE", "DISPATCHED", "T1"))
            self.assertEqual(len(self.J.read(p)), 6)


class NoModel(unittest.TestCase):
    def test_imports(self):
        tree = ast.parse((ROOT / "ga" / "vm" / "journal.py").read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add("." * n.level + (n.module or ""))
        self.assertLessEqual(mods, {"__future__", "hashlib", "json", "pathlib", "typing", "..forms", "ga.forms"}, mods)
        code = ("import sys, ga.vm.journal\nbad=[m for m in sys.modules if m.startswith(('ga.llm','ga.backends',"
                "'ga.gemini','ga.act','ga.hub','ga.adapters'))]\nprint(bad)\nsys.exit(1 if bad else 0)")
        p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
