"""CMD-GA49 D1: shadow rows carry error/served/asks; shadow-compare's gate is 10 consecutive agreements; report/2
model is the served one. Offline, 0 model runs."""
import unittest

from ga.bridge import build_report
from ga.forms import parse_text
from ga.hub import shadow_compare
from tests.test_ga42 import World
from tests.test_ga42_shadow import hub
from ga.hub import read_jsonl


class Boom:
    bare = True

    def run_turn(self, *a, **k):
        e = RuntimeError("down")
        e.reason = "http_503"
        raise e


class ShadowRow(unittest.TestCase):
    def test_runner_error_is_in_the_row(self):
        w = World(self)
        w.report()
        h = hub(w, ["ACCEPT"], shadow=True)
        h.runner = Boom()
        h.tick()
        (row,) = read_jsonl(w.tmp / ".ga/hub/shadow.jsonl")
        self.assertEqual(row["decision"], "ASK_HUMAN")
        self.assertEqual(row["error"], "backend:http_503")
        self.assertIsNone(row["served"])
        self.assertEqual(len(row["asks"]), 1)

    def test_served_and_no_error_on_success(self):
        w = World(self)
        w.report()
        hub(w, ["ACCEPT"], shadow=True).tick()
        (row,) = read_jsonl(w.tmp / ".ga/hub/shadow.jsonl")
        self.assertIsNone(row["error"])
        self.assertEqual(row["served"], "fake-small")


def rows(n, bad=(), err=()):
    b = [{"id": f"C{i}", "rev": 1, "decision": "ACCEPT"} for i in range(n)]
    s = [{"id": f"C{i}", "rev": 1, "at": f"2026-10-05T00:00:{i:02d}Z",
          "decision": "ASK_HUMAN" if i in err else ("SEND_BACK" if i in bad else "ACCEPT"),
          "error": "backend:x" if i in err else None} for i in range(n)]
    return b, s


class Gate(unittest.TestCase):
    def test_zero_agreements_is_not_ok(self):
        b, s = rows(18, bad=range(18))
        o = shadow_compare(b, s)
        self.assertFalse(o["gate_ok"])
        self.assertEqual(o["gate"], "gate 0/10")

    def test_ten_agreements_ok_nine_not(self):
        self.assertTrue(shadow_compare(*rows(10))["gate_ok"])
        o = shadow_compare(*rows(9))
        self.assertFalse(o["gate_ok"])
        self.assertEqual(o["gate"], "gate 9/10")

    def test_shadow_error_resets_the_run_and_is_counted_apart(self):
        o = shadow_compare(*rows(15, err={9}))
        self.assertEqual(o["shadow_errors"], ["C9 rev 1"])
        self.assertEqual(o["agree"], 14)
        self.assertFalse(o["gate_ok"])  # only 5 since the error
        self.assertEqual(o["false_accepts"], [])
        self.assertTrue(shadow_compare(*rows(20, err={9}))["gate_ok"])

    def test_a_false_accept_blocks_even_after_ten(self):
        b, s = rows(12)
        b[0]["decision"] = "SEND_BACK"
        o = shadow_compare(b, s)
        self.assertEqual(o["false_accepts"], ["C0 rev 1"])
        self.assertFalse(o["gate_ok"])


class BridgeModel(unittest.TestCase):
    def model(self, turns):
        cfg = {"name": "agy", "workdir": "/nonexistent", "max_answer_chars": 100}
        run = {"code": 0, "out": "ok", "events": [{"event": "turn", **t} for t in turns] + [{"event": "end", "status": "done"}]}
        head = {"id": "CMD-T1", "rev": 1, "done_when": [{"id": "D1"}]}
        r = parse_text(build_report(cfg, head, run))[0]
        return next((x["value"] for x in r["results"] if x["name"] == "model"), None)

    def test_served_not_configured(self):
        self.assertEqual(self.model([{"ok": True, "model": "gpt-oss-120b-medium", "served": ["gemini-3.8-flash-high"]}]),
                         "gemini-3.8-flash-high")

    def test_unknown_is_none(self):
        self.assertIsNone(self.model([{"ok": True, "model": "gpt-oss-120b-medium"}]))


if __name__ == "__main__":
    unittest.main()


class ShadowNotify(unittest.TestCase):
    def test_notify_and_mailbox_rows_carry_the_new_fields(self):
        from ga.forms import parse_text, validate, hard
        w = World(self)
        w.report()
        h = hub(w, ["ACCEPT"], shadow=True)
        h.runner = Boom()
        h.tick()
        (row,) = read_jsonl(w.tmp / ".ga/hub/shadow.jsonl")
        text = h.shadow_form(row)
        head, _ = parse_text(text)
        self.assertEqual(hard(validate(head)), [])
        self.assertEqual(head["shadow"]["error"], "backend:http_503")
        self.assertEqual(head["shadow"]["asks"], ["the model turn failed"])
        r = {**head["shadow"], "tokens": {"input": None, "output": None}}  # what shadow_rows_from_mailbox returns
        self.assertEqual(r["error"], "backend:http_503")
        out = shadow_compare([{"id": row["id"], "rev": row["rev"], "decision": "ACCEPT"}], [r])
        self.assertEqual(out["shadow_errors"], [f"{row['id']} rev {row['rev']}"])
