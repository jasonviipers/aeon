import time

import pytest

from aeon.config import RiskCfg, UniverseCfg
from aeon.market import Market, Spec, size_contracts

DAY = 86400


def inst_row(inst, **kw):
    row = {"instId": inst, "state": "live", "ctType": "linear", "settleCcy": "USDT", "instCategory": "1",
           "ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1", "lever": "100",
           "expTime": "", "alias": "", "instFamily": inst.rsplit("-", 1)[0]}
    row.update(kw)
    return row


def spot_row(inst, **kw):
    base, quote = inst.split("-")
    row = {"instId": inst, "state": "live", "baseCcy": base, "quoteCcy": quote, "instCategory": "1",
           "lotSz": "0.000001", "minSz": "0.01", "tickSz": "0.01", "lever": "10"}
    row.update(kw)
    return row


def exp_ms(days):
    return str(int((time.time() + days * DAY) * 1000))


SWAPS = [inst_row("BTC-USDT-SWAP"), inst_row("ETH-USDT-SWAP", ctVal="0.1"), inst_row("SOL-USDT-SWAP", ctVal="1"),
         inst_row("BTC-USD-SWAP", settleCcy="BTC", ctType="inverse")]            # inverse: never offered
FUTURES = [
    inst_row("BTC-USD_UM-261030", settleCcy="USD", alias="this_month", expTime=exp_ms(13)),
    inst_row("BTC-USD_UM-261225", settleCcy="USD", alias="quarter", expTime=exp_ms(69)),        # same coin, less liquid
    inst_row("ETH-USD_UM-261127", settleCcy="USD", alias="next_month", expTime=exp_ms(41), ctVal="0.1"),
    inst_row("SOL-USD_UM-261020", settleCcy="USD", alias="this_month", expTime=exp_ms(2), ctVal="1"),  # too close to expiry
    inst_row("BTC-USD_UM_XPERP-310404", settleCcy="USD", alias="this_five_years", expTime=exp_ms(1600),
             instFamily="BTC-USD_UM_XPERP"),
    inst_row("BTC-USD-261225", settleCcy="BTC", ctType="inverse", expTime=exp_ms(69)),         # coin-margined: never offered
]
SPOTS = [spot_row("BTC-USDT", minSz="0.00001"), spot_row("ETH-USDT", minSz="0.0001"), spot_row("SOL-USDT"),
         spot_row("DOGE-USDT"),
         spot_row("USDC-USDT"), spot_row("SOL-USDC")]                                          # stable base / wrong quote

TICKS = {
    "SWAP": {"BTC-USDT-SWAP": (80000, 5e9), "ETH-USDT-SWAP": (2400, 4e9), "SOL-USDT-SWAP": (100, 3e8),
             "BTC-USD-SWAP": (80000, 1e9)},
    "FUTURES": {"BTC-USD_UM-261030": (80400, 1e6), "BTC-USD_UM-261225": (80900, 2e5), "ETH-USD_UM-261127": (2410, 3e5),
                "SOL-USD_UM-261020": (100, 4e5), "BTC-USD_UM_XPERP-310404": (80010, 2e8), "BTC-USD-261225": (80000, 9e5)},
    "SPOT": {"BTC-USDT": (80000, 4e8), "ETH-USDT": (2400, 2.5e8), "SOL-USDT": (100, 5e7), "DOGE-USDT": (0.08, 1e6),
             "USDC-USDT": (1.0, 9e8), "SOL-USDC": (100, 9e7)},
}


class FakeOkx:
    async def instruments(self, inst_type, family=None):
        return {"SWAP": SWAPS, "FUTURES": FUTURES, "SPOT": SPOTS}[inst_type]

    async def tickers(self, inst_type):
        out = []
        for inst, (last, usd) in TICKS[inst_type].items():
            # derivatives report volume in coins, spot already in USDT
            vol = usd if inst_type == "SPOT" else usd / last
            out.append({"instId": inst, "last": str(last), "open24h": str(last * 0.99), "volCcy24h": str(vol)})
        return out

    async def candles(self, inst, bar="15m", limit=100):
        base = 100.0
        return [[str(1_700_000_000_000 - i * 60_000), str(base), str(base + 1), str(base - 1), str(base + (50 - i) * 0.05),
                 "1", "1", "1", "1"] for i in range(limit)]

    async def funding_rate(self, inst):
        return {"fundingRate": "0.0001"}

    async def index_tickers(self, quote="USDT"):
        return {"BTC-USDT": 80000.0, "ETH-USDT": 2400.0}


def make_market(**uni):
    return Market(FakeOkx(), RiskCfg(), UniverseCfg(**uni))


async def test_only_tradable_contracts_are_loaded():
    m = make_market()
    await m.load_specs()
    kinds = {k: sorted(s.inst for s in m.specs.values() if s.kind == k) for k in ("swap", "futures", "spot")}
    assert kinds["swap"] == ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]                  # inverse swap dropped
    assert "BTC-USD-261225" not in kinds["futures"]                                              # coin-margined dropped
    assert "BTC-USD_UM_XPERP-310404" not in kinds["futures"]                                     # XPERP is opt-in
    assert "USDC-USDT" not in kinds["spot"] and "SOL-USDC" not in kinds["spot"]                  # stablecoin / non-USDT quote
    fut = m.specs["BTC-USD_UM-261030"]
    assert fut.kind == "futures" and fut.tag == "fut-mon" and fut.days_left() == pytest.approx(13, abs=0.1)
    assert m.specs["BTC-USDT"].kind == "spot" and m.specs["BTC-USDT"].max_lever == 1.0 and m.specs["BTC-USDT"].tag == "spot"


async def test_xperp_futures_can_be_enabled():
    m = make_market(futures_include_xperp=True)
    await m.load_specs()
    x = m.specs["BTC-USD_UM_XPERP-310404"]
    assert x.kind == "futures" and x.tag == "fut-5y" and x.days_left() > 1000


async def test_universe_has_a_quota_per_kind_and_sane_futures():
    m = make_market(top_n=2, top_n_futures=5, top_n_spot=2, min_volume_usd=30_000_000, futures_min_volume_usd=100_000)
    await m.load_specs()
    chosen, tick = await m.pick_universe(200.0, set())
    by = lambda k: [i for i in chosen if m.specs[i].kind == k]
    assert by("swap") == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]                      # top 2 by volume
    assert by("spot") == ["BTC-USDT", "ETH-USDT"]                                # SOL spot is next but the quota is 2
    # futures: BTC's most liquid expiry only, ETH, and NOT the SOL one expiring in 2 days
    assert by("futures") == ["BTC-USD_UM-261030", "ETH-USD_UM-261127"]
    assert set(tick) >= set(chosen)


async def test_futures_use_their_own_volume_bar_and_held_positions_always_included():
    m = make_market(futures_min_volume_usd=500_000)                              # ETH's 300k and BTC quarter 200k now fail
    await m.load_specs()
    chosen, _ = await m.pick_universe(200.0, {"DOGE-USDT"})
    assert [i for i in chosen if m.specs[i].kind == "futures"] == ["BTC-USD_UM-261030"]
    assert "DOGE-USDT" in chosen                                                  # held, though far below the volume bar


async def test_enabled_markets_can_be_narrowed_at_runtime():
    m = make_market()
    await m.load_specs()
    m.enabled = {"spot"}                                                          # e.g. the OKX account is in Spot mode
    chosen, tick = await m.pick_universe(200.0, set())
    assert chosen and all(m.specs[i].kind == "spot" for i in chosen)
    assert all(m.specs[i].kind == "spot" for i in tick)


async def test_list_mode_picks_exact_ids_of_any_kind():
    m = make_market(mode="list", instruments=["SOL-USDT", "BTC-USD_UM-261030", "ETH-USDT-SWAP", "NOPE-USDT"])
    await m.load_specs()
    chosen, _ = await m.pick_universe(200.0, set())
    assert chosen == ["SOL-USDT", "BTC-USD_UM-261030", "ETH-USDT-SWAP"]


async def test_snapshot_computes_basis_and_skips_irrelevant_funding():
    m = make_market()
    await m.load_specs()
    chosen, tick = await m.pick_universe(200.0, set())
    snaps = await m.snapshot(chosen, tick)
    fut = snaps["BTC-USD_UM-261030"]
    assert fut.basis == pytest.approx((80400 / 80000 - 1) * 100) and fut.funding is None
    assert fut.ann_basis == pytest.approx(fut.basis * 365 / fut.days_left, rel=1e-6)
    assert snaps["BTC-USDT-SWAP"].funding == pytest.approx(0.0001)                 # perps carry funding
    assert snaps["BTC-USDT"].funding is None and snaps["BTC-USDT"].basis is None   # spot has neither
    assert snaps["BTC-USDT"].tag == "spot" and snaps["BTC-USDT"].kind == "spot"


def test_spot_sizing_uses_coin_lots():
    spot = Spec("SOL-USDT", "SOL", 1.0, 0.000001, 0.01, 0.01, 1.0, kind="spot")
    assert size_contracts(50.0, 104.8, spot) == pytest.approx(0.477099, abs=1e-6)     # 50 USDT of SOL, floored to the lot
    assert spot.min_notional(104.8) == pytest.approx(1.048)
    assert spot.days_left() is None
