"""Agent-level behaviour that needs no network: the capital cap, the expiry watchdog, spot defaults."""
import time

import pytest

from aeon.agent import Agent
from aeon.brain import HeuristicBrain
from aeon.broker import Account, PaperBroker, Position
from aeon.config import Config
from aeon.feed import PriceFeed
from aeon.journal import Journal
from aeon.market import Market, Spec
from aeon.risk import Proposal, RiskEngine

SOL_SPOT = Spec("SOL-USDT", "SOL", 1.0, 0.000001, 0.01, 0.01, 1.0, kind="spot")


def make_agent(mode="paper", budget=200.0):
    cfg = Config()
    cfg.run.mode, cfg.run.budget_usdt = mode, budget
    journal = Journal(":memory:")
    market = Market(None, cfg.risk, cfg.universe)
    broker = PaperBroker(200.0, 0.0005, 0.0, 0.001)
    agent = Agent(cfg, market, PriceFeed(None), broker, RiskEngine(cfg.risk, journal), HeuristicBrain(), journal)
    return agent, broker, market


async def test_capital_cap_on_demo_ignores_the_rest_of_the_account():
    agent, broker, _ = make_agent("demo", budget=200.0)

    class BigAccount:                                       # a demo account: ~100k USDT sitting there
        async def account(self, prices):
            return Account(equity=100_000.0, available=99_000.0, balance=100_000.0, positions={})
    agent.broker = BigAccount()
    agent.stats = {"realized": 5.0}
    acct = await agent._account({})
    assert acct.equity == pytest.approx(205.0)              # budget + what AEON has made, nothing more
    assert acct.available == pytest.approx(205.0)           # and it can only spend that


async def test_capital_cap_counts_open_profit_and_margin():
    agent, _, _ = make_agent("demo", budget=200.0)
    held = Position("SOL-USDT-SWAP", "long", 1.0, 1.0, 100.0, 2.0, mark=110.0)        # +10 unrealised, margin 50

    class Acct:
        async def account(self, prices):
            return Account(equity=100_000.0, available=99_000.0, balance=100_000.0, positions={held.inst: held})
    agent.broker = Acct()
    agent.stats = {"realized": -3.0}
    acct = await agent._account({})
    assert acct.equity == pytest.approx(200 - 3 + 10) and acct.available == pytest.approx(207 - 50)


async def test_paper_mode_ignores_the_budget_setting():
    agent, _, _ = make_agent("paper", budget=50.0)
    acct = await agent._account({})
    assert acct.equity == pytest.approx(200.0)


async def test_expiring_future_is_closed_before_settlement():
    agent, broker, market = make_agent()
    soon = Spec("BTC-USD_UM-261030", "BTC", 0.01, 0.01, 0.01, 0.1, 20.0, kind="futures", expiry_ts=time.time() + 0.5 * 86400)
    later = Spec("ETH-USD_UM-261127", "ETH", 0.1, 0.01, 0.01, 0.01, 20.0, kind="futures", expiry_ts=time.time() + 30 * 86400)
    market.specs.update({soon.inst: soon, later.inst: later})
    await broker.open(soon, "long", 0.5, 3.0, sl=79000.0, tp=None, price=81000.0, reason="t")
    await broker.open(later, "long", 0.5, 3.0, sl=2300.0, tp=None, price=2400.0, reason="t")
    agent.feed.prices.update({soon.inst: 81000.0, later.inst: 2400.0})
    acct = await broker.account(agent.feed.prices)
    await agent._expiry_watch(acct)
    assert soon.inst not in broker.positions and later.inst in broker.positions         # only the one inside 1 day
    assert any("closing it before settlement" in e.text for e in agent.events)
    assert any(e.kind == "fill" and "closing before expiry" in e.text for e in agent.events)


async def test_spot_proposal_without_a_side_defaults_to_long_and_trades():
    agent, broker, market = make_agent()
    market.specs[SOL_SPOT.inst] = SOL_SPOT
    market.enabled = {"spot"}
    agent.universe = [SOL_SPOT.inst]
    agent.feed.set(SOL_SPOT.inst, 100.0)                   # a live price, with its timestamp
    agent.account = await agent._account({})
    await agent._handle(Proposal("open", "SOL-USDT", margin_pct=20, stop_loss=96.0, take_profit=108.0, reason="spot buy"), 1)
    assert "SOL-USDT" in broker.positions and broker.positions["SOL-USDT"].kind == "spot"
    assert any(e.kind == "action" and "SOL/spot" in e.text and "OPEN LONG" in e.text for e in agent.events)


async def test_unknown_instrument_gets_a_helpful_rejection():
    agent, _, _ = make_agent()
    await agent._handle(Proposal("open", "BTC-USDT-261030", side="long", stop_loss=1.0), 1)
    assert any("exact id from the MARKET table" in e.text for e in agent.events)


async def test_every_action_links_to_the_cycles_database_id_not_a_per_run_counter():
    from aeon.brain import Decision
    agent, _, _ = make_agent()
    agent.j.add_cycle("paper", "an older run", "", "", 0, 0, 0.0, 0.0)        # ids 1 and 2 are already taken
    agent.j.add_cycle("paper", "another", "", "", 0, 0, 0.0, 0.0)
    cid = agent._book_decision(Decision(view="now"), 1)                       # this run's cycle number 1
    assert cid == 3
    await agent._handle(Proposal("open", "NOPE-USDT", side="long", stop_loss=1.0), cid)    # rejected
    await agent._handle(Proposal("hold", reason="quiet"), cid)
    rows = agent.j._query("SELECT cycle_id, status FROM actions ORDER BY id")
    assert rows == [(3, "rejected"), (3, "done")]
