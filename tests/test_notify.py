"""Discord alerts: embed shape, filtering, dedupe/rate limiting, and delivery over a mock webhook."""
import json
import time

import httpx
import pytest

from aeon.config import Config, NotifyCfg
from aeon.notify import GREEN, RED, DiscordNotifier, _is_halt, build_embed

FOOTER = "AEON · PAPER · deepseek-flash"
URL = "https://discord.com/api/webhooks/123/abc"


def notifier(**kw):
    kw.setdefault("enabled", True)
    cfg = NotifyCfg(**kw)
    n = DiscordNotifier(cfg, mode="paper", label="deepseek-flash", env={"DISCORD_WEBHOOK_URL": URL})
    n.throttle = 0.0
    return n


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def field_names(embed):
    return [f["name"] for f in embed.get("fields", [])]


# --------------------------------------------------------------------------- embed shape
def test_entry_embed_shows_price_size_stop_target_and_risk():
    e = build_embed("fill", "OPEN LONG SOL 3x | SL 96 TP 108 | risk $4.05", dict(
        event="open", inst="SOL-USDT-SWAP", side="long", price=100.0, contracts=2.0, fee=0.1,
        pnl=-0.1, trade_pnl=None, reason="SOL breakout", spec_kind="swap", leverage=3.0,
        sl=96.0, tp=108.0, notional=200.0, max_loss=4.05), time.time(), FOOTER)
    assert e["title"] == "OPEN LONG 3x · SOL-USDT-SWAP"
    assert e["color"] == GREEN and e["description"] == "SOL breakout"
    assert field_names(e) == ["Price", "Size", "Stop", "Target", "Risk at stop", "Fee"]
    stop = next(f for f in e["fields"] if f["name"] == "Stop")
    assert stop["value"] == "96.00 (-4.00%)"                 # distance from the entry price, too
    assert e["footer"]["text"] == FOOTER and "timestamp" in e


def test_losing_close_is_red_and_shows_the_whole_trade():
    e = build_embed("fill", "CLOSE SOL", dict(
        event="close", inst="SOL-USDT-SWAP", side="long", price=95.0, contracts=2.0, fee=0.1,
        pnl=-10.1, trade_pnl=-10.1, reason="stopped out", est=False), time.time(), FOOTER)
    assert e["color"] == RED and e["title"] == "CLOSE LONG · SOL-USDT-SWAP"
    assert "-10.10" in next(f for f in e["fields"] if f["name"] == "PnL")["value"]
    assert "Whole trade" in field_names(e)


def test_winning_close_is_green_and_estimated_figures_are_marked():
    e = build_embed("fill", "CLOSE SOL", dict(
        event="close", inst="SOL-USDT-SWAP", side="short", price=95.0, contracts=1.0, fee=0.05,
        pnl=5.0, trade_pnl=None, reason="take profit", est=True), time.time(), FOOTER)
    assert e["color"] == GREEN and "(est.)" in next(f for f in e["fields"] if f["name"] == "PnL")["value"]
    assert "Whole trade" not in field_names(e)


def test_spot_sizes_are_coins_not_contracts():
    e = build_embed("fill", "OPEN LONG SOL/spot", dict(
        event="open", inst="SOL-USDT", side="long", price=100.0, contracts=1.5, fee=0.15, pnl=-0.15,
        spec_kind="spot", reason="buying the dip"), time.time(), FOOTER)
    assert "1.5 coins" in next(f for f in e["fields"] if f["name"] == "Size")["value"]
    assert "spot" in e["title"]


def test_kill_switch_is_red_and_pings_halt_is_detected():
    e = build_embed("risk", "KILL SWITCH: drawdown 25.0% hit the 25% limit", None, time.time(), FOOTER)
    assert e["color"] == RED and e["title"] == "Kill switch / halt"
    assert _is_halt("risk", "KILL SWITCH: whatever") and _is_halt("risk", "HALTED: drawdown limit")
    assert not _is_halt("risk", "REJECTED open BTC: no room") and not _is_halt("error", "KILL SWITCH")


def test_errors_are_titled_by_what_broke():
    for text, title in (("brain failed (2x): timeout", "Brain failure"),
                        ("cycle crashed: ValueError: x", "Cycle crashed"),
                        ("monitor: ConnectionError: x", "Monitor error"),
                        ("close SOL failed: rejected", "Order failed")):
        assert build_embed("error", text, None, time.time(), FOOTER)["title"] == title


def test_startup_embed_splits_the_boot_line_into_fields():
    e = build_embed("system", "AEON online | PAPER | equity 200.00 USDT", dict(
        startup=True, mode="paper", equity=200.0, brain="llm (deepseek-flash)",
        indexed="12 perps, 5 dated futures"), time.time(), FOOTER)
    assert e["title"] == "AEON online" and field_names(e) == ["Mode", "Equity", "Brain", "Indexed"]
    assert e["fields"][0]["value"] == "PAPER"


def test_unknown_shapes_still_produce_a_readable_embed():
    e = build_embed("weird", "something happened", None, time.time(), FOOTER)
    assert e["title"] == "weird" and e["description"] == "something happened"


# --------------------------------------------------------------------------- filtering
def test_think_and_unlisted_events_are_never_queued():
    n = notifier(events=["fill", "risk", "error"])
    n.post("think", "the model is thinking out loud")
    n.post("action", "OPEN LONG BTC")
    n.post("system", "next check in 15 min")          # system is not in events and not a startup
    assert len(n._q) == 0
    n.post("fill", "OPEN LONG BTC")                   # listed
    assert len(n._q) == 1


def test_startup_message_is_posted_even_when_system_is_not_listed():
    n = notifier(events=["fill"])
    n.post("system", "AEON online | PAPER", {"startup": True})
    assert len(n._q) == 1 and n._q[0][0] == "system"


def test_identical_messages_inside_the_cooldown_are_dropped():
    n = notifier(cooldown_s=60.0)
    n.post("risk", "position unprotected")            # the monitor can repeat this every 5 seconds
    n.post("risk", "position unprotected")
    n.post("risk", "position unprotected")
    assert len(n._q) == 1 and n.dropped == 2
    n.post("risk", "a different message")             # different text still gets through
    assert len(n._q) == 2


def test_burst_cap_drops_the_excess():
    n = notifier(max_per_minute=3, cooldown_s=0.0)
    for i in range(6):
        n.post("risk", f"message {i}")
    assert len(n._q) == 3 and n.dropped == 3


def test_disabled_when_the_webhook_url_is_missing_or_not_discord():
    off = DiscordNotifier(NotifyCfg(enabled=True), env={})
    assert not off.enabled and "DISCORD_WEBHOOK_URL is empty" in off.problem
    off.post("fill", "nothing happens")
    assert len(off._q) == 0
    wrong = DiscordNotifier(NotifyCfg(enabled=True), env={"DISCORD_WEBHOOK_URL": "https://example.com/hook"})
    assert not wrong.enabled and "not a Discord webhook URL" in wrong.problem
    quiet = DiscordNotifier(NotifyCfg(enabled=False), env={"DISCORD_WEBHOOK_URL": URL})
    assert not quiet.enabled and quiet.problem == ""    # off on purpose is not a problem


# --------------------------------------------------------------------------- delivery
async def test_a_fill_is_posted_as_one_clean_embed():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(204)

    n = notifier()
    n._http = mock_client(handler)
    n.post("fill", "OPEN LONG SOL", dict(event="open", inst="SOL-USDT-SWAP", side="long", price=100.0,
                                         contracts=1.0, fee=0.05, pnl=-0.05, reason="breakout"))
    await n._process()
    assert n.sent == 1 and len(seen) == 1
    body = json.loads(seen[0].content)
    assert len(body["embeds"]) == 1 and body["embeds"][0]["color"] == GREEN
    assert body["allowed_mentions"] == {"parse": []}          # an ordinary fill mentions nobody
    assert "content" not in body


async def test_a_halt_pings_here_and_model_text_cannot():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(204)

    n = notifier()
    n._http = mock_client(handler)
    n.post("risk", "KILL SWITCH: drawdown 25% hit the limit")
    n.post("risk", 'model output: {"reason": "@everyone buy now"}')
    await n._process()
    halt, other = json.loads(seen[0].content), json.loads(seen[1].content)
    assert halt["content"] == "@here" and halt["allowed_mentions"] == {"parse": ["everyone"]}
    assert "content" not in other and other["allowed_mentions"] == {"parse": []}


async def test_rate_limit_is_respected_then_the_post_succeeds():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"retry_after": 0.01}) if len(calls) == 1 else httpx.Response(204)

    n = notifier()
    n._http = mock_client(handler)
    await n._send(build_embed("fill", "x", {"event": "open", "inst": "BTC-USDT-SWAP"}, time.time(), FOOTER), False)
    assert len(calls) == 2 and n.sent == 1


async def test_a_failed_post_is_counted_and_never_raised():
    n = notifier()
    n._http = mock_client(lambda request: httpx.Response(400, text="bad embeds"))
    n.post("fill", "OPEN LONG SOL", {"event": "open", "inst": "SOL-USDT-SWAP"})
    await n._process()                                        # must not raise into the agent loop
    assert n.dropped == 1 and n.sent == 0


async def test_aclose_delivers_what_is_still_queued():
    seen = []
    n = notifier()
    n._http = mock_client(lambda request: seen.append(request) or httpx.Response(204))
    n.post("fill", "OPEN LONG SOL", {"event": "open", "inst": "SOL-USDT-SWAP"})
    await n.aclose()
    assert len(seen) == 1 and n.sent == 1


# --------------------------------------------------------------------------- config
def test_config_accepts_the_notify_section(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[notify]\nenabled = true\nevents = ["fill", "error"]\ncooldown_s = 5\nmax_per_minute = 3\n',
                 encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.notify.enabled and cfg.notify.events == ["fill", "error"]
    assert cfg.notify.cooldown_s == 5.0 and cfg.notify.max_per_minute == 3


@pytest.mark.parametrize("text, needle", [
    ('[notify]\nnope = 1\n', "unknown key"),
    ('[notify]\nevents = ["fill", "shouting"]\n', "notify.events"),
    ('[notify]\nevents = []\n', "notify.events"),
    ('[notify]\nmax_per_minute = 0\n', "max_per_minute"),
    ('[notify]\ncooldown_s = -1\n', "cooldown_s"),
    ('[nonsense]\nx = 1\n', "unknown section"),
])
def test_config_rejects_bad_notify_settings(tmp_path, text, needle):
    p = tmp_path / "c.toml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=needle):
        Config.load(p)
