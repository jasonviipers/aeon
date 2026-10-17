import pytest

from aeon.broker import BrokerError, PaperBroker
from aeon.market import Spec

SOL = Spec("SOL-USDT-SWAP", "SOL", ct_val=1.0, lot_sz=0.01, min_sz=0.01, tick_sz=0.01, max_lever=50)


def broker(**kw):
    return PaperBroker(start_equity=200.0, fee_rate=kw.get("fee", 0.0005), slippage=kw.get("slip", 0.0))


async def test_round_trip_accounting_reconciles():
    b = broker()
    f_open = await b.open(SOL, "long", 2.0, 3.0, sl=95.0, tp=120.0, price=100.0, reason="t")
    acct = await b.account({"SOL-USDT-SWAP": 110.0})
    assert acct.upl == pytest.approx(20.0)
    assert acct.equity == pytest.approx(200.0 - f_open.fee + 20.0)
    f_close = await b.close(SOL, 110.0, "t")
    # whole-trade net pnl = gross 20 - both fees; balance moved by exactly that
    assert f_close.trade_pnl == pytest.approx(20.0 - f_open.fee - f_close.fee)
    assert b.balance == pytest.approx(200.0 + f_close.trade_pnl)
    assert not b.positions


async def test_short_profits_when_price_falls():
    b = broker(fee=0.0)
    await b.open(SOL, "short", 1.0, 2.0, sl=105.0, tp=None, price=100.0, reason="t")
    f = await b.close(SOL, 90.0, "t")
    assert f.trade_pnl == pytest.approx(10.0)


async def test_stop_loss_take_profit_and_liquidation_triggers():
    b = broker(fee=0.0)
    await b.open(SOL, "long", 1.0, 2.0, sl=95.0, tp=110.0, price=100.0, reason="t")
    assert await b.tick({"SOL-USDT-SWAP": 99.0}) == []
    fills = await b.tick({"SOL-USDT-SWAP": 94.0})
    assert [f.reason for f in fills] == ["stop_loss"] and fills[0].trade_pnl == pytest.approx(-6.0)

    await b.open(SOL, "long", 1.0, 2.0, sl=95.0, tp=110.0, price=100.0, reason="t")
    fills = await b.tick({"SOL-USDT-SWAP": 111.0})
    assert fills[0].reason == "take_profit" and fills[0].trade_pnl == pytest.approx(11.0)

    # 20x with a stop far below the liquidation price: liquidation fires first and costs the margin
    await b.open(SOL, "long", 1.0, 20.0, sl=50.0, tp=None, price=100.0, reason="t")
    fills = await b.tick({"SOL-USDT-SWAP": 94.0})
    assert fills[0].reason == "liquidated"
    assert fills[0].trade_pnl == pytest.approx(-(1 / 20 - 0.005) * 100.0)


async def test_partial_reduce_then_close_keeps_the_ledger_honest():
    b = broker()
    start = b.balance
    fills = [await b.open(SOL, "long", 2.0, 3.0, sl=95.0, tp=None, price=100.0, reason="t")]
    fills.append(await b.reduce(SOL, 1.0, 105.0, "t"))
    assert b.positions["SOL-USDT-SWAP"].contracts == pytest.approx(1.0)
    fills.append(await b.close(SOL, 102.0, "t"))
    assert b.balance == pytest.approx(start + sum(f.pnl for f in fills))
    assert fills[-1].trade_pnl == pytest.approx(sum(f.pnl for f in fills))


async def test_add_averages_entry_and_margin_is_enforced():
    b = broker(fee=0.0)
    await b.open(SOL, "long", 1.0, 2.0, sl=90.0, tp=None, price=100.0, reason="t")
    await b.add(SOL, "long", 1.0, sl=92.0, tp=None, price=110.0, reason="t")
    p = b.positions["SOL-USDT-SWAP"]
    assert p.contracts == 2.0 and p.entry == pytest.approx(105.0) and p.sl == 92.0
    with pytest.raises(BrokerError):
        await b.open(Spec("ETH-USDT-SWAP", "ETH", 1.0, 0.01, 0.01, 0.01, 50), "long", 100.0, 2.0,
                     sl=1.0, tp=None, price=100.0, reason="too big")


async def test_state_survives_restart():
    b = broker()
    await b.open(SOL, "long", 1.0, 2.0, sl=95.0, tp=None, price=100.0, reason="t")
    clone = broker()
    clone.load(b.dump())
    assert clone.balance == pytest.approx(b.balance) and clone.positions["SOL-USDT-SWAP"].sl == 95.0
