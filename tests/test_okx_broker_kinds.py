"""OkxBroker with spot and dated futures, against a fake exchange that mimics OKX's order mechanics:
spot buy fees are charged in the coin, sell fees in USDT, and balances move with every fill."""
import json
import time

import pytest

from aeon import okx_broker
from aeon.broker import BrokerError
from aeon.market import Spec
from aeon.okx import OkxError
from aeon.okx_broker import OkxBroker

SOL = Spec("SOL-USDT", "SOL", 1.0, 0.000001, 0.01, 0.01, 1.0, kind="spot")
BTC = Spec("BTC-USDT", "BTC", 1.0, 0.00000001, 0.00001, 0.1, 1.0, kind="spot")
FUT = Spec("BTC-USD_UM-261030", "BTC", 0.01, 0.01, 0.01, 0.1, 20.0, kind="futures",
           expiry_ts=time.time() + 13 * 86400, alias="this_month")
SPECS = {s.inst: s for s in (SOL, BTC, FUT)}


class FakeExchange:
    def __init__(self, acct_lvl="2", pos_mode="net_mode"):
        self.acct_lvl, self.pos_mode = acct_lvl, pos_mode
        self.calls, self.algos, self.fill_rows = [], [], []
        self.balances = {"USDT": 1000.0}
        self.pos_rows = {"SWAP": [], "FUTURES": []}
        self.pos_queries = []
        self.px = {"SOL-USDT": 100.0, "BTC-USDT": 80000.0, "BTC-USD_UM-261030": 81000.0}
        self.info, self.fail_algo, self._n = {}, 0, 0

    def kinds(self, name):
        return [c[1] for c in self.calls if c[0] == name]

    async def account_config(self):
        return [{"posMode": self.pos_mode, "acctLv": self.acct_lvl}]

    async def balance(self):
        d = [{"ccy": c, "cashBal": str(v), "availBal": str(v), "eq": str(v)} for c, v in self.balances.items()]
        return [{"totalEq": "1000", "details": d}]

    async def set_leverage(self, inst, lever, mgn, ps):
        self.calls.append(("lev", inst, lever, mgn, ps))

    async def place_order(self, body):
        self.calls.append(("order", body))
        self._n += 1
        oid, inst, sz = str(self._n), body["instId"], float(body["sz"])
        px = self.px[inst]
        if body["tdMode"] == "cash":                          # spot
            base = inst.split("-")[0]
            if body["side"] == "buy":
                fee = sz * 0.001                              # taken in the coin bought
                self.balances[base] = self.balances.get(base, 0.0) + sz - fee
                self.balances["USDT"] -= sz * px
                self.info[oid] = {"state": "filled", "avgPx": str(px), "accFillSz": str(sz), "fee": str(-fee), "feeCcy": base}
            else:
                fee = sz * px * 0.001                         # taken in USDT
                self.balances[base] -= sz
                self.balances["USDT"] += sz * px - fee
                self.info[oid] = {"state": "filled", "avgPx": str(px), "accFillSz": str(sz), "fee": str(-fee), "feeCcy": "USDT"}
        else:                                                 # derivatives: contracts, USDT fee
            self.info[oid] = {"state": "filled", "avgPx": str(px), "accFillSz": str(sz), "fee": "-0.1", "feeCcy": "USDT"}
        return [{"ordId": oid, "sCode": "0"}]

    async def get_order(self, inst, ord_id):
        return self.info[ord_id]

    async def algos_pending(self, ord_type, inst_type=None):
        return [a for a in self.algos if a["ordType"] == ord_type]

    async def place_algo(self, body):
        if self.fail_algo:
            self.fail_algo -= 1
            raise OkxError("51000", "stop rejected", "/api/v5/trade/order-algo")
        self.calls.append(("algo", body))
        self.algos.append({"algoId": f"a{len(self.algos) + 1}", "instId": body["instId"], "ordType": body["ordType"],
                           "slTriggerPx": body.get("slTriggerPx", ""), "tpTriggerPx": body.get("tpTriggerPx", "")})
        return [{"sCode": "0"}]

    async def cancel_algos(self, items):
        self.calls.append(("cancel", items))
        ids = {i["algoId"] for i in items}
        self.algos = [a for a in self.algos if a["algoId"] not in ids]

    async def positions(self, inst_type="SWAP"):
        self.pos_queries.append(inst_type)
        return self.pos_rows.get(inst_type, [])

    async def positions_history(self, inst, limit=3, inst_type="SWAP"):
        return []

    async def fills(self, inst_type, inst, limit=30):
        return self.fill_rows

    async def close_position(self, body):
        self.calls.append(("close", body))
        return [{}]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(okx_broker.asyncio, "sleep", instant)


async def broker(**kw):
    ex = FakeExchange(**kw)
    b = OkxBroker(ex, SPECS)
    await b.init()
    return ex, b


def last_algo(ex):
    return [c[1] for c in ex.calls if c[0] == "algo"][-1]


# --------------------------------------------------------------------------- spot
async def test_spot_buy_is_a_cash_order_in_coins_and_the_stop_follows():
    ex, b = await broker()
    fill = await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=110.0, price=100.0, reason="t")
    order = ex.kinds("order")[0]
    assert order["tdMode"] == "cash" and order["side"] == "buy" and order["ordType"] == "market"
    assert order["sz"] == "2" and order["tgtCcy"] == "base_ccy"              # size in COINS, not USDT
    assert "attachAlgoOrds" not in order and "posSide" not in order and not ex.kinds("lev")   # no leverage on spot
    # the buy fee was taken in SOL, so only 1.998 SOL arrived: the stop must protect exactly that
    assert b._spot["SOL-USDT"]["qty"] == pytest.approx(1.998)
    algo = last_algo(ex)
    assert algo["tdMode"] == "cash" and algo["side"] == "sell" and algo["ordType"] == "oco" and algo["sz"] == "1.998"
    assert algo["slTriggerPx"] == "95" and algo["tpTriggerPx"] == "110"
    assert "closeFraction" not in algo and "reduceOnly" not in algo         # those are derivatives-only
    assert fill.event == "open" and fill.fee == pytest.approx(0.2) and fill.pnl == pytest.approx(-0.2)


async def test_spot_cannot_be_shorted():
    ex, b = await broker()
    with pytest.raises(BrokerError, match="short"):
        await b.open(SOL, "short", 1.0, 1.0, sl=105.0, tp=None, price=100.0, reason="t")
    assert not ex.kinds("order")


async def test_spot_close_cancels_the_stop_then_sells_what_arrived():
    ex, b = await broker()
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=110.0, price=100.0, reason="t")
    ex.calls.clear()
    ex.px["SOL-USDT"] = 110.0
    fill = await b.close(SOL, 110.0, "take profit")
    names = [c[0] for c in ex.calls]
    assert names.index("cancel") < names.index("order")                      # the stop must not fire on top of the sale
    sell = ex.kinds("order")[0]
    assert sell["side"] == "sell" and sell["sz"] == "1.998" and sell["tgtCcy"] == "base_ccy"
    # whole trade: (110-100) * 1.998 coins, minus the sell fee 0.21978 and the buy fee 0.2
    assert fill.event == "close" and fill.trade_pnl == pytest.approx(19.98 - 0.21978 - 0.2)
    assert b._spot == {} and ex.algos == []


async def test_spot_partial_reduce_re_protects_the_remainder():
    ex, b = await broker()
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    fill = await b.reduce(SOL, 1.0, 100.0, "trim")
    assert fill.event == "reduce" and ex.kinds("order")[-1]["sz"] == "1"
    assert b._spot["SOL-USDT"]["qty"] == pytest.approx(0.998)
    algo = last_algo(ex)
    assert algo["sz"] == "0.998" and algo["ordType"] == "conditional" and algo["slTriggerPx"] == "95"
    assert len(ex.algos) == 1                                               # the old stop was replaced, not duplicated


async def test_only_what_aeon_bought_is_ever_sold():
    ex, b = await broker()
    ex.balances["BTC"] = 5.0                                                # coins you already held
    with pytest.raises(BrokerError, match="only sells what it bought"):
        await b.close(BTC, 80000.0, "t")
    assert not ex.kinds("order")


async def test_spot_holding_shows_up_as_a_position_and_stop_fire_is_booked():
    ex, b = await broker()
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=110.0, price=100.0, reason="t")
    assert await b.tick({"SOL-USDT": 100.0}) == []
    pos = b._known["SOL-USDT"]
    assert pos.kind == "spot" and pos.contracts == pytest.approx(1.998) and pos.lever == 1.0 and pos.sl == 95.0 and pos.tp == 110.0
    assert ex.pos_queries == ["SWAP", "FUTURES"]                            # derivatives positions are polled too

    # the stop fires on the exchange: the coins are gone and OKX recorded the sale
    ex.balances["SOL"] = 0.0
    ex.algos.clear()
    ex.fill_rows = [{"side": "sell", "fillPx": "95", "fillSz": "1.998", "fee": "-0.18981", "feeCcy": "USDT",
                     "ts": str(int(time.time() * 1000))}]
    fills = await b.tick({"SOL-USDT": 94.0})
    assert len(fills) == 1 and fills[0].event == "close" and fills[0].price == pytest.approx(95.0)
    assert fills[0].trade_pnl == pytest.approx((95 - 100) * 1.998 - 0.18981 - 0.2)
    assert b._spot == {} and "SOL-USDT" not in b._known


async def test_missing_spot_stop_is_re_armed():
    ex, b = await broker()
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    await b.tick({"SOL-USDT": 100.0})
    ex.algos.clear()                                                        # someone cancelled it
    await b.tick({"SOL-USDT": 100.0})
    assert len(ex.algos) == 1 and any("re-armed" in n for n in b.drain_notes())


async def test_a_failed_stop_after_a_spot_buy_is_reported_and_retried():
    ex, b = await broker()
    ex.fail_algo = 1                                                        # OKX rejects the stop once
    fill = await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    assert fill.event == "open" and "SOL-USDT" in b._spot                   # the buy happened and is tracked
    assert any("stop order could not be placed" in n for n in b.drain_notes())
    await b.tick({"SOL-USDT": 100.0})                                       # next monitor pass fixes it
    assert len(ex.algos) == 1


async def test_spot_add_averages_the_entry():
    ex, b = await broker()
    await b.open(SOL, "long", 1.0, 1.0, sl=90.0, tp=None, price=100.0, reason="t")
    ex.px["SOL-USDT"] = 110.0
    await b.add(SOL, "long", 1.0, sl=95.0, tp=None, price=110.0, reason="t")
    s = b._spot["SOL-USDT"]
    assert s["qty"] == pytest.approx(1.998) and s["entry"] == pytest.approx((100 * 0.999 + 110 * 0.999) / 1.998)
    assert last_algo(ex)["sz"] == "1.998" and last_algo(ex)["slTriggerPx"] == "95"


# --------------------------------------------------------------------------- dated futures
async def test_dated_future_order_has_the_perpetual_shape_and_is_tracked_with_its_expiry():
    ex, b = await broker()
    fill = await b.open(FUT, "long", 0.5, 3.0, sl=79000.04, tp=85000.0, price=81000.0, reason="t")
    order = ex.kinds("order")[0]
    assert order["instId"] == "BTC-USD_UM-261030" and order["tdMode"] == "isolated" and order["side"] == "buy"
    assert order["sz"] == "0.5" and order["attachAlgoOrds"][0]["slTriggerPx"] == "79000"
    assert ex.calls[0] == ("lev", "BTC-USD_UM-261030", 3.0, "isolated", None)     # leverage is set before the order
    assert fill.event == "open" and b._desired[FUT.inst] == (79000.04, 85000.0)

    ex.algos.append({"algoId": "x", "instId": FUT.inst, "ordType": "oco", "slTriggerPx": "79000", "tpTriggerPx": "85000"})
    ex.pos_rows["FUTURES"] = [{"instId": FUT.inst, "pos": "0.5", "posSide": "net", "avgPx": "81000", "markPx": "81100",
                               "lever": "3", "upl": "5", "imr": "135", "cTime": str(int(time.time() * 1000))}]
    await b.tick({FUT.inst: 81100.0})
    pos = b._known[FUT.inst]
    assert pos.kind == "futures" and pos.expiry == pytest.approx(FUT.expiry_ts) and pos.sl == 79000.0 and pos.contracts == 0.5


async def test_spot_only_account_never_polls_derivatives():
    ex, b = await broker(acct_lvl="1")
    assert b.allowed_kinds == {"spot"}
    await b.tick({})
    assert ex.pos_queries == []                                             # Spot mode: no futures/swap position calls


# --------------------------------------------------------------------------- restart safety
async def test_state_survives_a_restart():
    ex, b = await broker()
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=110.0, price=100.0, reason="t")
    await b.tick({"SOL-USDT": 100.0})
    saved = json.loads(json.dumps(b.dump()))                                # what the journal stores

    b2 = OkxBroker(ex, SPECS)
    await b2.init()
    b2.load(saved)
    assert b2._desired["SOL-USDT"] == (95.0, 110.0)
    assert b2._spot["SOL-USDT"]["qty"] == pytest.approx(1.998)
    assert b2._known["SOL-USDT"].kind == "spot"
    # it still knows the position, so a stop that fired while it was offline is booked, not forgotten
    ex.balances["SOL"] = 0.0
    ex.algos.clear()
    ex.fill_rows = [{"side": "sell", "fillPx": "95", "fillSz": "1.998", "fee": "-0.19", "feeCcy": "USDT",
                     "ts": str(int(time.time() * 1000))}]
    fills = await b2.tick({"SOL-USDT": 94.0})
    assert len(fills) == 1 and fills[0].event == "close"
