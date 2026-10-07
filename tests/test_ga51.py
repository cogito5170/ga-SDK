"""CMD-GA51 D1: hub.json "model": "auto" follows the model the bridge saw served; check_served still holds against the
resolved model; ga vm migrates only the models ga itself wrote. Offline (fake backend), 0 model runs.
VI-04b (baseline amendment): the shadow hub makes no model turn any more, so the model plumbing is pinned on the
non-shadow hub, whose ledger row carries the same model fields."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ga.backends.base import BackendTurn, check_served
from ga.bridge import record_served
from ga.hub import HUB_MODEL_DEFAULT, read_jsonl, resolve_model
from ga.vm import core
from tests.test_ga42 import World
from tests.test_ga42_shadow import hub


class Host:
    """A fake agv: serves ``serves`` whatever it was made for, and enforces check_served like the real backends."""

    def __init__(self, model, serves):
        self.model, self.serves, self.bare = model, serves, True

    def run_turn(self, prompt, session_id=None, *, system=None, **k):
        check_served([self.serves], self.model)
        return BackendTurn("ACCEPT", [self.serves], {"input_tokens": 10, "output_tokens": 5}, "anthropic", None, 0.01, 1)


def shadow_tick(t, served_record, serves):
    w = World(t)
    w.report()
    sf = w.tmp / "served.json"
    if served_record is not None:
        sf.write_text(json.dumps({"model": served_record, "at": "2026-10-06T00:00:00Z"}), encoding="utf-8")
    w.conf.update(model="auto", served_file=str(sf))
    h = hub(w, ["ACCEPT"], shadow=False)
    h.runner = None  # the backend is made from hub.json, as on the VM
    made = []

    def create(name, model, opts, ctx):
        made.append(model)
        return Host(model, serves)
    with mock.patch("ga.backends.create", create):
        h.tick()
    (row,) = read_jsonl(w.tmp / ".ga/hub/ledger/2026-10-05.jsonl")
    return row, made, h


class Auto(unittest.TestCase):
    def test_auto_resolves_to_the_served_model(self):
        row, made, _ = shadow_tick(self, "gemini-3.8-flash-high", "gemini-3.8-flash-high")
        self.assertEqual(made, ["gemini-3.8-flash-high"])
        self.assertFalse(row["error"])  # the ledger row writes "" for no error
        self.assertEqual(row["decision"], "ACCEPT")
        self.assertEqual((row["model_configured"], row["model_resolved"]), ("auto", "gemini-3.8-flash-high"))
        self.assertNotIn("model_note", row)

    def test_mismatch_against_the_resolved_model_still_asks_a_human(self):
        row, made, _ = shadow_tick(self, "gemini-3.8-flash-high", "gpt-oss-120b-medium")
        self.assertEqual(made, ["gemini-3.8-flash-high"])
        self.assertEqual(row["decision"], "ASK_HUMAN")
        self.assertEqual(row["error"], "backend:served_model_mismatch")

    def test_unknown_falls_back_to_the_code_default_with_a_note(self):
        row, made, _ = shadow_tick(self, None, HUB_MODEL_DEFAULT)
        self.assertEqual(made, [HUB_MODEL_DEFAULT])
        self.assertEqual((row["model_configured"], row["model_resolved"]), ("auto", HUB_MODEL_DEFAULT))
        self.assertIn("no served model", row["model_note"])

    def test_a_written_model_is_used_as_written(self):
        self.assertEqual(resolve_model({"model": "my-model", "served_file": "/nonexistent"}),
                         {"configured": "my-model", "resolved": "my-model"})

    def test_a_new_served_model_makes_a_new_backend_next_tick(self):
        _, _, h = shadow_tick(self, "gemini-3.8-flash-high", "gemini-3.8-flash-high")
        first = h.runner
        Path(h.conf["served_file"]).write_text(json.dumps({"model": "gpt-oss-120b-medium"}), encoding="utf-8")
        self.assertEqual(h._resolve_model()["resolved"], "gpt-oss-120b-medium")
        self.assertIsNone(h.runner)
        self.assertIsNotNone(first)

    def test_bridge_records_the_served_rung_and_the_hub_reads_it(self):
        with tempfile.TemporaryDirectory() as d:
            sf = Path(d) / "bridge" / "served.json"
            run = {"events": [{"event": "turn", "model": "gpt-oss-120b-medium", "served": ["gemini-3.8-flash-high"]}]}
            self.assertEqual(record_served({"served_file": str(sf)}, run, now=lambda: "T"), "gemini-3.8-flash-high")
            self.assertEqual(json.loads(sf.read_text()), {"model": "gemini-3.8-flash-high", "at": "T"})
            self.assertEqual(resolve_model({"model": "auto", "served_file": str(sf)})["resolved"], "gemini-3.8-flash-high")
            self.assertIsNone(record_served({"served_file": str(Path(d) / "x.json")}, {"events": [{"event": "turn"}]}))
            self.assertFalse((Path(d) / "x.json").exists())

    def test_a_fallback_chain_records_the_last_rung(self):
        with tempfile.TemporaryDirectory() as d:
            sf = Path(d) / "served.json"
            run = {"events": [{"event": "turn", "served": ["rung-a", "rung-b"]}]}
            self.assertEqual(record_served({"served_file": str(sf)}, run, now=lambda: "T"), "rung-b")
            self.assertEqual(json.loads(sf.read_text())["model"], "rung-b")
            self.assertEqual(resolve_model({"model": "auto", "served_file": str(sf)})["resolved"], "rung-b")


class Migration(unittest.TestCase):
    def home(self, model):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d, True)
        (d / ".ga").mkdir()
        (d / ".ga" / "hub.json").write_text(json.dumps({"name": "baseline", "model": model, "shadow": True}))
        return d

    def model(self, d):
        return json.loads((d / ".ga" / "hub.json").read_text())["model"]

    def test_ga_defaults_become_auto(self):
        for old in ("gemini-3.1-pro-high", "gpt-oss-120b-medium"):
            d, lines = self.home(old), []
            self.assertTrue(core.migrate_hub_model(d, say=lines.append))
            self.assertEqual(self.model(d), "auto")
            self.assertEqual(len(lines), 1)
            self.assertIn(old, lines[0])
            self.assertEqual(json.loads((d / ".ga" / "hub.json").read_text())["name"], "baseline")

    def test_a_user_model_is_left_alone(self):
        for mine in ("gemini-3.8-flash-high", "auto", None):
            d, lines = self.home(mine), []
            self.assertFalse(core.migrate_hub_model(d, say=lines.append))
            self.assertEqual(self.model(d), mine)
            self.assertEqual(lines, [])

    def test_one_system_event_only_when_rewritten(self):
        ev = Path(tempfile.mkdtemp()) / "events.jsonl"
        with mock.patch.dict("os.environ", {"GA_EVENTS": str(ev)}):
            core.migrate_hub_model(self.home("gemini-3.8-flash-high"), say=lambda m: None)
            self.assertFalse(ev.exists() and ev.read_text().strip())
            core.migrate_hub_model(self.home("gpt-oss-120b-medium"), say=lambda m: None)
        (line,) = [json.loads(x) for x in ev.read_text().splitlines()]
        self.assertEqual((line["type"], line["status"]), ("SYSTEM", "DONE"))
        self.assertEqual(line["metadata"]["old"], "gpt-oss-120b-medium")

    def test_dry_run_changes_nothing(self):
        d, lines = self.home("gemini-3.1-pro-high"), []
        self.assertTrue(core.migrate_hub_model(d, dry_run=True, say=lines.append))
        self.assertEqual(self.model(d), "gemini-3.1-pro-high")
        self.assertTrue(lines[0].startswith("would:"))

    def test_new_installs_write_auto(self):
        self.assertEqual(core.hub_conf(Path("/home/x"))["model"], "auto")

    def test_update_migrates(self):
        d = self.home("gemini-3.1-pro-high")
        lines = []
        core.update(d, runner=mock.Mock(), free=lambda p: 10 ** 12, say=lines.append, now=lambda: "T")
        self.assertEqual(self.model(d), "auto")
        self.assertTrue(any("-> auto" in x for x in lines))


if __name__ == "__main__":
    unittest.main()
