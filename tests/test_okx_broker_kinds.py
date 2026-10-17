"""OkxBroker with spot and dated futures, against a fake exchange that mimics OKX's order mechanics:
spot buy fees are charged in the coin, sell fees in USDT, balances move with every fill, stops can fire,
and replies can be lost."""
import json
import time

import pytest

from aeon import okx_broker
from aeon.broker import BrokerError, PendingOrder
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
        self.calls, self.algos = [], []
        self.algo_states: dict[str, dict] = {}            # algoId -> details once it is no longer pending
        self.balances = {"USDT": 1000.0}
        self.pos_rows = {"SWAP": [], "FUTURES": []}
        self.pos_queries = []
        self.px = {"SOL-USDT": 100.0, "BTC-USDT": 80000.0, "BTC-USD_UM-261030": 81000.0}
        self.info, self.by_cl = {}, {}
        self.fail_algo = 0               # reject the next N stop placements
        self.lose_reply = False          # accept the next order but drop the reply ("net" error)
        self.fail_get = 0                # the next N order lookups fail with a network error
        self._n = 0

    def kinds(self, name):
        return [c[1] for c in self.calls if c[0] == name]

    async def account_config(self):
        return [{"posMode": self.pos_mode, "acctLv": self.acct_lvl}]

    async def balance(self):
        d = [{"ccy": c, "cashBal": str(v), "availBal": str(v), "eq": str(v)} for c, v in self.balances.items()]
        return [{"totalEq": "1000", "details": d}]

    async def set_leverage(self, inst, lever, mgn, ps):
        self.calls.append(("lev", inst, lever, mgn, ps))

    def _execute(self, body, px=None):
        """Fill a market order immediately and move balances, like OKX does."""
        self._n += 1
        oid, inst, sz = str(self._n), body["instId"], float(body["sz"])
        px = px or self.px[inst]
        if body["tdMode"] == "cash":
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
        else:
            self.info[oid] = {"state": "filled", "avgPx": str(px), "accFillSz": str(sz), "fee": "-0.1", "feeCcy": "USDT"}
        if body.get("clOrdId"):
            self.by_cl[body["clOrdId"]] = oid
        return oid

    async def place_order(self, body):
        self.calls.append(("order", body))
        oid = self._execute(body)
        if self.lose_reply:
            self.lose_reply = False
            raise OkxError("net", "ReadTimeout: the reply never arrived", "/api/v5/trade/order")
        return [{"ordId": oid, "sCode": "0"}]

    async def get_order(self, inst, ord_id=None, cl_id=None):
        if self.fail_get:
            self.fail_get -= 1
            raise OkxError("net", "ConnectError", "/api/v5/trade/order")
        oid = ord_id or self.by_cl.get(cl_id)
        if oid is None or oid not in self.info:
            raise OkxError("51603", "Order does not exist", "/api/v5/trade/order")
        return self.info[oid]

    async def algos_pending(self, ord_type, inst_type=None):
        return [a for a in self.algos if a["ordType"] == ord_type]

    async def place_algo(self, body):
        if self.fail_algo:
            self.fail_algo -= 1
            raise OkxError("51000", "stop rejected", "/api/v5/trade/order-algo")
        self.calls.append(("algo", body))
        algo_id = f"a{len(self.calls)}"
        self.algos.append({"algoId": algo_id, "instId": body["instId"], "ordType": body["ordType"],
                           "slTriggerPx": body.get("slTriggerPx", ""), "tpTriggerPx": body.get("tpTriggerPx", ""),
                           "sz": body.get("sz", ""), "tdMode": body.get("tdMode")})
        return [{"algoId": algo_id, "sCode": "0"}]

    async def cancel_algos(self, items):
        self.calls.append(("cancel", items))
        ids = {i["algoId"] for i in items}
        for a in self.algos:
            if a["algoId"] in ids:
                self.algo_states[a["algoId"]] = {"algoId": a["algoId"], "state": "canceled"}
        self.algos = [a for a in self.algos if a["algoId"] not in ids]

    async def algo_details(self, algo_id):
        return self.algo_states.get(algo_id, {"algoId": algo_id, "state": "live"})

    def cancel_by_hand(self, inst):
        """You cancel the stop in the OKX app: it leaves the book and OKX reports it as canceled."""
        for a in [a for a in self.algos if a["instId"] == inst]:
            self.algos.remove(a)
            self.algo_states[a["algoId"]] = {"algoId": a["algoId"], "state": "canceled"}

    def fire(self, inst, side="sl"):
        """The exchange triggers our stop (or target): it places and fills a market sell for the stop's size."""
        a = next(a for a in self.algos if a["instId"] == inst)
        self.algos.remove(a)
        px = float(a["slTriggerPx"] if side == "sl" else a["tpTriggerPx"])
        oid = self._execute({"instId": inst, "tdMode": a["tdMode"], "side": "sell", "sz": a["sz"]}, px=px)
        self.algo_states[a["algoId"]] = {"algoId": a["algoId"], "state": "effective", "ordIdList": [oid], "actualSide": side}
        return px

    async def positions(self, inst_type="SWAP"):
        self.pos_queries.append(inst_type)
        return self.pos_rows.get(inst_type, [])

    async def positions_history(self, inst, limit=3, inst_type="SWAP"):
        return []

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
    assert order["clOrdId"].startswith("aeon")                              # recoverable if the reply is lost
    assert "attachAlgoOrds" not in order and "posSide" not in order and not ex.kinds("lev")   # no leverage on spot
    # the buy fee was taken in SOL, so only 1.998 SOL arrived: the stop must protect exactly that
    assert b._spot["SOL-USDT"]["qty"] == pytest.approx(1.998)
    algo = last_algo(ex)
    assert algo["tdMode"] == "cash" and algo["side"] == "sell" and algo["ordType"] == "oco" and algo["sz"] == "1.998"
    assert algo["slTriggerPx"] == "95" and algo["tpTriggerPx"] == "110"
    assert "closeFraction" not in algo and "reduceOnly" not in algo         # those are derivatives-only
    assert fill.event == "open" and fill.fee == pytest.approx(0.2) and fill.pnl == pytest.approx(-0.2)
    assert b._pending == {}


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
    assert b._spot == {} and ex.algos == [] and "SOL-USDT" not in b._known


async def test_spot_partial_reduce_re_protects_the_remainder():
    ex, b = await broker()
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    fill = await b.reduce(SOL, 1.0, 100.0, "trim")
    assert fill.event == "reduce" and ex.kinds("order")[-1]["sz"] == "1"
    assert b._spot["SOL-USDT"]["qty"] == pytest.approx(0.998)
    algo = last_algo(ex)
    assert algo["sz"] == "0.998" and algo["ordType"] == "conditional" and algo["slTriggerPx"] == "95"
    assert len(ex.algos) == 1                                               # the old stop was replaced, not duplicated
    assert b._known["SOL-USDT"].contracts == pytest.approx(0.998)           # visible at once, not on the next tick


async def test_only_what_aeon_bought_is_ever_sold():
    ex, b = await broker()
    ex.balances["BTC"] = 5.0                                                # coins you already held
    with pytest.raises(BrokerError, match="only sells what it bought"):
        await b.close(BTC, 80000.0, "t")
    assert not ex.kinds("order")


async def test_spot_holding_shows_up_as_a_position_and_its_stop_firing_is_booked():
    ex, b = await broker()
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=110.0, price=100.0, reason="t")
    assert await b.tick({"SOL-USDT": 100.0}) == []
    pos = b._known["SOL-USDT"]
    assert pos.kind == "spot" and pos.contracts == pytest.approx(1.998) and pos.lever == 1.0 and pos.sl == 95.0 and pos.tp == 110.0
    assert ex.pos_queries == ["SWAP", "FUTURES"]                            # derivatives positions are polled too

    ex.fire("SOL-USDT", "sl")                                               # the stop fires on the exchange
    fills = await b.tick({"SOL-USDT": 94.0})
    assert len(fills) == 1 and fills[0].event == "close" and fills[0].reason == "stop_loss"
    assert fills[0].price == pytest.approx(95.0) and not fills[0].est
    assert fills[0].trade_pnl == pytest.approx((95 - 100) * 1.998 - 1.998 * 95 * 0.001 - 0.2)
    assert b._spot == {} and "SOL-USDT" not in b._known


async def test_take_profit_firing_is_booked_as_such():
    ex, b = await broker()
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=110.0, price=100.0, reason="t")
    await b.tick({"SOL-USDT": 100.0})
    ex.fire("SOL-USDT", "tp")
    fills = await b.tick({"SOL-USDT": 111.0})
    assert fills[0].reason == "take_profit" and fills[0].trade_pnl > 0


async def test_coins_you_already_held_do_not_hide_a_stop_out():
    """Review finding F2: with 10 SOL already in the wallet, AEON used to miss its own stop-out and re-arm a
    stop against your coins. Now the stop order itself tells AEON its coins were sold."""
    ex, b = await broker()
    ex.balances["SOL"] = 10.0
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    await b.tick({"SOL-USDT": 100.0})
    ex.fire("SOL-USDT", "sl")
    fills = await b.tick({"SOL-USDT": 94.0})
    assert [f.event for f in fills] == ["close"] and fills[0].sz if hasattr(fills[0], "sz") else True
    assert fills[0].contracts == pytest.approx(1.998)
    assert b._spot == {}                                                    # AEON owns nothing any more...
    assert ex.algos == [] and ex.balances["SOL"] == pytest.approx(10.0)      # ...no stop on your 10 SOL, all still there
    await b.tick({"SOL-USDT": 94.0})
    assert ex.algos == [] and not ex.kinds("order")[1:]                     # and it never touches them later


async def test_coins_sold_outside_aeon_are_booked_as_an_estimate():
    ex, b = await broker()
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    await b.tick({"SOL-USDT": 100.0})
    ex.balances["SOL"] = 0.0                                                # you sold them in the OKX app
    fills = await b.tick({"SOL-USDT": 101.0})
    assert len(fills) == 1 and fills[0].event == "close" and fills[0].est and fills[0].reason == "sold outside AEON"
    assert b._spot == {}


async def test_missing_spot_stop_is_re_armed():
    ex, b = await broker()
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    await b.tick({"SOL-USDT": 100.0})
    ex.cancel_by_hand("SOL-USDT")                                           # someone cancelled it
    await b.tick({"SOL-USDT": 100.0})
    assert len(ex.algos) == 1 and any("re-armed" in n for n in b.drain_notes())


async def test_a_stop_that_just_left_the_book_is_not_re_armed_blind():
    """Right after a stop fires, OKX can briefly list it as neither pending nor finished. Re-arming then could
    put a stop on coins that are already sold (or on yours), so AEON waits for OKX's answer (up to 60s)."""
    ex, b = await broker()
    ex.balances["SOL"] = 10.0
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    await b.tick({"SOL-USDT": 100.0})
    algo = ex.algos[0]
    ex.algos.clear()                                                        # off the book, details still say "live"
    await b.tick({"SOL-USDT": 95.0})
    assert ex.algos == [] and len(ex.kinds("algo")) == 1                    # no blind re-arm
    ex.algos.append(algo)
    ex.fire("SOL-USDT", "sl")                                               # now OKX reports it fired
    fills = await b.tick({"SOL-USDT": 95.0})
    assert [f.reason for f in fills] == ["stop_loss"] and b._spot == {} and ex.algos == []


async def test_if_okx_never_explains_a_vanished_stop_aeon_re_arms_after_a_minute():
    ex, b = await broker()
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    await b.tick({"SOL-USDT": 100.0})
    ex.algos.clear()
    await b.tick({"SOL-USDT": 100.0})
    assert ex.algos == []
    b._unprotected_since["SOL-USDT"] -= 61                                  # a minute goes by
    await b.tick({"SOL-USDT": 100.0})
    assert len(ex.algos) == 1 and ex.algos[0]["sz"] == "1.998"


async def test_a_stop_that_is_not_ours_does_not_count_as_protection():
    ex, b = await broker()
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    ex.cancel_by_hand("SOL-USDT")
    ex.algos.append({"algoId": "yours", "instId": "SOL-USDT", "ordType": "conditional", "slTriggerPx": "50",
                     "tpTriggerPx": "", "sz": "0.1", "tdMode": "cash"})    # a stop you placed by hand
    await b.tick({"SOL-USDT": 100.0})
    # your small stop did not count as protecting AEON's 1.998 SOL: AEON placed its own full-size stop...
    assert any(c[0] == "algo" and c[1]["sz"] == "1.998" for c in ex.calls[1:])
    assert any(a["sz"] == "1.998" and a["slTriggerPx"] == "95" for a in ex.algos)
    # ...and left yours alone
    assert any(a["algoId"] == "yours" for a in ex.algos)
    assert not any(c[0] == "cancel" and any(i["algoId"] == "yours" for i in c[1]) for c in ex.calls)


async def test_a_failed_stop_after_a_spot_buy_is_reported_and_retried():
    ex, b = await broker()
    ex.fail_algo = 1                                                        # OKX rejects the stop once
    fill = await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    assert fill.event == "open" and "SOL-USDT" in b._spot                   # the buy happened and is tracked
    assert any("stop order could not be placed" in n and "UNPROTECTED" in n for n in b.drain_notes())
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


# --------------------------------------------------------------------------- lost replies (finding A1 / F3)
async def test_spot_buy_whose_fill_cannot_be_confirmed_is_reconciled_not_forgotten():
    ex, b = await broker()
    ex.fail_get = 8                                                         # every confirmation lookup fails
    with pytest.raises(PendingOrder, match="not confirmed"):
        await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="dip buy")
    assert ex.balances["SOL"] == pytest.approx(1.998)                       # OKX did buy the coins
    assert len(b._pending) == 1 and b._desired["SOL-USDT"] == (95.0, None)  # ...and AEON remembers the order
    fills = await b.tick({"SOL-USDT": 100.0})                               # lookups work again
    assert [f.event for f in fills] == ["open"] and fills[0].reason == "dip buy"
    assert b._spot["SOL-USDT"]["qty"] == pytest.approx(1.998) and b._pending == {}
    assert len(ex.algos) == 1 and ex.algos[0]["sz"] == "1.998"              # and it is protected
    assert any("not confirmed at the time" in n for n in b.drain_notes())


async def test_lost_reply_on_send_is_found_by_client_id():
    ex, b = await broker()
    ex.lose_reply = True                                                    # OKX took the order, the reply vanished
    with pytest.raises(PendingOrder, match="no reply"):
        await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    (cl, p), = b._pending.items()
    assert p["ord_id"] is None and cl in ex.by_cl                           # only the client id is known
    fills = await b.tick({"SOL-USDT": 100.0})
    assert [f.event for f in fills] == ["open"] and "SOL-USDT" in b._spot


async def test_an_order_that_never_reached_okx_is_dropped_after_a_minute():
    ex, b = await broker()

    async def no_reply(body):
        raise OkxError("net", "ConnectError before anything was sent", "/api/v5/trade/order")
    ex.place_order = no_reply
    with pytest.raises(PendingOrder):
        await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    await b.tick({"SOL-USDT": 100.0})
    assert len(b._pending) == 1                                             # too early to give up
    next(iter(b._pending.values()))["ts"] -= 120
    await b.tick({"SOL-USDT": 100.0})
    assert b._pending == {} and "SOL-USDT" not in b._desired and b._spot == {}
    assert any("never reached OKX" in n for n in b.drain_notes())


async def test_a_definitive_rejection_leaves_nothing_behind():
    ex, b = await broker()

    async def rejected(body):
        raise OkxError("51008", "Insufficient balance", "/api/v5/trade/order")
    ex.place_order = rejected
    with pytest.raises(OkxError, match="Insufficient"):
        await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    assert b._pending == {} and b._desired == {} and b._spot == {}


async def test_pending_orders_survive_a_restart():
    ex, b = await broker()
    ex.fail_get = 8
    with pytest.raises(PendingOrder):
        await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    saved = json.loads(json.dumps(b.dump()))
    b2 = OkxBroker(ex, SPECS)
    await b2.init()
    b2.load(saved)
    fills = await b2.tick({"SOL-USDT": 100.0})
    assert [f.event for f in fills] == ["open"] and "SOL-USDT" in b2._spot


async def test_state_is_saved_the_moment_an_order_is_recorded():
    ex, b = await broker()
    saves = []
    b.on_change = lambda: saves.append(json.loads(json.dumps(b.dump())))
    await b.open(SOL, "long", 2.0, 1.0, sl=95.0, tp=None, price=100.0, reason="t")
    assert saves and saves[0]["pending"]                                    # saved BEFORE the order was sent
    assert saves[-1]["pending"] == {} and "SOL-USDT" in saves[-1]["spot"]


# --------------------------------------------------------------------------- dated futures / derivatives
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


async def test_a_new_position_is_visible_immediately_not_on_the_next_tick():
    """Review finding F1: a second decision in the same cycle must see the first one's position."""
    ex, b = await broker()
    await b.open(FUT, "long", 0.5, 3.0, 79000.0, None, 81000.0, "t")
    acct = await b.account({})
    assert FUT.inst in acct.positions and acct.positions[FUT.inst].contracts == 0.5
    assert acct.positions[FUT.inst].side == "long" and acct.positions[FUT.inst].lever == 3.0
    await b.open(SOL, "long", 1.0, 1.0, 95.0, None, 100.0, "t")
    assert set((await b.account({})).positions) == {FUT.inst, "SOL-USDT"}


async def test_derivative_add_updates_the_position_at_once():
    ex, b = await broker()
    await b.open(FUT, "long", 0.5, 3.0, 79000.0, None, 81000.0, "t")
    ex.px[FUT.inst] = 82000.0
    await b.add(FUT, "long", 0.5, 79000.0, None, 82000.0, "t")
    pos = (await b.account({})).positions[FUT.inst]
    assert pos.contracts == 1.0 and pos.entry == pytest.approx(81500.0)


async def test_an_executed_add_is_booked_even_if_its_new_stop_fails():
    """Review finding F5: the add happened on the exchange; it must reach the journal."""
    ex, b = await broker()
    await b.open(FUT, "long", 0.5, 3.0, 79000.0, None, 81000.0, "t")
    ex.fail_algo = 1
    fill = await b.add(FUT, "long", 0.2, 79000.0, None, 81000.0, "t")       # no exception any more
    assert fill.event == "add" and fill.contracts == 0.2
    assert any("UNPROTECTED" in n for n in b.drain_notes())
    assert b._desired[FUT.inst] == (79000.0, None) and FUT.inst in b._unprotected_since


async def test_an_executed_reduce_is_booked_even_if_its_new_stop_fails():
    ex, b = await broker()
    await b.open(FUT, "long", 0.5, 3.0, 79000.0, None, 81000.0, "t")
    ex.fail_algo = 1
    fill = await b.reduce(FUT, 0.2, 81000.0, "trim")
    assert fill.event == "reduce" and fill.contracts == 0.2
    assert (await b.account({})).positions[FUT.inst].contracts == pytest.approx(0.3)
    reduce_order = ex.kinds("order")[-1]
    assert reduce_order["reduceOnly"] is True and reduce_order["side"] == "sell"


async def test_non_numeric_stops_never_reach_the_exchange():
    ex, b = await broker()
    with pytest.raises(BrokerError, match="non-numeric"):
        await b.open(FUT, "long", 0.5, 3.0, float("nan"), None, 81000.0, "t")
    with pytest.raises(BrokerError, match="non-numeric"):
        await b.set_protection(FUT, "long", 79000.0, float("inf"))
    assert not ex.kinds("order") and not ex.kinds("algo")


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
    assert b2._known["SOL-USDT"].kind == "spot" and b2._algo_ids["SOL-USDT"]
    # it still knows its stop, so a stop that fired while it was offline is booked, not forgotten
    ex.fire("SOL-USDT", "sl")
    fills = await b2.tick({"SOL-USDT": 94.0})
    assert len(fills) == 1 and fills[0].event == "close" and fills[0].reason == "stop_loss"
