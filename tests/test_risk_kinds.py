"""Risk rules specific to spot and dated futures."""
import time

import pytest

from aeon.broker import Account, Position
from aeon.config import RiskCfg
from aeon.market import Spec
from aeon.risk import Proposal, RiskEngine

DAY = 86400
SOL_SPOT = Spec("SOL-USDT", "SOL", 1.0, 0.000001, 0.01, 0.01, 1.0, kind="spot")
SOL_PERP = Spec("SOL-USDT-SWAP", "SOL", 1.0, 0.01, 0.01, 0.01, 50.0, kind="swap")


def future(days):
    return Spec("BTC-USD_UM-261030", "BTC", 0.01, 0.01, 0.01, 0.1, 20.0, kind="futures",
                expiry_ts=time.time() + days * DAY, alias="this_month")


def acct(positions=None, equity=200.0):
    return Account(equity=equity, available=equity, balance=equity, positions=positions or {})


def prop(inst="SOL-USDT", **kw):
    base = dict(type="open", inst=inst, side="long", margin_pct=20, leverage=3, stop_loss=96.0, take_profit=108.0)
    base.update(kw)
    return Proposal(**base)


def test_spot_is_long_only():
    r = RiskEngine(RiskCfg())
    v = r.evaluate_open(prop(side="short", stop_loss=104.0, take_profit=92.0), acct(), SOL_SPOT, 100.0, {"SOL-USDT"})
    assert not v.ok and "long-only" in v.why


def test_spot_has_no_leverage_and_pays_spot_fees():
    r = RiskEngine(RiskCfg())
    v = r.evaluate_open(prop(margin_pct=25, leverage=5), acct(), SOL_SPOT, 100.0, {"SOL-USDT"})
    assert v.ok and v.leverage == 1.0 and any("no leverage" in n for n in v.notes)
    assert v.margin == pytest.approx(v.notional)                              # all of it is cash
    # 25% of 200 = $50 requested; the 35% margin cap does not bind, but the 2% risk cap does with a 4% stop:
    # allowed notional = 4 / (0.04 + 2 * 0.001 spot fee) = ~95, so $50 goes through untouched
    assert v.notional == pytest.approx(50.0, abs=0.5)
    assert v.max_loss == pytest.approx(v.notional * (0.04 + 0.002), rel=1e-6)   # spot round-trip fee is 0.2%, not 0.1%


def test_spot_size_is_clipped_by_cash_and_risk_like_everything_else():
    r = RiskEngine(RiskCfg())
    v = r.evaluate_open(prop(margin_pct=35, stop_loss=98.0), acct(), SOL_SPOT, 100.0, {"SOL-USDT"})
    assert v.ok and v.max_loss <= 4.0 + 1e-6                                   # 2% stop -> risk cap binds well below $70


def test_dated_future_is_refused_near_expiry():
    r = RiskEngine(RiskCfg(futures_min_days_to_open=3.0))
    uni = {"BTC-USD_UM-261030"}
    p = prop("BTC-USD_UM-261030", stop_loss=79000.0, take_profit=84000.0)
    near = r.evaluate_open(p, acct(), future(1.5), 81000.0, uni)
    assert not near.ok and "expires in 1.5 days" in near.why
    assert r.evaluate_open(p, acct(), future(20), 81000.0, uni).ok
    # adding to a position is also refused close to expiry
    held = Position("BTC-USD_UM-261030", "long", 0.5, 0.01, 81000.0, 3.0, mark=81000.0, sl=79000.0, kind="futures")
    v = r.evaluate_add(Proposal("add", "BTC-USD_UM-261030", margin_pct=10), acct({held.inst: held}), future(1.5), 81000.0)
    assert not v.ok and "expires" in v.why


def test_one_position_per_coin_across_kinds():
    r = RiskEngine(RiskCfg())
    perp = Position("SOL-USDT-SWAP", "long", 1.0, 1.0, 100.0, 3.0, mark=100.0, sl=97.0)
    v = r.evaluate_open(prop("SOL-USDT"), acct({perp.inst: perp}), SOL_SPOT, 100.0, {"SOL-USDT", "SOL-USDT-SWAP"})
    assert not v.ok and "one position per asset" in v.why and "SOL-USDT-SWAP" in v.why
    # ...a different coin is fine, and the rule can be switched off
    other = Position("ETH-USDT-SWAP", "long", 0.02, 1.0, 2400.0, 3.0, mark=2400.0, sl=2350.0)    # $48 notional
    assert r.evaluate_open(prop("SOL-USDT"), acct({other.inst: other}), SOL_SPOT, 100.0, {"SOL-USDT"}).ok
    relaxed = RiskEngine(RiskCfg(one_position_per_asset=False))
    assert relaxed.evaluate_open(prop("SOL-USDT"), acct({perp.inst: perp}), SOL_SPOT, 100.0, {"SOL-USDT"}).ok


def test_perpetual_rules_are_unchanged():
    r = RiskEngine(RiskCfg(max_leverage=5))
    v = r.evaluate_open(prop("SOL-USDT-SWAP", leverage=50), acct(), SOL_PERP, 100.0, {"SOL-USDT-SWAP"})
    assert v.ok and v.leverage == 5 and any("leverage 50x -> 5x" in n for n in v.notes)
    assert not any("spot" in n for n in v.notes)


def test_reducing_a_spot_position():
    r = RiskEngine(RiskCfg())
    held = Position("SOL-USDT", "long", 2.0, 1.0, 100.0, 1.0, mark=100.0, sl=96.0, kind="spot")
    v = r.evaluate_reduce(Proposal("reduce", "SOL-USDT", pct=50), held, SOL_SPOT)
    assert v.ok and v.contracts == pytest.approx(1.0)
