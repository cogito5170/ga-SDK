"""CMD-GA32 S1/D1: ga/net on the real NET packages (ga-sdk[net]) and on the thin adapters, the same behaviour tests on both.

``MODES`` reruns every test class of test_ga31 (the peer runtime) once with the NET path forced on and once with the
fallback forced on; the rest compares the two numbers-for-numbers, checks the NET path really calls the packages (the
mutation in tests/mutations_ga32.py: fallback used when the extra is installed), and runs the fallback with the five
packages blocked from import. A test run on a venv without the extra skips the NET half.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ga31  # noqa: E402

from ga import _pins, l0  # noqa: E402
from ga.net import dc, pi as P, real  # noqa: E402
from ga.net.state import CONTRACT, State  # noqa: E402

HAVE_NET = real.importable()
ROOT = Path(__file__).resolve().parents[1]


def _variant(cls, on: bool, tag: str):
    class V(cls):
        def setUp(self):
            self._net = real.forced(on)
            self._net.__enter__()
            super().setUp()

        def tearDown(self):
            super().tearDown()
            self._net.__exit__(None, None, None)

    V.__name__ = V.__qualname__ = f"{cls.__name__}_{tag}"
    V.__module__ = __name__
    return V


for _n, _c in list(vars(test_ga31).items()):
    if isinstance(_c, type) and issubclass(_c, unittest.TestCase) and _c.__module__ == "test_ga31":
        globals()[f"{_n}_fallback"] = _variant(_c, False, "fallback")
        globals()[f"{_n}_net"] = unittest.skipUnless(HAVE_NET, "ga-sdk[net] not installed")(_variant(_c, True, "net"))
del _n, _c


@unittest.skipUnless(HAVE_NET, "ga-sdk[net] not installed")
class NetPathIsUsed(unittest.TestCase):
    """Fallback used when the extra is installed = the mutation these tests kill."""

    def test_enabled_when_installed_and_forced_off_by_env(self):
        self.assertTrue(real.enabled())
        with mock.patch.dict("os.environ", {"GA_NET": "fallback"}):
            self.assertFalse(real.enabled())
            self.assertEqual(real.source(), "fallback")

    def test_pi_comes_from_ms_network(self):
        with real.forced(True):
            self.assertEqual(P.source(), "ms.network")
            with mock.patch("ms.network.pi", return_value=0.5) as m:
                self.assertEqual(P.pi([], [], ["r"], ["r"], 0.0), 0.5)
            self.assertTrue(m.called)
            with mock.patch("ms.network.activate", return_value=(True, "ms")) as m:
                self.assertEqual(P.activate("A", "B", 0.0, P.NetConfig()), (True, "ms"))
            self.assertTrue(m.called)
            self.assertEqual(P.Edges().to_dict()["source"], "ms.network")
        with real.forced(False):
            self.assertEqual(P.source(), P.SOURCE_PORT)

    def test_peer_events_come_from_l0_telemetry(self):
        with real.forced(True), mock.patch("telemetry.event.make", wraps=__import__("telemetry.event").event.make) as m:
            l0.peer_message("sent", "run", 0, sender="A", to="B", msg_id="m1", schema="peer/1", nbytes=10)
        self.assertEqual(m.call_args.args[0], "peer.message.sent")
        with real.forced(False), mock.patch("telemetry.event.make") as m:
            l0.peer_message("sent", "run", 0, sender="A", to="B", msg_id="m1", schema="peer/1", nbytes=10)
        self.assertFalse(m.called)

    def test_purpose_comes_from_dc(self):
        with real.forced(True), mock.patch("dc.peer.peer_interaction_purpose",
                                           wraps=__import__("dc.peer").peer.peer_interaction_purpose) as m:
            dc.purpose(["agent.a"], [])
        self.assertTrue(m.called)
        with real.forced(False), mock.patch("dc.peer.peer_interaction_purpose") as m:
            dc.purpose(["agent.a"], [])
        self.assertFalse(m.called)

    def test_the_export_contract_is_dcs(self):
        from dc.sources import SENSOR_CONTRACT
        self.assertEqual(CONTRACT, SENSOR_CONTRACT)


@unittest.skipUnless(HAVE_NET, "ga-sdk[net] not installed")
class NetEqualsFallback(unittest.TestCase):
    def both(self, f):
        with real.forced(True):
            a = f()
        with real.forced(False):
            b = f()
        self.assertEqual(a, b)
        return a

    def test_peer_event(self):
        ev = self.both(lambda: l0.peer_message("received", "r", 3, sender="A", to="B", msg_id="m", schema="exchange/1",
                                               nbytes=9, in_reply_to="p"))
        self.assertEqual(ev["data"]["tokens_est"], 3)
        self.both(lambda: l0.peer_message("received", "r", 4, sender="A", to="B", msg_id="m", schema=None, nbytes=9))

    def test_pi_and_activation(self):
        inter = [P.Interaction(ts=1.0, transition=True, uncertain_before=2, uncertain_after=1),
                 P.Interaction(ts=2.0, transition=True, invalidated=True), P.Interaction(ts=3.0, verify=True)]
        obs = [P.Obs(0.0), P.Obs(1.0, True)]
        cfg = P.NetConfig(theta=0.3, half_life=10.0)
        v = self.both(lambda: P.pi(inter, obs, ["r", "s"], ["r"], 5.0, cfg))
        self.assertGreater(v, 0)
        for kw in ({}, {"missing": ["r"], "covers": {"B": ["r"]}}, {"verify_open": True},
                   {"missing": ["r"], "covers": {"B": ["r"], "C": ["r"]}}):
            self.both(lambda kw=kw: P.activate("A", "B", 0.1, cfg, **kw))
            self.both(lambda kw=kw: P.activate("A", "B", 0.9, cfg, **kw))

    def test_purpose(self):
        p = self.both(lambda: dc.purpose(["agent.a", "agent.b"], ["agent.c"]))
        self.assertEqual((p["name"], p["actions"], p["default"]), ("peer_interaction", ["consult", "send", "skip"], "skip"))
        self.both(lambda: dc.purpose([], []))

    def test_state_rules_agree_with_llmsensor_peer_rules(self):
        """The node's State (F1-F3) and llmsensor.state.peer give the same verdict on the same observation."""
        from llmsensor.state import peer as SP
        from llmsensor.state.engine import StateEngine
        from llmsensor.state.model import Basis, Evidence, Level, State as LS, Status
        ent, name = "agent:x", "health"
        E = StateEngine()
        ev = Evidence("e1", Level.OBSERVED if hasattr(Level, "OBSERVED") else list(Level)[0], "e1")
        E.current[(ent, name)] = LS(ent, name, "ok", list(Status)[0], list(Basis)[0], "r", 1, "c", "", (ev,), None, None, None)
        st = State("n")
        st.observe("agent.health", "ok", "e1", "self", ["agent.health"])
        cases = [("session:B", "ok", "e1"), ("session:B", "ok", "e2"), ("session:B", "bad", "e3"), ("session:C", "bad", "e3")]
        # no state change either way: ga calls evidence it already holds 'duplicate' (F3), llmsensor 'consistent'
        ok = {("duplicate", "consistent"), ("same", "consistent"), ("contradicts", "contradicts"), ("duplicate", "duplicate")}
        for sender, value, evid in cases:
            got = st.observe("agent.health", value, evid, sender, ["agent.health"])
            ref = SP.observe_peer(E, sender, ent, name, value, evid)
            self.assertIn((got, ref), ok, (sender, value, evid))
        self.assertEqual(st.uncertain(), ["agent.health"])
        self.assertTrue(SP.is_uncertain(E, ent, name))


class FallbackWithoutTheExtra(unittest.TestCase):
    def test_the_adapters_run_with_the_five_packages_blocked(self):
        code = (
            "import builtins, json\n"
            "real_import = builtins.__import__\n"
            "def guard(name, *a, **k):\n"
            "    if name.split('.')[0] in {'telemetry', 'ms', 'dc', 'llmsensor', 'action'}:\n"
            "        raise ImportError('blocked: ' + name)\n"
            "    return real_import(name, *a, **k)\n"
            "builtins.__import__ = guard\n"
            "from ga.net import real, pi as P, dc\n"
            "from ga import l0\n"
            "assert not real.enabled() and real.source() == 'fallback'\n"
            "assert P.source() == P.SOURCE_PORT\n"
            "ev = l0.peer_message('sent', 'r', 0, sender='A', to='B', msg_id='m', schema=None, nbytes=4)\n"
            "p = dc.purpose(['agent.a'], [])\n"
            "print(json.dumps([ev['type'], p['actions'], round(P.pi([], [], ['r'], ['r'], 0.0), 6)]))\n")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), ["peer.message.sent", ["consult", "send", "skip"], 0.333333])


class NetPins(unittest.TestCase):
    def test_pyproject_extra_says_what_net_pins_say(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        try:
            import tomllib
            extra = tomllib.loads(text)["project"]["optional-dependencies"]["net"]
        except ImportError:
            import re
            body = re.search(r"^net\s*=\s*\[(.*?)^\]", text, re.S | re.M).group(1)
            extra = re.findall(r'^\s*"([^"]*)"', body, re.M)
        self.assertEqual(sorted(extra), sorted(_pins.net_requirements()))

    def test_five_packages_by_full_sha(self):
        self.assertEqual(sorted(p[0] for p in _pins.NET_PINS.values()),
                         ["action-contract", "dc", "l0-telemetry", "llmsensor", "ms"])
        for _, _, url, sha in _pins.NET_PINS.values():
            self.assertRegex(sha, r"^[0-9a-f]{40}$")
            self.assertTrue(url.startswith("https://github.com/cogito5170/"))

    def test_installed_net_packages_are_the_pinned_ones(self):
        from importlib import metadata
        for dist, _, _, sha in _pins.NET_PINS.values():
            try:
                d = metadata.distribution(dist)
            except metadata.PackageNotFoundError:
                self.skipTest(f"{dist} is not installed")
            self.assertEqual(d.version, _pins.NET_VERSIONS[dist])
            direct = json.loads(d.read_text("direct_url.json") or "{}")
            self.assertEqual(direct.get("vcs_info", {}).get("commit_id"), sha, dist)


if __name__ == "__main__":
    unittest.main()
