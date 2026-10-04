"""CMD-GR9: the doctor replay follows K13's rule D (rlo-sdk >= 0.8.0, BD-246 option B).

Stale-only health no longer denies ('Bash after 2h idle' passes); unknown health still does ('Bash beside a call with
no result': the session's first calls, sent together, with no result yet). The expectation follows the rlo version the
doctor reports for the replay venv, never a sha. The preset does not ask for the old D (no --health-ttl).
"""
import json
import sys

from ga.rlo import remote

from .test_remote import RemoteBase, VENV

UNKNOWN = "Bash beside a call with no result"
IDLE = "Bash after 2h idle"


class VersionGateTest(RemoteBase):
    def test_k13_follows_the_version_not_a_sha(self):
        self.assertFalse(remote.k13("0.7.0"))
        self.assertFalse(remote.k13("0.6.0"))
        self.assertTrue(remote.k13("0.8.0"))
        self.assertTrue(remote.k13("0.8.2"))
        self.assertTrue(remote.k13("1.0"))
        self.assertEqual(remote.k13(None), remote.k13(remote._pins.VERSIONS["rlo-sdk"]))  # unknown: what ga pins

    def test_the_idle_case_expects_k13_d_on_08_and_the_old_d_before(self):
        old = {c.name: c for c in remote.cases(rlo="0.7.0")}
        new = {c.name: c for c in remote.cases(rlo="0.8.2")}
        self.assertEqual((old[IDLE].want, old[IDLE].react["cause"]), ("D", "stale"))
        self.assertEqual((new[IDLE].want, new[IDLE].react), ("pass", None))
        for cs in (old, new):  # unknown health: D on both
            self.assertEqual((cs[UNKNOWN].want, cs[UNKNOWN].react["cause"]), ("D", "unavailable"))

    def test_doctor_reports_the_replayed_rlo(self):
        from .world import run_cli

        self.init()
        rc, out, _ = run_cli("doctor", "--profile", "remote", "--work-repo", str(self.repo), "--venv", VENV, "--json")
        checks = {c["check"]: c for c in json.loads(out)["checks"]}
        v = remote.rlo_version(VENV)
        import rlo

        self.assertEqual(v, rlo.__version__)
        self.assertTrue(checks["remote.rlo"]["ok"])
        self.assertIn(f"rlo-sdk {v} replayed", checks["remote.rlo"]["detail"])
        self.assertIn("K13" if remote.k13(v) else "before K13", checks["remote.rlo"]["detail"])
        self.assertIsNone(remote.rlo_version(self.tmp / "no-venv"))


class RuleDReplayTest(RemoteBase):
    def setUp(self):
        super().setUp()
        self.init()

    def replayed(self):
        return {n: (ok, d) for n, ok, d in remote.replay(self.repo, sys.prefix, only=[IDLE, UNKNOWN])}

    def test_unknown_health_is_denied_by_d(self):
        ok, detail = self.replayed()[UNKNOWN]
        self.assertTrue(ok, detail)
        self.assertIn("denied D", detail)
        self.assertIn("kind=wait_previous", detail)

    def test_the_idle_case_matches_the_installed_rlo(self):
        ok, detail = self.replayed()[IDLE]
        self.assertTrue(ok, detail)
        self.assertEqual(detail.startswith("not blocked"), remote.k13(remote.rlo_version(sys.prefix)))

    def test_mutant_old_d_asked_for_in_the_preset_is_killed(self):
        """S2/D1: a guard that asks rlo for the pre-K13 D (--health-ttl) is caught -- statically, and on rlo >= 0.8 by
        the idle replay (stale health denies again). On 0.7 rlo rejects the flag, so the guard fails closed."""
        self.edit("ops/rlo/guard.py", 'argv += ["--record", REC]', 'argv += ["--health-ttl", "--record", REC]')
        self.assertTrue(any("--health-ttl" in p for p in remote.problems(self.repo)))
        bad = self.doctor_failed()
        self.assertIn("remote.preset", bad)
        self.assertIn(f"remote.replay.{IDLE}", bad)


if __name__ == "__main__":
    import unittest

    unittest.main()
