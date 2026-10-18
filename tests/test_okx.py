import base64
import hashlib
import hmac

import httpx
import pytest

from aeon import okx_broker
from aeon.broker import BrokerError
from aeon.indicators import ema, rsi
from aeon.market import Spec
from aeon.okx import OkxClient, OkxError, fmt, px_str, round_step
from aeon.okx_broker import OkxBroker

SOL = Spec("SOL-USDT-SWAP", "SOL", ct_val=1.0, lot_sz=0.01, min_sz=0.01, tick_sz=0.01, max_lever=50)


def test_signature_matches_okx_scheme():
    c = OkxClient("k", "secret", "pass")
    ts, body = "2026-10-17T09:00:00.000Z", '{"a":1}'
    want = base64.b64encode(hmac.new(b"secret", f"{ts}POST/api/v5/trade/order{body}".encode(),
                                     hashlib.sha256).digest()).decode()
    assert c._sign(ts, "POST", "/api/v5/trade/order", body) == want


def test_number_formatting():
    assert fmt(100) == "100" and fmt(0.00001) == "0.00001" and fmt(1.50) == "1.5"
    assert px_str(123.456, 0.01) == "123.46" and px_str(81644.83, 0.1) == "81644.8"
    assert round_step(0.30000000000000004, 0.01) == 0.3


def test_indicators_basic():
    rising = [float(i) for i in range(1, 40)]
    assert rsi(rising) == 100.0
    assert rsi([5.0] * 30) == 50.0
    assert ema([1, 2, 3], 3)[-1] > ema([1, 2, 3], 3)[0]


class FakeOkx:
    def __init__(self, pos_mode="net_mode"):
        self.pos_mode, self.calls, self.algos = pos_mode, [], []

    async def account_config(self):
        return [{"posMode": self.pos_mode, "acctLv": "2"}]

    async def set_leverage(self, inst, lever, mgn, ps):
        self.calls.append(("lev", inst, lever, mgn, ps))

    async def place_order(self, body):
        self.calls.append(("order", body))
        return [{"ordId": "42", "sCode": "0"}]

    async def get_order(self, inst, ord_id):
        return {"state": "filled", "avgPx": "100.5", "accFillSz": "2", "fee": "-0.1"}

    async def algos_pending(self, ord_type):
        return list(self.algos) if ord_type == "oco" else []

    async def place_algo(self, body):
        self.calls.append(("algo", body))
        return [{"algoId": "7", "sCode": "0"}]

    async def cancel_algos(self, items):
        self.calls.append(("cancel", items))

    async def positions(self, t="SWAP"):
        return []


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(okx_broker.asyncio, "sleep", instant)


async def test_open_sends_isolated_market_order_with_attached_stop_in_net_mode():
    fake = FakeOkx()
    b = OkxBroker(fake, {SOL.inst: SOL})
    await b.init()
    fill = await b.open(SOL, "long", 2.0, 3.0, sl=97.123, tp=105.0, price=100.0, reason="t")
    order = next(c[1] for c in fake.calls if c[0] == "order")
    assert order["tdMode"] == "isolated" and order["ordType"] == "market" and order["side"] == "buy"
    assert order["sz"] == "2" and "posSide" not in order and order["clOrdId"].startswith("aeon")
    att = order["attachAlgoOrds"][0]
    assert att["slTriggerPx"] == "97.12" and att["slOrdPx"] == "-1" and att["tpTriggerPx"] == "105"
    assert next(c for c in fake.calls if c[0] == "lev")[2:] == (3.0, "isolated", None)
    assert fill.price == 100.5 and fill.fee == pytest.approx(0.1) and fill.pnl == pytest.approx(-0.1)


async def test_hedge_mode_is_refused_for_derivatives_rather_than_mis_tracked():
    b = OkxBroker(FakeOkx("long_short_mode"), {SOL.inst: SOL})
    with pytest.raises(BrokerError, match="net mode only"):
        await b.init()


async def test_hedge_mode_is_harmless_for_a_spot_only_setup():
    b = OkxBroker(FakeOkx("long_short_mode"), {}, kinds={"spot"})
    await b.init()
    assert b.allowed_kinds == {"spot"}


async def test_set_protection_cancels_old_then_places_full_position_stop():
    fake = FakeOkx()
    fake.algos = [{"algoId": "old1", "instId": SOL.inst, "slTriggerPx": "95", "tpTriggerPx": ""}]
    b = OkxBroker(fake, {SOL.inst: SOL})
    await b.init()
    await b.set_protection(SOL, "long", 96.0, None)
    kinds = [c[0] for c in fake.calls]
    assert kinds.index("cancel") < kinds.index("algo")
    algo = next(c[1] for c in fake.calls if c[0] == "algo")
    assert algo["ordType"] == "conditional" and algo["closeFraction"] == "1" and algo["side"] == "sell"
    assert algo["reduceOnly"] is True and algo["slTriggerPx"] == "96"


async def test_spot_mode_account_degrades_to_spot_only():
    class Spot(FakeOkx):
        async def account_config(self):
            return [{"posMode": "net_mode", "acctLv": "1"}]
    b = OkxBroker(Spot(), {})
    await b.init()                                              # no longer refuses: perps/futures are just switched off
    assert b.allowed_kinds == {"spot"} and "Spot mode" in b.restrictions[0] and "swap" in b.restrictions[0]
    with pytest.raises(Exception, match="Spot mode"):          # ...but a config with nothing tradable still fails loudly
        await OkxBroker(Spot(), {}, kinds={"swap", "futures"}).init()


# --------------------------------------------------------------------------- an unclear reply to a write is not a "no"
def client_with(handler, **kw) -> OkxClient:
    c = OkxClient("k", "s", "p", **kw)
    c._http = httpx.AsyncClient(base_url="https://okx.test", transport=httpx.MockTransport(handler))
    return c


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_a_server_error_on_a_write_means_outcome_unknown(status):
    c = client_with(lambda req: httpx.Response(status, text="<html>bad gateway</html>"))
    with pytest.raises(OkxError) as e:
        await c.post("/api/v5/trade/order", {"instId": "X"})
    assert e.value.code == "net"                      # the broker treats "net" as "look the order up", not "rejected"


async def test_a_200_with_garbage_on_a_write_means_outcome_unknown():
    c = client_with(lambda req: httpx.Response(200, text="not json"))
    with pytest.raises(OkxError) as e:
        await c.post("/api/v5/trade/order", {"instId": "X"})
    assert e.value.code == "net"


async def test_a_definite_refusal_on_a_write_is_still_a_refusal():
    c = client_with(lambda req: httpx.Response(200, json={"code": "51008", "msg": "Insufficient balance", "data": []}))
    with pytest.raises(OkxError) as e:
        await c.post("/api/v5/trade/order", {"instId": "X"})
    assert e.value.code == "51008"
    c = client_with(lambda req: httpx.Response(403, text="blocked by firewall"))
    with pytest.raises(OkxError) as e:
        await c.post("/api/v5/trade/order", {"instId": "X"})
    assert e.value.code == "403"                      # never reached the matching engine


async def test_reads_still_retry_server_errors_and_succeed(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(502) if len(calls) < 3 else httpx.Response(200, json={"code": "0", "data": [{"ts": "1"}]})
    async def instant(_):
        return None
    monkeypatch.setattr("aeon.okx.asyncio.sleep", instant)
    assert await client_with(handler).get("/api/v5/public/time") == [{"ts": "1"}] and len(calls) == 3


async def test_server_time_is_returned_in_seconds():
    c = client_with(lambda req: httpx.Response(200, json={"code": "0", "data": [{"ts": "1792360473000"}]}))
    assert await c.server_time() == 1792360473.0
