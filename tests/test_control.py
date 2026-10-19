"""The local control channel: authenticated, loopback-only, bounded, confirmed, and no stronger than the TUI keys."""
import asyncio
import contextlib
import json
import os
import stat
import sys
import time
from types import SimpleNamespace

import pytest

import aeon.__main__ as entry
from aeon import control, health
from aeon.agent import Agent
from aeon.brain import HeuristicBrain
from aeon.broker import BrokerError, PaperBroker
from aeon.config import Config
from aeon.control import ControlServer
from aeon.feed import PriceFeed
from aeon.journal import Journal
from aeon.market import Market, Spec
from aeon.risk import RiskEngine

SOL = Spec("SOL-USDT-SWAP", "SOL", ct_val=1.0, lot_sz=0.01, min_sz=0.01, tick_sz=0.01, max_lever=50)
ETH = Spec("ETH-USDT-SWAP", "ETH", ct_val=1.0, lot_sz=0.01, min_sz=0.01, tick_sz=0.01, max_lever=50)
NOT_WINDOWS = pytest.mark.skipif(os.name == "nt", reason="file mode bits only mean something on POSIX")


# --------------------------------------------------------------------------- helpers
def make_agent():
    cfg = Config()
    journal = Journal(":memory:")
    market = Market(None, cfg.risk, cfg.universe)
    broker = PaperBroker(200.0, 0.0005, 0.0, 0.001)
    agent = Agent(cfg, market, PriceFeed(None), broker, RiskEngine(cfg.risk, journal), HeuristicBrain(), journal)
    market.specs.update({SOL.inst: SOL, ETH.inst: ETH})
    agent.feed.prices.update({SOL.inst: 100.0, ETH.inst: 50.0})
    return agent


async def hold(agent, *specs):
    """Open a long in each spec on the paper broker and load the account into the agent, as the monitor would."""
    for spec in specs:
        px = agent.feed.prices[spec.inst]
        await agent.broker.open(spec, "long", 1.0, 2.0, sl=px * 0.9, tp=px * 1.2, price=px, reason="test")
    agent.account = await agent._account(dict(agent.feed.prices))


@contextlib.asynccontextmanager
async def serving(agent, tmp_path):
    server = ControlServer(agent, tmp_path / "aeon.control")
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _strict(name):
    raise ValueError(f"non-finite number {name} on the wire")


async def raw(server, payload: bytes) -> dict:
    """One connection, one line out, one line back: no client library between the test and the server."""
    reader, writer = await asyncio.open_connection(control.HOST, server.port)
    try:
        writer.write(payload)
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), 5.0)
    finally:
        writer.close()
    return json.loads(line, parse_constant=_strict)


async def ask(server, cmd, confirm=None, token=None, **extra) -> dict:
    body = {"token": server.token if token is None else token, "cmd": cmd, **extra}
    if confirm is not None:
        body["confirm"] = confirm
    return await raw(server, json.dumps(body).encode() + b"\n")


# --------------------------------------------------------------------------- where the token lives
async def test_server_binds_loopback_only_and_publishes_port_and_token_in_a_file(tmp_path):
    agent = make_agent()
    async with serving(agent, tmp_path) as server:
        host, port = server._server.sockets[0].getsockname()[:2]
        assert host == "127.0.0.1" and port == server.port > 0
        assert len(server._server.sockets) == 1                          # no second (IPv6 / wildcard) listener
        data = json.loads((tmp_path / "aeon.control").read_text())
        assert data == {"port": server.port, "token": server.token}
        assert len(server.token) >= 40                                   # 256 random bits, url-safe
    assert not (tmp_path / "aeon.control").exists()                      # removed on a clean stop


@NOT_WINDOWS
async def test_token_file_is_owner_only(tmp_path):
    async with serving(make_agent(), tmp_path):
        assert stat.S_IMODE((tmp_path / "aeon.control").stat().st_mode) == 0o600


@NOT_WINDOWS
def test_write_private_never_exposes_the_file_even_under_a_lax_umask(tmp_path):
    old = os.umask(0)
    try:
        control.write_private(tmp_path / "x", "secret")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "x").stat().st_mode) == 0o600


def test_every_start_gets_a_fresh_token(tmp_path):
    a, b = ControlServer(make_agent(), tmp_path / "a"), ControlServer(make_agent(), tmp_path / "b")
    assert a.token != b.token


def test_control_file_sits_next_to_the_heartbeat_unless_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("AEON_HEARTBEAT_FILE", str(tmp_path / "hb"))
    monkeypatch.delenv("AEON_CONTROL_FILE", raising=False)
    assert control.control_path("demo") == tmp_path / "aeon-demo.control"
    assert control.control_path("demo").parent == health.heartbeat_path("demo").parent
    monkeypatch.setenv("AEON_CONTROL_FILE", str(tmp_path / "elsewhere"))
    assert control.control_path("demo") == tmp_path / "elsewhere"


@pytest.mark.parametrize("raw_value, want", [("", True), ("1", True), ("on", True), ("0", False), ("off", False),
                                             (" False ", False), ("NO", False)])
def test_the_channel_can_be_switched_off(monkeypatch, raw_value, want):
    monkeypatch.setenv("AEON_CONTROL", raw_value)
    assert control.control_enabled() is want


# --------------------------------------------------------------------------- the client's trust in that file
def _st(mode, uid=1000):
    return SimpleNamespace(st_mode=mode, st_uid=uid)


def test_private_problem_flags_files_the_client_should_not_trust():
    assert control.private_problem(_st(stat.S_IFREG | 0o600), 1000) is None
    assert "readable by other users" in control.private_problem(_st(stat.S_IFREG | 0o644), 1000)
    assert "readable by other users" in control.private_problem(_st(stat.S_IFREG | 0o660), 1000)
    assert "owned by uid 1000" in control.private_problem(_st(stat.S_IFREG | 0o600, uid=1000), 1001)
    assert "not a regular file" in control.private_problem(_st(stat.S_IFLNK | 0o777), 1000)
    assert "not a regular file" in control.private_problem(_st(stat.S_IFDIR | 0o700), 1000)
    assert control.private_problem(_st(stat.S_IFREG | 0o666), None) is None          # Windows: no mode bits to judge


def test_root_may_use_the_agents_file_but_not_a_lax_one():
    assert control.private_problem(_st(stat.S_IFREG | 0o600, uid=1000), 0) is None   # docker compose exec -u root
    assert control.private_problem(_st(stat.S_IFREG | 0o644, uid=1000), 0) is not None


def test_read_endpoint_explains_each_way_the_file_can_be_unusable(tmp_path):
    with pytest.raises(ValueError, match="no control file"):
        control.read_endpoint(tmp_path / "missing")
    for bad in ("{not json", "[]", '{"port": 0, "token": "t"}', '{"port": 70000, "token": "t"}',
                '{"port": "x", "token": "t"}', '{"port": 5}', '{"port": 5, "token": ""}', '{"port": 5, "token": 7}'):
        (tmp_path / "f").write_text(bad)
        with pytest.raises(ValueError, match="malformed"):
            control.read_endpoint(tmp_path / "f")
    (tmp_path / "f").write_text('{"port": 5, "token": "t"}')
    assert control.read_endpoint(tmp_path / "f") == (5, "t")


def test_a_missing_control_file_points_at_the_mode_that_is_running(tmp_path):
    (tmp_path / "aeon-paper.control").write_text("{}")
    with pytest.raises(ValueError, match="add --mode paper"):
        control.read_endpoint(tmp_path / "aeon-demo.control")        # config.toml says demo, the daemon runs paper
    (tmp_path / "aeon-live.control").write_text("{}")
    with pytest.raises(ValueError) as e:                             # two candidates: do not guess which one is meant
        control.read_endpoint(tmp_path / "aeon-demo.control")
    assert "no control file" in str(e.value) and "--mode" not in str(e.value)
    with pytest.raises(ValueError) as e:                             # an explicit AEON_CONTROL_FILE name: no globbing
        control.read_endpoint(tmp_path / "custom-name")
    assert "--mode" not in str(e.value)


async def test_a_stale_control_file_gives_a_clear_error_not_a_hang(tmp_path):
    async with serving(make_agent(), tmp_path) as server:
        path, port = server.path, server.port
        saved = path.read_text()
    path.write_text(saved)                                               # the agent is gone, the file is left behind
    with pytest.raises(ValueError, match=f"cannot connect to AEON on 127.0.0.1:{port}"):
        await control.request(path, "status")


# --------------------------------------------------------------------------- authentication and request hygiene
async def test_a_wrong_missing_or_malformed_token_is_refused_and_changes_nothing(tmp_path, capsys):
    agent = make_agent()
    async with serving(agent, tmp_path) as server:
        for token in ("nope", "", server.token[:-1], server.token + "x", server.token.upper(), 12345, None, [server.token]):
            r = await raw(server, json.dumps({"token": token, "cmd": "pause"}).encode() + b"\n")
            assert r == {"ok": False, "error": "unauthorized"}
        r = await raw(server, json.dumps({"cmd": "pause"}).encode() + b"\n")                  # no token at all
        assert r == {"ok": False, "error": "unauthorized"}
        # an unauthorised caller learns nothing about the commands either
        r = await raw(server, json.dumps({"token": "nope", "cmd": "no-such-command"}).encode() + b"\n")
        assert r == {"ok": False, "error": "unauthorized"}
    assert agent.paused is False and agent.status == "STARTING"
    assert "refused a request" in capsys.readouterr().err


async def test_the_token_is_never_logged_printed_or_echoed(tmp_path, capsys):
    agent = make_agent()
    await hold(agent, SOL)
    agent.on_event = entry.print_event                                   # headless: events go to stdout
    async with serving(agent, tmp_path) as server:
        seen = [await ask(server, "status"), await ask(server, "pause"), await ask(server, "unpause"),
                await ask(server, "cycle"), await ask(server, "flatten"), await ask(server, "flatten", confirm=True),
                await ask(server, "kill", confirm=True), await ask(server, "resume", confirm=True),
                await ask(server, "bogus"), await ask(server, "status", token="wrong-" + server.token),
                await raw(server, b"not json\n"), await raw(server, b"[1,2]\n")]
        out = capsys.readouterr()
        everything = out.out + out.err + json.dumps(seen) + "\n".join(e.text for e in agent.events)
        assert server.token not in everything
        assert "wrong-" + server.token not in everything


@pytest.mark.parametrize("payload, error", [
    (b"this is not json\n", "not JSON"),
    (b"[1, 2, 3]\n", "expected a JSON object"),
    (b'"status"\n', "expected a JSON object"),
    (b"POST / HTTP/1.1\r\n", "not JSON"),                                # a browser or scanner poking at the port
])
async def test_garbage_requests_are_rejected(tmp_path, payload, error):
    async with serving(make_agent(), tmp_path) as server:
        r = await raw(server, payload)
        assert r["ok"] is False and error in r["error"]
        assert (await ask(server, "status"))["ok"] is True               # and the server carries on


async def test_unknown_and_non_string_commands_are_rejected(tmp_path):
    agent = make_agent()
    async with serving(agent, tmp_path) as server:
        for cmd in ("restart", "", "STATUS", "status ", "toggle_pause", "__class__", ["status"], {"a": 1}, None, 7):
            r = await raw(server, json.dumps({"token": server.token, "cmd": cmd}).encode() + b"\n")
            assert r["ok"] is False and "unknown command" in r["error"], cmd
    assert agent.paused is False


async def test_oversized_requests_are_refused_before_they_are_parsed(tmp_path):
    async with serving(make_agent(), tmp_path) as server:
        big = json.dumps({"token": server.token, "cmd": "status", "pad": "x" * 5000}).encode() + b"\n"
        assert len(big) > control.MAX_REQUEST_BYTES
        r = await raw(server, big)
        assert r["ok"] is False and "at most 4096 bytes" in r["error"]
        r = await raw(server, b"x" * (control.MAX_REQUEST_BYTES + 100))   # and no newline at all
        assert r["ok"] is False and "at most 4096 bytes" in r["error"]
        assert (await ask(server, "status"))["ok"] is True


async def test_a_client_that_never_finishes_its_request_is_cut_off(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "READ_TIMEOUT_S", 0.2)
    async with serving(make_agent(), tmp_path) as server:
        t0 = time.monotonic()
        r = await raw(server, b'{"token": "abc')                           # half a request, then silence
        assert r["ok"] is False and "timed out" in r["error"]
        assert time.monotonic() - t0 < 3.0


async def test_a_client_that_hangs_up_without_a_request_is_harmless(tmp_path):
    async with serving(make_agent(), tmp_path) as server:
        _, w = await asyncio.open_connection(control.HOST, server.port)
        w.close()
        await asyncio.sleep(0.05)
        assert (await ask(server, "status"))["ok"] is True
        await asyncio.sleep(0.05)
        assert not server._writers


async def test_connections_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "MAX_CLIENTS", 1)
    async with serving(make_agent(), tmp_path) as server:
        idle_r, idle_w = await asyncio.open_connection(control.HOST, server.port)       # occupies the only slot
        await asyncio.sleep(0.05)
        r = await raw(server, b"{}\n")
        assert r == {"ok": False, "error": "busy: too many connections"}
        idle_w.close()
        await asyncio.sleep(0.05)
        assert (await ask(server, "status"))["ok"] is True                  # the slot came back


async def test_an_unexpected_failure_in_a_command_never_takes_the_server_down(tmp_path, capsys):
    agent = make_agent()
    async with serving(agent, tmp_path) as server:
        async def boom(reason=""):
            raise RuntimeError("exchange on fire: secret-detail")
        agent.flatten = boom
        r = await ask(server, "flatten", confirm=True)
        assert r == {"ok": False, "error": "internal error"}                # no internals leak to the client
        assert (await ask(server, "status"))["ok"] is True
        assert "RuntimeError" in capsys.readouterr().err


# --------------------------------------------------------------------------- status
async def test_status_reports_everything_an_operator_needs(tmp_path):
    agent = make_agent()
    await hold(agent, SOL, ETH)
    agent.j.add_cycle("paper", "view", "", "", 0, 0, 0.0, 0.0)
    agent.cycle_no, agent.next_cycle_ts = 3, time.time() + 600
    agent.last_beat = time.time() - 12
    agent.status = "RUNNING"
    async with serving(agent, tmp_path) as server:
        r = await ask(server, "status")
    assert r["ok"] is True
    assert (r["mode"], r["status"], r["paused"], r["thinking"]) == ("paper", "RUNNING", False, False)
    assert r["brain"] == "heuristic (rules)"
    assert r["equity"] == pytest.approx(agent.account.equity) and r["available"] < r["equity"]
    by_name = {p["name"]: p for p in r["positions"]}
    assert set(by_name) == {"SOL", "ETH"}
    sol = by_name["SOL"]
    assert (sol["inst"], sol["side"], sol["contracts"], sol["entry"], sol["leverage"]) == ("SOL-USDT-SWAP", "long", 1.0, 100.0, 2.0)
    assert sol["stop"] == pytest.approx(90.0) and sol["target"] == pytest.approx(120.0)
    assert r["risk"]["killed"] is False and r["risk"]["kill_reason"] == "" and r["risk"]["day_blocked"] is False
    assert r["risk"]["positions"] == 2 and r["risk"]["dd_limit"] == agent.cfg.risk.max_drawdown_pct
    assert r["cycle_no"] == 3 and 590 < r["next_cycle_in_s"] <= 600
    assert r["last_cycle_ts"] is not None and 0 <= r["last_cycle_age_s"] < 5
    assert 11 < r["stalled_for_s"] < 15 and r["monitor_alive"] is True
    assert 0 <= r["uptime_s"] < 60


async def test_status_works_before_the_first_account_fetch_and_with_nothing_booked_yet(tmp_path):
    agent = make_agent()                                                   # account is None, no cycles, no positions
    async with serving(agent, tmp_path) as server:
        r = await ask(server, "status")
    assert r["ok"] and r["equity"] is None and r["positions"] == [] and r["last_cycle_ts"] is None
    assert r["last_cycle_age_s"] is None and r["next_cycle_in_s"] is None and r["risk"]["killed"] is False


async def test_status_shows_the_halt_its_reason_and_the_day_block(tmp_path):
    agent = make_agent()
    await hold(agent)
    agent.risk.trip_kill("drawdown 26.0% hit the 25% limit")
    agent.risk.day_blocked = True
    agent._refresh_status()
    async with serving(agent, tmp_path) as server:
        r = await ask(server, "status")
    assert r["status"] == "HALTED"
    assert r["risk"]["killed"] is True and r["risk"]["kill_reason"] == "drawdown 26.0% hit the 25% limit"
    assert r["risk"]["day_blocked"] is True


async def test_status_flags_a_dead_background_task_and_stays_strict_json(tmp_path):
    agent = make_agent()
    await hold(agent, SOL)
    agent.account.equity = float("nan")                                    # nothing non-finite may reach the wire

    class Dead:
        def done(self):
            return True
    agent._tasks = [Dead()]
    async with serving(agent, tmp_path) as server:
        r = await ask(server, "status")                                    # raw() rejects NaN / Infinity outright
    assert r["monitor_alive"] is False and r["stalled_for_s"] is None and r["equity"] is None


def test_the_last_cycle_time_comes_from_the_journal():
    j = Journal(":memory:")
    assert j.last_cycle_ts() is None
    j.add_cycle("paper", "v", "", "", 0, 0, 0.0, 0.0)
    assert time.time() - 5 < j.last_cycle_ts() <= time.time()


# --------------------------------------------------------------------------- pause / unpause / cycle
async def test_pause_and_unpause_are_idempotent_so_a_retry_cannot_flip_the_state(tmp_path):
    agent = make_agent()
    agent.status = "RUNNING"
    async with serving(agent, tmp_path) as server:
        assert (await ask(server, "unpause"))["message"] == "not paused" and agent.paused is False
        r = await ask(server, "pause")
        assert r["ok"] and r["status"] == "PAUSED" and agent.paused is True
        r = await ask(server, "pause")                                     # a doubled command
        assert r["ok"] and r["message"] == "already paused" and agent.paused is True
        r = await ask(server, "unpause")
        assert r["ok"] and r["status"] == "RUNNING" and agent.paused is False
        await ask(server, "unpause")
        assert agent.paused is False
    assert sum("PAUSED" in e.text for e in agent.events) == 1              # the toggle ran once, not twice


async def test_unpause_does_not_lift_a_halt(tmp_path):
    agent = make_agent()
    await hold(agent)
    agent.toggle_pause()
    agent.risk.trip_kill("drawdown")
    agent._refresh_status()
    async with serving(agent, tmp_path) as server:
        r = await ask(server, "unpause")
    assert r["ok"] and "still HALTED" in r["message"] and r["status"] == "HALTED" and agent.risk.killed


async def test_cycle_requests_one_forced_decision_like_the_c_key(tmp_path):
    agent = make_agent()
    async with serving(agent, tmp_path) as server:
        r = await ask(server, "cycle")
    assert r["ok"] is True and agent._force is True and agent._wake.is_set()
    assert any(e.text == "manual cycle requested" for e in agent.events)


async def test_cycle_says_so_when_paused_and_refuses_when_halted_or_already_thinking(tmp_path):
    agent = make_agent()
    async with serving(agent, tmp_path) as server:
        agent.toggle_pause()
        assert "runs anyway" in (await ask(server, "cycle"))["message"]
        agent._force = False
        agent.thinking = True
        r = await ask(server, "cycle")
        assert r["ok"] is False and "already in progress" in r["error"] and agent._force is False
        agent.thinking = False
        agent.risk.trip_kill("drawdown")
        r = await ask(server, "cycle")
        assert r["ok"] is False and "HALTED" in r["error"] and agent._force is False


# --------------------------------------------------------------------------- confirmation
@pytest.mark.parametrize("cmd", ["flatten", "kill", "resume"])
@pytest.mark.parametrize("confirm", [None, False, "true", "yes", 1, 0, [], {"x": 1}])
async def test_dangerous_commands_do_nothing_without_an_explicit_true(tmp_path, cmd, confirm):
    agent = make_agent()
    await hold(agent, SOL)
    agent.risk.trip_kill("drawdown")                                       # so that resume would have something to do
    agent.risk.peak = 300.0
    killed_before = agent.risk.killed
    async with serving(agent, tmp_path) as server:
        body = {"token": server.token, "cmd": cmd}
        if confirm is not None:
            body["confirm"] = confirm
        r = await raw(server, json.dumps(body).encode() + b"\n")
    assert r["ok"] is False and "needs explicit confirmation" in r["error"]
    assert SOL.inst in agent.broker.positions                               # nothing was closed
    assert agent.risk.killed is killed_before and agent.risk.peak == 300.0  # nothing was resumed or re-based
    assert not any("control channel" in e.text for e in agent.events)       # and no audit line: it never ran


async def test_the_harmless_commands_need_no_confirmation(tmp_path):
    agent = make_agent()
    async with serving(agent, tmp_path) as server:
        for cmd in ("status", "pause", "unpause", "cycle"):
            assert (await ask(server, cmd))["ok"] is True, cmd


# --------------------------------------------------------------------------- flatten / kill
async def test_flatten_closes_everything_and_trading_continues(tmp_path):
    agent = make_agent()
    await hold(agent, SOL, ETH)
    agent.status = "RUNNING"
    async with serving(agent, tmp_path) as server:
        r = await ask(server, "flatten", confirm=True)
    assert r["ok"] is True and r["positions_before"] == 2 and r["positions_after"] == 0
    assert "2 position(s) closed" in r["message"]
    assert not agent.broker.positions and not agent.account.positions
    assert agent.risk.killed is False and agent.status == "RUNNING"          # flatten is not a halt
    assert sum(e.kind == "fill" and "manual flatten (ctl)" in e.text for e in agent.events) == 2
    assert any(e.kind == "risk" and e.text == "control channel: flatten requested" for e in agent.events)


async def test_flatten_with_nothing_open_is_a_clean_no_op(tmp_path):
    agent = make_agent()
    await hold(agent)
    async with serving(agent, tmp_path) as server:
        r = await ask(server, "flatten", confirm=True)
    assert r["ok"] is True and r["positions_before"] == 0 and r["positions_after"] == 0


async def test_flatten_reports_failure_when_positions_remain(tmp_path):
    agent = make_agent()
    await hold(agent, SOL)

    async def broken_close(spec, price, reason):
        raise BrokerError("rejected")
    agent.broker.close = broken_close
    async with serving(agent, tmp_path) as server:
        r = await ask(server, "flatten", confirm=True)
    assert r["ok"] is False and r["positions_after"] == 1 and "still open" in r["error"]
    assert any(e.kind == "error" and "flatten SOL-USDT-SWAP failed" in e.text for e in agent.events)


async def test_kill_closes_everything_and_halts_until_resumed(tmp_path):
    agent = make_agent()
    await hold(agent, SOL, ETH)
    async with serving(agent, tmp_path) as server:
        r = await ask(server, "kill", confirm=True)
        assert r["ok"] is True and r["status"] == "HALTED" and r["positions_after"] == 0
        assert not agent.broker.positions
        assert agent.risk.killed is True and agent.risk.kill_reason == "manual kill switch (ctl)"
        assert agent.status == "HALTED"
        assert agent.j.kv_get("risk_state")["killed"] is True               # persisted: it survives a restart
        r = await ask(server, "status")
        assert r["status"] == "HALTED" and r["risk"]["kill_reason"] == "manual kill switch (ctl)"
    assert any(e.kind == "risk" and e.text == "control channel: kill requested" for e in agent.events)
    assert any(e.kind == "risk" and e.text.startswith("KILL SWITCH") for e in agent.events)


async def test_the_audit_line_does_not_look_like_a_halt_to_the_alerting(tmp_path):
    from aeon.notify import _is_halt
    agent = make_agent()
    await hold(agent, SOL)
    async with serving(agent, tmp_path) as server:
        await ask(server, "kill", confirm=True)
        await ask(server, "resume", confirm=True)
    audit = [e for e in agent.events if e.text.startswith("control channel:")]
    assert {e.text for e in audit} == {"control channel: kill requested", "control channel: resume requested"}
    assert not any(_is_halt(e.kind, e.text) for e in audit)                  # the real "KILL SWITCH" line pings, not these


# --------------------------------------------------------------------------- resume
async def test_resume_does_exactly_what_the_tui_r_key_does(tmp_path):
    via_ctl, via_key = make_agent(), make_agent()
    for a in (via_ctl, via_key):
        await hold(a)
        a.risk.peak = 300.0
        a.risk.trip_kill("drawdown 33.3% hit the 25% limit")
        a._refresh_status()
        assert a.status == "HALTED"
    via_key.resume()                                                        # what tui.action_resume calls
    async with serving(via_ctl, tmp_path) as server:
        r = await ask(server, "resume", confirm=True)
    assert r["ok"] is True and r["status"] == "RUNNING" and "baseline reset" in r["message"]
    for field in ("killed", "kill_reason", "peak", "day_blocked", "trades_today"):
        assert getattr(via_ctl.risk, field) == getattr(via_key.risk, field), field
    assert via_ctl.risk.killed is False and via_ctl.risk.peak == pytest.approx(via_ctl.account.equity)  # re-based
    assert via_ctl.status == via_key.status == "RUNNING"
    assert via_ctl.j.kv_get("risk_state")["killed"] is False and via_ctl.j.kv_get("risk_state")["peak"] == via_ctl.risk.peak
    assert via_ctl._wake.is_set()                                            # the decision loop is woken, as on the R key
    assert any(e.text == "trading resumed by owner; drawdown baseline reset to current equity" for e in via_ctl.events)


async def test_resume_on_a_healthy_agent_is_refused_and_leaves_the_drawdown_baseline_alone(tmp_path):
    agent = make_agent()
    await hold(agent)
    agent.risk.peak = 300.0                                                 # a real 33% drawdown, but not halted yet
    async with serving(agent, tmp_path) as server:
        r = await ask(server, "resume", confirm=True)
    assert r["ok"] is False and "nothing to resume" in r["error"]
    assert agent.risk.peak == 300.0                                         # the TUI's guard: R does nothing unless killed


async def test_resume_waits_for_the_first_account_fetch(tmp_path):
    agent = make_agent()
    agent.risk.trip_kill("drawdown")
    async with serving(agent, tmp_path) as server:
        r = await ask(server, "resume", confirm=True)
    assert r["ok"] is False and "account not loaded" in r["error"] and agent.risk.killed is True


async def test_kill_then_resume_round_trip_leaves_a_working_agent(tmp_path):
    agent = make_agent()
    await hold(agent, SOL)
    async with serving(agent, tmp_path) as server:
        await ask(server, "kill", confirm=True)
        assert (await ask(server, "cycle"))["ok"] is False                   # halted: no thinking
        await ask(server, "resume", confirm=True)
        assert (await ask(server, "cycle"))["ok"] is True
        r = await ask(server, "status")
    assert r["status"] == "RUNNING" and r["risk"]["killed"] is False and r["positions"] == []


# --------------------------------------------------------------------------- slow exchange calls
async def test_a_slow_flatten_outlives_the_timeout_and_still_completes(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "SLOW_TIMEOUT_S", 0.1)
    agent = make_agent()
    release, finished = asyncio.Event(), []

    async def slow_flatten(reason="manual flatten"):
        await release.wait()
        finished.append(reason)
    agent.flatten = slow_flatten
    async with serving(agent, tmp_path) as server:
        r = await ask(server, "flatten", confirm=True)
        assert r["ok"] is False and "still working" in r["error"] and "status" in r["error"]
        assert finished == [] and len(server.controller._jobs) == 1          # not cancelled by the timeout
        release.set()
        await asyncio.sleep(0.05)
        assert finished == ["manual flatten (ctl)"] and not server.controller._jobs


async def test_a_client_that_disconnects_mid_flatten_does_not_abort_it(tmp_path):
    agent = make_agent()
    release, finished = asyncio.Event(), []

    async def slow_flatten(reason="manual flatten"):
        await release.wait()
        finished.append(reason)
    agent.flatten = slow_flatten
    async with serving(agent, tmp_path) as server:
        reader, writer = await asyncio.open_connection(control.HOST, server.port)
        writer.write(json.dumps({"token": server.token, "cmd": "flatten", "confirm": True}).encode() + b"\n")
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()                                                       # the operator's ssh session dropped
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.sleep(0.05)
        assert finished == ["manual flatten (ctl)"]


async def test_shutdown_lets_a_flatten_already_under_way_finish(tmp_path):
    agent = make_agent()
    done = []
    server = ControlServer(agent, tmp_path / "c")
    await server.start()

    async def work():
        await asyncio.sleep(0.15)
        done.append(True)
    job = asyncio.create_task(server.controller._finish(work()))
    await asyncio.sleep(0.02)
    await server.stop()
    assert done == [True] and (await job) is True


async def test_state_changing_commands_run_one_at_a_time(tmp_path):
    agent = make_agent()
    await hold(agent, SOL)
    order = []
    real_flatten = agent.flatten

    async def traced_flatten(reason="manual flatten"):
        order.append("flatten-start")
        await asyncio.sleep(0.1)
        await real_flatten(reason)
        order.append("flatten-end")
    agent.flatten = traced_flatten
    async with serving(agent, tmp_path) as server:
        first = asyncio.create_task(ask(server, "flatten", confirm=True))
        await asyncio.sleep(0.02)
        second = await ask(server, "pause")                                  # queued behind the flatten
        order.append("pause-done")
        assert (await first)["ok"] and second["ok"]
    assert order == ["flatten-start", "flatten-end", "pause-done"]


# --------------------------------------------------------------------------- the client and the CLI
async def test_request_round_trips_through_the_control_file(tmp_path):
    agent = make_agent()
    async with serving(agent, tmp_path) as server:
        r = await control.request(server.path, "pause")
        assert r["ok"] and agent.paused
        r = await control.request(server.path, "flatten", confirm=False)
        assert r["ok"] is False and "explicit confirmation" in r["error"]


async def test_request_with_a_token_the_server_does_not_know_is_unauthorized(tmp_path):
    async with serving(make_agent(), tmp_path) as server:
        server.path.write_text(json.dumps({"port": server.port, "token": "stolen-or-stale"}))
        r = await control.request(server.path, "status")
    assert r == {"ok": False, "error": "unauthorized"}


async def test_request_gives_up_on_a_server_that_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "READ_TIMEOUT_S", 0.1)
    monkeypatch.setattr(control, "CLIENT_PAD_S", 0.1)

    gate = asyncio.Event()

    async def silent(reader, writer):
        await gate.wait()
        writer.close()
    server = await asyncio.start_server(silent, control.HOST, 0)
    port = server.sockets[0].getsockname()[1]
    (tmp_path / "c").write_text(json.dumps({"port": port, "token": "t"}))
    try:
        with pytest.raises(ValueError, match="no reply within"):
            await control.request(tmp_path / "c", "status")
    finally:
        gate.set()
        server.close()


async def test_request_refuses_a_reply_that_is_not_a_json_object(tmp_path):
    async def liar(reader, writer):
        await reader.readline()
        writer.write(b"[1, 2]\n")
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(liar, control.HOST, 0)
    (tmp_path / "c").write_text(json.dumps({"port": server.sockets[0].getsockname()[1], "token": "t"}))
    try:
        with pytest.raises(ValueError, match="unreadable reply"):
            await control.request(tmp_path / "c", "status")
    finally:
        server.close()


def _fake_request(monkeypatch, reply=None, error=None):
    calls = []

    async def fake(path, cmd, confirm=False):
        calls.append((cmd, confirm))
        if error:
            raise ValueError(error)
        return reply
    monkeypatch.setattr(control, "request", fake)
    return calls


@pytest.mark.parametrize("cmd", ["flatten", "kill", "resume"])
def test_the_cli_will_not_even_connect_without_yes(monkeypatch, capsys, cmd):
    calls = _fake_request(monkeypatch, reply={"ok": True})
    assert control.run_ctl("paper", cmd, confirmed=False) == 2
    assert calls == [] and "--yes" in capsys.readouterr().err


def test_the_cli_exit_codes(monkeypatch, capsys):
    _fake_request(monkeypatch, reply={"ok": True, "message": "decision cycles paused", "status": "PAUSED"})
    assert control.run_ctl("paper", "pause", False) == 0
    assert capsys.readouterr().out.strip() == "pause: decision cycles paused [PAUSED]"
    _fake_request(monkeypatch, reply={"ok": False, "error": "nothing to resume: the kill switch is not tripped"})
    assert control.run_ctl("paper", "resume", True) == 1                       # refused by the agent
    assert "nothing to resume" in capsys.readouterr().err
    _fake_request(monkeypatch, error="no control file at /tmp/x")
    assert control.run_ctl("paper", "status", False) == 2                      # could not talk to it
    assert "ctl: no control file" in capsys.readouterr().err


def run_main(argv):
    """entry.main() raises SystemExit, which must not escape into the event loop that is serving the test."""
    old = sys.argv
    sys.argv = ["aeon", *argv]
    try:
        entry.main()
        return 0
    except SystemExit as e:
        return e.code
    finally:
        sys.argv = old


async def test_ctl_end_to_end_through_main(monkeypatch, tmp_path, capsys):
    hb = tmp_path / "hb"
    monkeypatch.setenv("AEON_HEARTBEAT_FILE", str(hb))
    monkeypatch.delenv("AEON_CONTROL_FILE", raising=False)
    monkeypatch.delenv("AEON_MODE", raising=False)
    cfg = str(tmp_path / "none.toml")
    agent = make_agent()
    await hold(agent, SOL)
    server = ControlServer(agent, control.control_path(agent.mode))
    await server.start()
    try:
        assert await asyncio.to_thread(run_main, ["--config", cfg, "--ctl", "status"]) == 0
        out = capsys.readouterr().out
        assert "AEON PAPER" in out and "SOL" in out and "stop 90" in out and "target 120" in out
        assert await asyncio.to_thread(run_main, ["--config", cfg, "--ctl", "flatten"]) == 2          # no --yes
        assert SOL.inst in agent.broker.positions
        assert await asyncio.to_thread(run_main, ["--config", cfg, "--ctl", "flatten", "--yes"]) == 0
        assert not agent.broker.positions
        assert await asyncio.to_thread(run_main, ["--config", cfg, "--ctl", "resume", "--yes"]) == 1  # not halted
    finally:
        await server.stop()
    assert await asyncio.to_thread(run_main, ["--config", cfg, "--ctl", "status"]) == 2                # agent gone
    assert "no control file" in capsys.readouterr().err


def test_ctl_rejects_an_unknown_command_at_argument_parsing(monkeypatch, capsys):
    assert run_main(["--ctl", "format-disk"]) == 2
    assert "invalid choice" in capsys.readouterr().err


# --------------------------------------------------------------------------- text for the operator
async def test_status_text_for_each_state(tmp_path):
    agent = make_agent()
    await hold(agent, SOL)
    agent.status, agent.cycle_no, agent.next_cycle_ts = "RUNNING", 4, time.time() + 300
    agent.j.add_cycle("paper", "v", "", "", 0, 0, 0.0, 0.0)
    text = control.format_status(control.snapshot(agent))
    assert "AEON PAPER | RUNNING | brain heuristic (rules)" in text
    assert "positions: 1" in text and "SOL" in text and "stop 90" in text and "target 120" in text
    assert "cycles: 4 this run | last decision" in text and "monitor: checked in" in text
    assert "HALTED" not in text and "PAUSED" not in text

    agent.toggle_pause()
    assert "PAUSED: no new decisions" in control.format_status(control.snapshot(agent))

    agent.risk.trip_kill("drawdown 26.0% hit the 25% limit")
    agent._refresh_status()
    halted = control.format_status(control.snapshot(agent))
    assert "HALTED: drawdown 26.0% hit the 25% limit  ->  --ctl resume --yes" in halted
    assert "PAUSED: no new decisions" not in halted                              # the halt is the news

    flat = make_agent()
    assert "positions: 0 (flat)" in control.format_status(control.snapshot(flat))
    assert "equity -" in control.format_status(control.snapshot(flat))            # no account fetched yet

    agent._tasks = [SimpleNamespace(done=lambda: True)]
    assert "A BACKGROUND TASK HAS DIED" in control.format_status(control.snapshot(agent))


def test_reply_formatting():
    assert control.format_reply("pause", {"ok": True, "message": "decision cycles paused"}) == ("pause: decision cycles paused", 0)
    assert control.format_reply("kill", {"ok": True, "message": "HALTED", "status": "HALTED"}) == ("kill: HALTED [HALTED]", 0)
    assert control.format_reply("flatten", {"ok": False, "error": "unauthorized"}) == ("flatten: unauthorized", 1)
    assert control.format_reply("flatten", {"ok": False}) == ("flatten: failed", 1)


# --------------------------------------------------------------------------- wired into the headless run
class _StubSetup:
    def __init__(self, agent):
        self.agent, self.cfg, self.journal = agent, agent.cfg, agent.j
        self.closed = False

    async def aclose(self):
        self.closed = True


async def _headless(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("AEON_HEARTBEAT_FILE", str(tmp_path / "hb"))
    monkeypatch.setenv("AEON_WATCHDOG_S", "0")
    monkeypatch.delenv("AEON_CONTROL_FILE", raising=False)
    monkeypatch.delenv("AEON_CONTROL", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    agent = make_agent()

    async def fake_start(decisions=True):
        agent._tasks = []
        agent.account = await agent._account({})
    agent.start = fake_start
    setup = _StubSetup(agent)
    return agent, setup, asyncio.create_task(entry.run_headless(setup, once=False))


async def test_headless_run_serves_the_channel_and_removes_the_file_when_it_stops(monkeypatch, tmp_path, capsys):
    agent, setup, task = await _headless(monkeypatch, tmp_path)
    await asyncio.sleep(0.2)
    path = tmp_path / "aeon-paper.control"
    assert path.exists()                                                       # next to the heartbeat file
    r = await control.request(path, "status")
    assert r["ok"] and r["mode"] == "paper"
    assert (await control.request(path, "pause"))["ok"] and agent.paused
    err = capsys.readouterr().err
    assert "control channel ready on 127.0.0.1" in err
    token = json.loads(path.read_text())["token"]
    assert token not in err                                                    # the path is announced, never the token
    task.cancel()
    await task                                                                 # a SIGTERM-style stop still returns normally
    assert not path.exists() and setup.closed
    with pytest.raises(ValueError):
        await control.request(path, "status")


async def test_headless_run_can_switch_the_channel_off(monkeypatch, tmp_path, capsys):
    agent, setup, task = await _headless(monkeypatch, tmp_path, AEON_CONTROL="off")
    await asyncio.sleep(0.2)
    assert not (tmp_path / "aeon-paper.control").exists() and (tmp_path / "hb").exists()
    assert "control channel is off" in capsys.readouterr().err
    task.cancel()
    await task


async def test_headless_run_survives_a_control_channel_that_cannot_start(monkeypatch, tmp_path):
    bad = tmp_path / "no-such-dir" / "aeon.control"
    agent, setup, task = await _headless(monkeypatch, tmp_path, AEON_CONTROL_FILE=str(bad))
    await asyncio.sleep(0.2)
    assert not task.done() and (tmp_path / "hb").exists()                      # the trader keeps running, watched
    alert = [e for e in agent.events if e.kind == "error" and "control channel unavailable" in e.text]
    assert alert and "terminal UI" in alert[0].text                            # and the operator is told (Discord too)
    task.cancel()
    await task
    assert setup.closed


async def test_one_shot_runs_never_open_the_channel(monkeypatch, tmp_path):
    monkeypatch.setenv("AEON_HEARTBEAT_FILE", str(tmp_path / "hb"))
    agent = make_agent()

    async def fake_start(decisions=True):
        agent._tasks = []

    async def fake_cycle():
        assert not (tmp_path / "aeon-paper.control").exists()
    agent.start, agent.cycle = fake_start, fake_cycle
    real_sleep = asyncio.sleep
    monkeypatch.setattr(entry.asyncio, "sleep", lambda *_: real_sleep(0))        # skip the one-shot's 1s + 2s waits
    await entry.run_headless(_StubSetup(agent), once=True)
    assert not (tmp_path / "aeon-paper.control").exists()
