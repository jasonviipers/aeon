"""Regression tests for the code-review findings fixed before demo testing: non-numeric values, price
freshness per instrument (and the REST fallback for every market kind), unconfirmed orders at the agent
level, and halt alerts that can't be crowded out. Broker-level fixes live in test_okx_broker_kinds.py."""
import json
import time

import pytest

from aeon.agent import Agent
from aeon.brain import HeuristicBrain, _num, extract_json, parse_decision
from aeon.broker import Account, BrokerError, Fill, PaperBroker, PendingOrder, Position
from aeon.config import Config, NotifyCfg, RiskCfg
from aeon.feed import PriceFeed, inst_type_of
from aeon.journal import Journal
from aeon.market import Market, Spec
from aeon.notify import DiscordNotifier
from aeon.risk import Proposal, RiskEngine

SOL_PERP = Spec("SOL-USDT-SWAP", "SOL", 1.0, 0.01, 0.01, 0.01, 50.0)
SOL_SPOT = Spec("SOL-USDT", "SOL", 1.0, 0.000001, 0.01, 0.01, 1.0, kind="spot")
UNI = {"SOL-USDT-SWAP"}


def acct(equity=200.0, positions=None):
    return Account(equity=equity, available=equity, balance=equity, positions=positions or {})


# --------------------------------------------------------------------------- NaN / infinity (finding F7)
def test_nan_and_infinity_from_the_model_become_missing_values():
    obj = extract_json('{"actions": [{"type": "open", "inst": "SOL-USDT-SWAP", "side": "long", '
                       '"stop_loss": NaN, "take_profit": Infinity, "leverage": -Infinity, "margin_pct": 10}]}')
    a = parse_decision(obj).actions[0]
    assert a.stop_loss is None and a.take_profit is None and a.leverage is None and a.margin_pct == 10
    assert _num(float("nan")) is None and _num(float("inf")) is None and _num("1e400") is None and _num("2.5") == 2.5


def test_a_nan_stop_is_rejected_by_the_risk_engine():
    r = RiskEngine(RiskCfg())
    nan = float("nan")
    v = r.evaluate_open(Proposal("open", "SOL-USDT-SWAP", side="long", margin_pct=20, leverage=2, stop_loss=nan),
                        acct(), SOL_PERP, 100.0, UNI)
    assert not v.ok and "usable number" in v.why                        # it used to be accepted with max_loss=nan
    for bad in (dict(take_profit=float("inf")), dict(margin_pct=nan), dict(leverage=float("inf"))):
        p = Proposal("open", "SOL-USDT-SWAP", side="long", stop_loss=97.0, **{"margin_pct": 20, "leverage": 2, **bad})
        assert not r.evaluate_open(p, acct(), SOL_PERP, 100.0, UNI).ok, bad
    assert not r.evaluate_open(Proposal("open", "SOL-USDT-SWAP", side="long", margin_pct=-5, stop_loss=97.0),
                               acct(), SOL_PERP, 100.0, UNI).ok         # a negative size is not "no room"
    assert not r.evaluate_open(Proposal("open", "SOL-USDT-SWAP", side="long", stop_loss=97.0),
                               acct(), SOL_PERP, float("nan"), UNI).ok
    assert not r.evaluate_open(Proposal("open", "SOL-USDT-SWAP", side="long", stop_loss=97.0),
                               acct(equity=float("nan")), SOL_PERP, 100.0, UNI).ok


def test_adjust_and_reduce_reject_non_numbers_too():
    r = RiskEngine(RiskCfg())
    pos = Position("SOL-USDT-SWAP", "long", 1.0, 1.0, 100.0, 2.0, mark=104.0, sl=98.0)
    assert not r.evaluate_adjust(Proposal("adjust", pos.inst, stop_loss=float("nan")), pos, 104.0).ok
    assert not r.evaluate_adjust(Proposal("adjust", pos.inst, take_profit=float("inf")), pos, 104.0).ok
    assert not r.evaluate_reduce(Proposal("reduce", pos.inst, pct=float("nan")), pos, SOL_PERP).ok


async def test_the_paper_broker_refuses_a_stop_that_could_never_trigger():
    b = PaperBroker(200.0, 0.0005, 0.0)
    with pytest.raises(BrokerError, match="non-numeric"):
        await b.open(SOL_PERP, "long", 0.5, 2.0, float("nan"), None, 100.0, "t")
    await b.open(SOL_PERP, "long", 0.5, 2.0, 97.0, None, 100.0, "t")
    with pytest.raises(BrokerError, match="non-numeric"):
        await b.set_protection(SOL_PERP, "long", float("inf"), None)


# --------------------------------------------------------------------------- price freshness (finding F9)
def test_instrument_types_are_read_from_the_id():
    assert inst_type_of("SOL-USDT-SWAP") == "SWAP" and inst_type_of("SOL-USDT") == "SPOT"
    assert inst_type_of("BTC-USD_UM-261030") == "FUTURES"


class FakeTickers:
    def __init__(self, fail=False):
        self.asked, self.fail = [], fail

    async def tickers(self, inst_type):
        self.asked.append(inst_type)
        if self.fail:
            raise ConnectionError("down")
        rows = {"SWAP": [{"instId": "SOL-USDT-SWAP", "last": "101"}],
                "SPOT": [{"instId": "SOL-USDT", "last": "100.5"}],
                "FUTURES": [{"instId": "BTC-USD_UM-261030", "last": "81000"}]}
        return rows[inst_type]


async def test_rest_fallback_refreshes_every_market_kind():
    feed = PriceFeed(FakeTickers())
    feed._subs = {"SOL-USDT-SWAP", "SOL-USDT", "BTC-USD_UM-261030"}
    await feed._rest_poll()
    assert sorted(feed.okx.asked) == ["FUTURES", "SPOT", "SWAP"]           # it used to ask for SWAP only
    assert feed.prices == {"SOL-USDT-SWAP": 101.0, "SOL-USDT": 100.5, "BTC-USD_UM-261030": 81000.0}


def test_a_fresh_perpetual_does_not_make_a_stale_spot_price_look_fresh():
    feed = PriceFeed(None)
    feed.set("SOL-USDT", 100.0, ts=time.time() - 600)
    feed.set("SOL-USDT-SWAP", 101.0)
    assert feed.age() < 1                                                   # the connection is alive...
    assert feed.age("SOL-USDT-SWAP") < 1 and feed.age("SOL-USDT") > 599     # ...but each price has its own age
    assert feed.age("NEVER-SEEN") == float("inf")


def make_agent(feed_okx=None, broker=None, mode="paper"):
    cfg = Config()
    cfg.run.mode = mode
    journal = Journal(":memory:")
    market = Market(None, cfg.risk, cfg.universe)
    market.specs.update({SOL_PERP.inst: SOL_PERP, SOL_SPOT.inst: SOL_SPOT})
    broker = broker or PaperBroker(200.0, 0.0005, 0.0, 0.001)
    agent = Agent(cfg, market, PriceFeed(feed_okx), broker, RiskEngine(cfg.risk, journal), HeuristicBrain(), journal)
    agent.universe = [SOL_PERP.inst, SOL_SPOT.inst]
    return agent, broker, journal


async def test_a_stale_price_is_refreshed_from_okx_before_trading():
    agent, broker, _ = make_agent(FakeTickers())
    agent.feed.set(SOL_PERP.inst, 90.0, ts=time.time() - 400)               # quiet market: last tick 400s ago
    agent.account = await agent._account({})
    await agent._handle(Proposal("open", SOL_PERP.inst, side="long", margin_pct=10, leverage=2, stop_loss=98.0), 1)
    assert SOL_PERP.inst in broker.positions
    assert broker.positions[SOL_PERP.inst].entry == pytest.approx(101.0)    # traded at the refreshed price, not 90


async def test_no_new_risk_on_a_price_that_cannot_be_refreshed_but_exits_still_work():
    agent, broker, _ = make_agent(FakeTickers(fail=True))
    await broker.open(SOL_SPOT, "long", 0.5, 1.0, 95.0, None, 100.0, "t")
    agent.feed.set(SOL_PERP.inst, 100.0, ts=time.time() - 400)
    agent.feed.set(SOL_SPOT.inst, 100.0, ts=time.time() - 400)
    await agent._handle(Proposal("open", SOL_PERP.inst, side="long", margin_pct=10, leverage=2, stop_loss=98.0), 1)
    assert SOL_PERP.inst not in broker.positions
    assert any("stale price" in e.text for e in agent.events)
    await agent._handle(Proposal("close", SOL_SPOT.inst, reason="get out"), 2)  # reducing risk is never blocked
    assert SOL_SPOT.inst not in broker.positions


# --------------------------------------------------------------------------- unconfirmed orders at the agent level
class PendingBroker(PaperBroker):
    """A broker whose order went out but whose fill could not be confirmed."""

    async def open(self, spec, side, contracts, lever, sl, tp, price, reason):
        raise PendingOrder(f"{spec.inst}: order sent but its fill is not confirmed yet")


async def test_an_unconfirmed_order_is_reported_as_pending_not_as_a_failure():
    agent, _, journal = make_agent(broker=PendingBroker(200.0, 0.0005, 0.0))
    agent.feed.set(SOL_PERP.inst, 100.0)
    await agent._handle(Proposal("open", SOL_PERP.inst, side="long", margin_pct=10, leverage=2, stop_loss=98.0,
                                 reason="breakout"), 1)
    assert [a["status"] for a in journal.recent_actions()] == ["pending"]
    assert not any(e.kind == "error" for e in agent.events)
    assert any(e.kind == "risk" and "not confirmed" in e.text for e in agent.events)
    assert agent.thesis[SOL_PERP.inst] == "breakout"                        # kept for when the fill is booked


async def test_an_entry_booked_later_by_reconciliation_gets_its_thesis():
    agent, _, journal = make_agent()
    agent._record_fill(Fill(time.time(), SOL_SPOT.inst, "long", "open", 100.0, 1.0, 0.1, -0.1, None, "dip buy"))
    assert agent.thesis[SOL_SPOT.inst] == "dip buy" and journal.kv_get("thesis")[SOL_SPOT.inst] == "dip buy"


async def test_the_agent_saves_broker_state_the_moment_the_broker_changes():
    class Recording(PaperBroker):
        on_change = None

        async def init(self):
            return None
    broker = Recording(200.0, 0.0005, 0.0)
    agent, _, journal = make_agent(broker=broker, mode="demo")
    agent.broker.on_change = None
    if hasattr(agent.broker, "on_change"):                                  # what Agent.start() does
        agent.broker.on_change = agent._save_broker_state
    broker.balance = 123.0
    broker.on_change()
    assert journal.kv_get("okx_state")["balance"] == 123.0


# --------------------------------------------------------------------------- halt alerts (finding F15)
URL = "https://discord.com/api/webhooks/123/abc"


def notifier(**kw):
    kw.setdefault("enabled", True)
    return DiscordNotifier(NotifyCfg(**kw), env={"DISCORD_WEBHOOK_URL": URL})


def test_a_halt_gets_through_even_when_the_burst_cap_is_used_up():
    n = notifier(max_per_minute=2, cooldown_s=0.0)
    for i in range(5):
        n.post("error", f"order failed {i}")                                # routine noise uses up the cap
    assert len(n._q) == 2 and n.dropped == 3
    n.post("risk", "KILL SWITCH: drawdown 25.0% hit the 25% limit")
    assert len(n._q) == 3 and n._q[0][1].startswith("KILL SWITCH")          # admitted, and first in line


def test_critical_alerts_keep_their_order_ahead_of_routine_ones():
    n = notifier(cooldown_s=0.0)
    n.post("fill", "OPEN LONG SOL")
    n.post("risk", "KILL SWITCH: manual")
    n.post("risk", "SOL-USDT: UNPROTECTED, could not re-arm stop")
    n.post("fill", "CLOSE SOL")
    assert [t for _, t, _, _ in n._q] == ["KILL SWITCH: manual", "SOL-USDT: UNPROTECTED, could not re-arm stop",
                                          "OPEN LONG SOL", "CLOSE SOL"]


def test_a_full_queue_makes_room_for_a_halt():
    n = notifier(cooldown_s=0.0, max_per_minute=10_000)
    n.max_queue = 5
    for i in range(5):
        n.post("fill", f"fill {i}")
    n.post("fill", "one too many")                                          # routine overflow is dropped
    assert len(n._q) == 5 and n._q[-1][1] == "fill 4"
    n.post("risk", "HALTED: drawdown limit")
    assert len(n._q) == 5 and n._q[0][1] == "HALTED: drawdown limit"         # the newest routine one made room


async def test_the_queue_counter_stays_right_after_delivery():
    import httpx
    n = notifier(cooldown_s=0.0)
    n.throttle = 0.0
    n._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    n.post("risk", "KILL SWITCH: one")
    n.post("fill", "routine")
    await n._process()
    assert n._front == 0 and n.sent == 2
    n.post("fill", "later routine")
    n.post("risk", "KILL SWITCH: two")
    assert n._q[0][1] == "KILL SWITCH: two"


def test_dedupe_memory_does_not_grow_when_dedupe_is_off():
    n = notifier(cooldown_s=0.0, max_per_minute=100_000)
    for i in range(1000):
        n.post("fill", f"fill {i}")
    assert n._seen == {}
