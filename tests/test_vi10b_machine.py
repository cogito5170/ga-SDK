"""VI-10b (baseline acceptance test, VMAUTO; research/VM_INTERIOR_DESIGN.md §2): the work-item state machine T1-T11 in
shadow, no sessions. Every step appends exactly one journal/1 row (VI-10a); the state is fold(journal); replaying the
same events gives the same rows and states. T2, T3, T4 (and T6', the re-dispatch) are recorded as `would_do`.

ga/vm/machine.py (imports only re, typing, ga.forms, ga.forms.kinds, ga.net.pool, ga.vm.journal):
  ACTIVE = ("DISPATCHED", "ACTING", "REPORTED", "VERDICT");  DONE = ("INTEGRATED", "DEPLOYED", "OBSERVED")
  WOULD_DO = {"T2": "dispatch: admit a session for the item", "T3": "start the session at the base sha",
              "T4": "run the commit gate on the reported sha", "T6'": "dispatch again after the send-back"}
  class Machine:
    __init__(rows=()): self.rows = copies of rows; self.state = journal.fold(self.rows);
      self.items = {item: row["record"]["work"]} for every T1 row with guard.ok true
    step(ev) -> row, ev = {"event", "item", "at", "inputs" (optional dict, default {})}:
      unknown event -> JournalError; item not in state and event != "T1" -> JournalError (no row)
      cur = state.get(item, "NONE"); (ok, why, record) = the guard below; to = JOURNAL_TRANSITIONS[event][1] if ok else cur
      row = {"schema": "journal/1", "id": "J-<len(rows)+1>", "at": ev["at"], "item", "from_state": cur, "to_state": to,
             "event", "guard": {"ok", "why"}, "inputs_hash": journal.inputs_hash(inputs), "record"}
      a row with a hard journal/1 problem -> JournalError; else append it, state[item] = to (unless to is "NONE"),
      a T1 with ok sets items[item] = record["work"]; return the row.
  Guards (refused -> ok false, record {}; `why` is free text, non-empty):
    any event but T11: cur != JOURNAL_TRANSITIONS[event][0] -> refused
    T1   work = {"after": sorted(inputs.after or []), "files": sorted(inputs.files or [])}; files empty -> refused;
         ga.net.pool._cycle({k: items[k]["after"]} + {item: work.after}, item) -> refused; else record {"work": work}
    T2, T6'  every `after` item's state in DONE, else refused; refused when pool.overlap(items[item].files,
         items[o].files) for any other item o whose state is in ACTIVE; refused when inputs.max_concurrent is not None
         and the number of other items in ACTIVE >= it; else record {"would_do": WOULD_DO[event]}
    T3   record {"would_do": WOULD_DO["T3"]}
    T4   inputs.sha must match ^[0-9a-f]{7,40}$ else refused; record {"would_do": WOULD_DO["T4"], "sha": sha}
    T5   same sha rule; record {"sha": sha}
    T6   inputs.decision == "SEND_BACK" else refused; n = earlier T6 rows of this item with guard.ok; refused when
         inputs.sendback_cap is not None and n >= it; record {"reason": str(inputs.reason or ""), "sendbacks": n + 1}
    T7   decision == "SHADOW" else refused; record {"reason": str(inputs.reason or "")}
    T8   decision == "ACCEPT" and inputs.halt is not True, else refused; record {}
    T9   inputs.descendant is True else refused; record {"running_sha": str(inputs.running_sha or "")}
    T10  inputs.alarm is False else refused; record {}
    T11  cur == "CANCELLED" -> refused; else ok from any state, record {"reason": str(inputs.reason or "")}
  replay(events) -> (rows, state) of a fresh Machine stepped through the events.
"""
import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ga.forms import hard, validate

ROOT = Path(__file__).resolve().parent.parent
SHA = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"


def e(event, item, n, **inputs):
    ev = {"event": event, "item": item, "at": f"2026-10-07T03:{n:02d}:00Z"}
    if inputs:
        ev["inputs"] = inputs
    return ev


# A at full length (with one send-back), B waits for A, C overlaps A's files, D is cancelled, E is shadowed
EVENTS = [
    e("T1", "CMD-A1", 0, files=["ga/x.py"], after=[]),
    e("T1", "CMD-B1", 1, files=["ga/y.py"], after=["CMD-A1"]),
    e("T1", "CMD-C1", 2, files=["ga/*"]),
    e("T1", "CMD-D1", 3, files=["docs/d.md"]),
    e("T1", "CMD-E1", 4, files=["docs/e.md"]),
    e("T1", "CMD-F1", 5, files=[]),                         # refused: no files
    e("T1", "CMD-A1", 6, files=["ga/x.py"]),                # refused: already planned
    e("T2", "CMD-A1", 7),
    e("T2", "CMD-B1", 8),                                   # refused: after A not integrated
    e("T2", "CMD-C1", 9),                                   # refused: files overlap A (active)
    e("T2", "CMD-D1", 10, max_concurrent=1),                # refused: A is active, cap 1
    e("T3", "CMD-A1", 11),
    e("T4", "CMD-A1", 12, sha="xyz"),                       # refused: no sha
    e("T4", "CMD-A1", 13, sha=SHA),
    e("T5", "CMD-A1", 14, sha=SHA),
    e("T8", "CMD-A1", 15, decision="SEND_BACK"),            # refused: not an accept
    e("T6", "CMD-A1", 16, decision="SEND_BACK", reason="survived m1", sendback_cap=1),
    e("T6'", "CMD-A1", 17),
    e("T3", "CMD-A1", 18),
    e("T4", "CMD-A1", 19, sha=SHA),
    e("T5", "CMD-A1", 20, sha=SHA),
    e("T6", "CMD-A1", 21, decision="SEND_BACK", sendback_cap=1),   # refused: cap reached
    e("T8", "CMD-A1", 22, decision="ACCEPT", halt=True),           # refused: halt
    e("T8", "CMD-A1", 23, decision="ACCEPT"),
    e("T9", "CMD-A1", 24, descendant=False),                # refused
    e("T9", "CMD-A1", 25, descendant=True, running_sha=SHA),
    e("T10", "CMD-A1", 26),                                 # refused: alarm not reported
    e("T10", "CMD-A1", 27, alarm=False),
    e("T2", "CMD-B1", 28),                                  # A done: B dispatched
    e("T2", "CMD-C1", 29),                                  # refused: still overlaps? no: B is ga/y.py, C ga/* -> overlap
    e("T11", "CMD-D1", 30, reason="spec withdrawn"),
    e("T11", "CMD-D1", 31),                                 # refused: already cancelled
    e("T2", "CMD-E1", 32),
    e("T3", "CMD-E1", 33),
    e("T4", "CMD-E1", 34, sha=SHA[:7]),
    e("T5", "CMD-E1", 35, sha=SHA[:7]),
    e("T7", "CMD-E1", 36, decision="SHADOW", reason="UI feel"),
]
OK = [True, True, True, True, True, False, False, True, False, False, False, True, False, True, True, False, True, True,
      True, True, True, False, False, True, False, True, False, True, True, False, True, False, True, True, True, True,
      True]
FINAL = {"CMD-A1": "OBSERVED", "CMD-B1": "DISPATCHED", "CMD-C1": "PLANNED", "CMD-D1": "CANCELLED", "CMD-E1": "SHADOW"}


class Steps(unittest.TestCase):
    def setUp(self):
        from ga.vm import journal as J
        from ga.vm import machine as M
        self.J, self.M = J, M

    def test_constants(self):
        self.assertEqual(self.M.ACTIVE, ("DISPATCHED", "ACTING", "REPORTED", "VERDICT"))
        self.assertEqual(self.M.DONE, ("INTEGRATED", "DEPLOYED", "OBSERVED"))
        self.assertEqual(set(self.M.WOULD_DO), {"T2", "T3", "T4", "T6'"})

    def test_one_valid_row_per_step_and_the_guards(self):
        rows, state = self.M.replay(EVENTS)
        self.assertEqual(len(rows), len(EVENTS))
        self.assertEqual([r["guard"]["ok"] for r in rows], OK)
        for i, (r, ev) in enumerate(zip(rows, EVENTS), 1):
            self.assertEqual(hard(validate(r)), [], r)
            self.assertEqual((r["id"], r["at"], r["item"], r["event"]), (f"J-{i}", ev["at"], ev["item"], ev["event"]))
            self.assertEqual(r["inputs_hash"], self.J.inputs_hash(ev.get("inputs", {})))
            self.assertTrue(r["guard"]["why"].strip())
            if not r["guard"]["ok"]:
                self.assertEqual((r["to_state"], r["record"]), (r["from_state"], {}))
        self.assertEqual(state, FINAL)
        self.assertEqual(self.J.fold(rows), FINAL)

    def test_would_do_is_recorded_for_t2_t3_t4_and_re_dispatch(self):
        rows, _ = self.M.replay(EVENTS)
        for r in rows:
            if r["guard"]["ok"] and r["event"] in ("T2", "T3", "T4", "T6'"):
                self.assertEqual(r["record"]["would_do"], self.M.WOULD_DO[r["event"]], r)
            else:
                self.assertNotIn("would_do", r["record"], r)
        t4 = [r for r in rows if r["event"] == "T4" and r["guard"]["ok"]]
        self.assertEqual([r["record"]["sha"] for r in t4], [SHA, SHA, SHA[:7]])

    def test_records(self):
        rows, _ = self.M.replay(EVENTS)
        self.assertEqual(rows[0]["record"], {"work": {"after": [], "files": ["ga/x.py"]}})
        self.assertEqual(rows[1]["record"], {"work": {"after": ["CMD-A1"], "files": ["ga/y.py"]}})
        self.assertEqual(rows[16]["record"], {"reason": "survived m1", "sendbacks": 1})
        self.assertEqual(rows[14]["record"], {"sha": SHA})
        self.assertEqual(rows[23]["record"], {})
        self.assertEqual(rows[25]["record"], {"running_sha": SHA})
        self.assertEqual(rows[30]["record"], {"reason": "spec withdrawn"})
        self.assertEqual(rows[36]["record"], {"reason": "UI feel"})

    def test_after_cycle_is_refused(self):
        m = self.M.Machine()
        m.step(e("T1", "CMD-A1", 0, files=["a"], after=["CMD-B1"]))
        r = m.step(e("T1", "CMD-B1", 1, files=["b"], after=["CMD-A1"]))
        self.assertFalse(r["guard"]["ok"])
        self.assertEqual((r["from_state"], r["to_state"]), ("NONE", "NONE"))
        self.assertEqual(m.state, {"CMD-A1": "PLANNED"})

    def test_unknown_item_or_event_raises_and_writes_nothing(self):
        m = self.M.Machine()
        with self.assertRaises(self.J.JournalError):
            m.step(e("T2", "CMD-Z1", 0))
        with self.assertRaises(self.J.JournalError):
            m.step(e("T99", "CMD-Z1", 0))
        self.assertEqual(m.rows, [])

    def test_wrong_state_is_a_refused_row(self):
        m = self.M.Machine()
        m.step(e("T1", "CMD-A1", 0, files=["a"]))
        r = m.step(e("T5", "CMD-A1", 1, sha=SHA))
        self.assertEqual((r["guard"]["ok"], r["from_state"], r["to_state"]), (False, "PLANNED", "PLANNED"))


class Replay(unittest.TestCase):
    def setUp(self):
        from ga.vm import journal as J
        from ga.vm import machine as M
        self.J, self.M = J, M

    def test_twice_identical(self):
        a = self.M.replay(EVENTS)
        b = self.M.replay(json.loads(json.dumps(EVENTS)))
        self.assertEqual(a, b)
        self.assertEqual("".join(self.J.dumps(r) for r in a[0]), "".join(self.J.dumps(r) for r in b[0]))

    def test_resume_from_the_journal_on_disk(self):
        rows, state = self.M.replay(EVENTS)
        cut = 20
        head, _ = self.M.replay(EVENTS[:cut])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "journal.jsonl"
            for r in head:
                self.J.append(p, r)
            m = self.M.Machine(self.J.read(p))
            self.assertEqual(m.state, self.J.fold(head))
            for ev in EVENTS[cut:]:
                self.J.append(p, m.step(ev))
            self.assertEqual(m.rows, rows)
            self.assertEqual(m.state, state)
            self.assertEqual(self.J.fold(self.J.read(p)), state)


class Shadow(unittest.TestCase):
    def test_no_session_and_no_model_code(self):
        tree = ast.parse((ROOT / "ga" / "vm" / "machine.py").read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add("." * n.level + (n.module or ""))
        allowed = {"__future__", "re", "typing", "..forms", "..forms.kinds", "..net.pool", ".journal",
                   "ga.forms", "ga.forms.kinds", "ga.net.pool", "ga.vm.journal"}
        self.assertLessEqual(mods, allowed, mods)
        code = ("import sys, ga.vm.machine\nbad=[m for m in sys.modules if m.startswith(('ga.llm','ga.backends',"
                "'ga.gemini','ga.act','ga.hub','ga.bridge','ga.net.node','ga.net.real'))]\nprint(bad)\nsys.exit(1 if bad else 0)")
        p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
