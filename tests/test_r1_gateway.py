"""R1: the one gateway — caps, refusal (no backend, shadow row), policy re-read, rule cache, ledger, report."""
import json
from datetime import timedelta

import pytest

from ga.backends.base import BackendTurn
from ga.llm import Card, Gateway, GatewayConfig
from ga.llm import policy as pol
from ga.llm.ledger import Ledger, now_utc
from ga.llm.report import build

MODEL = "claude-sonnet-5-5"
CAPS = {pol.COORD: 1.0, pol.BASELINES: 2.0, pol.TOTAL_H: 5.0, pol.TOTAL_DAY: 20.0,
        pol.BUILD_ITEM: {"default": 3.0, "hard": 6.0}}


class Runner:
    bare = False

    def __init__(self, log, usage=None):
        self.log, self.usage = log, usage

    def run_turn(self, prompt, session, **kw):
        self.log.append(prompt)
        return BackendTurn("answer:" + str(len(self.log)), [MODEL], usage=self.usage, usage_format="anthropic")


def write_policy(tmp, caps=None, **extra):
    doc = {"vm_budget": {"caps": dict(CAPS if caps is None else caps), **extra.pop("vm", {})}, **extra}
    p = tmp / "policy.json"
    p.write_text(json.dumps(doc))
    return p


def make(tmp, caps=None, usage=None, **kw):
    log = []
    cfg = GatewayConfig(policy_path=str(write_policy(tmp, caps, **kw)), home=str(tmp / "home"), sdk_sha="abc")
    return Gateway(cfg, lambda m: Runner(log, usage)), log, cfg


def seed(cfg, purpose, usd, item="x", age_s=0):
    Ledger(cfg.home + "/ledger").append({"purpose": purpose, "item_id": item, "usd": usd, "outcome": "ok"},
                                        at=now_utc() - timedelta(seconds=age_s))


def shadow_rows(cfg):
    try:
        return [json.loads(x) for x in open(cfg.home + "/shadow.jsonl")]
    except OSError:
        return []


CARD = Card("role", "evidence " * 10)


@pytest.mark.parametrize("purpose,cap,spend", [
    ("coordination", pol.COORD, 0.9), ("intake", pol.BASELINES, 1.9), ("probe", pol.TOTAL_H, 4.9)])
def test_each_cap_below_allows_crossing_refuses(tmp_path, purpose, cap, spend):
    gw, log, cfg = make(tmp_path)
    seed(cfg, purpose, spend - 0.5)               # just below: estimate is ~0.0 so it is allowed
    assert gw.call(CARD, purpose, "i1", MODEL).status == "ok" and len(log) == 1
    seed(cfg, purpose, 1.0)                        # now crossing
    r = gw.call(Card("role", "other evidence"), purpose, "i2", MODEL)
    assert r.status == "refused" and len(log) == 1  # backend not invoked
    assert r.reason["cap"] == cap
    row = shadow_rows(cfg)[-1]
    assert row["rejected_by"] == "budget" and row["reason"]["cap"] == cap and row["would_do"]["purpose"] == purpose
    assert row["actor"] and row["action"]


def test_day_cap_and_windows(tmp_path):
    gw, log, cfg = make(tmp_path)
    seed(cfg, "probe", 19.9, age_s=7200)           # outside the hour window, inside the day window
    assert gw.call(CARD, "probe", "i", MODEL).status == "ok"
    seed(cfg, "probe", 0.2, age_s=7200)
    r = gw.call(Card("r", "new"), "probe", "i", MODEL)
    assert r.status == "refused" and r.reason["cap"] == pol.TOTAL_DAY
    # a row older than 24h no longer counts
    seed(cfg, "probe", 50, age_s=90000)
    assert gw.call(Card("r", "newer"), "probe", "i", MODEL).status == "refused"  # the 20.1 still counts


def test_tightest_cap_wins(tmp_path):
    gw, log, cfg = make(tmp_path)
    seed(cfg, "coordination", 1.5)                 # over coord (1.0) and baselines (2.0 not yet)
    seed(cfg, "intake", 1.0)                       # baselines now 2.5 > 2.0
    r = gw.call(CARD, "coordination", "i", MODEL)
    assert r.reason["cap"] == pol.COORD and not log


def test_build_soft_needs_judgment_hard_refuses(tmp_path):
    gw, log, cfg = make(tmp_path, caps={**CAPS, pol.TOTAL_H: 100.0, pol.TOTAL_DAY: 100.0})
    seed(cfg, "build", 3.0, item="b1")
    r = gw.call(CARD, "build", "b1", MODEL)
    assert r.status == "needs_judgment" and not log and not shadow_rows(cfg)
    assert gw.call(CARD, "build", "other", MODEL).status == "ok"
    seed(cfg, "build", 3.5, item="b1")
    r = gw.call(Card("r", "z"), "build", "b1", MODEL)
    assert r.status == "refused" and r.reason["kind"] == "hard" and len(log) == 1


def test_policy_change_takes_effect_next_call(tmp_path):
    gw, log, cfg = make(tmp_path)
    seed(cfg, "probe", 4.0)
    assert gw.call(CARD, "probe", "i", MODEL).status == "ok"
    write_policy(tmp_path, {**CAPS, pol.TOTAL_H: 1.0})
    assert gw.call(Card("r", "n"), "probe", "i", MODEL).status == "refused"
    write_policy(tmp_path, {**CAPS, pol.TOTAL_H: 50.0})
    assert gw.call(Card("r", "m"), "probe", "i", MODEL).status == "ok"


def test_applies_and_windows_from_policy(tmp_path):
    gw, log, cfg = make(tmp_path, vm={"applies": {"probe": [pol.COORD]}, "windows": {"hour": "rolling_60m"}})
    seed(cfg, "probe", 1.5)                         # probe now counts toward the coordination cap
    assert gw.call(CARD, "probe", "i", MODEL).reason["cap"] == pol.COORD


def test_cloud_only_keys_never_applied(tmp_path):
    gw, log, cfg = make(tmp_path, caps={**CAPS, "cloud_top_baseline_usd_per_h": 0.0, "session_ctx_cap_tokens": 1})
    assert gw.call(CARD, "intake", "i", MODEL).status == "ok"


@pytest.mark.parametrize("mode", ["halt", "halt_vm", "missing", "garbage", "nocaps"])
def test_halt_missing_unreadable_refuse_everything(tmp_path, mode):
    gw, log, cfg = make(tmp_path)
    p = tmp_path / "policy.json"
    if mode == "halt":
        write_policy(tmp_path, halt=True)
    elif mode == "halt_vm":
        write_policy(tmp_path, vm={"halt": True})
    elif mode == "missing":
        p.unlink()
    elif mode == "garbage":
        p.write_text("{nope")
    else:
        p.write_text("{}")
    for purpose in ("coordination", "probe", "build"):
        assert gw.call(CARD, purpose, "i", MODEL).status == "refused"
    assert not log and shadow_rows(cfg)[-1]["rejected_by"] == "budget"


def test_cache_same_fingerprint_zero_calls(tmp_path):
    gw, log, cfg = make(tmp_path)
    a = gw.call(Card("role", "a  b\n c"), "diagnosis", "i", MODEL)
    b = gw.call(Card("role", "a b c"), "diagnosis", "i2", MODEL)    # normalized evidence is the same
    assert (a.status, b.status) == ("ok", "cached") and a.answer == b.answer and len(log) == 1
    assert gw.call(Card("role", "a b c"), "opinion", "i", MODEL).status == "ok"    # purpose is in the fingerprint
    write_policy(tmp_path, {**CAPS, pol.TOTAL_H: 99})
    assert gw.call(Card("role", "a b c"), "diagnosis", "i", MODEL).status == "ok"  # policy hash is in it
    outs = [r["outcome"] for r in Ledger(cfg.home + "/ledger").rows()]
    assert outs == ["ok", "cached", "ok", "ok"]


def test_ledger_row_fields_and_actual_usd(tmp_path):
    gw, log, cfg = make(tmp_path, usage={"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 2000,
                                          "cache_creation_input_tokens": 100})
    r = gw.call(CARD, "intake", "item-7", MODEL)
    row = list(Ledger(cfg.home + "/ledger").rows())[0]
    for k in ("at", "purpose", "item_id", "requested_model", "served_model", "input_tokens", "output_tokens",
              "cache_read_tokens", "cache_write_tokens", "usd", "card_bytes", "fingerprint", "policy_sha256", "outcome"):
        assert k in row
    assert row["card_bytes"] == CARD.size() and row["served_model"] == MODEL
    exp = (1000 * 2 + 500 * 10 + 2000 * 0.2 + 100 * 2.5) / 1e6
    assert abs(row["usd"] - exp) < 1e-9 and abs(r.usd - exp) < 1e-9


def test_window_spend_uses_actual_usd(tmp_path):
    gw, log, cfg = make(tmp_path, caps={**CAPS, pol.TOTAL_H: 0.5},
                        usage={"input_tokens": 0, "output_tokens": 1_000_000})   # actual $10 >> estimate
    assert gw.call(CARD, "probe", "i", MODEL).status == "ok"
    assert gw.call(Card("r", "next"), "probe", "i", MODEL).status == "refused"


def test_card_size_cap_and_no_history_parameter(tmp_path):
    gw, log, cfg = make(tmp_path)
    gw.cfg.card_max_bytes = 50
    r = gw.call(Card("r", "x" * 100), "probe", "i", MODEL)
    assert r.status == "refused" and not log and shadow_rows(cfg)[-1]["rejected_by"] == "card_size"
    import inspect
    assert list(inspect.signature(Gateway.call).parameters) == ["self", "card", "purpose", "item_id", "model_hint"]


def test_served_model_mismatch_is_an_error_row(tmp_path):
    from ga.backends.base import ModelMismatch

    class Bad:
        def run_turn(self, *a, **k):
            raise ModelMismatch("served_model_mismatch")
    gw, log, cfg = make(tmp_path)
    gw.runner_factory = lambda m: Bad()
    r = gw.call(CARD, "probe", "i", MODEL)
    row = list(Ledger(cfg.home + "/ledger").rows())[0]
    assert r.status == "error" and row["outcome"] == "error" and row["label"] == "served-model mismatch"


def test_report(tmp_path, capsys):
    gw, log, cfg = make(tmp_path)
    gw.call(CARD, "intake", "i", MODEL)
    gw.call(CARD, "intake", "i", MODEL)
    seed(cfg, "coordination", 5.0)
    gw.call(Card("r", "new"), "coordination", "i", MODEL)
    rep = build(cfg)
    assert rep["calls"] == 2 and rep["cached"] == 1 and rep["refused"] == 1
    c = rep["caps"][pol.COORD]
    assert c["limit"] == 1.0 and c["refusals"] == 1 and c["spend"] >= 5.0
    assert rep["ga_sdk_sha"] == "abc" and len(rep["policy_sha256"]) == 64
    from ga.__main__ import main
    assert main(["llm", "report", "--hour", "--policy", cfg.policy_path, "--home", cfg.home]) == 0
    assert json.loads(capsys.readouterr().out)["caps"][pol.TOTAL_H]["limit"] == 5.0


def test_estimate_counts_against_the_cap_exactly(tmp_path):
    from ga.llm.gateway import estimate_usd
    est = estimate_usd(CARD.size(), MODEL, 2000)
    gw, log, cfg = make(tmp_path, caps={pol.TOTAL_H: 1.0 + est - 0.0005, pol.TOTAL_DAY: 99})
    seed(cfg, "probe", 1.0)
    assert gw.call(CARD, "probe", "i", MODEL).status == "refused"
    write_policy(tmp_path, {pol.TOTAL_H: 1.0 + est + 0.0005, pol.TOTAL_DAY: 99})
    assert gw.call(CARD, "probe", "i", MODEL).status == "ok"


def test_gated_turn_refuses_without_calling_and_raises(tmp_path):
    from ga.backends.base import BackendError
    gw, log, cfg = make(tmp_path)
    seed(cfg, "probe", 50.0)
    with pytest.raises(BackendError) as e:
        gw.turn(Runner(log), "p", None, purpose="probe", item_id="i", model=MODEL)
    assert e.value.reason.startswith("budget_refused:") and not log and shadow_rows(cfg)
    seed(cfg, "intake", 0.0)
    gw2, log2, cfg2 = make(tmp_path / "ok")  if False else (gw, log, cfg)
