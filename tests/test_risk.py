import time

import pytest

from aeon.broker import Account, Position
from aeon.config import RiskCfg
from aeon.market import Spec
from aeon.risk import Proposal, RiskEngine

SOL = Spec("SOL-USDT-SWAP", "SOL", ct_val=1.0, lot_sz=0.01, min_sz=0.01, tick_sz=0.01, max_lever=50)
UNI = {"SOL-USDT-SWAP", "ETH-USDT-SWAP"}


def acct(equity=200.0, available=None, positions=None):
    return Account(equity=equity, available=equity if available is None else available,
                   balance=equity, positions=positions or {})


def long_prop(**kw):
    base = dict(type="open", inst="SOL-USDT-SWAP", side="long", margin_pct=20, leverage=3,
                stop_loss=98.0, take_profit=106.0)
    base.update(kw)
    return Proposal(**base)


def test_size_is_clipped_to_risk_per_trade():
    r = RiskEngine(RiskCfg())
    v = r.evaluate_open(long_prop(margin_pct=35, leverage=5), acct(), SOL, 100.0, UNI)
    assert v.ok
    # 2% of 200 = $4 max loss. Loss at the stop (2% away) plus fees must stay inside that.
    assert v.max_loss <= 4.0 + 1e-6
    assert any("risk-per-trade" in n for n in v.notes)


def test_small_request_is_not_inflated():
    r = RiskEngine(RiskCfg())
    v = r.evaluate_open(long_prop(margin_pct=5, leverage=2), acct(), SOL, 100.0, UNI)
    assert v.ok and v.notional == pytest.approx(20.0, abs=1.0)


def test_stop_rules():
    r = RiskEngine(RiskCfg())
    a = acct()
    assert "stop_loss is required" in r.evaluate_open(long_prop(stop_loss=None), a, SOL, 100.0, UNI).why
    assert "wrong side" in r.evaluate_open(long_prop(stop_loss=101.0), a, SOL, 100.0, UNI).why
    assert "minimum" in r.evaluate_open(long_prop(stop_loss=99.9), a, SOL, 100.0, UNI).why
    assert "maximum" in r.evaluate_open(long_prop(stop_loss=80.0), a, SOL, 100.0, UNI).why
    assert "wrong side" in r.evaluate_open(long_prop(take_profit=95.0), a, SOL, 100.0, UNI).why
    short = long_prop(side="short", stop_loss=102.0, take_profit=94.0)
    assert r.evaluate_open(short, a, SOL, 100.0, UNI).ok


def test_leverage_is_capped():
    r = RiskEngine(RiskCfg(max_leverage=5))
    v = r.evaluate_open(long_prop(leverage=50), acct(), SOL, 100.0, UNI)
    assert v.ok and v.leverage == 5


def test_universe_and_duplicates_and_position_limit():
    r = RiskEngine(RiskCfg(max_open_positions=1))
    assert "universe" in r.evaluate_open(long_prop(inst="DOGE-USDT-SWAP"), acct(), SOL, 100.0, UNI).why
    held = Position("ETH-USDT-SWAP", "long", 1, 1.0, 100.0, 2.0, mark=100.0, sl=98.0)
    a = acct(positions={"ETH-USDT-SWAP": held})
    assert "position limit" in r.evaluate_open(long_prop(), a, SOL, 100.0, UNI).why
    r2 = RiskEngine(RiskCfg())
    same = Position("SOL-USDT-SWAP", "long", 1, 1.0, 100.0, 2.0, mark=100.0, sl=98.0)
    assert "already in a position" in r2.evaluate_open(long_prop(), acct(positions={"SOL-USDT-SWAP": same}),
                                                       SOL, 100.0, UNI).why


def test_drawdown_kill_switch_and_resume():
    r = RiskEngine(RiskCfg(max_drawdown_pct=25))
    r.update_equity(200.0)
    assert not r.killed
    events = r.update_equity(149.0)             # -25.5% from peak
    assert r.killed and any("KILL SWITCH" in e for e in events)
    assert "kill switch" in r.evaluate_open(long_prop(), acct(149.0), SOL, 100.0, UNI).why
    r.resume(149.0)
    assert not r.killed and r.peak == 149.0


def test_daily_loss_block_and_new_day():
    r = RiskEngine(RiskCfg(daily_loss_limit_pct=8))
    t0 = time.time()
    r.update_equity(200.0, t0)
    r.update_equity(183.0, t0 + 60)             # -8.5% on the day
    assert r.day_blocked
    assert "daily loss" in r.evaluate_open(long_prop(), acct(183.0), SOL, 100.0, UNI).why
    r.update_equity(183.0, t0 + 86400 * 1.01)   # next UTC day
    assert not r.day_blocked and r.day_start == 183.0


def test_reentry_cooldown_and_trade_cap():
    r = RiskEngine(RiskCfg(reentry_cooldown_min=30, max_trades_per_day=2))
    now = time.time()
    r.on_close("SOL-USDT-SWAP", now - 5 * 60)
    assert "cooldown" in r.evaluate_open(long_prop(), acct(), SOL, 100.0, UNI, now).why
    r.on_close("SOL-USDT-SWAP", now - 31 * 60)
    assert r.evaluate_open(long_prop(), acct(), SOL, 100.0, UNI, now).ok
    r.on_trade(); r.on_trade()
    assert "trades per day" in r.evaluate_open(long_prop(), acct(), SOL, 100.0, UNI, now).why


def test_stops_only_tighten():
    r = RiskEngine(RiskCfg())
    pos = Position("SOL-USDT-SWAP", "long", 1, 1.0, 100.0, 2.0, mark=104.0, sl=98.0, tp=110.0)
    ok = r.evaluate_adjust(Proposal("adjust", "SOL-USDT-SWAP", stop_loss=101.0), pos, 104.0)
    assert ok.ok and ok.stop_loss == 101.0 and ok.take_profit == 110.0
    bad = r.evaluate_adjust(Proposal("adjust", "SOL-USDT-SWAP", stop_loss=95.0), pos, 104.0)
    assert not bad.ok and "closer" in bad.why
    short = Position("SOL-USDT-SWAP", "short", 1, 1.0, 100.0, 2.0, mark=96.0, sl=102.0)
    assert r.evaluate_adjust(Proposal("adjust", "SOL-USDT-SWAP", stop_loss=99.0), short, 96.0).ok
    assert not r.evaluate_adjust(Proposal("adjust", "SOL-USDT-SWAP", stop_loss=105.0), short, 96.0).ok


def test_add_respects_total_risk():
    r = RiskEngine(RiskCfg())
    pos = Position("SOL-USDT-SWAP", "long", 1.9, 1.0, 100.0, 3.0, mark=100.0, sl=98.0)   # already near the cap
    a = acct(positions={"SOL-USDT-SWAP": pos})
    v = r.evaluate_add(Proposal("add", "SOL-USDT-SWAP", margin_pct=20), a, SOL, 100.0)
    assert not v.ok or v.max_loss <= 4.0 + 1e-6


def test_reduce_sizes():
    r = RiskEngine(RiskCfg())
    pos = Position("SOL-USDT-SWAP", "long", 2.0, 1.0, 100.0, 3.0, mark=100.0, sl=98.0)
    v = r.evaluate_reduce(Proposal("reduce", "SOL-USDT-SWAP", pct=50), pos, SOL)
    assert v.ok and v.contracts == pytest.approx(1.0)
    full = r.evaluate_reduce(Proposal("reduce", "SOL-USDT-SWAP", pct=100), pos, SOL)
    assert full.ok and full.contracts == pytest.approx(2.0)


def test_min_order_rejected_when_over_limits():
    big = Spec("BTC-USDT-SWAP", "BTC", ct_val=0.01, lot_sz=1.0, min_sz=1.0, tick_sz=0.1, max_lever=100)  # ~$800 min
    r = RiskEngine(RiskCfg())
    prop = long_prop(inst="BTC-USDT-SWAP", stop_loss=78400.0, take_profit=84000.0)   # 2% stop
    v = r.evaluate_open(prop, acct(), big, 80000.0, {"BTC-USDT-SWAP"})
    assert not v.ok and "minimum order" in v.why
