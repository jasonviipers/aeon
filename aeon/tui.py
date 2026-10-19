"""AEON terminal UI: a Bloomberg-style multi-panel cockpit with a neon palette."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, ScreenStackError
from textual.css.query import NoMatches
from textual.binding import Binding
from textual.containers import Container, Grid, Horizontal, Vertical, VerticalScroll
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


def render_llm_meter(meter: dict, day: dict, last: dict | None, *, priced: bool, equity: float, rule_bot: bool,
                     width: int = 34) -> Text:
    """The LLM METER panel: tokens and cost since the journal began, the rolling 24 hours, and what that
    costs as a share of the account (the number that decides whether a model is affordable on a small balance)."""
    def row(label: str, value: str, style: str = TEXT) -> Text:
        room = max(width - 11, 8)                          # a narrow panel clips the value rather than wrapping the row
        t = Text(f"{label:<11}", style=DIM, no_wrap=True, overflow="ellipsis")
        t.append(f"{value[:room]:>{room}}", style=style)
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


# --------------------------------------------------------------------------- responsive layout
WIDE_MIN, MEDIUM_MIN, SHORT_ROWS = 150, 100, 30
QUIT_GRACE_S = 8.0           # how long Quit waits for the agent to close its connections before leaving anyway


def layout_mode(width: int, height: int) -> tuple[str, bool]:
    """(tier, short) for a terminal of this size. wide = three columns, medium = two columns with the risk row
    underneath, narrow = one column that scrolls. short (under 30 rows) swaps the header and KPI tiles for one line each."""
    return ("wide" if width >= WIDE_MIN else "medium" if width >= MEDIUM_MIN else "narrow"), height < SHORT_ROWS


@dataclass(frozen=True)
class Col:
    key: str
    label: str
    width: int
    right: bool = False      # right-aligned (numbers)
    rank: int = 0            # who survives a narrow terminal first: lower = kept longer
    flex: bool = False       # takes whatever width is left (and is dropped when less than `width` remains)


CELL_PAD = 2                 # DataTable pads every cell by one space each side


def fit_columns(cols: list[Col], avail: int, flex_max: int = 140) -> dict[str, int]:
    """{key: width} for the columns that fit in `avail` characters, in rank order of importance. Columns come back
    in their declared order when iterated through `cols`; the flex column is sized to what is left over."""
    chosen: dict[str, int] = {}
    used = 0
    for c in sorted((c for c in cols if not c.flex), key=lambda c: c.rank):
        if used + c.width + CELL_PAD <= avail:
            chosen[c.key] = c.width
            used += c.width + CELL_PAD
    for c in cols:
        if c.flex and avail - used - CELL_PAD >= c.width:
            chosen[c.key] = min(avail - used - CELL_PAD, flex_max)
    return chosen


class FitTable(DataTable):
    """A DataTable that shows as many of its columns as the width allows, most important first, and re-chooses them
    whenever it is resized. Callers read `shown` for the keys currently present and `generation` to learn that the
    columns (and so the rows) were rebuilt."""

    def __init__(self, cols: list[Col], **kw):
        super().__init__(**kw)
        self.cols = cols
        self.shown: tuple[str, ...] = ()
        self.widths: dict[str, int] = {}
        self.generation = 0

    def on_resize(self, event: events.Resize) -> None:
        self.refit()

    def refit(self) -> bool:
        avail = self.size.width - 1                       # one column is the scrollbar
        if avail < 8:
            return False
        widths = fit_columns(self.cols, avail)
        keys = tuple(c.key for c in self.cols if c.key in widths)
        if keys == self.shown and widths == self.widths:
            return False
        self.clear(columns=True)
        for c in self.cols:
            if c.key in widths:
                self.add_column(Text(c.label, justify="right" if c.right else "left"), width=widths[c.key], key=c.key)
        self.shown, self.widths = keys, widths
        self.generation += 1
        return True


def fit_line(left: list[tuple[int, Text]], right: list[tuple[int, Text]], width: int, gap: int = 3) -> Text:
    """One line from prioritised pieces: the least important (highest number) are dropped until it fits, the left
    group is packed to the left and the right group to the right edge."""
    items = [(p, 0, t) for p, t in left] + [(p, 1, t) for p, t in right]

    def need(its) -> int:
        l, r = [t for _, side, t in its if side == 0], [t for _, side, t in its if side == 1]
        return (sum(len(t) for t in l) + gap * max(len(l) - 1, 0) + sum(len(t) for t in r) + gap * max(len(r) - 1, 0)
                + (gap if l and r else 0))

    while len(items) > 1 and need(items) > width:
        items.remove(max(items, key=lambda it: it[0]))      # ties: the first one added at that priority
    out = Text(no_wrap=True, overflow="crop")
    l, r = [t for _, side, t in items if side == 0], [t for _, side, t in items if side == 1]
    for i, t in enumerate(l):
        out.append_text(t)
        if i < len(l) - 1:
            out.append(" " * gap)
    if r:
        rtxt = Text(" " * gap, no_wrap=True).join(r) if len(r) > 1 else r[0]
        out.append(" " * max(width - len(out) - len(rtxt), gap))
        out.append_text(rtxt)
    out.truncate(max(width, 1), overflow="crop")          # never wider than asked, even when one piece alone is too long
    return out


def join_fit(parts: list[str], width: int, sep: str = " · ") -> str:
    """The first parts that fit in `width`, joined (most important first); the first is cut if it alone is too long."""
    out = ""
    for part in parts:
        nxt = part if not out else out + sep + part
        if len(nxt) > width:
            break
        out = nxt
    return out or (parts[0][:max(width, 1)] if parts else "")


def text_sig(t: Text) -> tuple:
    """Everything that changes how a Text looks. Text.__eq__ ignores the base style and justification, so a price that
    turns from green to red with the same digits would otherwise look 'unchanged'."""
    return (t.plain, str(t.style), t.justify, tuple(t._spans))


WATCH_COLS = [
    Col("sym", "SYMBOL", 9, False, 0), Col("kind", "TYPE", 8, False, 2), Col("last", "LAST", 11, True, 1),
    Col("d24", "24H", 8, True, 3), Col("h1", "1H", 7, True, 4), Col("rsi", "RSI", 4, True, 6),
    Col("trend", "TR", 3, False, 5), Col("fund", "FUND·BASIS", 11, True, 7),
]
POS_COLS = [
    Col("sym", "SYMBOL", 9, False, 0), Col("kind", "TYPE", 8, False, 9), Col("side", "SIDE", 6, False, 1),
    Col("lev", "LEV", 5, True, 8), Col("size", "SIZE $", 9, True, 7), Col("entry", "ENTRY", 11, True, 5),
    Col("mark", "MARK", 11, True, 4), Col("pnl", "P&L $", 9, True, 2), Col("pnlp", "P&L %", 8, True, 3),
    Col("sl", "STOP", 11, True, 6), Col("tp", "TARGET", 11, True, 10), Col("held", "HELD", 7, True, 11),
    Col("why", "THESIS", 20, False, 12, flex=True),
]


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
    REFRESH_S = 0.5                                   # how often the panels are rebuilt (tests shorten it)
    BINDINGS = [
        Binding("p", "pause", "Pause"),
        Binding("c", "cycle", "Think"),
        Binding("m", "memory", "Memory"),
        Binding("f", "flatten", "Flatten"),
        Binding("k", "kill", "Kill"),
        Binding("r", "resume", "Resume"),
        Binding("q", "quit", "Quit"),
    ]
    # The layout follows the terminal. Three tiers, set as classes on the app by `_apply_layout`:
    #   -wide   (150+ cols)  three columns: watchlist | chart + agent stream | risk, executions, LLM meter
    #   -medium (100-149)    two columns (watchlist | chart + stream) with risk, executions, LLM meter in a row below
    #   -narrow (<100)       one column, panels stacked, the page scrolls (mouse wheel, arrows, PgUp/PgDn)
    # -short (<30 rows) replaces the header's market tape and the KPI tiles by one line each.
    CSS = """
    Screen { background: #04060b; color: #c9d4e5; }
    #topbar { height: 3; background: #070b14; border-bottom: heavy #ff9f1a; padding: 0 1; }
    AeonApp.-short #topbar { height: 2; }
    #body { height: 1fr; scrollbar-size: 1 1; scrollbar-color: #16314d; scrollbar-background: #04060b; }
    #kpis { layout: grid; grid-size: 6 1; height: 4; }
    AeonApp.-narrow #kpis { grid-size: 3 2; height: 8; }
    #kpiline { display: none; height: auto; padding: 0 1; background: #060a12; }
    AeonApp.-short #kpis { display: none; }
    AeonApp.-short #kpiline { display: block; }
    .tile { height: 4; border: round #16314d; background: #060a12; padding: 0 1;
            border-title-color: #5fd7ff; border-title-style: bold; }
    .panel { border: round #16314d; background: #060a12; border-title-color: #ff9f1a;
             border-title-style: bold; border-subtitle-color: #5b6b82; }
    #main { height: 1fr; min-height: 24; }
    #left, #center, #right { height: 100%; width: 100%; }
    AeonApp.-wide #main { layout: grid; grid-size: 3 1; grid-columns: 1.2fr 1fr 40; }
    AeonApp.-medium #main { layout: grid; grid-size: 2 2; grid-columns: 1fr 1fr; grid-rows: 1fr 10; min-height: 22; }
    AeonApp.-medium #right { column-span: 2; layout: horizontal; }
    AeonApp.-medium #right > .panel { width: 1fr; height: 100%; }
    AeonApp.-narrow #main { layout: vertical; height: auto; min-height: 0; }
    AeonApp.-narrow #left, AeonApp.-narrow #center, AeonApp.-narrow #right { height: auto; }
    #watch { height: 1fr; }
    #chart { height: 2fr; min-height: 6; padding: 0 1; }
    #stream { height: 3fr; min-height: 6; padding: 0 1; }
    #risk { height: 10; padding: 0 1; }
    #fills { height: 1fr; min-height: 5; padding: 0 1; }
    #llm { height: 9; padding: 0 1; }
    #pos { height: auto; min-height: 5; max-height: 12; }
    AeonApp.-narrow #watch { height: 16; }
    AeonApp.-narrow #chart { height: 11; }
    AeonApp.-narrow #stream { height: 14; }
    AeonApp.-narrow #risk { height: 10; }
    AeonApp.-narrow #fills { height: 9; }
    AeonApp.-narrow #llm { height: 9; }
    DataTable { background: #060a12; scrollbar-size: 1 1; }
    DataTable > .datatable--header { background: #0a1424; color: #5fd7ff; text-style: bold; }
    DataTable > .datatable--cursor { background: #12304d; }
    RichLog { background: #060a12; scrollbar-size: 1 1; scrollbar-color: #16314d; scrollbar-background: #060a12; }
    Footer { background: #070b14; }
    Footer > .footer--key { background: #ff9f1a; color: #04060b; text-style: bold; }
    Footer > .footer--description { color: #8aa0bf; background: #070b14; }
    Confirm, Memory { align: center middle; background: #000000 70%; }
    #dialog { width: 90%; max-width: 66; height: auto; border: heavy #ff3b5c; background: #080d18; padding: 1 2; }
    #dlg-title { color: #ff3b5c; text-style: bold; margin-bottom: 1; }
    #dlg-body { margin-bottom: 1; }
    #dlg-choices { height: 1; width: auto; }
    .choice { width: auto; color: #5b6b82; }
    .choice:hover { color: #c9d4e5; text-style: bold; }
    #yes { margin-right: 6; }
    #mem-scroll { height: 1fr; }
    #memory { width: 95%; max-width: 96; height: 85%; border: heavy #00e5ff; background: #080d18; padding: 1 2; }
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
        self._layout: tuple[str, bool] | None = None
        self._cells: dict[str, dict[str, dict[str, tuple]]] = {}     # table id -> row key -> column -> text_sig
        self._table_gen: dict[str, int] = {}
        self._shown: dict[str, object] = {}                          # panel id -> what it last displayed
        self._broken: set[str] = set()                               # panels that raised once (reported once)

    # --- layout --------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with VerticalScroll(id="body"):
            with Grid(id="kpis"):
                for i, name in enumerate(("EQUITY", "TOTAL P&L", "TODAY", "DRAWDOWN", "WIN RATE", "BRAIN")):
                    tile = Static(classes="tile", id=f"k{i}")
                    tile.border_title = name
                    yield tile
            yield Static(id="kpiline")
            with Container(id="main"):
                with Vertical(id="left"):
                    yield FitTable(WATCH_COLS, id="watch", classes="panel", cursor_type="none")
                with Vertical(id="center"):
                    yield Static(id="chart", classes="panel")
                    yield RichLog(id="stream", classes="panel", wrap=True, markup=False, highlight=False, max_lines=400)
                with Vertical(id="right"):
                    yield Static(id="risk", classes="panel")
                    yield RichLog(id="fills", classes="panel", wrap=True, markup=False, highlight=False, max_lines=300)
                    yield Static(id="llm", classes="panel")
            yield FitTable(POS_COLS, id="pos", classes="panel", cursor_type="none")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#watch").border_title = "◈ WATCHLIST · LIVE"
        self.query_one("#pos").border_title = "◈ POSITIONS"
        self.query_one("#chart").border_title = "◈ EQUITY CURVE"
        self.query_one("#stream").border_title = "◈ AGENT STREAM · what it thinks and does"
        self.query_one("#risk").border_title = "◈ RISK GOVERNOR"
        self.query_one("#fills").border_title = "◈ EXECUTIONS"
        self.query_one("#llm").border_title = "◈ LLM METER"
        self._apply_layout(*layout_mode(self.size.width, self.size.height))
        self.query_one("#body").focus()                           # arrow keys and PgUp/PgDn scroll a stacked layout
        self.run_worker(self._boot(), name="boot", exit_on_error=False)
        self.set_interval(self.REFRESH_S, self.refresh_ui)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_layout(*layout_mode(event.size.width, event.size.height))

    def _apply_layout(self, mode: str, short: bool) -> None:
        if self._layout == (mode, short):
            return
        self._layout = (mode, short)
        for name in ("wide", "medium", "narrow"):
            self.set_class(name == mode, f"-{name}")
        self.set_class(short, "-short")
        q = self.query_one
        main, body = q("#main"), q("#body")
        self._arrange(body, [q("#kpis"), q("#kpiline")] + ([main, q("#pos")] if mode == "wide" else [q("#pos"), main]))
        self._arrange(main, [q("#left"), q("#center"), q("#right")] if mode != "narrow"
                      else [q("#center"), q("#right"), q("#left")])             # narrow: what it is doing comes first
        self._arrange(q("#center"), [q("#chart"), q("#stream")] if mode != "narrow" else [q("#stream"), q("#chart")])
        self._shown.clear()                                                     # panels re-measure and redraw at the new size

    @staticmethod
    def _arrange(parent, order: list) -> None:
        for i, widget in enumerate(order):
            current = parent.children[i]
            if current is not widget:
                parent.move_child(widget, before=current)

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

        def go(ok: bool | None) -> None:
            if ok:
                self._log_stream("flattening: closing every position at market…", AMBER)
                self.run_worker(self.agent.flatten("manual flatten"), exit_on_error=False)
        self.push_screen(Confirm("FLATTEN ALL POSITIONS",
                                 f"Close all {n} open position(s) at market now.\nTrading continues afterwards."), go)

    def action_kill(self) -> None:
        def go(ok: bool | None) -> None:
            if ok:
                self._log_stream("kill switch: closing every position and halting…", RED)
                self.run_worker(self.agent.kill("manual kill switch"), exit_on_error=False)
        self.push_screen(Confirm("KILL SWITCH",
                                 "Close every position at market and HALT the agent.\n"
                                 "It will not trade again until you press R."), go)

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
                self._log_stream("closing…", DIM)
                try:                                      # a hung connection must not keep the screen from closing
                    await asyncio.wait_for(self.setup.aclose(), QUIT_GRACE_S)
                except Exception:
                    pass
                self.exit()
        self.push_screen(Confirm("QUIT AEON", note + "\nThe agent stops thinking while closed."), done)

    # --- refresh -------------------------------------------------------------
    def refresh_ui(self) -> None:
        """Every half second. Cheap by design (a few ms): each panel is built from data already in memory, only
        redrawn when its content changed, and one panel failing must not take the whole terminal down."""
        self._safe(self._drain_events)
        if not self.agent.account:
            self._safe(self._banner)
            return
        for panel in (self._topbar, self._kpis, self._watchlist, self._positions, self._chart, self._risk, self._llm):
            self._safe(panel)

    def _safe(self, fn) -> None:
        try:
            fn()
        except (NoMatches, ScreenStackError):
            return                                        # the screen is still composing, or already closing: nothing to draw on
        except Exception as e:
            name = fn.__name__
            if name not in self._broken:                  # say so once, in the stream, and keep the other panels alive
                self._broken.add(name)
                try:
                    self._log_stream(f"display error in {name.strip('_')}: {type(e).__name__}: {e}", RED)
                except Exception:
                    pass

    def _put(self, widget_id: str, content, sig=None) -> None:
        """Update a Static only when what it shows would actually change."""
        sig = sig if sig is not None else (text_sig(content) if isinstance(content, Text) else None)
        if sig is not None and self._shown.get(widget_id) == sig:
            return
        self._shown[widget_id] = sig
        self.query_one(f"#{widget_id}", Static).update(content)

    def _banner(self) -> None:
        t = Text()
        t.append("◢◤ AEON ", style=f"bold {AMBER}")
        if self._boot_error:
            t.append(f"  offline: {self._boot_error}", style=f"bold {RED}")
        else:
            t.append("  booting: indexing OKX markets…", style=CYAN)
        self._put("topbar", t)

    def _drain_events(self) -> None:
        for ev in list(self.agent.events):
            if ev.id <= self._last_eid:
                continue
            if ev.kind == "fill":
                m = ev.meta or {}
                value = m.get("trade_pnl") if m.get("trade_pnl") is not None else m.get("pnl", 0.0)
                color = CYAN if m.get("event") in ("open", "add") else pnl_color(value or 0.0)
                self.query_one("#fills", RichLog).write(Text.assemble((utc(ev.ts) + " ", DIM), (ev.text, color)))
            else:
                self._log_event(ev)
            self._last_eid = ev.id                       # only once it is on screen: an early tick must not swallow events

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

    def _width(self, widget_id: str, fallback: int) -> int:
        w = self.query_one(f"#{widget_id}").size.width
        return w if w > 0 else max(fallback, 20)

    def _topbar(self) -> None:
        a, now = self.agent, time.time()
        short = bool(self._layout and self._layout[1])
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
        cycle = f"cycle #{a.cycle_no}" + ("" if a.thinking or a.status != "RUNNING" else f" · next {int(nxt // 60):02d}:{int(nxt % 60):02d}")
        line = fit_line(
            left=[(0, Text("◢◤ AEON", style=f"bold {AMBER}")), (1, Text(f" {a.mode.upper()} ", style=mode_style)),
                  (2, status), (4, Text(cycle, style=DIM)), (9, Text("AUTONOMOUS TRADER", style=DIM))],
            right=[(3, Text(utc(now, "%H:%M:%S") + " UTC", style=TEXT)),
                   (5, Text.assemble((f"feed {feed.transport.upper()} ", DIM), ("●", fcolor), (f" {min(feed.age(), 99):.0f}s", DIM)))],
            width=self._width("topbar", self.size.width - 2))
        self.query_one("#topbar", Static).update(line if short else Group(line, self._tape()))

    def _tape(self) -> Text:
        t, width = Text(no_wrap=True, overflow="crop"), max(self._width("topbar", self.size.width - 2) - 1, 20)
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

    def _tile(self, idx: int, big: str, big_color: str, subs: list[str]) -> None:
        inner = self._width(f"k{idx}", 14)
        t = Text(no_wrap=True, overflow="crop")
        t.append(big[:inner] + "\n", style=f"bold {big_color}")
        t.append(join_fit(subs, inner), style=DIM)                      # a narrow tile keeps the first parts, drops the rest
        self._put(f"k{idx}", t)

    def _kpis(self) -> None:
        a = self.agent
        acct, st, rs = a.account, a.stats or {}, a.risk_status or {}
        total = acct.equity - a.start_equity
        total_pct = total / a.start_equity * 100 if a.start_equity else 0.0
        day_pct = rs.get("day_pnl", 0.0)
        day_usd = acct.equity * day_pct / 100 / (1 + day_pct / 100) if rs else 0.0
        dd = rs.get("drawdown", 0.0)
        wr = st.get("win_rate")
        label = a.brain.label.split("/")[-1][:20]                       # tokens and cost live in the LLM METER panel
        if self._layout and self._layout[1]:                            # short terminal: one line instead of six tiles
            parts = [(0, Text(f"EQUITY {acct.equity:,.2f}", style=f"bold {TEXT}")),
                     (1, Text(f"P&L {total:+,.2f} ({total_pct:+.1f}%)", style=f"bold {pnl_color(total)}")),
                     (2, Text(f"DAY {day_pct:+.2f}%", style=pnl_color(day_pct))),
                     (3, Text(f"DD {dd:.1f}/{a.cfg.risk.max_drawdown_pct:g}%", style=GREEN if dd < 5 else AMBER if dd < 15 else RED)),
                     (5, Text("WIN —" if wr is None else f"WIN {wr:.0f}%", style=DIM if wr is None else GREEN if wr >= 50 else AMBER)),
                     (6, Text(label, style=MAG))]
            self._put("kpiline", fit_line(parts, [], self._width("kpiline", self.size.width - 2), gap=2))
            return
        self._tile(0, f"{acct.equity:,.2f}", TEXT, [f"start {a.start_equity:,.2f}", "USDT"])
        self._tile(1, f"{total:+,.2f} {total_pct:+.1f}%", pnl_color(total),
                   [f"real {st.get('realized', 0.0):+.2f}", f"open {acct.upl:+.2f}"])
        self._tile(2, f"{day_pct:+.2f}%", pnl_color(day_pct),
                   [f"{day_usd:+.2f} USDT", f"floor -{a.cfg.risk.daily_loss_limit_pct:g}%"])
        self._tile(3, f"{dd:.2f}%", GREEN if dd < 5 else AMBER if dd < 15 else RED,
                   [f"peak {rs.get('peak', acct.equity):,.2f}", f"halt {a.cfg.risk.max_drawdown_pct:g}%"])
        self._tile(4, "—" if wr is None else f"{wr:.0f}%", DIM if wr is None else (GREEN if wr >= 50 else AMBER),
                   [f"{st.get('wins', 0)}W / {st.get('losses', 0)}L"] + ([] if st.get("best") is None else [f"best {st['best']:+.2f}"]))
        self._tile(5, label, MAG, ([calls(a.meter['calls'])] + ([f"{a.llm_latency:.1f}s"] if a.llm_latency else []))
                   if a.brain.name != "heuristic" else ["no LLM", "no cost"])

    def _sync(self, table: FitTable, rows: dict[str, dict[str, Text]]) -> None:
        """Make the table show `rows` (row key -> column key -> cell), touching only what changed. The table picks its
        own columns to fit its width; when it rebuilds them it forgets its rows and they are added again here."""
        if not table.shown:
            return
        cache = self._cells.setdefault(table.id, {})
        if self._table_gen.get(table.id) != table.generation:
            self._table_gen[table.id] = table.generation
            cache.clear()
        have = {rk.value for rk in table.rows}
        for key in have - rows.keys():
            table.remove_row(key)
            cache.pop(key, None)
        for key, cells in rows.items():
            sigs = {c: text_sig(cells[c]) for c in table.shown}
            if key in have:
                old = cache.get(key, {})
                for col in table.shown:
                    if old.get(col) != sigs[col]:
                        table.update_cell(key, col, cells[col], update_width=False)
            else:
                table.add_row(*[cells[c] for c in table.shown], key=key)
            cache[key] = sigs

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
            rows[inst] = dict(
                sym=Text(("● " if inst in held else "  ") + inst.split("-")[0], style=f"bold {AMBER}" if inst in held else CYAN),
                kind=Text(s.tag, style=kind_color),
                last=Text(px(live), style=tick_color, justify="right"),
                d24=Text(f"{d24:+.2f}%", style=pnl_color(d24), justify="right"),
                h1=Text(f"{h1:+.2f}%", style=pnl_color(h1), justify="right"),
                rsi=Text(f"{rsi:.0f}", style=rsi_color, justify="right"),
                trend=Text(arrow, style=tcolor),
                fund=carry,
            )
        w = self.query_one("#watch", FitTable)
        self._sync(w, rows)
        sub = f"{len(rows)} of {len(a.market.specs)} markets"
        if w.border_subtitle != sub:
            w.border_subtitle = sub

    def _positions(self) -> None:
        a, rows = self.agent, {}
        for inst, p in a.account.positions.items():
            p.mark = self._live(inst, p.mark or p.entry)
            held = int((time.time() - p.opened_ts) // 60)
            c = pnl_color(p.upl)
            spec = a.market.specs.get(inst)
            rows[inst] = dict(
                sym=Text(inst.split("-")[0], style=f"bold {AMBER}"),
                kind=Text(spec.tag if spec else p.kind, style={"swap": DIM, "futures": MAG, "spot": GREEN}.get(p.kind, DIM)),
                side=Text(p.side.upper(), style=f"bold {GREEN if p.side == 'long' else RED}"),
                lev=Text(f"{p.lever:g}x", justify="right"),
                size=Text(f"{p.notional:,.1f}", justify="right"),
                entry=Text(px(p.entry), justify="right"),
                mark=Text(px(p.mark), justify="right"),
                pnl=Text(f"{p.upl:+.2f}", style=f"bold {c}", justify="right"),
                pnlp=Text(f"{p.upl_pct:+.1f}%", style=c, justify="right"),
                sl=Text(px(p.sl), style=RED, justify="right"),
                tp=Text(px(p.tp), style=GREEN, justify="right"),
                held=Text(f"{held // 60}h{held % 60:02d}" if held >= 60 else f"{held}m", style=DIM, justify="right"),
                why=Text(a.thesis.get(inst, "-")[:160], style=DIM),
            )
        t = self.query_one("#pos", FitTable)
        self._sync(t, rows)
        sub = (f"{len(rows)} open · exposure ${a.account.notional:,.0f} · margin ${a.account.margin_used:,.2f}"
               if rows else "flat · waiting for a setup worth taking")
        if t.border_subtitle != sub:
            t.border_subtitle = sub

    def _chart(self) -> None:
        a, chart = self.agent, self.query_one("#chart", Static)
        series = [v for _, v in a.journal_series()] + [v for ts, v in a.equity_hist if ts > a.journal_series_end()]
        w, h = chart.size.width - 10, chart.size.height - 1            # .size is already inside the border and padding
        if w < 10 or h < 3:
            return
        key = (w, h, tuple(series))
        if self._shown.get("chart") == key:
            return
        self._shown["chart"] = key
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
        text = render_llm_meter(
            a.meter, a.llm24, a.last_llm, priced=cfg.input_price_per_m > 0 or cfg.output_price_per_m > 0,
            equity=a.account.equity if a.account else 0.0, rule_bot=a.brain.name == "heuristic",
            width=max(panel.size.width, 24))
        self._put("llm", text)
        sub = a.brain.label.split("/")[-1][:24]                        # the model, in the corner of the panel
        if panel.border_subtitle != sub:
            panel.border_subtitle = sub

    def _risk(self) -> None:
        a, rs, c = self.agent, self.agent.risk_status, self.agent.cfg.risk
        if not rs:
            return
        inner = max(self.query_one("#risk").size.width, 24)

        def row(label: str, value: float, limit: float, text: str) -> Text:
            t = Text(f"{label:<9} ", style=DIM)
            t.append_text(bar(value, limit, max(4, min(16, inner - 10 - 3 - len(text)))))    # the bar takes what the text leaves
            t.append(f" {text}", style=TEXT)
            return t

        lines = [
            row("DRAWDOWN", rs["drawdown"], rs["dd_limit"], f"{rs['drawdown']:.1f}/{rs['dd_limit']:g}%"),
            row("DAY LOSS", max(-rs["day_pnl"], 0) + 0.0, rs["day_limit"], f"{max(-rs['day_pnl'], 0) + 0.0:.1f}/{rs['day_limit']:g}%"),
            row("EXPOSURE", rs["exposure"], rs["exposure_limit"], f"{rs['exposure']:.2f}/{rs['exposure_limit']:g}x"),
            row("POSITIONS", rs["positions"], rs["positions_limit"], f"{rs['positions']}/{rs['positions_limit']}"),
            row("ENTRIES", rs["trades_today"], rs["trades_limit"], f"{rs['trades_today']}/{rs['trades_limit']}"),
        ]
        out = Text("\n").join(lines)
        out.append("\n")
        if rs["killed"]:
            out.append("\n■ KILL SWITCH TRIPPED\n", style=f"bold {RED}")
            out.append(rs["kill_reason"][:max(inner - 2, 10)], style=RED)
        elif rs["day_blocked"]:
            out.append("\n⚠ NO NEW RISK TODAY\n", style=f"bold {AMBER}")
            out.append("daily loss limit hit", style=AMBER)
        else:
            out.append("\n● GOVERNOR ARMED\n", style=f"bold {GREEN}")
            out.append(f"{c.max_leverage:g}x max · {c.risk_per_trade_pct:g}%/trade · stops {c.min_stop_pct:g}-{c.max_stop_pct:g}%"[:max(inner - 2, 10)], style=DIM)
        self._put("risk", out)
