import json
from dataclasses import asdict

import pytest

from aeon.broker import BrokerError, Fill, PaperBroker
from aeon.config import RiskCfg, UniverseCfg
from aeon.feed import PriceFeed
from aeon.indicators import ema, rsi
from aeon.market import InstSnap, Market, Spec


SPOT = Spec("SOL-USDT", "SOL", 1.0, 0.01, 0.01, 0.01, 1.0, kind="spot")
OTHER = Spec("ETH-USDT", "ETH", 1.0, 0.01, 0.01, 0.01, 1.0, kind="spot")
SWAP = Spec("SOL-USDT-SWAP", "SOL", 1.0, 0.01, 0.01, 0.01, 10.0)
EDGE = 1_800_000_000.0


@pytest.mark.parametrize("mark", [50.0, 100.0, 200.0])
async def test_unsold_spot_does_not_change_spendable_cash(mark):
    broker = PaperBroker(1000.0, 0.001, 0.0)
    await broker.open(SPOT, "long", 8.0, 1.0, None, None, 100.0, "test")
    account = await broker.account({SPOT.inst: mark})
    assert account.available == pytest.approx(199.2)
    assert account.equity == pytest.approx(999.2 + 8.0 * (mark - 100.0))
    with pytest.raises(BrokerError, match="insufficient"):
        await broker.open(OTHER, "long", 3.0, 1.0, None, None, 100.0, "test")
    with pytest.raises(BrokerError, match="insufficient"):
        await broker.add(SPOT, "long", 5.0, None, None, mark, "test")


async def test_spot_cash_released_only_on_sale_and_survives_old_state_load():
    broker = PaperBroker(1000.0, 0.0, 0.0)
    await broker.open(SPOT, "long", 8.0, 1.0, None, None, 100.0, "test")
    await broker.account({SPOT.inst: 200.0})
    restored = PaperBroker(0.0, 0.0, 0.0)
    restored.load(broker.dump())
    assert restored._available() == pytest.approx(200.0)
    await restored.reduce(SPOT, 2.0, 200.0, "test")
    assert restored._available() == pytest.approx(600.0)
    await restored.close(SPOT, 200.0, "test")
    assert restored._available() == pytest.approx(1800.0)


@pytest.mark.parametrize("kind", ["swap", "futures"])
async def test_derivative_margin_and_mixed_account_keep_existing_behavior(kind):
    derivative = Spec(SWAP.inst, SWAP.base, 1.0, 0.01, 0.01, 0.01, 10.0, kind=kind)
    broker = PaperBroker(1000.0, 0.0, 0.0)
    await broker.open(derivative, "long", 4.0, 2.0, None, None, 100.0, "test")
    account = await broker.account({derivative.inst: 150.0})
    assert account.available == pytest.approx(1000.0)
    await broker.open(SPOT, "long", 5.0, 1.0, None, None, 100.0, "test")
    account = await broker.account({derivative.inst: 150.0, SPOT.inst: 200.0})
    assert account.available == pytest.approx(500.0)


def test_fill_and_snapshot_new_fields_are_optional():
    fill = Fill(1.0, SPOT.inst, "long", "open", 100.0, 1.0)
    assert fill.execution_id == ""
    fill.execution_id = "order:trade"
    assert Fill(**asdict(fill)).execution_id == "order:trade"
    assert InstSnap(SPOT.inst, 100.0).price_ts == 0.0


def candles(interval, forming=True):
    rows = []
    for offset in range(60):
        close = 100.0 - offset
        rows.append([str(int((EDGE - (offset + 1) * interval) * 1000)),
                     str(close), str(close + 1), str(close - 1), str(close), "1", "1", "1", "1"])
    if forming:
        rows.insert(0, [str(int(EDGE * 1000)), "9999", "9999", "1", "9999", "1", "1", "1", "0"])
    return rows


class OfflineOkx:
    def __init__(self, clock, timestamp=None, forming=True):
        self.clock = clock
        self.timestamp = timestamp
        self.bars = {"15m": candles(900.0, forming), "1H": candles(3600.0, forming)}

    async def tickers(self, inst_type):
        self.clock[0] += 30.0
        row = {"instId": SPOT.inst, "last": "110", "open24h": "100", "volCcy24h": "1000"}
        if self.timestamp is not None:
            row["ts"] = self.timestamp
        return [row]

    async def candles(self, inst, bar, limit):
        self.clock[0] += 30.0
        return self.bars[bar]


def market_with_clock(monkeypatch, timestamp=None, forming=True):
    clock = [EDGE + 600.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    okx = OfflineOkx(clock, timestamp, forming)
    market = Market(okx, RiskCfg(), UniverseCfg(markets=["spot"]))
    market.specs[SPOT.inst] = SPOT
    return market, clock


@pytest.mark.parametrize("exchange_timestamp", [True, False])
async def test_market_price_timestamp_is_source_or_request_start(monkeypatch, exchange_timestamp):
    source = EDGE + 300.125
    market, clock = market_with_clock(monkeypatch, str(int(source * 1000)) if exchange_timestamp else None)
    started = clock[0]
    tick = await market.tickers()
    expected = source if exchange_timestamp else started
    assert tick[SPOT.inst]["price_ts"] == expected
    snapshot = (await market.snapshot([SPOT.inst], tick))[SPOT.inst]
    assert snapshot.price_ts == expected
    assert snapshot.price_ts < clock[0]


async def test_legacy_tickers_use_snapshot_start_before_candle_requests(monkeypatch):
    market, clock = market_with_clock(monkeypatch)
    started = clock[0]
    tick = {SPOT.inst: {"last": 110.0, "chg24h": 0.0, "vol_usd": 1000.0}}
    snapshot = (await market.snapshot([SPOT.inst], tick))[SPOT.inst]
    assert snapshot.price_ts == started
    assert clock[0] > started
    assert "price_ts" not in tick[SPOT.inst]


@pytest.mark.parametrize("forming", [True, False])
async def test_return_horizons_and_indicators_ignore_forming_bars(monkeypatch, forming):
    market, clock = market_with_clock(monkeypatch, forming=forming)
    tick = {SPOT.inst: {"last": 110.0, "chg24h": 0.0, "vol_usd": 1000.0, "price_ts": clock[0]}}
    snapshot = (await market.snapshot([SPOT.inst], tick))[SPOT.inst]
    assert snapshot.ret15m == pytest.approx((110.0 / 99.0 - 1) * 100)
    assert snapshot.ret1h == pytest.approx((110.0 / 96.0 - 1) * 100)
    assert snapshot.ret4h == pytest.approx((110.0 / 96.0 - 1) * 100)
    closed = list(reversed([100.0 - offset for offset in range(60)]))
    assert snapshot.closes_1h == closed[-24:]
    assert snapshot.ema20 == ema(closed, 20)[-1]
    assert snapshot.rsi1h == rsi(closed)


async def test_missing_bar_does_not_shift_horizons_by_list_index(monkeypatch):
    market, clock = market_with_clock(monkeypatch, forming=False)
    del market.okx.bars["15m"][2]
    tick = {SPOT.inst: {"last": 110.0, "chg24h": 0.0, "vol_usd": 1000.0, "price_ts": clock[0]}}
    snapshot = (await market.snapshot([SPOT.inst], tick))[SPOT.inst]
    assert snapshot.ret1h == pytest.approx((110.0 / 96.0 - 1) * 100)


async def test_missing_reference_and_unconfirmed_history_are_not_used(monkeypatch):
    market, clock = market_with_clock(monkeypatch, forming=False)
    market.okx.bars["15m"][4][8] = "0"
    market.okx.bars["1H"][4][8] = "0"
    tick = {SPOT.inst: {"last": 110.0, "chg24h": 0.0, "vol_usd": 1000.0, "price_ts": clock[0]}}
    snapshot = (await market.snapshot([SPOT.inst], tick))[SPOT.inst]
    assert snapshot.ret1h == 0.0
    assert snapshot.ret4h == 0.0
    assert 96.0 not in snapshot.closes_1h


async def test_legacy_candles_without_confirmation_still_exclude_open_intervals(monkeypatch):
    market, clock = market_with_clock(monkeypatch)
    market.okx.bars = {bar: [row[:8] for row in rows] for bar, rows in market.okx.bars.items()}
    tick = {SPOT.inst: {"last": 110.0, "chg24h": 0.0, "vol_usd": 1000.0, "price_ts": clock[0]}}
    snapshot = (await market.snapshot([SPOT.inst], tick))[SPOT.inst]
    assert max(snapshot.closes_1h) == 100.0
    assert snapshot.ret15m == pytest.approx((110.0 / 99.0 - 1) * 100)


@pytest.mark.parametrize("exchange_timestamp", [True, False])
async def test_rest_feed_preserves_source_or_request_start_and_stale_age(monkeypatch, exchange_timestamp):
    source = EDGE - 600.125
    market, clock = market_with_clock(monkeypatch, str(int(source * 1000)) if exchange_timestamp else None)
    feed = PriceFeed(market.okx)
    started = clock[0]
    await feed.refresh([SPOT.inst])
    assert feed.ts[SPOT.inst] == (source if exchange_timestamp else started)
    assert feed.age(SPOT.inst) >= (1200.0 if exchange_timestamp else 30.0)
    await feed.refresh([SPOT.inst])
    if exchange_timestamp:
        assert feed.ts[SPOT.inst] == source


async def test_ws_feed_preserves_exchange_timestamp_and_legacy_receipt_fallback(monkeypatch):
    market, clock = market_with_clock(monkeypatch)
    feed = PriceFeed(market.okx)

    async def messages():
        yield json.dumps({"data": [{"instId": SPOT.inst, "last": "110", "ts": str(int((EDGE - 600) * 1000))}]})
        clock[0] += 30.0
        yield json.dumps({"data": [{"instId": OTHER.inst, "last": "100"}]})

    await feed._read(messages())
    assert feed.ts[SPOT.inst] == EDGE - 600.0
    assert feed.ts[OTHER.inst] == clock[0]
    assert feed.age(SPOT.inst) == 1230.0
