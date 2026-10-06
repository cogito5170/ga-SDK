"""CMD-GA56 D1: the hub's daily turn cap counts only paid turns, and a cap that holds mail alerts baseline-ops once per
UTC day (never into the hub's own inbox). Offline (fake mailbox/judge/backend), 0 model runs."""
import json
import tempfile
import unittest
from pathlib import Path

from ga.console import collectors as C
from ga.hub import MailHub, read_jsonl
from tests.test_ga42 import Fake, World


def put_rows(w, rows, shadow=True):
    d = w.tmp / ".ga" / "hub"
    d.mkdir(parents=True, exist_ok=True)
    f = d / ("shadow.jsonl" if shadow else "ledger/2026-10-06.jsonl")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def hub(w, day=lambda: "2026-10-06", cap=2, shadow=True, **conf):
    w.conf["daily_turns"] = cap
    w.conf.update(conf)
    w.runner = Fake(["ASK_HUMAN q"] * 9)
    return MailHub(w.conf, ga_dir=w.tmp / ".ga", mailbox=w.mail, runner=w.runner, judge_fn=w.judgement("success"),
                   apply_fn=lambda *a: "x", today=day, shadow=shadow)


def free(i):
    return {"at": "2026-10-06T01:00:0%dZ" % i, "mail": f"old{i}", "tokens": {"input": None, "output": None}}


def paid(i):
    return {"at": "2026-10-06T02:00:0%dZ" % i, "mail": f"paid{i}", "tokens": {"input": 10, "output": 5}}


def alerts(w, to="baseline-ops"):
    return [h for t, h, _ in w.mail.sent if h.get("kind") == "alert" and t == to]


class Cap(unittest.TestCase):
    def test_free_rows_do_not_count(self):
        w = World(self)
        put_rows(w, [free(i) for i in range(5)] + [{**free(6), "tokens": {"input": 0, "output": 0}}, {"at": "2026-10-06T03:00:00Z"}])
        self.assertEqual(hub(w).turns_today(), 0)
        put_rows(w, [free(1), paid(1), {**free(2), "tokens": {"input": None, "output": 3}}])
        self.assertEqual(hub(w).turns_today(), 2)

    def test_free_ledger_lines_do_not_count_either(self):
        w = World(self)
        put_rows(w, [{"input": None, "output": None}, {"input": 7, "output": None}, {"input": None, "output": None}], shadow=False)
        self.assertEqual(hub(w, shadow=False).turns_today(), 1)

    def test_free_rows_do_not_block_mail(self):
        w = World(self)
        w.report()
        put_rows(w, [free(i) for i in range(6)])
        res = hub(w).tick()
        self.assertFalse(any("cap" in p for p in res.plan))
        self.assertEqual(alerts(w), [])
        self.assertEqual(len(read_jsonl(w.tmp / ".ga/hub/shadow.jsonl")), 7)

    def test_cap_holds_paid_rows_and_alerts_once_a_day(self):
        w = World(self)
        w.report()
        put_rows(w, [paid(1), paid(2)])
        h = hub(w)
        res = h.tick()
        self.assertTrue(any("daily turn cap 2 reached" in p for p in res.plan))
        self.assertEqual(len(read_jsonl(w.tmp / ".ga/hub/shadow.jsonl")), 2)  # nothing judged
        (a,) = alerts(w)
        self.assertEqual(a["schema"], "notify/1")
        self.assertRegex(a["note"], r"^daily turn cap 2 reached at 2026-10-06T\d\d:\d\dZ; 1 reports waiting; resumes 2026-10-07T00:00Z$")
        self.assertEqual(h.load_state()["cap_alerted"], "2026-10-06")
        self.assertEqual(h.load_state()["cap"], {"limit": 2, "used": 2, "waiting": 1})
        h.tick()
        self.assertEqual(len(alerts(w)), 1)  # second tick, same day: no new alert
        rows = read_jsonl(w.tmp / ".ga/hub/shadow.jsonl")
        put_rows(w, [{**r, "at": "2026-10-07T0" + r["at"][12:]} for r in rows])
        h2 = hub(w, day=lambda: "2026-10-07")
        h2.tick()
        self.assertEqual(len(alerts(w)), 2)  # next day: a new alert

    def test_failed_alert_mail_is_retried_next_tick(self):
        w = World(self)
        w.report()
        put_rows(w, [paid(1), paid(2)])
        h = hub(w)
        real, n = w.mail.send, [0]

        def flaky(to, text, sender=None):
            if to == "baseline-ops" and n[0] == 0:
                n[0] += 1
                raise OSError("push failed")
            return real(to, text, sender)
        w.mail.send = flaky
        h.tick()
        self.assertEqual(alerts(w), [])
        self.assertNotIn("cap_alerted", h.load_state())
        h.tick()
        self.assertEqual(len(alerts(w)), 1)

    def test_alert_never_goes_to_the_hubs_own_inbox(self):
        w = World(self)
        w.report()
        put_rows(w, [paid(1), paid(2)])
        hub(w, alert_to="baseline").tick()  # the hub's name is "baseline"
        self.assertEqual([t for t, h, _ in w.mail.sent if h.get("kind") == "alert"], [])
        self.assertEqual([m for m in w.mail.box.get("baseline", []) if "alert" in m.path], [])

    def test_no_alert_when_nothing_waits(self):
        w = World(self)
        put_rows(w, [paid(1), paid(2)])
        hub(w).tick()
        self.assertEqual(alerts(w), [])

    def test_console_state_shows_the_cap(self):
        w = World(self)
        w.report()
        put_rows(w, [paid(1), paid(2)])
        hub(w).tick()
        cfg = {"hub_state": str(w.tmp / ".ga/hub/state.json")}
        self.assertEqual(C.hub_cap(cfg), {"limit": 2, "used": 2, "waiting": 1})
        self.assertIsNone(C.hub_cap({"hub_state": str(w.tmp / "none.json")}))
        st = C.state({**cfg, "baseline": str(w.tmp), "repos": [], "bridge": None})
        self.assertEqual(st["cap"], {"limit": 2, "used": 2, "waiting": 1})


if __name__ == "__main__":
    unittest.main()
