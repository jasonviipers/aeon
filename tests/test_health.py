"""Liveness: heartbeat file, Docker healthcheck, watchdog, dead man's switch, and the clean SIGTERM-style shutdown."""
import asyncio
import json
import sys
import threading
import time

import httpx
import pytest

import aeon.__main__ as entry
from aeon import health
from aeon.agent import Agent
from aeon.brain import HeuristicBrain
from aeon.broker import PaperBroker
from aeon.config import Config
from aeon.feed import PriceFeed
from aeon.journal import Journal
from aeon.market import Market
from aeon.risk import RiskEngine


def test_heartbeat_roundtrip_and_staleness(tmp_path):
    path = tmp_path / "hb"
    health.write_heartbeat(path, ts=1000.0, status="RUNNING")
    assert json.loads(path.read_text())["status"] == "RUNNING"
    assert health.check_heartbeat(path, max_age=60, now=1030.0)[0] is True
    ok, msg = health.check_heartbeat(path, max_age=60, now=1100.0)
    assert ok is False and "silent for 100s" in msg


def test_a_missing_or_garbled_heartbeat_is_unhealthy(tmp_path):
    assert health.check_heartbeat(tmp_path / "nope")[0] is False
    bad = tmp_path / "bad"
    bad.write_text("{not json")
    assert health.check_heartbeat(bad)[0] is False


def test_heartbeat_path_honours_the_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AEON_HEARTBEAT_FILE", str(tmp_path / "x"))
    assert health.heartbeat_path("demo") == tmp_path / "x"
    monkeypatch.delenv("AEON_HEARTBEAT_FILE")
    assert health.heartbeat_path("demo").name == "aeon-demo.heartbeat"


@pytest.mark.parametrize("raw, want", [("", health.WATCHDOG_S), ("120", 120.0), ("0", 0.0),
                                       ("junk", health.WATCHDOG_S), ("-5", 0.0)])
def test_watchdog_limit_comes_from_the_environment(monkeypatch, raw, want):
    monkeypatch.setenv("AEON_WATCHDOG_S", raw)
    assert health.watchdog_seconds() == want


def test_watchdog_trips_once_when_the_silence_exceeds_the_limit():
    tripped, evt = [], threading.Event()
    wd = health.Watchdog(lambda: 99.0, limit=10.0, on_trip=lambda age: (tripped.append(age), evt.set()), poll=0.01)
    wd.start()
    assert evt.wait(2.0) and tripped == [99.0]
    wd.join(2.0)
    assert not wd.is_alive()                                   # it ends after tripping


def test_watchdog_stays_quiet_while_the_monitor_keeps_checking_in():
    tripped = []
    wd = health.Watchdog(lambda: 1.0, limit=10.0, on_trip=tripped.append, poll=0.01)
    wd.start()
    time.sleep(0.15)
    wd.stop()
    wd.join(2.0)
    assert tripped == []


def test_the_default_trip_ends_the_process_with_a_distinct_exit_code(monkeypatch, capsys):
    codes = []
    monkeypatch.setattr(health.os, "_exit", codes.append)
    health._die(700.0)
    assert codes == [health.EXIT_CODE] and "watchdog" in capsys.readouterr().err


def test_ping_url_must_be_https(monkeypatch):
    monkeypatch.setenv("HEARTBEAT_URL", "http://hc.example/ping")
    assert health.ping_url() == ""
    monkeypatch.setenv("HEARTBEAT_URL", " https://hc.example/ping ")
    assert health.ping_url() == "https://hc.example/ping"
    monkeypatch.delenv("HEARTBEAT_URL")
    assert health.ping_url() == ""


async def test_pings_only_while_healthy_and_survives_a_dead_endpoint():
    hits, healthy = [], {"v": True}

    def handler(req):
        hits.append(str(req.url))
        raise httpx.ConnectError("down")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task = asyncio.create_task(health.ping_loop("https://hc.example/p", lambda: healthy["v"], every=0.02, client=client))
    await asyncio.sleep(0.12)
    assert len(hits) >= 2 and not task.done()                  # kept going despite ConnectError
    healthy["v"] = False
    await asyncio.sleep(0.1)
    after = len(hits)
    await asyncio.sleep(0.1)
    assert len(hits) == after                                  # silence is the signal
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await client.aclose()


# --------------------------------------------------------------------------- the agent side
def make_agent():
    cfg = Config()
    cfg.run.monitor_seconds = 1.0
    journal = Journal(":memory:")
    return Agent(cfg, Market(None, cfg.risk, cfg.universe), PriceFeed(None), PaperBroker(200.0, 0.0005, 0.0, 0.001),
                 RiskEngine(cfg.risk, journal), HeuristicBrain(), journal)


def test_stalled_for_counts_silence_and_is_infinite_when_a_task_died():
    agent = make_agent()
    agent.last_beat = time.time() - 30
    assert 29 < agent.stalled_for() < 32

    class Dead:
        def done(self):
            return True
    agent._tasks = [Dead()]
    assert agent.stalled_for() == float("inf")


async def test_the_monitor_loop_beats_even_when_a_pass_fails():
    agent = make_agent()
    beats = []
    agent.on_beat = beats.append

    async def boom():
        raise RuntimeError("exchange on fire")
    agent._monitor_once = boom
    agent.cfg.run.monitor_seconds = 0.01
    task = asyncio.create_task(agent._monitor_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(beats) >= 2                                     # a failing pass is still a live loop
    assert any("exchange on fire" in e.text for e in agent.events)


def test_a_broken_heartbeat_callback_never_takes_the_monitor_down():
    agent = make_agent()

    def broken(ts):
        raise OSError("disk full")
    agent.on_beat = broken
    agent._beat()                                              # must not raise


# --------------------------------------------------------------------------- the entry point
def test_healthcheck_exit_codes(monkeypatch, tmp_path, capsys):
    hb = tmp_path / "hb"
    monkeypatch.setenv("AEON_HEARTBEAT_FILE", str(hb))
    monkeypatch.setattr(sys, "argv", ["aeon", "--healthcheck", "--config", str(tmp_path / "none.toml")])
    with pytest.raises(SystemExit) as e:
        entry.main()
    assert e.value.code == 1                                   # no heartbeat yet
    health.write_heartbeat(hb)
    with pytest.raises(SystemExit) as e:
        entry.main()
    assert e.value.code == 0 and "alive" in capsys.readouterr().out


def test_clock_skew_assumes_the_reply_took_half_the_round_trip():
    assert entry.clock_skew(server_ts=100.0, sent=99.0, received=101.0) == pytest.approx(0.0)
    assert entry.clock_skew(server_ts=100.0, sent=109.0, received=111.0) == pytest.approx(10.0)


class _StubSetup:
    def __init__(self, agent):
        self.agent, self.cfg, self.journal = agent, agent.cfg, agent.j
        self.closed = False

    async def aclose(self):
        self.closed = True


async def test_a_signal_style_cancel_shuts_down_cleanly_instead_of_raising(monkeypatch, tmp_path, capsys):
    """SIGTERM/SIGINT cancel the main task; run_headless must clean up and return, not leak CancelledError."""
    monkeypatch.setenv("AEON_HEARTBEAT_FILE", str(tmp_path / "hb"))
    monkeypatch.setenv("AEON_WATCHDOG_S", "0")
    agent = make_agent()

    async def fake_start(decisions=True):
        agent._tasks = []
    agent.start = fake_start
    setup = _StubSetup(agent)
    task = asyncio.create_task(entry.run_headless(setup, once=False))
    await asyncio.sleep(0.1)
    assert (tmp_path / "hb").exists()                          # first heartbeat is written at start
    task.cancel()
    await task                                                 # returns normally
    assert setup.closed and "stopping" in capsys.readouterr().err


async def test_a_failed_start_exits_nonzero_with_one_clear_line(capsys):
    agent = make_agent()

    async def broken(decisions=True):
        raise ConnectionError("no route to host")
    agent.start = broken
    setup = _StubSetup(agent)
    with pytest.raises(SystemExit) as e:
        await entry.run_headless(setup, once=False)
    assert e.value.code == 1 and setup.closed
    assert "startup failed: ConnectionError: no route to host" in capsys.readouterr().err
