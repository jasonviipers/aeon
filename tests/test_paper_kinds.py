"""Paper broker: spot (long-only cash, 0.1% fee, never liquidated) and dated futures (settle at expiry)."""
import time

import pytest

from aeon.broker import BrokerError, PaperBroker
from aeon.market import Spec

SOL_SPOT = Spec("SOL-USDT", "SOL", 1.0, 0.000001, 0.01, 0.01, 1.0, kind="spot")
FUT = Spec("BTC-USD_UM-261030", "BTC", 0.01, 0.01, 0.01, 0.1, 20.0, kind="futures",
           expiry_ts=time.time() + 13 * 86400, alias="this_month")


def broker():
    return PaperBroker(200.0, fee_rate=0.0005, slippage=0.0, spot_fee_rate=0.001)


async def test_spot_round_trip_pays_the_spot_fee_and_uses_all_cash():
    b = broker()
    f_open = await b.open(SOL_SPOT, "long", 1.0, 5.0, sl=95.0, tp=None, price=100.0, reason="t")   # leverage ask is ignored
    pos = b.positions["SOL-USDT"]
    assert pos.kind == "spot" and pos.lever == 1.0 and pos.liq is None and pos.margin == pytest.approx(100.0)
    assert f_open.fee == pytest.approx(0.1)                                    # 0.1% of $100, not 0.05%
    acct = await b.account({"SOL-USDT": 110.0})
    assert acct.available == pytest.approx(200 - 0.1 - 100)
    f_close = await b.close(SOL_SPOT, 110.0, "t")
    assert f_close.trade_pnl == pytest.approx(10.0 - 0.1 - 0.11)
    assert b.balance == pytest.approx(200 + f_close.trade_pnl)


async def test_spot_cannot_be_shorted_or_levered_into_a_liquidation():
    b = broker()
    with pytest.raises(BrokerError, match="short"):
        await b.open(SOL_SPOT, "short", 1.0, 1.0, sl=105.0, tp=None, price=100.0, reason="t")
    await b.open(SOL_SPOT, "long", 1.0, 1.0, sl=None, tp=None, price=100.0, reason="t")
    assert await b.tick({"SOL-USDT": 1.0}) == []                               # a 99% crash: spot has no liquidation


async def test_spot_stop_and_target_fire_like_other_positions():
    b = broker()
    await b.open(SOL_SPOT, "long", 1.0, 1.0, sl=95.0, tp=110.0, price=100.0, reason="t")
    fills = await b.tick({"SOL-USDT": 94.0})
    assert [f.reason for f in fills] == ["stop_loss"] and fills[0].trade_pnl < 0


async def test_dated_future_settles_if_the_agent_lets_it_expire():
    b = broker()
    spec = Spec(FUT.inst, "BTC", 0.01, 0.01, 0.01, 0.1, 20.0, kind="futures", expiry_ts=time.time() + 3600, alias="this_month")
    await b.open(spec, "long", 0.1, 3.0, sl=79000.0, tp=None, price=81000.0, reason="t")      # 0.1 contract = $81 notional
    assert b.positions[FUT.inst].expiry == pytest.approx(spec.expiry_ts)
    assert await b.tick({FUT.inst: 81500.0}) == []                              # not yet
    b.positions[FUT.inst].expiry = time.time() - 1                             # time passes
    fills = await b.tick({FUT.inst: 81500.0})
    assert [f.reason for f in fills] == ["expired"] and fills[0].trade_pnl > 0


async def test_mixed_book_and_state_roundtrip_keeps_kinds():
    b = broker()
    await b.open(SOL_SPOT, "long", 0.5, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    await b.open(FUT, "short", 0.1, 3.0, sl=83000.0, tp=None, price=81000.0, reason="t")
    clone = broker()
    clone.load(b.dump())
    assert clone.positions["SOL-USDT"].kind == "spot" and clone.positions["SOL-USDT"].liq is None
    assert clone.positions[FUT.inst].kind == "futures" and clone.positions[FUT.inst].expiry == pytest.approx(FUT.expiry_ts)
    assert clone.positions[FUT.inst].liq is not None


async def test_old_saved_state_without_kind_still_loads():
    clone = broker()
    clone.load({"balance": 150.0, "positions": [{"inst": "SOL-USDT-SWAP", "side": "long", "contracts": 1.0, "ct_val": 1.0,
                                                 "entry": 100.0, "lever": 3.0, "mark": 100.0}]})
    assert clone.positions["SOL-USDT-SWAP"].kind == "swap"
