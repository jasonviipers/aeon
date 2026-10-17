"""AEON terminal UI: a Bloomberg-style multi-panel cockpit with a neon palette."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Label, RichLog, Static

CYAN, MAG, AMBER, GREEN, RED = "#00e5ff", "#ff2bd6", "#ff9f1a", "#00ff9c", "#ff3b5c"
BLUE, DIM, TEXT, FAINT = "#3d8bff", "#5b6b82", "#c9d4e5", "#2a3a52"
BLOCKS = " ▁▂▃▄▅▆▇█"


# --------------------------------------------------------------------------- helpers
def pnl_color(x: float) -> str:
    return GREEN if x > 0 else RED if x < 0 else DIM


def px(x: float | None) -> str:
    if x is None:
        return "-"
    a = abs(x)
    if a >= 1000:
        return f"{x:,.1f}"
    d = 2 if a >= 10 else 3 if a >= 1 else 4 if a >= 0.1 else 6
    return f"{x:.{d}f}"


def utc(ts: float, fmt: str = "%H:%M") -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(fmt)


def bar(value: float, limit: float, width: int = 16, invert: bool = False) -> Text:
    """Usage bar: green while far from the limit, amber then red as it closes in."""
    ratio = 0.0 if limit <= 0 else max(0.0, min(value / limit, 1.0))
    filled = round(ratio * width)
    color = GREEN if ratio < 0.5 else AMBER if ratio < 0.8 else RED
    t = Text("▕", style=FAINT)
    t.append("█" * filled, style=color)
    t.append("░" * (width - filled), style=FAINT)
    t.append("▏", style=FAINT)
    return t


def usd(x: float) -> str:
    return f"${x:.4f}" if x < 1 else f"${x:,.2f}"


def calls(n: int) -> str:
    return f"{n} call" + ("" if n == 1 else "s")


def render_llm_meter(meter: dict, day: dict, last: dict | None, *, priced: bool, equity: float, rule_bot: bool) -> Text:
    """The LLM METER panel: tokens and cost since the journal began, the rolling 24 hours, and what that
    costs as a share of the account (the number that decides whether a model is affordable on a small balance)."""
    def row(label: str, value: str, style: str = TEXT) -> Text:
        t = Text(f"{label:<11}", style=DIM)
        t.append(f"{value:>23}", style=style)
        return t

    used = bool(meter["tin"] or meter["tout"])
    unpriced = used and not priced
    drag = day["cost"] / equity * 100.0 if equity > 0 else 0.0
    drag_color = GREEN if drag < 0.1 else AMBER if drag < 0.5 else RED
    lines = [
        row("TOKENS IN", f"{meter['tin']:,}", CYAN),
        row("TOKENS OUT", f"{meter['tout']:,}", CYAN),
        row("LLM COST", "no prices set" if unpriced else usd(meter["cost"]), AMBER if unpriced else f"bold {MAG}"),
        row("LAST 24H", f"{usd(day['cost'])} · {calls(day['calls'])}"),
        row("DRAG", "-" if unpriced else f"{drag:.3f}% of equity/day", DIM if unpriced else drag_color),
        row("LAST CALL", f"{last['tin']:,}+{last['tout']:,} · {usd(last['cost'])}" if last and (last["tin"] or last["tout"]) else "-"),
    ]
    out = Text("\n").join(lines)
    if unpriced:
        out.append("\nset prices in config.toml [llm]", style=AMBER)
    elif rule_bot and not used:
        out.append("\nrule bot: no LLM tokens used", style=DIM)
    return out


def resample(values: list[float], n: int) -> list[float]:
    if not values or n <= 0:
        return []
    if len(values) <= n:
        return values
    out = []
    for i in range(n):
        lo, hi = int(i * len(values) / n), max(int((i + 1) * len(values) / n), int(i * len(values) / n) + 1)
        out.append(values[hi - 1])
    return out


def area_chart(values: list[float], width: int, height: int, up: bool) -> list[Text]:
    """Multi-row filled chart using eighth-block glyphs, with a vertical colour gradient."""
    vals = resample(values, width)
    if len(vals) < 2 or height < 2:
        return [Text("collecting data…", style=DIM)]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        lo, hi = lo - 0.5, hi + 0.5
    levels = height * 8
    heights = [int((v - lo) / (hi - lo) * (levels - 1)) + 1 for v in vals]
    palette_up = ["#0b4f6c", "#0a7a99", "#00a6c4", "#00c9e0", CYAN]
    palette_dn = ["#5c1030", "#8c1646", "#c01f5e", "#e62a72", MAG]
    pal = palette_up if up else palette_dn
    lines: list[Text] = []
    pad = width - len(vals)
    for r in range(height - 1, -1, -1):
        color = pal[min(int(r / max(height - 1, 1) * (len(pal) - 1) + 0.5), len(pal) - 1)]
        t = Text(" " * pad)
        for h in heights:
            fill = max(0, min(h - r * 8, 8))
            t.append(BLOCKS[fill], style=color)
        lines.append(t)
    return lines


# --------------------------------------------------------------------------- modals
class _Dialog(ModalScreen):
    """Shared behaviour. The options at the bottom ("[Y] confirm   [N] cancel") are plain text that can be
    clicked as well as typed, a click outside the box cancels, and a result is only delivered once even if
    the user double-clicks or mashes a key."""
    _backdrop_result = None
    _choices: dict = {}          # widget id -> result delivered when that text is clicked

    def _finish(self, result) -> None:
        if not getattr(self, "_done", False):
            self._done = True
            self.dismiss(result)

    def on_click(self, event: events.Click) -> None:
        widget = self.get_widget_at(event.screen_x, event.screen_y)[0]
        if widget is self:                                   # clicked the dim area outside the box
            self._finish(self._backdrop_result)
        elif widget.id in self._choices:
            self._finish(self._choices[widget.id])


def _choice(label: str, id_: str) -> Static:
    return Static(Text(label), id=id_, classes="choice")     # Text, not a str: "[Y]" must not be parsed as markup


class Confirm(_Dialog):
    """Confirm / cancel. Press Y or click "[Y] confirm"; press N / Esc or click "[N] cancel"."""
    BINDINGS = [Binding("y", "yes", "Yes"), Binding("n,escape", "no", "No")]
    _backdrop_result = False
    _choices = {"yes": True, "no": False}

    def __init__(self, title: str, body: str):
        super().__init__()
        self._title, self._body = title, body

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dlg-title", markup=False)
            yield Label(self._body, id="dlg-body", markup=False)
            with Horizontal(id="dlg-choices"):
                yield _choice("[Y] confirm", "yes")
                yield _choice("[N] cancel", "no")

    def action_yes(self) -> None:
        self._finish(True)

    def action_no(self) -> None:
        self._finish(False)


class Memory(_Dialog):
    """The agent's latest view, trade theses and private notes. Close with Esc, M or Q, or click "[Esc] close"."""
    BINDINGS = [Binding("escape,m,q", "close", "Close")]
    _choices = {"close": None}

    def __init__(self, view: str, notes: str, thesis: dict[str, str]):
        super().__init__()
        self._view, self._notes, self._thesis = view, notes, thesis

    def compose(self) -> ComposeResult:
        t = Text()
        t.append("LATEST MARKET VIEW\n", style=f"bold {AMBER}")
        t.append((self._view or "(no cycle yet)") + "\n\n", style=TEXT)
        t.append("OPEN-POSITION THESES\n", style=f"bold {AMBER}")
        for inst, why in self._thesis.items():
            t.append(f"{inst.split('-')[0]:<7}", style=CYAN)
            t.append(f"{why}\n", style=TEXT)
        if not self._thesis:
            t.append("(none)\n", style=DIM)
        t.append("\nAGENT'S PRIVATE NOTES (its memory between cycles)\n", style=f"bold {AMBER}")
        t.append(self._notes.strip() or "(empty)", style=TEXT)
        with Vertical(id="memory"):
            yield Label("◈ AGENT MEMORY", id="mem-title", markup=False)
            with VerticalScroll(id="mem-scroll"):
                yield Static(t)
            with Horizontal(id="dlg-choices"):
                yield _choice("[Esc] close", "close")

    def action_close(self) -> None:
        self._finish(None)


# --------------------------------------------------------------------------- app
class AeonApp(App):
    TITLE = "AEON"
    BINDINGS = [
        Binding("p", "pause", "Pause/Resume"),
        Binding("c", "cycle", "Think now"),
        Binding("m", "memory", "Memory"),
        Binding("f", "flatten", "Flatten all"),
        Binding("k", "kill", "Kill switch"),
        Binding("r", "resume", "Resume"),
        Binding("q", "quit", "Quit"),
    ]
    CSS = """
    Screen { background: #04060b; color: #c9d4e5; }
    #topbar { height: 4; background: #070b14; border-bottom: heavy #ff9f1a; padding: 0 1; }
    #kpis { height: 5; }
    .tile { width: 1fr; height: 5; border: round #16314d; background: #060a12; padding: 0 1;
            border-title-color: #5fd7ff; border-title-style: bold; }
    #main { height: 1fr; }
    #left { width: 44%; }
    #center { width: 1fr; }
    #right { width: 38; }
    .panel { border: round #16314d; background: #060a12; border-title-color: #ff9f1a;
             border-title-style: bold; border-subtitle-color: #5b6b82; }
    #watch { height: 1fr; }
    #chart { height: 14; padding: 0 1; }
    #stream { height: 1fr; padding: 0 1; }
    #risk { height: 12; padding: 0 1; }
    #fills { height: 1fr; padding: 0 1; }
    #llm { height: 9; padding: 0 1; }
    #pos { height: 9; }
    DataTable { background: #060a12; scrollbar-size: 1 1; }
    DataTable > .datatable--header { background: #0a1424; color: #5fd7ff; text-style: bold; }
    DataTable > .datatable--cursor { background: #12304d; }
    RichLog { background: #060a12; scrollbar-size: 1 1; scrollbar-color: #16314d; scrollbar-background: #060a12; }
    Footer { background: #070b14; }
    Footer > .footer--key { background: #ff9f1a; color: #04060b; text-style: bold; }
    Footer > .footer--description { color: #8aa0bf; background: #070b14; }
    Confirm, Memory { align: center middle; background: #000000 70%; }
    #dialog { width: 66; height: auto; border: heavy #ff3b5c; background: #080d18; padding: 1 2; }
    #dlg-title { color: #ff3b5c; text-style: bold; margin-bottom: 1; }
    #dlg-body { margin-bottom: 1; }
    #dlg-choices { height: 1; width: auto; }
    .choice { width: auto; color: #5b6b82; }
    .choice:hover { color: #c9d4e5; text-style: bold; }
    #yes { margin-right: 6; }
    #mem-scroll { height: 1fr; }
    #memory { width: 96; height: 80%; border: heavy #00e5ff; background: #080d18; padding: 1 2; }
    #mem-title { color: #00e5ff; text-style: bold; margin-bottom: 1; }
    """

    def __init__(self, setup):
        super().__init__()
        self.setup = setup
        self.agent = setup.agent
        self._last_eid = 0
        self._prev_px: dict[str, float] = {}
        self._booted = False
        self._boot_error = ""
        self._watch_cols = ("sym", "kind", "last", "d24", "h1", "rsi", "trend", "fund")
        self._pos_cols = ("sym", "kind", "side", "lev", "size", "entry", "mark", "pnl", "pnlp", "sl", "tp", "held", "why")

    # --- layout --------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with Horizontal(id="kpis"):
            for i, name in enumerate(("EQUITY", "TOTAL P&L", "TODAY", "DRAWDOWN", "WIN RATE", "BRAIN")):
                tile = Static(classes="tile", id=f"k{i}")
                tile.border_title = name
                yield tile
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield DataTable(id="watch", classes="panel", cursor_type="none")
            with Vertical(id="center"):
                yield Static(id="chart", classes="panel")
                yield RichLog(id="stream", classes="panel", wrap=True, markup=False, highlight=False, max_lines=400)
            with Vertical(id="right"):
                yield Static(id="risk", classes="panel")
                yield RichLog(id="fills", classes="panel", wrap=True, markup=False, highlight=False, max_lines=300)
                yield Static(id="llm", classes="panel")
        yield DataTable(id="pos", classes="panel", cursor_type="none")
        yield Footer()

    def on_mount(self) -> None:
        watch = self.query_one("#watch", DataTable)
        watch.border_title = "◈ WATCHLIST · LIVE"
        for key, label, width in (("sym", "SYMBOL", 9), ("kind", "TYPE", 8), ("last", "LAST", 11), ("d24", "24H", 8),
                                  ("h1", "1H", 7), ("rsi", "RSI", 4), ("trend", "TR", 3), ("fund", "FUND·BASIS", 11)):
            watch.add_column(Text(label, justify="right" if key not in ("sym", "kind", "trend") else "left"), width=width, key=key)
        pos = self.query_one("#pos", DataTable)
        pos.border_title = "◈ POSITIONS"
        for key, label, width in (("sym", "SYMBOL", 9), ("kind", "TYPE", 8), ("side", "SIDE", 6), ("lev", "LEV", 5),
                                  ("size", "SIZE $", 9), ("entry", "ENTRY", 11), ("mark", "MARK", 11),
                                  ("pnl", "P&L $", 9), ("pnlp", "P&L %", 8), ("sl", "STOP", 11), ("tp", "TARGET", 11),
                                  ("held", "HELD", 7), ("why", "THESIS", 70)):
            pos.add_column(Text(label, justify="left" if key in ("sym", "kind", "side", "why") else "right"), width=width, key=key)
        self.query_one("#chart").border_title = "◈ EQUITY CURVE"
        self.query_one("#stream").border_title = "◈ AGENT STREAM · what it thinks and does"
        self.query_one("#risk").border_title = "◈ RISK GOVERNOR"
        self.query_one("#fills").border_title = "◈ EXECUTIONS"
        self.query_one("#llm").border_title = "◈ LLM METER"
        self.run_worker(self._boot(), name="boot", exit_on_error=False)
        self.set_interval(0.5, self.refresh_ui)

    async def _boot(self) -> None:
        try:
            await self.agent.start()
            self._booted = True
        except Exception as e:                        # show the failure instead of a blank screen
            self._boot_error = f"{type(e).__name__}: {e}"
            self._log_stream(f"BOOT FAILED: {self._boot_error}", RED)

    # --- actions -------------------------------------------------------------
    def action_pause(self) -> None:
        self.agent.toggle_pause()

    def action_cycle(self) -> None:
        self.agent.cycle_now()

    def action_memory(self) -> None:
        self.push_screen(Memory(self.agent.last_view, self.agent.notes, self.agent.thesis))

    def action_resume(self) -> None:
        if self.agent.risk.killed:
            self.agent.resume()
        else:
            self._log_stream("nothing to resume: the kill switch is not tripped (use P to un-pause)", DIM)

    def action_flatten(self) -> None:
        n = len(self.agent.account.positions) if self.agent.account else 0
        if not n:
            self._log_stream("no open positions to flatten", DIM)
            return
        self.push_screen(Confirm("FLATTEN ALL POSITIONS",
                                 f"Close all {n} open position(s) at market now.\nTrading continues afterwards."),
                         lambda ok: self.run_worker(self.agent.flatten("manual flatten")) if ok else None)

    def action_kill(self) -> None:
        self.push_screen(Confirm("KILL SWITCH",
                                 "Close every position at market and HALT the agent.\n"
                                 "It will not trade again until you press R."),
                         lambda ok: self.run_worker(self.agent.kill("manual kill switch")) if ok else None)

    async def action_quit(self) -> None:
        n = len(self.agent.account.positions) if self.agent.account else 0
        if n and self.agent.mode == "paper":
            note = f"{n} paper position(s) are open. Paper stops only work while AEON runs."
        elif n:
            note = f"{n} position(s) stay open and keep their exchange-side stops."
        else:
            note = "No open positions."

        async def done(ok: bool | None) -> None:
            if ok:
                await self.setup.aclose()
                self.exit()
        self.push_screen(Confirm("QUIT AEON", note + "\nThe agent stops thinking while closed."), done)

    # --- refresh -------------------------------------------------------------
    def refresh_ui(self) -> None:
        self._drain_events()
        a = self.agent
        if not a.account:
            self.query_one("#topbar", Static).update(self._boot_banner())
            return
        self.query_one("#topbar", Static).update(self._topbar())
        self._kpis()
        self._watchlist()
        self._positions()
        self._chart()
        self._risk()
        self._llm()

    def _boot_banner(self) -> Text:
        t = Text()
        t.append("◢◤ AEON ", style=f"bold {AMBER}")
        if self._boot_error:
            t.append(f"  offline: {self._boot_error}", style=f"bold {RED}")
        else:
            t.append("  booting: indexing OKX perpetuals…", style=CYAN)
        return t

    def _drain_events(self) -> None:
        for ev in list(self.agent.events):
            if ev.id <= self._last_eid:
                continue
            self._last_eid = ev.id
            if ev.kind == "fill":
                m = ev.meta or {}
                value = m.get("trade_pnl") if m.get("trade_pnl") is not None else m.get("pnl", 0.0)
                color = CYAN if m.get("event") in ("open", "add") else pnl_color(value or 0.0)
                self.query_one("#fills", RichLog).write(Text.assemble((utc(ev.ts) + " ", DIM), (ev.text, color)))
            else:
                self._log_event(ev)

    def _log_event(self, ev) -> None:
        style, tag = {"think": (CYAN, "◈"), "action": (AMBER, "▶"), "risk": (MAG, "⚠"),
                      "error": (RED, "✖"), "system": (DIM, "·")}.get(ev.kind, (TEXT, "·"))
        self.query_one("#stream", RichLog).write(
            Text.assemble((utc(ev.ts) + " ", FAINT), (tag + " ", style),
                          (ev.text, f"bold {style}" if ev.kind in ("action", "error") else style if ev.kind != "think" else TEXT)))

    def _log_stream(self, text: str, color: str) -> None:
        self.query_one("#stream", RichLog).write(Text.assemble((utc(time.time()) + " ", FAINT), (text, color)))

    # --- panels --------------------------------------------------------------
    def _live(self, inst: str, fallback: float) -> float:
        return self.agent.feed.prices.get(inst, fallback)

    def _topbar(self) -> Group:
        a, now = self.agent, time.time()
        mode_style = {"paper": f"bold {CYAN} reverse", "demo": f"bold {AMBER} reverse", "live": f"bold {RED} reverse"}[a.mode]
        if a.thinking:
            status = Text(f"{'◐◓◑◒'[int(now * 4) % 4]} THINKING", style=f"bold {MAG}")
        elif a.status == "RUNNING":
            status = Text("● RUNNING", style=f"bold {GREEN}")
        elif a.status == "PAUSED":
            status = Text("❚❚ PAUSED", style=f"bold {AMBER}")
        elif a.status == "HALTED":
            status = Text("■ HALTED", style=f"bold {RED}")
        else:
            status = Text("○ STARTING", style=DIM)
        feed = a.feed
        fcolor = GREEN if feed.age() < 15 else AMBER if feed.age() < 60 else RED
        nxt = max(a.next_cycle_ts - now, 0)
        left = Text.assemble(("◢◤ AEON", f"bold {AMBER}"), ("  AUTONOMOUS PERPETUALS TERMINAL", DIM))
        mid = Text.assemble((f" {a.mode.upper()} ", mode_style), "   ", status,
                            (f"   cycle #{a.cycle_no}" + ("" if a.thinking or a.status != "RUNNING" else f" · next {int(nxt // 60):02d}:{int(nxt % 60):02d}"), DIM))
        right = Text.assemble((f"feed {feed.transport.upper()} ", DIM), ("●", fcolor),
                              (f" {min(feed.age(), 99):.0f}s   ", DIM), (utc(now, "%H:%M:%S") + " UTC", TEXT))
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="center", ratio=1)
        grid.add_column(justify="right", ratio=1)
        grid.add_row(left, mid, right)
        return Group(grid, self._tape())

    def _tape(self) -> Text:
        t, width = Text(no_wrap=True, overflow="crop"), max(self.size.width - 4, 40)
        for inst in self.agent.universe:
            s = self.agent.snaps.get(inst)
            if not s:
                continue
            live = self._live(inst, s.last)
            open24 = s.last / (1 + s.chg24h / 100) if s.chg24h > -99 else s.last
            chg = (live / open24 - 1) * 100 if open24 else 0.0
            seg = Text.assemble((inst.split("-")[0] + " ", CYAN), (px(live) + " ", TEXT),
                                (f"{'▲' if chg >= 0 else '▼'}{abs(chg):.1f}%", pnl_color(chg)), ("   ", FAINT))
            if len(t) + len(seg) > width:
                break
            t.append_text(seg)
        if not len(t):
            t.append("waiting for the first market scan…", style=DIM)
        return t

    def _tile(self, idx: int, big: str, big_color: str, sub: Text | str) -> None:
        t = Text()
        t.append(big + "\n", style=f"bold {big_color}")
        t.append_text(sub if isinstance(sub, Text) else Text(sub, style=DIM))
        self.query_one(f"#k{idx}", Static).update(t)

    def _kpis(self) -> None:
        a = self.agent
        acct, st, rs = a.account, a.stats or {}, a.risk_status or {}
        total = acct.equity - a.start_equity
        total_pct = total / a.start_equity * 100 if a.start_equity else 0.0
        day_usd = acct.equity * rs.get("day_pnl", 0.0) / 100 / (1 + rs.get("day_pnl", 0.0) / 100) if rs else 0.0
        self._tile(0, f"{acct.equity:,.2f}", TEXT, f"USDT · started {a.start_equity:,.2f}")
        self._tile(1, f"{total:+,.2f}  {total_pct:+.1f}%", pnl_color(total),
                   f"realised {st.get('realized', 0.0):+.2f} · open {acct.upl:+.2f}")
        self._tile(2, f"{rs.get('day_pnl', 0.0):+.2f}%", pnl_color(rs.get("day_pnl", 0.0)),
                   f"{day_usd:+.2f} USDT · floor -{a.cfg.risk.daily_loss_limit_pct:g}%")
        dd = rs.get("drawdown", 0.0)
        self._tile(3, f"{dd:.2f}%", GREEN if dd < 5 else AMBER if dd < 15 else RED,
                   f"peak {rs.get('peak', acct.equity):,.2f} · halt at {a.cfg.risk.max_drawdown_pct:g}%")
        wr = st.get("win_rate")
        self._tile(4, "—" if wr is None else f"{wr:.0f}%", DIM if wr is None else (GREEN if wr >= 50 else AMBER),
                   f"{st.get('wins', 0)}W / {st.get('losses', 0)}L" +
                   ("" if st.get("best") is None else f" · best {st['best']:+.2f}"))
        label = a.brain.label.split("/")[-1][:20]                       # tokens and cost live in the LLM METER panel
        self._tile(5, label, MAG, (calls(a.meter['calls']) + (f" · {a.llm_latency:.1f}s" if a.llm_latency else ""))
                   if a.brain.name != "heuristic" else "no LLM, no cost")

    def _sync(self, table: DataTable, cols: tuple[str, ...], rows: dict[str, list[Text]]) -> None:
        have = {rk.value for rk in table.rows}
        for key in have - rows.keys():
            table.remove_row(key)
        for key, cells in rows.items():
            if key in have:
                for col, cell in zip(cols, cells):
                    table.update_cell(key, col, cell, update_width=False)
            else:
                table.add_row(*cells, key=key)

    def _watchlist(self) -> None:
        a, rows = self.agent, {}
        held = set(a.account.positions)
        for inst in a.universe:
            s = a.snaps.get(inst)
            if not s:
                continue
            live = self._live(inst, s.last)
            prev = self._prev_px.get(inst, live)
            self._prev_px[inst] = live
            open24 = s.last / (1 + s.chg24h / 100) if s.chg24h > -99 else s.last
            d24 = (live / open24 - 1) * 100 if open24 else 0.0
            h1 = s.ret1h + (live / s.last - 1) * 100
            tick_color = GREEN if live > prev else RED if live < prev else TEXT
            arrow, tcolor = {"up": ("▲", GREEN), "down": ("▼", RED)}.get(s.trend, ("◆", DIM))
            rsi = s.rsi1h or 0
            rsi_color = RED if rsi >= 70 else GREEN if rsi <= 30 else TEXT
            if s.kind == "swap":                       # perpetuals show their funding rate
                fund = (s.funding or 0) * 100
                carry = Text(f"{fund:+.4f}%", style=pnl_color(-fund) if abs(fund) > 0.03 else DIM, justify="right")
            elif s.kind == "futures":                  # dated futures show the annualised basis and days left
                carry = Text(f"{s.ann_basis:+.1f}%a {s.days_left:.0f}d" if s.ann_basis is not None and s.days_left is not None
                             else f"{s.days_left:.0f}d" if s.days_left is not None else "-", style=MAG, justify="right")
            else:
                carry = Text("-", style=FAINT, justify="right")
            kind_color = {"swap": DIM, "futures": MAG, "spot": GREEN}.get(s.kind, DIM)
            rows[inst] = [
                Text(("● " if inst in held else "  ") + inst.split("-")[0], style=f"bold {AMBER}" if inst in held else CYAN),
                Text(s.tag, style=kind_color),
                Text(px(live), style=tick_color, justify="right"),
                Text(f"{d24:+.2f}%", style=pnl_color(d24), justify="right"),
                Text(f"{h1:+.2f}%", style=pnl_color(h1), justify="right"),
                Text(f"{rsi:.0f}", style=rsi_color, justify="right"),
                Text(arrow, style=tcolor),
                carry,
            ]
        w = self.query_one("#watch", DataTable)
        self._sync(w, self._watch_cols, rows)
        w.border_subtitle = f"{len(rows)} of {len(a.market.specs)} markets"

    def _positions(self) -> None:
        a, rows = self.agent, {}
        for inst, p in a.account.positions.items():
            p.mark = self._live(inst, p.mark or p.entry)
            held = int((time.time() - p.opened_ts) // 60)
            c = pnl_color(p.upl)
            spec = a.market.specs.get(inst)
            rows[inst] = [
                Text(inst.split("-")[0], style=f"bold {AMBER}"),
                Text(spec.tag if spec else p.kind, style={"swap": DIM, "futures": MAG, "spot": GREEN}.get(p.kind, DIM)),
                Text(p.side.upper(), style=f"bold {GREEN if p.side == 'long' else RED}"),
                Text(f"{p.lever:g}x", justify="right"),
                Text(f"{p.notional:,.1f}", justify="right"),
                Text(px(p.entry), justify="right"),
                Text(px(p.mark), justify="right"),
                Text(f"{p.upl:+.2f}", style=f"bold {c}", justify="right"),
                Text(f"{p.upl_pct:+.1f}%", style=c, justify="right"),
                Text(px(p.sl), style=RED, justify="right"),
                Text(px(p.tp), style=GREEN, justify="right"),
                Text(f"{held // 60}h{held % 60:02d}" if held >= 60 else f"{held}m", style=DIM, justify="right"),
                Text(a.thesis.get(inst, "-")[:110], style=DIM),
            ]
        t = self.query_one("#pos", DataTable)
        self._sync(t, self._pos_cols, rows)
        t.border_subtitle = (f"{len(rows)} open · exposure ${a.account.notional:,.0f} · margin ${a.account.margin_used:,.2f}"
                             if rows else "flat · waiting for a setup worth taking")

    def _chart(self) -> None:
        a, chart = self.agent, self.query_one("#chart", Static)
        series = [v for _, v in a.journal_series()] + [v for ts, v in a.equity_hist if ts > a.journal_series_end()]
        w, h = chart.size.width - 2 - 10, chart.size.height - 2 - 1
        if w < 10 or h < 3:
            return
        lines = area_chart(series, w, h, up=(series[-1] >= series[0]) if series else True)
        lo, hi = (min(series), max(series)) if series else (0.0, 0.0)
        out = Text()
        for i, line in enumerate(lines):
            label = f"{hi:>9,.2f} " if i == 0 else f"{lo:>9,.2f} " if i == len(lines) - 1 else " " * 10
            out.append(label, style=DIM)
            out.append_text(line)
            out.append("\n")
        out.append(" " * 10 + f"{'older':<{max(w - 4, 1)}}now", style=FAINT)
        chart.update(out)
        chart.border_subtitle = f"{len(series)} samples"

    def _llm(self) -> None:
        a = self.agent
        cfg = a.cfg.llm
        panel = self.query_one("#llm", Static)
        panel.update(render_llm_meter(
            a.meter, a.llm24, a.last_llm, priced=cfg.input_price_per_m > 0 or cfg.output_price_per_m > 0,
            equity=a.account.equity if a.account else 0.0, rule_bot=a.brain.name == "heuristic"))
        panel.border_subtitle = a.brain.label.split("/")[-1][:24]      # the model, in the corner of the panel

    def _risk(self) -> None:
        a, rs, c = self.agent, self.agent.risk_status, self.agent.cfg.risk
        if not rs:
            return

        def row(label: str, value: float, limit: float, text: str) -> Text:
            t = Text(f"{label:<9}", style=DIM)
            t.append_text(bar(value, limit, 14))
            t.append(f" {text}", style=TEXT)
            return t

        lines = [
            row("DRAWDOWN", rs["drawdown"], rs["dd_limit"], f"{rs['drawdown']:.1f}/{rs['dd_limit']:g}%"),
            row("DAY LOSS", max(-rs["day_pnl"], 0), rs["day_limit"], f"{max(-rs['day_pnl'], 0):.1f}/{rs['day_limit']:g}%"),
            row("EXPOSURE", rs["exposure"], rs["exposure_limit"], f"{rs['exposure']:.2f}/{rs['exposure_limit']:g}x"),
            row("POSITIONS", rs["positions"], rs["positions_limit"], f"{rs['positions']}/{rs['positions_limit']}"),
            row("ENTRIES", rs["trades_today"], rs["trades_limit"], f"{rs['trades_today']}/{rs['trades_limit']}/day"),
        ]
        out = Text("\n").join(lines)
        out.append("\n")
        if rs["killed"]:
            out.append("\n■ KILL SWITCH TRIPPED\n", style=f"bold {RED}")
            out.append(rs["kill_reason"][:34], style=RED)
        elif rs["day_blocked"]:
            out.append("\n⚠ NO NEW RISK TODAY\n", style=f"bold {AMBER}")
            out.append("daily loss limit hit", style=AMBER)
        else:
            out.append("\n● GOVERNOR ARMED\n", style=f"bold {GREEN}")
            out.append(f"{c.max_leverage:g}x max · {c.risk_per_trade_pct:g}%/trade · stops {c.min_stop_pct:g}-{c.max_stop_pct:g}%", style=DIM)
        self.query_one("#risk", Static).update(out)
