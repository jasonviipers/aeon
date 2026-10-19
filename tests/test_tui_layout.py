"""The terminal UI must be usable at any terminal size, stay quick, and survive a resize, a dialog and a broken panel.
Runs the real AeonApp in Textual's headless harness against a paper agent whose network start-up is replaced by data."""
import asyncio
import time

import pytest
from rich.text import Text

from aeon import tui
from aeon.agent import Agent
from aeon.brain import HeuristicBrain
from aeon.broker import PaperBroker
from aeon.config import Config
from aeon.feed import PriceFeed
from aeon.journal import Journal
from aeon.market import InstSnap, Market, Spec
from aeon.risk import RiskEngine
from aeon.tui import (Col, AeonApp, FitTable, fit_columns, fit_line, join_fit, layout_mode, text_sig)

SOL = Spec("SOL-USDT-SWAP", "SOL", ct_val=1.0, lot_sz=0.01, min_sz=0.01, tick_sz=0.01, max_lever=50)
COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "NEAR", "WLD", "HYPE", "SAND", "PUMP", "CT", "ZEC"]


# --------------------------------------------------------------------------- pure helpers
@pytest.mark.parametrize("w, h, want", [(200, 50, ("wide", False)), (150, 40, ("wide", False)), (149, 40, ("medium", False)),
                                        (100, 30, ("medium", False)), (99, 30, ("narrow", False)),
                                        (120, 29, ("medium", True)), (80, 24, ("narrow", True)), (40, 10, ("narrow", True))])
def test_layout_tiers_follow_the_terminal_size(w, h, want):
    assert layout_mode(w, h) == want


COLS = [Col("a", "A", 10, False, 0), Col("b", "B", 10, True, 3), Col("c", "C", 10, True, 1),
        Col("d", "D", 10, True, 2), Col("note", "NOTE", 20, False, 9, flex=True)]


def test_fit_columns_keeps_the_most_important_and_the_declared_order():
    assert list(fit_columns(COLS, 100)) == ["a", "c", "d", "b", "note"]          # everything fits (rank order)
    assert list(fit_columns(COLS, 40)) == ["a", "c", "d"]                        # rank 0, 1, 2 survive; 3 and the note go
    assert list(fit_columns(COLS, 25)) == ["a", "c"]
    assert fit_columns(COLS, 5) == {}                                            # nothing fits: nothing is shown


def test_the_flex_column_takes_what_is_left_and_only_when_it_can_have_its_minimum():
    wide = fit_columns(COLS, 120)
    assert wide["note"] == 120 - 4 * 12 - 2 and wide["note"] >= 20
    assert "note" not in fit_columns(COLS, 4 * 12 + 2 + 19)                      # 19 left over is under its minimum of 20
    assert fit_columns(COLS, 10_000)["note"] == 140                              # and it does not stretch forever


def test_fit_columns_never_exceeds_the_width():
    for avail in range(8, 260, 7):
        chosen = fit_columns(COLS, avail)
        assert sum(w + tui.CELL_PAD for w in chosen.values()) <= avail


def test_fit_line_drops_the_least_important_pieces_and_right_aligns_the_rest():
    left = [(0, Text("AEON")), (1, Text("PAPER")), (4, Text("cycle #1 next 09:55"))]
    right = [(2, Text("12:00:00 UTC")), (3, Text("feed WS"))]
    full = fit_line(left, right, 80)
    assert len(full) == 80 and full.plain.startswith("AEON") and full.plain.rstrip().endswith("feed WS")
    for width in range(8, 80):
        line = fit_line(left, right, width)
        assert len(line) <= width, width
        assert line.plain.startswith("AEON")                                      # the logo always survives
    assert "cycle" not in fit_line(left, right, 40).plain                         # priority 4 goes first
    mid = fit_line(left, right, 30).plain
    assert "12:00:00 UTC" in mid and "feed WS" not in mid                         # the clock (2) outlives the feed (3)
    assert fit_line([(0, Text("A VERY LONG ONLY PIECE"))], [], 5).plain == "A VER"


def test_join_fit_keeps_leading_parts_and_cuts_a_lone_overlong_one():
    assert join_fit(["real +0.00", "open +0.00"], 30) == "real +0.00 · open +0.00"
    assert join_fit(["real +0.00", "open +0.00"], 12) == "real +0.00"
    assert join_fit(["started 200.00"], 8) == "started "
    assert join_fit([], 10) == ""


def test_text_sig_sees_a_colour_change_that_text_equality_misses():
    a, b = Text("2,415.8", style="green", justify="right"), Text("2,415.8", style="red", justify="right")
    assert a == b                                                                 # Rich's own equality: same plain, no spans
    assert text_sig(a) != text_sig(b)
    assert text_sig(a) == text_sig(Text("2,415.8", style="green", justify="right"))


# --------------------------------------------------------------------------- the real app, headless
class StubSetup:
    def __init__(self, agent):
        self.agent, self.cfg, self.journal = agent, agent.cfg, agent.j
        self.closed = False

    async def aclose(self):
        self.closed = True


def make_app(positions=1):
    cfg = Config()
    cfg.run.mode, cfg.notify.enabled = "paper", False
    journal = Journal(":memory:")
    market = Market(None, cfg.risk, cfg.universe)
    agent = Agent(cfg, market, PriceFeed(None), PaperBroker(200.0, 0.0005, 0.0, 0.001), RiskEngine(cfg.risk, journal),
                  HeuristicBrain(), journal)
    market.specs[SOL.inst] = SOL
    snaps = {}
    for i, c in enumerate(COINS):
        for kind, tag, suffix in (("swap", "perp", "-USDT-SWAP"), ("spot", "spot", "-USDT")):
            inst = c + suffix
            snaps[inst] = InstSnap(inst, 100.0 + i, chg24h=1.5 - i * 0.3, ret1h=0.2 - i * 0.05, rsi1h=40.0 + i, trend="up" if i % 2 else "down",
                                   funding=0.0001 if kind == "swap" else None, kind=kind, tag=tag)
            agent.feed.prices[inst] = 100.0 + i

    async def fake_start(decisions=True):                       # what Agent.start leaves behind, without the network
        for _ in range(positions):
            await agent.broker.open(SOL, "long", 1.0, 2.0, sl=95.0, tp=120.0, price=100.0, reason="trend continuation, funding is cheap")
        agent.feed.prices[SOL.inst] = 101.0
        agent.thesis[SOL.inst] = "trend continuation, funding is cheap " * 3
        agent.account = await agent._account(dict(agent.feed.prices))
        agent.universe, agent.snaps, agent.start_equity = list(snaps), snaps, 200.0
        agent.risk.update_equity(agent.account.equity)
        agent.risk_status = agent.risk.status(agent.account)
        agent.status, agent.cycle_no, agent.next_cycle_ts = "RUNNING", 3, time.time() + 600
        agent.stats = {"realized": 1.25, "wins": 3, "losses": 2, "win_rate": 60.0, "best": 2.5}
        agent.emit("system", "AEON online | PAPER | equity 200.00 USDT | brain: heuristic (rules)")
        agent.emit("think", "Momentum is mixed, nothing worth the fees; holding the current position.")
        agent.emit("fill", "OPEN   SOL      long  1 @ 100 | fee 0.050 | pnl -0.05", event="open", pnl=-0.05)
    agent.start = fake_start
    app = AeonApp(StubSetup(agent))
    app.REFRESH_S = 0.1                                          # tick fast: the tests would otherwise sit through 0.5 s ticks
    return app, agent


async def settle(app, pilot, seconds=0.4):
    """Let the app boot, lay out and run a few 0.5 s refresh ticks."""
    t0 = time.time()
    while not app.agent.account and time.time() - t0 < 5:
        await pilot.pause(0.05)
    await pilot.pause(seconds)


def panels(app):
    return {i: app.query_one(f"#{i}") for i in ("watch", "chart", "stream", "risk", "fills", "llm", "pos")}


SIZES = [(200, 50), (150, 38), (120, 38), (100, 30), (99, 40), (80, 24), (64, 18)]


@pytest.mark.parametrize("w, h", SIZES)
async def test_every_panel_gets_a_usable_area_at_every_terminal_size(w, h):
    app, agent = make_app()
    async with app.run_test(size=(w, h)) as pilot:
        await settle(app, pilot)
        tier, short = layout_mode(w, h)
        assert app.has_class(f"-{tier}") and app.has_class("-short") == short
        assert not app._broken, app._broken
        for name, widget in panels(app).items():
            r = widget.region
            assert r.width >= min(w - 2, 28), (name, r)                         # no panel is squeezed to a sliver
            assert r.height >= (5 if name in ("pos", "fills") else 6 if name in ("stream", "chart", "watch") else 9), (name, r)
            assert r.x >= 0 and r.right <= w, (name, r, "sticks out sideways")
        for name in ("watch", "pos"):
            t = app.query_one(f"#{name}", FitTable)
            assert t.shown and t.virtual_size.width <= t.size.width + 1, (name, t.virtual_size, t.size)
        assert app.query_one("#pos", FitTable).row_count == 1
        assert app.query_one("#watch", FitTable).row_count == len(COINS) * 2
        top = app.query_one("#topbar")
        assert top.region.width == w and top.region.height == (2 if short else 3)
        if tier != "narrow" and h >= 38:
            assert app.query_one("#body").max_scroll_y == 0, "a big enough terminal shows everything without scrolling"


@pytest.mark.parametrize("w, h", [(200, 50), (80, 24)])
async def test_what_the_agent_says_reaches_the_screen_wherever_it_is(w, h):
    app, agent = make_app()
    async with app.run_test(size=(w, h)) as pilot:
        await settle(app, pilot)
        stream = app.query_one("#stream")
        text = "\n".join(line.text for line in stream.lines)
        assert "AEON online" in text and "Momentum is mixed" in text
        assert "OPEN" in "\n".join(line.text for line in app.query_one("#fills").lines)


async def test_the_columns_adapt_to_the_width_and_keep_the_essentials():
    app, agent = make_app()
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(app, pilot)
        pos, watch = app.query_one("#pos", FitTable), app.query_one("#watch", FitTable)
        assert set(pos.shown) == {c.key for c in tui.POS_COLS}                  # everything, thesis included, at 200 columns
        assert pos.widths["why"] > 40
        await pilot.resize_terminal(80, 24)
        await pilot.pause(0.4)
        assert {"sym", "side", "pnl"} <= set(pos.shown) and "why" not in pos.shown        # the essentials survive
        assert {"sym", "last", "kind"} <= set(watch.shown)                       # spot vs perp rows stay distinguishable
        assert pos.virtual_size.width <= pos.size.width + 1


async def test_resizing_while_running_reflows_without_losing_rows_or_state():
    app, agent = make_app()
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(app, pilot)
        body = app.query_one("#body")
        order = lambda: [w.id for w in body.children]                           # noqa: E731
        assert order() == ["kpis", "kpiline", "main", "pos"] and app.has_class("-wide")
        for w, h, tier in ((120, 38, "medium"), (80, 24, "narrow"), (200, 50, "wide"), (100, 30, "medium"), (99, 31, "narrow")):
            await pilot.resize_terminal(w, h)
            await pilot.pause(0.4)
            assert app.has_class(f"-{tier}"), (w, h)
            assert order() == (["kpis", "kpiline", "main", "pos"] if tier == "wide" else ["kpis", "kpiline", "pos", "main"])
            assert [c.id for c in app.query_one("#main").children] == (
                ["center", "right", "left"] if tier == "narrow" else ["left", "center", "right"])
            assert [c.id for c in app.query_one("#center").children] == (["stream", "chart"] if tier == "narrow" else ["chart", "stream"])
            assert app.query_one("#pos", FitTable).row_count == 1 and app.query_one("#watch", FitTable).row_count == len(COINS) * 2
            assert not app._broken and app.is_running
        assert agent.paused is False


async def test_keys_still_work_after_a_resize_and_scroll_keys_move_a_stacked_page():
    app, agent = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        body = app.query_one("#body")
        assert body.max_scroll_y > 0 and body.has_focus                         # a stacked page scrolls, and it is focused
        await pilot.press("pagedown")
        await pilot.pause(0.2)
        assert body.scroll_y > 0
        await pilot.press("home")
        await pilot.pause(0.2)
        assert body.scroll_y == 0
        await pilot.press("p")
        assert agent.paused is True
        await pilot.press("p")
        assert agent.paused is False
        await pilot.press("c")
        assert agent._force is True


async def test_refresh_stays_cheap_so_keys_are_never_stuck_behind_it():
    app, agent = make_app()
    async with app.run_test(size=(200, 50)) as pilot:
        await settle(app, pilot)
        t0 = time.perf_counter()
        for _ in range(40):
            app.refresh_ui()
        per_ms = (time.perf_counter() - t0) / 40 * 1000
        assert per_ms < 40, f"refresh_ui takes {per_ms:.1f} ms; it runs twice a second on the UI's own event loop"


async def test_an_unchanged_screen_is_not_redrawn():
    app, agent = make_app()
    async with app.run_test(size=(120, 38)) as pilot:
        await settle(app, pilot)
        app.refresh_ui()                                                          # reach a steady state
        calls = {"cell": 0, "static": 0}
        pos = app.query_one("#pos", FitTable)
        orig_cell = pos.update_cell
        pos.update_cell = lambda *a, **k: (calls.__setitem__("cell", calls["cell"] + 1), orig_cell(*a, **k))[1]
        orig_static = tui.Static.update
        tui.Static.update = lambda self, *a, **k: (calls.__setitem__("static", calls["static"] + 1), orig_static(self, *a, **k))[1]
        try:
            app.refresh_ui()
            app.refresh_ui()
            quiet = dict(calls)
            agent.feed.prices[SOL.inst] = 103.0                                  # a real change: the position's mark and P&L
            app.refresh_ui()
        finally:
            tui.Static.update = orig_static
        assert quiet["cell"] == 0 and quiet["static"] <= 2, quiet                 # only the clock-bearing header may repaint
        assert calls["cell"] >= 2 and "mark" in pos.shown


async def test_a_price_that_only_changes_colour_is_still_repainted():
    app, agent = make_app()
    async with app.run_test(size=(120, 38)) as pilot:
        await settle(app, pilot)
        watch = app.query_one("#watch", FitTable)
        app.refresh_ui()
        seen = []
        orig = watch.update_cell
        watch.update_cell = lambda *a, **k: (seen.append(a[1]), orig(*a, **k))[1]
        agent.feed.prices["BTC-USDT-SWAP"] = agent.feed.prices["BTC-USDT-SWAP"] + 0.001      # same digits, now an up-tick
        app.refresh_ui()
        assert "last" in seen


def test_a_refresh_before_the_screen_exists_is_harmless_and_loses_no_events():
    app, agent = make_app()
    agent.emit("system", "an early event")
    app.refresh_ui()                                                              # the timer can beat the first layout: no DOM yet
    assert app._last_eid == 0 and not app._broken                                 # nothing raised, and the event is still to be shown


async def test_one_broken_panel_does_not_take_the_terminal_down():
    app, agent = make_app()
    async with app.run_test(size=(120, 38)) as pilot:
        await settle(app, pilot)

        def boom():
            raise RuntimeError("bad data")
        boom.__name__ = "_risk"
        app._risk = boom
        for _ in range(3):
            app.refresh_ui()
            await pilot.pause(0.1)
        assert app.is_running and "_risk" in app._broken
        text = "\n".join(line.text for line in app.query_one("#stream").lines)
        assert text.count("display error in risk") == 1                          # said once, not every half second
        await pilot.press("p")
        assert agent.paused is True                                               # and the keys still work


async def test_dialogs_fit_on_a_tiny_terminal_and_flatten_gives_feedback():
    app, agent = make_app()
    async with app.run_test(size=(60, 20)) as pilot:
        await settle(app, pilot)
        await pilot.press("f")
        await pilot.pause(0.3)
        dialog = app.screen.query_one("#dialog")
        assert dialog.region.width <= 60 and dialog.region.x >= 0 and dialog.region.bottom <= 20
        await pilot.press("y")
        await pilot.pause(1.0)
        assert not agent.broker.positions                                         # confirmed: it was flattened
        assert "flattening" in "\n".join(line.text for line in app.query_one("#stream").lines)
        await pilot.press("m")
        await pilot.pause(0.3)
        mem = app.screen.query_one("#memory")
        assert mem.region.width <= 60 and mem.region.bottom <= 20
        await pilot.press("escape")
        await pilot.pause(0.2)


async def test_quit_does_not_hang_on_a_stuck_shutdown(monkeypatch):
    monkeypatch.setattr(tui, "QUIT_GRACE_S", 0.2)
    app, agent = make_app()

    async def stuck():
        await asyncio.sleep(60)
    app.setup.aclose = stuck
    exited = []
    app.exit = lambda *a, **k: exited.append(True)
    async with app.run_test(size=(120, 38)) as pilot:
        await settle(app, pilot)
        await pilot.press("q")
        await pilot.pause(0.2)
        t0 = time.perf_counter()
        await pilot.press("y")
        await pilot.pause(0.8)
        assert exited == [True]                                                   # it left despite the stuck shutdown
        assert time.perf_counter() - t0 < 3.0


async def test_the_boot_banner_fits_a_narrow_terminal_and_a_failed_boot_says_why():
    app, agent = make_app()

    async def broken(decisions=True):
        raise ConnectionError("no route to host")
    agent.start = broken
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause(1.2)
        assert "no route to host" in app._boot_error
        assert any("BOOT FAILED" in line.text for line in app.query_one("#stream").lines)
