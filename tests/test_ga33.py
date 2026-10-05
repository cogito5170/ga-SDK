"""CMD-GA33: peer mode's work queue and node pool. Fake backends only: 0 network, 0 model calls."""
import io
import json
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import net_world as nw  # noqa: E402
from ga import __main__ as cli  # noqa: E402
from ga.net import pool as P  # noqa: E402
from ga.net.state import State  # noqa: E402

REF = "image.img1.description"


def network(roles=None, **pool):
    net = nw.t1_network()
    net["nodes"] = {"B": net["nodes"]["B"]}
    base = {"max_live": 2, "max_queue": 20, "max_spawn_per_round": 5, "max_depth": 2, "idle_rounds": 3,
            "roles": roles or {"writer": {"backends": ["fake_text"], "pack_max_tokens": 3000},
                               "seer": {"backends": ["fake_text", "fake_vision"], "pack_max_tokens": 3000}}}
    base.update(pool)
    net["pool"] = base
    return net


def item(i, role="writer", **kw):
    return {"id": i, "role": role, "goal": "say " + i, **kw}


class PoolCase(unittest.TestCase):
    def world(self, net=None, script=None):
        self.w = nw.World(self, net or network())
        if script:
            self.w.backends["fake_text"].script = script
        self.cfg = self.w.cfg()
        return self.w

    def pool(self):
        return P.Pool(self.w.cfg(), self.w.ga, get_backend=self.w.get, clock=self.w.clock)

    def round(self, pl=None):
        self.w.t[0] += 10
        return (pl or self.pool()).round()[-1]["pool"]

    def add(self, *items):
        for it in items:
            ok, why = self.pool().add(it)
            self.assertTrue(ok, why)

    def reg(self):
        return self.pool().reg()

    def pl0(self):
        f = self.w.ga / "pool" / "telemetry.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


class TestAllocation(PoolCase):
    def test_live_cap_queue_order_and_drain(self):
        self.world()
        self.add(*[item(f"CMD-T{k}") for k in range(6)])
        seen, order = [], []
        for _ in range(12):
            ev = self.round()
            seen.append(len(self.reg()["live"]))
            order += [s["item"] for s in ev["started"]]
        self.assertLessEqual(max(seen), 2)
        self.assertEqual(order, [f"CMD-T{k}" for k in range(6)])
        st = self.pool().status()
        self.assertEqual((st["queue"], st["live"], st["failed"]), ([], {}, []))
        self.assertEqual(st["done"], [f"CMD-T{k}" for k in range(6)])

    def test_spawn_per_round_cap_and_retired_dir(self):
        self.world(network(max_spawn_per_round=1))
        self.add(item("CMD-A1"), item("CMD-B1"))
        self.assertEqual(len(self.round()["started"]), 1)
        self.round()
        self.assertTrue((self.w.ga / "nodes" / "_retired" / "writer_1" / "state.json").exists())
        self.assertFalse((self.w.ga / "nodes" / "writer_1").exists())
        types = [(e["type"], e["data"]["node"]) for e in self.pl0() if e["type"].startswith("node.")]
        self.assertEqual(types[:2], [("node.started", "writer_1"), ("node.retired", "writer_1")])

    def test_restart_between_claim_and_seed_starts_nothing_twice(self):
        self.world()
        self.add(item("CMD-A1"))
        pl = self.pool()
        orig = pl._seed
        def boom(*a):
            raise RuntimeError("crash")
        pl._seed = boom
        with self.assertRaises(RuntimeError):
            self.round(pl)
        pl._seed = orig
        self.assertEqual(list(self.reg()["live"]), ["writer_1"])
        for _ in range(3):
            self.round()
        started = [e for e in self.pl0() if e["type"] == "node.started"]
        self.assertEqual([e["data"]["item"] for e in started], ["CMD-A1"])
        self.assertEqual(self.pool().status()["done"], ["CMD-A1"])
        self.assertEqual(self.reg()["n"], {"writer": 1})

    def test_restart_mid_retire_finishes_without_second_node(self):
        self.world()
        self.add(item("CMD-A1"))
        self.round()
        pl = self.pool()
        orig = pl._retire
        def boom(*a):
            orig(*a)
            raise RuntimeError("crash after the retire work, before the registry")
        pl._retire = boom
        with self.assertRaises(RuntimeError):
            self.round(pl)
        self.assertEqual(self.reg()["live"]["writer_1"]["retiring"], "done")
        self.round()
        self.assertEqual(self.reg()["live"], {})
        self.assertEqual(self.pool().status()["done"], ["CMD-A1"])
        self.assertEqual(self.reg()["n"], {"writer": 1})

    def test_idle_requeues_then_runs_budget_fails_the_item(self):
        net = network(idle_rounds=1)
        net["pool"]["roles"]["writer"]["budget"] = {"runs": 2}
        self.world(net)
        self.add(item("CMD-A1", uses=["never.known"]))
        for _ in range(8):
            self.round()
        st = self.pool().status()
        self.assertEqual((st["failed"], st["queue"], st["live"]), (["CMD-A1"], [], {}))
        self.assertEqual(self.reg()["n"], {"writer": 2})  # no third node: the runs budget was spent
        self.assertIn("work.failed", [e["type"] for e in self.pl0()])


class TestRoleState(PoolCase):
    def test_second_node_answers_a_known_item_with_no_turn_no_tool_no_message(self):
        w = self.world()
        self.add(item("CMD-L1", "seer", uses=[REF]))
        for _ in range(6):
            self.round()
        self.assertEqual(self.pool().status()["done"], ["CMD-L1"])
        rs = json.loads((w.ga / "roles" / "seer" / "state.json").read_text())
        self.assertEqual(rs["facts"][REF]["value"], nw.DESCRIPTION)
        turns = len(w.backends["fake_vision"].calls) + len(w.backends["fake_text"].calls)
        self.add(item("CMD-L2", "seer", uses=[REF], answer=REF))
        self.round()
        out = self.pool_step("seer_2")
        self.assertEqual(out["outcome"], "answered_from_state")
        self.assertEqual((out["sent"], out["received"], out["tool_calls"]), ([], 0, 0))
        self.assertEqual(len(w.backends["fake_vision"].calls) + len(w.backends["fake_text"].calls), turns)
        run = json.loads((w.ga / "nodes" / "seer_2" / "state.json").read_text())["done"]["CMD-L2"]
        self.assertEqual((run["turns"], run["verified"]), (0, True))

    def pool_step(self, me):
        self.w.t[0] += 10
        return self.w.node(me).step()

    def test_retire_merge_makes_a_contradicting_fact_uncertain_never_overwrites(self):
        role = State("r")
        role.observe("x", "one", "e0", "self", ["x"])
        node = State("r_1")
        node.observe("x", "two", "e1", "self", ["x"])
        node.observe("y", "why", "e2", "self", ["y"])
        P.merge_state(role, node, "r_1")
        self.assertTrue(role.facts["x"]["uncertain"])
        self.assertEqual(role.facts["x"]["value"], "one")
        self.assertEqual(role.facts["y"]["value"], "why")
        self.assertIn("F2", [t["rule"] for t in role.transitions])
        self.assertIsNone(role.value("x"))
        P.merge_state(role, node, "r_1")  # idempotent
        self.assertEqual(len(role.relations), 1)

    def test_retire_merges_through_the_runtime_and_keeps_facts(self):
        net = network()
        net["pool"]["roles"]["writer"]["facts"] = {"doc.title": {"value": "NEW", "evidence": "doc#2"}}
        w = self.world(net)
        (w.ga / "roles" / "writer").mkdir(parents=True)
        rs = State("writer")
        rs.observe("doc.title", "OLD", "doc#1", "self", ["doc.title"])
        (w.ga / "roles" / "writer" / "state.json").write_text(json.dumps(rs.to_dict()))
        self.add(item("CMD-A1"))
        for _ in range(3):
            self.round()
        got = json.loads((w.ga / "roles" / "writer" / "state.json").read_text())
        self.assertTrue(got["facts"]["doc.title"]["uncertain"])
        self.assertEqual(got["facts"]["doc.title"]["value"], "OLD")

    def test_retire_keeps_what_another_node_added_to_the_role_meanwhile(self):
        net = network()
        net["pool"]["roles"]["writer"]["facts"] = {"doc.title": {"value": "T", "evidence": "doc#1"}}
        w = self.world(net)
        self.add(item("CMD-A1"))
        self.round()  # node writer_1 starts (the role has no State yet)
        other = State("writer")
        other.observe("z", "zed", "ev-z", "session:other", ["z"])
        (w.ga / "roles" / "writer").mkdir(parents=True, exist_ok=True)
        (w.ga / "roles" / "writer" / "state.json").write_text(json.dumps(other.to_dict()))
        self.round()
        got = json.loads((w.ga / "roles" / "writer" / "state.json").read_text())["facts"]
        self.assertEqual((got["z"]["value"], got["doc.title"]["value"]), ("zed", "T"))

    def test_edges_fold_to_role_pairs_and_expand_for_live_peers(self):
        rp = P.fold_edges({}, {"interactions": {"seer_1": [{"ts": 1}]}, "observations": {"seer_1": [{"ts": 2}], "B": [{"ts": 3}]},
                               "flags": {"B": "open"}}, (lambda x: {"seer_1": "seer"}.get(x, x)))
        self.assertEqual(rp["interactions"], {"seer": [{"ts": 1}]})
        again = P.fold_edges(rp, {"interactions": {"seer_1": [{"ts": 1}]}}, (lambda x: {"seer_1": "seer"}.get(x, x)))
        self.assertEqual(again["interactions"], {"seer": [{"ts": 1}]})
        ex = P.expand_edges(rp, ["seer_2", "B"], lambda x: {"seer_2": "seer"}.get(x, x))
        self.assertEqual(ex["interactions"], {"seer_2": [{"ts": 1}]})
        self.assertEqual(ex["flags"], {"B": "open"})


def work_script(proposals):
    def script(prompt, system, model, options):
        text = nw.default_script(prompt, system, model, options)
        blocks = "".join("```work\n" + json.dumps(p) + "\n```\n" for p in proposals)
        return text.replace("```state", blocks + "```state")
    return script


class TestWorkProposals(PoolCase):
    def drop_reasons(self):
        return {e["data"]["id"]: e["data"]["reason"] for e in self.pl0() if e["type"] == "work.dropped"}

    def accepted(self):
        return [e["data"]["id"] for e in self.pl0() if e["type"] == "work.accepted"]

    def test_child_accepted_then_grandchild_dropped_over_depth(self):
        self.world(script=work_script([{"role": "writer", "goal": "child"}]))
        self.add(item("CMD-TOP1"))
        for _ in range(8):
            self.round()
        self.assertEqual(self.accepted(), ["CMD-TOP1.w1"])
        self.assertIn("CMD-W2.w1", self.drop_reasons())
        self.assertRegex(self.drop_reasons()["CMD-W2.w1"], "depth")
        self.assertEqual(self.pool().status()["done"], ["CMD-TOP1", "CMD-W2"])

    def test_over_queue_over_budget_unknown_role_and_check_key(self):
        net = network(max_queue=2, max_depth=3)
        net["pool"]["roles"]["writer"]["budget"] = {"work": 1}
        props = [{"role": "writer", "goal": "a"}, {"role": "writer", "goal": "b"},
                 {"role": "ghost", "goal": "c"}, {"role": "writer", "goal": "d", "check": ["rm", "-rf", "x"]}]
        self.world(net, script=work_script(props))
        self.add(item("CMD-TOP1"))
        self.round()
        self.round()
        r = self.drop_reasons()
        self.assertEqual(self.accepted(), ["CMD-TOP1.w1"])
        self.assertRegex(r["CMD-TOP1.w2"], "queue full")
        self.assertRegex(r["CMD-TOP1.w3"], "unknown role")
        self.assertRegex(r["CMD-TOP1.w4"], "not a proposal")

    def test_over_budget(self):
        net = network(max_depth=3)
        net["pool"]["roles"]["writer"]["budget"] = {"work": 1}
        self.world(net, script=work_script([{"role": "writer", "goal": "a"}, {"role": "writer", "goal": "b"}]))
        self.add(item("CMD-TOP1"))
        self.round()
        self.round()
        self.assertEqual(self.accepted(), ["CMD-TOP1.w1"])
        self.assertRegex(self.drop_reasons()["CMD-TOP1.w2"], "work budget")

    def test_no_check_key_even_when_accepted_items_are_read_back(self):
        self.world(script=work_script([{"role": "writer", "goal": "a"}]))
        self.add(item("CMD-TOP1"))
        self.round()
        self.round()
        found = list((self.w.ga / "queue").glob("**/*CMD-W2.json"))
        self.assertEqual(len(found), 1)
        child = json.loads(found[0].read_text())
        self.assertNotIn("check", child)
        self.assertEqual((child["parent"], child["depth"]), ("CMD-TOP1", 1))


class TestMixedAndSafety(PoolCase):
    def test_pool_nodes_are_peers_of_static_nodes_and_pi_sees_them(self):
        w = self.world()
        self.add(item("CMD-A1", uses=["never.known"]))
        self.round()
        self.assertEqual(P.peer_names(w.cfg(), w.ga), ["B", "writer_1"])
        self.pool_step("writer_1")
        self.assertIn("B", w.read("writer_1", "pi.json")["pi"])
        w.step("B")
        self.assertIn("writer_1", w.read("B", "pi.json")["pi"])

    def pool_step(self, me):
        self.w.t[0] += 10
        return self.w.node(me).step()

    def test_a_node_writes_only_in_its_dir(self):
        self.world()
        self.add(item("CMD-A1"))
        self.round()
        n = self.w.node("writer_1")
        with self.assertRaises(PermissionError):
            n._write("../outside.txt", "x")
        self.assertFalse((self.w.ga / "nodes" / "outside.txt").exists())

    def test_static_only_config_reads_no_registry_and_is_unchanged(self):
        w = nw.World(self)
        self.assertEqual(P.peer_names(w.cfg(), w.ga), ["A", "B", "C"])
        w.rounds(2)
        self.assertFalse((w.ga / "pool.json").exists())
        self.assertFalse((w.ga / "queue").exists())

    def test_unknown_node_is_an_error(self):
        w = self.world()
        from ga.net.router import ConfigError
        with self.assertRaises(ConfigError):
            w.node("writer_9")


class TestConfigAndCli(PoolCase):
    def problems(self, **pool):
        return [str(p) for p in P.problems_of(network(**pool))]

    def test_validation(self):
        self.assertEqual(self.problems(), [])
        self.assertTrue(self.problems(max_live=0))
        self.assertTrue(self.problems(bogus=1))
        self.assertTrue(self.problems(roles={"bad name": {"backends": ["x"]}}))
        self.assertTrue(self.problems(roles={"B": {"backends": ["x"]}}))  # collides with static node B
        self.assertTrue(self.problems(roles={"r": {"backends": ["x"], "task": {"id": "t"}}}))
        net = network()
        net["mode"] = "hub"
        self.assertTrue(P.problems_of(net))

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--config", str(self.w.config), *argv])
        finally:
            sys.stdin = old
        return code, out.getvalue(), err.getvalue()

    def test_work_add_ls_and_pool_status(self):
        self.world()
        f = self.w.tmp / "i.json"
        f.write_text(json.dumps(item("CMD-Q1")))
        code, out, _ = self.run_cli("work", "add", str(f))
        self.assertEqual(code, 0)
        self.assertIn("000001-CMD-Q1", out)
        self.assertEqual(self.run_cli("work", "add", str(f))[0], 2)  # duplicate id
        self.assertEqual(self.run_cli("work", "add", "-", stdin=json.dumps(item("CMD-Q2", role="nope")))[0], 2)
        self.assertEqual(self.run_cli("work", "add", "-", stdin=json.dumps(item("CMD-Q3")))[0], 0)
        self.assertEqual(json.loads(self.run_cli("work", "ls")[1])["queue"], ["CMD-Q1", "CMD-Q3"])
        st = json.loads(self.run_cli("pool", "status")[1])
        self.assertEqual((st["caps"]["max_live"], st["queue"], st["live"]), (2, ["CMD-Q1", "CMD-Q3"], {}))
        before = sorted(p.name for p in (self.w.ga / "queue").iterdir())
        self.assertEqual(before, ["000001-CMD-Q1.json", "000002-CMD-Q3.json"])

    def test_queue_full_is_refused(self):
        self.world(network(max_queue=1))
        self.add(item("CMD-A1"))
        ok, why = self.pool().add(item("CMD-B1"))
        self.assertFalse(ok)
        self.assertIn("full", why)


if __name__ == "__main__":
    unittest.main()
