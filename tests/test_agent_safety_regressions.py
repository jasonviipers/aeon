import asyncio
import json
import sqlite3
import time
from dataclasses import asdict

import pytest

from aeon.broker import Account, Fill
from aeon.health import check_heartbeat, write_heartbeat
from aeon.journal import Journal
from aeon.market import InstSnap
from aeon.risk import Proposal
from test_agent import SOL_SPOT, make_agent


def execution(identifier="execution-1"):
    return Fill(time.time(), SOL_SPOT.inst, "long", "open", 100.0, 1.0,
                0.1, -0.1, reason="test", execution_id=identifier)


def test_journal_atomically_records_outbox_and_deduplicates_replay():
    journal = Journal(":memory:")
    fill = execution()
    state = {"outbox": {fill.execution_id: asdict(fill)}, "spot": {"qty": 0.999}}
    journal.save_broker_state("okx_state", state)
    journal.save_broker_state("okx_state", state, [fill])
    assert journal.kv_get("okx_state") == state
    assert journal.stats()["realized"] == pytest.approx(-0.1)
    assert journal.risk_activity()[0] == 1
    assert not journal.add_trade(fill)
    journal.close()


def test_journal_rolls_back_fill_when_state_write_fails():
    journal = Journal(":memory:")
    journal.db.execute("CREATE TRIGGER reject_state BEFORE INSERT ON kv "
                       "BEGIN SELECT RAISE(ABORT, 'disk write failed'); END")
    fill = execution()
    with pytest.raises(sqlite3.IntegrityError, match="disk write failed"):
        journal.save_broker_state("okx_state", {"outbox": {fill.execution_id: asdict(fill)}})
    assert journal.stats()["realized"] == 0
    assert journal.kv_get("okx_state") is None
    journal.close()


def test_existing_journal_migrates_without_losing_trades(tmp_path):
    path = tmp_path / "legacy.db"
    database = sqlite3.connect(path)
    database.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, inst TEXT, "
                     "side TEXT, event TEXT, price REAL, contracts REAL, fee REAL, pnl REAL, "
                     "trade_pnl REAL, reason TEXT, est INTEGER)")
    database.execute("INSERT INTO trades(ts,event,pnl,fee) VALUES(?,?,?,?)", (time.time(), "open", -1, 1))
    database.commit()
    database.close()
    journal = Journal(str(path))
    assert journal.stats()["realized"] == -1
    assert journal.add_trade(execution())
    assert not journal.add_trade(execution())
    journal.close()


async def test_current_equity_updates_loss_gates_before_entry():
    agent, broker, market = make_agent()
    market.specs[SOL_SPOT.inst] = SOL_SPOT
    agent.universe = [SOL_SPOT.inst]
    agent.feed.set(SOL_SPOT.inst, 100.0)
    agent.risk.update_equity(200.0)
    broker.balance = 130.0
    await agent._handle(Proposal("open", SOL_SPOT.inst, side="long", margin_pct=10,
                                 stop_loss=96.0), 1)
    assert agent.risk.killed and agent.risk.day_blocked
    assert not broker.positions
    agent.j.close()


async def test_monitor_failures_remain_live_but_stop_safety_health():
    agent, _, _ = make_agent()
    agent.last_successful_monitor = time.time() - 1000
    agent.cfg.run.monitor_seconds = 0.001
    beats = []
    agent.on_beat = beats.append

    async def failed():
        raise RuntimeError("account unavailable")

    agent._monitor_once = failed
    task = asyncio.create_task(agent._monitor_loop())
    await asyncio.sleep(0.03)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert beats
    assert agent.stalled_for() >= 1000
    assert "account unavailable" in agent.monitor_error
    agent.j.close()


def test_healthcheck_uses_success_timestamp_and_reports_failure(tmp_path):
    path = tmp_path / "heartbeat"
    write_heartbeat(path, ts=1000, last_success=100, error="")
    assert not check_heartbeat(path, now=1001)[0]
    write_heartbeat(path, ts=1000, last_success=1000, error="stop checks unavailable")
    healthy, detail = check_heartbeat(path, now=1001)
    assert not healthy and "stop checks unavailable" in detail


async def test_budget_tracks_unjournaled_account_losses_and_survives_restart():
    agent, _, _ = make_agent("demo", budget=200.0)

    class ExchangeAccount:
        equity = 100_000.0

        async def account(self, prices):
            return Account(self.equity, self.equity, self.equity)

    exchange = ExchangeAccount()
    agent.broker = exchange
    assert (await agent._account({})).equity == 200.0
    exchange.equity -= 30.0
    assert (await agent._account({})).equity == 170.0
    saved = agent.j.kv_get("capital_reserve")
    agent.j.kv_set("capital_reserve", json.loads(json.dumps(saved)))
    assert (await agent._account({})).equity == 170.0
    agent.j.close()


async def test_budget_cannot_exceed_actual_account_equity():
    agent, _, _ = make_agent("demo", budget=200.0)

    class ExchangeAccount:
        async def account(self, prices):
            return Account(130.0, 130.0, 130.0)

    agent.broker = ExchangeAccount()
    account = await agent._account({})
    assert account.equity == 130.0
    assert account.available == 130.0
    agent.j.close()


def test_replayed_fill_counts_once_and_updates_stats_immediately():
    agent, _, _ = make_agent()
    fill = execution()
    agent._record_fill(fill)
    agent._record_fill(fill)
    assert agent.stats["realized"] == pytest.approx(-0.1)
    assert agent.risk.trades_today == 1
    assert len([event for event in agent.events if event.kind == "fill"]) == 1
    agent.j.close()
