"""The demo smoke test, run against a fake exchange that behaves like OKX for one perpetual and one spot pair.
The point of these tests is the verdict: it must pass on a healthy exchange, fail loudly when the exchange does not
do what AEON needs, and always leave the account flat."""
import time

import pytest

from aeon import okx_broker
from aeon.config import Config
from aeon.journal import Journal
from aeon.market import Spec
from aeon.smoke import run_smoke_demo, smoke
from test_okx_broker_kinds import FakeExchange

PERP = Spec("BTC-USDT-SWAP", "BTC", 0.01, 0.01, 0.01, 0.1, 100.0)
SPOT = Spec("BTC-USDT", "BTC", 1.0, 0.00000001, 0.00001, 0.1, 1.0, kind="spot")
SPECS = {PERP.inst: PERP, SPOT.inst: SPOT}


class SmokeExchange(FakeExchange):
    """FakeExchange plus what a perpetual needs: a position row per fill, attached stops, close-position."""

    def __init__(self):
        super().__init__()
        self.px.update({"BTC-USDT-SWAP": 80000.0, "BTC-USDT": 80000.0})
        self.attach_algos = True

    async def tickers(self, inst_type="SWAP"):
        return [{"instId": i, "last": str(p)} for i, p in self.px.items()]

    async def place_order(self, body):
        rows = await super().place_order(body)
        if body["tdMode"] == "isolated":
            inst, px = body["instId"], self.px[body["instId"]]
            self.pos_rows["SWAP"] = [{"instId": inst, "pos": body["sz"], "posSide": "net", "avgPx": str(px),
                                      "markPx": str(px), "lever": "2", "upl": "0", "cTime": str(int(time.time() * 1000))}]
            for att in (body.get("attachAlgoOrds") or []) if self.attach_algos else []:
                both = att.get("slTriggerPx") and att.get("tpTriggerPx")
                self.algos.append({"algoId": f"att{len(self.algos)}", "instId": inst, "ordType": "oco" if both else "conditional",
                                   "slTriggerPx": att.get("slTriggerPx", ""), "tpTriggerPx": att.get("tpTriggerPx", ""),
                                   "sz": body["sz"], "tdMode": "isolated"})
        return rows

    async def close_position(self, body):
        await super().close_position(body)
        self.pos_rows["SWAP"] = []
        self.algos = [a for a in self.algos if a["instId"] != body["instId"]]
        return [{}]

    async def positions_history(self, inst, limit=3, inst_type="SWAP"):
        return [{"uTime": str(int(time.time() * 1000)), "realizedPnl": "-0.05", "closeAvgPx": str(self.px[inst]),
                 "fee": "-0.04", "type": "2"}]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(okx_broker.asyncio, "sleep", instant)           # asyncio.sleep is one global function


async def run(ex):
    out: list[str] = []
    good = await smoke(ex, SPECS, out.append, kinds={"swap", "spot"})
    return good, "\n".join(out)


async def test_a_healthy_exchange_passes_both_legs_and_ends_flat():
    ex = SmokeExchange()
    good, text = await run(ex)
    assert good, text
    assert "[!!]" not in text and text.count("[ok]") >= 10
    assert "perpetual leg: BTC-USDT-SWAP" in text and "spot leg: BTC-USDT" in text
    assert ex.pos_rows["SWAP"] == [] and ex.algos == []                 # nothing left behind
    assert ex.balances.get("BTC", 0.0) == pytest.approx(0.0, abs=1e-9)   # the spot coins were sold again
    sizes = [c[1]["sz"] for c in ex.calls if c[0] == "order"]
    assert sizes[0] == "0.01"                                            # one minimum contract, nothing bigger


async def test_missing_attached_stops_are_reported_as_a_failure():
    ex = SmokeExchange()
    ex.attach_algos = False                                              # OKX did not create the attached SL/TP
    good, text = await run(ex)
    assert not good
    assert "[!!] stop-loss and take-profit are on the exchange" in text
    assert ex.pos_rows["SWAP"] == [] and ex.algos == []                  # still cleaned up


async def test_a_mid_leg_failure_is_reported_and_the_account_is_left_flat():
    ex = SmokeExchange()
    ex.fail_algo = 1                                                     # moving the stop is rejected
    good, text = await run(ex)
    assert not good and "[!!]" in text and "stop rejected" in text
    assert "cleaned up BTC-USDT-SWAP" in text
    assert ex.pos_rows["SWAP"] == [] and ex.algos == []


async def test_a_market_that_is_not_available_is_skipped_not_failed():
    ex = SmokeExchange()
    out: list[str] = []
    good = await smoke(ex, {SPOT.inst: SPOT}, out.append, kinds={"swap", "spot"})
    text = "\n".join(out)
    assert good and f"skipping {PERP.inst}" in text


async def test_nothing_to_test_is_not_a_pass():
    out: list[str] = []
    assert await smoke(SmokeExchange(), {}, out.append, kinds={"swap", "spot"}) is False


# --------------------------------------------------------------------------- the guard rails
async def test_it_refuses_to_run_outside_demo_mode():
    cfg = Config()
    cfg.run.mode = "live"
    with pytest.raises(SystemExit, match="only runs in demo mode"):
        await run_smoke_demo(cfg, say=lambda *_: None)


async def test_it_needs_demo_credentials(monkeypatch):
    for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"):
        monkeypatch.delenv(k, raising=False)
    cfg = Config()
    cfg.run.mode = "demo"
    with pytest.raises(SystemExit, match="OKX_API_KEY"):
        await run_smoke_demo(cfg, say=lambda *_: None)


async def test_it_will_not_run_while_the_daemon_holds_the_journal(monkeypatch, tmp_path):
    for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"):
        monkeypatch.setenv(k, "x")
    cfg = Config()
    cfg.run.mode, cfg.run.db_path = "demo", str(tmp_path / "aeon-demo.db")
    daemon = Journal(cfg.run.db_path)
    with pytest.raises(SystemExit, match="already using"):
        await run_smoke_demo(cfg, say=lambda *_: None)
    daemon.close()
