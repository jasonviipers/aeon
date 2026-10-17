"""LLM usage tracking: the journal's rolling window, the agent's bookkeeping, and the LLM METER panel."""
import time

import pytest

from aeon.agent import Agent
from aeon.brain import Decision, HeuristicBrain
from aeon.broker import PaperBroker
from aeon.config import Config
from aeon.feed import PriceFeed
from aeon.journal import Journal
from aeon.market import Market
from aeon.risk import RiskEngine
from aeon.tui import calls, render_llm_meter, usd

DAY = 86400


def make_agent(journal=None):
    cfg = Config()
    journal = journal or Journal(":memory:")
    agent = Agent(cfg, Market(None, cfg.risk, cfg.universe), PriceFeed(None), PaperBroker(200.0, 0.0005, 0.0),
                  RiskEngine(cfg.risk, journal), HeuristicBrain(), journal)
    return agent, journal


def test_journal_usage_is_summed_over_a_rolling_window():
    j = Journal(":memory:")
    j.add_cycle("paper", "v", "n", "{}", 1000, 100, 0.001, 1.0)
    j.add_cycle("paper", "v", "n", "{}", 2000, 200, 0.002, 1.0)
    j._exec("INSERT INTO cycles(ts,mode,view,notes,raw,tokens_in,tokens_out,cost,latency) VALUES(?,?,?,?,?,?,?,?,?)",
            (time.time() - 2 * DAY, "paper", "old", "", "{}", 9000, 900, 0.009, 1.0))
    day = j.llm_usage(time.time() - DAY)
    assert day == dict(calls=2, tin=3000, tout=300, cost=pytest.approx(0.003))
    total = j.llm_usage()
    assert total["calls"] == 3 and total["tin"] == 12000 and total["cost"] == pytest.approx(0.012)
    assert Journal(":memory:").llm_usage() == dict(calls=0, tin=0, tout=0, cost=0.0)        # an empty journal is all zeros


def test_agent_books_every_call_and_remembers_across_restarts():
    agent, journal = make_agent()
    agent._book_decision(Decision(view="v", tokens_in=5178, tokens_out=404, cost=0.0020, latency=3.2), 1)
    assert agent.meter["tin"] == 5178 and agent.meter["tout"] == 404 and agent.meter["calls"] == 1
    assert agent.last_llm == dict(tin=5178, tout=404, cost=0.0020, latency=3.2)
    note = agent._usage_note()
    assert "5,178 in / 404 out" in note and "$0.0020" in note and "24h: $0.0020" in note

    agent._book_decision(Decision(view="v", tokens_in=1000, tokens_out=100, cost=0.0005), 2)
    assert agent.meter["tin"] == 6178 and agent.meter["cost"] == pytest.approx(0.0025)
    assert agent.llm24["calls"] == 2 and agent.llm24["cost"] == pytest.approx(0.0025)

    restarted, _ = make_agent(journal)                                   # same journal = the same bot after a restart
    assert restarted.meter["tin"] == 6178 and restarted.meter["calls"] == 2


def test_the_rule_bot_costs_nothing_and_says_nothing():
    agent, _ = make_agent()
    agent._book_decision(Decision(view="rules"), 1)                       # no tokens
    assert agent._usage_note() == "" and agent.meter["calls"] == 1 and agent.llm24["cost"] == 0.0


def test_panel_shows_tokens_cost_and_the_drag_on_equity():
    meter = dict(tin=1_234_567, tout=98_765, cost=0.4123, calls=240)
    day = dict(calls=96, tin=500_000, tout=40_000, cost=0.5)
    last = dict(tin=5178, tout=404, cost=0.002, latency=3.2)
    text = render_llm_meter(meter, day, last, priced=True, equity=200.0, rule_bot=False).plain
    for needle in ("TOKENS IN", "1,234,567", "TOKENS OUT", "98,765", "LLM COST", "$0.4123", "LAST 24H",
                   "$0.5000 · 96 calls", "DRAG", "0.250% of equity/day", "LAST CALL", "5,178+404 · $0.0020"):
        assert needle in text, needle
    assert all(len(line) <= 34 for line in text.splitlines())            # fits the panel without wrapping


def test_panel_flags_a_model_with_no_prices_configured():
    meter = dict(tin=5000, tout=400, cost=0.0, calls=1)
    text = render_llm_meter(meter, dict(calls=1, tin=5000, tout=400, cost=0.0), None, priced=False, equity=200.0,
                            rule_bot=False).plain
    assert "no prices set" in text and "set prices in config.toml" in text and "LAST CALL" in text


def test_panel_for_the_rule_bot_and_for_a_fresh_start():
    zero = dict(tin=0, tout=0, cost=0.0, calls=0)
    text = render_llm_meter(zero, dict(calls=0, tin=0, tout=0, cost=0.0), None, priced=True, equity=200.0, rule_bot=True).plain
    assert "rule bot: no LLM tokens used" in text and "$0.0000" in text
    assert render_llm_meter(zero, dict(calls=0, tin=0, tout=0, cost=0.0), None, priced=True, equity=0.0,
                            rule_bot=False).plain                        # zero equity must not divide by zero


def test_cost_formatting():
    assert usd(0.00204) == "$0.0020" and usd(0.4123) == "$0.4123" and usd(12.345) == "$12.35" and usd(1234.5) == "$1,234.50"
    assert calls(0) == "0 calls" and calls(1) == "1 call" and calls(96) == "96 calls"
