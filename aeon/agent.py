"""The 24/7 agent: price monitor + decision cycles + execution through the risk engine."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass

from .brain import Context, Decision, LlmError, ParseError
from .broker import Account, BrokerError, Fill, PendingOrder
from .config import Config
from .feed import PriceFeed
from .journal import Journal
from .market import InstSnap, Market
from .okx import OkxError, round_step
from .risk import Proposal, RiskEngine

PRICE_REFRESH_S = 60.0       # older than this: fetch the price from OKX before acting on it
PRICE_MAX_AGE_S = 300.0      # older than this even after a refresh: never add risk on it


@dataclass
class Event:
    id: int
    ts: float
    kind: str      # system | think | action | fill | risk | error
    text: str
    meta: dict | None = None


class Agent:
    def __init__(self, cfg: Config, market: Market, feed: PriceFeed, broker, risk: RiskEngine,
                 brain, journal: Journal, notify=None):
        self.cfg, self.market, self.feed, self.broker = cfg, market, feed, broker
        self.risk, self.brain, self.j = risk, brain, journal
        self.notify = notify                             # optional DiscordNotifier (or anything with post())
        self.lock = asyncio.Lock()                       # one broker operation at a time
        self.events: deque[Event] = deque(maxlen=600)
        self.on_event = None                             # optional callback (headless printing)
        self._eid = 0
        self.mode = cfg.run.mode
        # state read by the UI
        self.started = time.time()
        self.status = "STARTING"                         # STARTING | RUNNING | PAUSED | HALTED
        self.paused = False
        self.thinking = False
        self.account: Account | None = None
        self.snaps: dict[str, InstSnap] = {}
        self.universe: list[str] = []
        self.equity_hist: deque[tuple[float, float]] = deque(maxlen=900)
        self.cycle_no = 0
        self.next_cycle_ts = 0.0
        self.last_view = ""
        self.notes: str = journal.kv_get("notes", "") or ""
        self.meter: dict = journal.kv_get("meter", None) or dict(tin=0, tout=0, cost=0.0, calls=0)
        self.llm24: dict = dict(calls=0, tin=0, tout=0, cost=0.0)      # rolling last 24 hours, from the journal
        self.last_llm: dict | None = None                               # tokens / cost / latency of the latest call
        self.llm_latency = 0.0
        self.llm_failures = 0
        self.start_equity = 0.0
        self.thesis: dict[str, str] = {}                 # why each open position was entered
        self.risk_status: dict = {}
        self.stats: dict = {}
        self._wake = asyncio.Event()
        self._force = False
        self._tasks: list[asyncio.Task] = []
        self._last_equity_log = 0.0
        self._last_stats = 0.0
        self._series: list[tuple[float, float]] = []
        self._series_ts = 0.0
        self._expiry_tried: dict[str, float] = {}
        self._snap_ts = 0.0                              # when the current market snapshot was taken
        self.last_beat = time.time()                     # the monitor loop's last completed pass
        self.on_beat = None                              # optional callback(ts), e.g. write the heartbeat file

    # --- account (with the capital cap) --------------------------------------
    async def _account(self, prices: dict[str, float] | None = None) -> Account:
        """The broker's account. On demo/live, AEON treats at most `run.budget_usdt` (plus what it has made or
        lost) as its capital, however much else sits in the OKX account: a demo account starts with ~100,000 USDT
        and coins, and a real one may hold other money."""
        acct = await self.broker.account(dict(self.feed.prices) if prices is None else prices)
        budget = self.cfg.run.budget_usdt
        if self.mode != "paper" and budget > 0:
            equity = budget + (self.stats.get("realized") or 0.0) + acct.upl
            acct.available = max(min(acct.available, equity - acct.margin_used), 0.0)
            acct.equity = equity
        return acct

    def _save_broker_state(self) -> None:
        self.j.kv_set("paper_state" if self.mode == "paper" else "okx_state", self.broker.dump())

    def _price(self, inst: str) -> tuple[float, float]:
        """The freshest price we have for an instrument and its age in seconds: the live feed, or the market
        snapshot taken at the start of this cycle, whichever is newer."""
        price, age = self.feed.prices.get(inst, 0.0), self.feed.age(inst)
        snap = self.snaps.get(inst)
        if snap and self._snap_ts and time.time() - self._snap_ts < age:
            price, age = snap.last, time.time() - self._snap_ts
        return price, age

    def _name(self, inst: str) -> str:
        """Short display name: SOL for a perpetual, SOL/spot, BTC/fut-mon."""
        spec = self.market.specs.get(inst)
        base = inst.split("-")[0]
        return base if not spec or spec.kind == "swap" else f"{base}/{spec.tag}"

    # --- equity history for the UI -------------------------------------------
    def journal_series(self) -> list[tuple[float, float]]:
        """One equity sample per minute from the journal (refreshed every 30s)."""
        now = time.time()
        if now - self._series_ts > 30:
            self._series, self._series_ts = self.j.equity_series(limit=600), now
        return self._series

    def journal_series_end(self) -> float:
        return self._series[-1][0] if self._series else 0.0

    # --- events --------------------------------------------------------------
    def emit(self, kind: str, text: str, **meta) -> None:
        self._eid += 1
        ev = Event(self._eid, time.time(), kind, text, meta or None)
        self.events.append(ev)
        if self.on_event:
            self.on_event(ev)
        if self.notify:
            self.notify.post(kind, text, ev.meta)

    # --- lifecycle -----------------------------------------------------------
    async def start(self, decisions: bool = True) -> None:
        await self.market.load_specs()
        await self.broker.init()
        if self.notify:
            self.notify.start()                             # Discord alerts (if configured)
            if self.notify.problem:
                self.emit("error", self.notify.problem)
        allowed = getattr(self.broker, "allowed_kinds", None)           # what the OKX account mode can trade
        if allowed is not None:
            self.market.enabled &= set(allowed)
        for msg in getattr(self.broker, "restrictions", []):
            self.emit("risk", msg)
        state = self.j.kv_get("paper_state" if self.mode == "paper" else "okx_state")
        if state:
            self.broker.load(state)
            if self.mode == "paper":
                self.emit("system", f"paper account restored: equity ${state['balance']:.2f} balance, "
                                    f"{len(state['positions'])} open position(s)")
            else:
                self.emit("system", f"restored saved state: {len(state.get('known', {}))} open position(s), "
                                    f"{len(state.get('spot', {}))} spot holding(s), "
                                    f"{len(state.get('desired', {}))} stop(s) on record")
        if hasattr(self.broker, "on_change"):              # save broker state the moment an order is recorded
            self.broker.on_change = self._save_broker_state
        self.stats = self.j.stats()
        self.llm24 = self.j.llm_usage(time.time() - 86400)
        prices = await self._seed_prices()
        self.account = await self._account(prices)
        self.start_equity = self.j.kv_get("start_equity") or 0.0
        if not self.start_equity:
            self.start_equity = self.account.equity
            self.j.kv_set("start_equity", self.start_equity)
        self.thesis = self.j.kv_get("thesis", {}) or {}
        self.risk.update_equity(self.account.equity)
        self.risk_status = self.risk.status(self.account)
        self.feed.watch(set(self.account.positions))
        self.equity_hist.append((time.time(), self.account.equity))
        self.status = "HALTED" if self.risk.killed else "RUNNING"
        counts = {k: sum(1 for s in self.market.specs.values() if s.kind == k) for k in ("swap", "futures", "spot")}
        live = [f"{counts[k]} {name}" for k, name in (("swap", "perps"), ("futures", "dated futures"), ("spot", "spot pairs"))
                if k in self.market.enabled]
        self.emit("system", f"AEON online | {self.mode.upper()} | equity {self.account.equity:.2f} USDT | "
                            f"brain: {self.brain.name} ({self.brain.label}) | indexed {', '.join(live) or 'nothing'}",
                  startup=True, mode=self.mode, equity=self.account.equity,
                  brain=f"{self.brain.name} ({self.brain.label})", indexed=", ".join(live) or "nothing")
        if self.risk.killed:
            self.emit("risk", f"HALTED: {self.risk.kill_reason}. Press R to resume once you have reviewed.")
        self._tasks = [asyncio.create_task(self.feed.run(), name="feed"),
                       asyncio.create_task(self._monitor_loop(), name="monitor")]
        if decisions:
            self._tasks.append(asyncio.create_task(self._decision_loop(), name="decisions"))
        self._beat()

    def _beat(self) -> None:
        self.last_beat = time.time()
        if self.on_beat:
            try:
                self.on_beat(self.last_beat)
            except Exception:                            # a full disk must not take the monitor down
                pass

    def stalled_for(self) -> float:
        """Seconds the monitor loop has been silent; infinite if a background task has died."""
        if any(t.done() for t in self._tasks):
            return float("inf")
        return time.time() - self.last_beat

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _seed_prices(self) -> dict[str, float]:
        try:
            tick = await self.market.tickers()
            return {i: v["last"] for i, v in tick.items()}
        except OkxError:
            return {}

    # --- controls (UI) -------------------------------------------------------
    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.emit("system", "decision cycles PAUSED (open positions keep their stops)" if self.paused
                  else "decision cycles resumed")
        self._refresh_status()

    def cycle_now(self) -> None:
        self._force = True
        self._wake.set()
        self.emit("system", "manual cycle requested")

    async def flatten(self, reason: str = "manual flatten") -> None:
        async with self.lock:
            acct = await self._account()
            for inst in list(acct.positions):
                spec = self.market.specs.get(inst)
                price = self.feed.prices.get(inst) or acct.positions[inst].mark
                if not spec:
                    self.emit("error", f"cannot close {inst}: unknown contract spec")
                    continue
                try:
                    self._record_fill(await self.broker.close(spec, price, reason))
                except (BrokerError, OkxError) as e:
                    self.emit("error", f"flatten {inst} failed: {e}")
            try:                                          # show the result now, not on the next 5-second tick
                self.account = await self._account()
            except OkxError as e:
                self.emit("error", f"account refresh after flatten failed: {e.msg}")

    async def kill(self, reason: str = "manual kill switch") -> None:
        self.risk.trip_kill(reason)
        self.emit("risk", f"KILL SWITCH: {reason}")
        if self.account:
            self.risk_status = self.risk.status(self.account)
        self._refresh_status()
        await self.flatten(reason)

    def resume(self) -> None:
        if self.account:
            self.risk.resume(self.account.equity)
            self.risk_status = self.risk.status(self.account)
        self.emit("system", "trading resumed by owner; drawdown baseline reset to current equity")
        self._refresh_status()
        self._wake.set()

    def _refresh_status(self) -> None:
        self.status = "HALTED" if self.risk.killed else "PAUSED" if self.paused else "RUNNING"

    # --- monitor loop --------------------------------------------------------
    async def _monitor_loop(self) -> None:
        while True:
            try:
                await self._monitor_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:                       # the monitor must never die
                self.emit("error", f"monitor: {type(e).__name__}: {e}")
            self._beat()
            await asyncio.sleep(self.cfg.run.monitor_seconds)

    async def _monitor_once(self) -> None:
        prices = dict(self.feed.prices)
        async with self.lock:
            fills = await self.broker.tick(prices)
            for note in self.broker.drain_notes():
                self.emit("risk", note)
            for f in fills:
                self._record_fill(f)
            try:
                acct = await self._account(prices)
            except OkxError as e:
                self.emit("error", f"account fetch failed: {e.msg}")
                return
            self.account = acct
            self.j.kv_set("paper_state" if self.mode == "paper" else "okx_state", self.broker.dump())
        await self._expiry_watch(acct)
        for msg in self.risk.update_equity(acct.equity):
            self.emit("risk", msg)
        self.risk_status = self.risk.status(acct)
        now = time.time()
        self.equity_hist.append((now, acct.equity))
        if now - self._last_equity_log > 60:
            self.j.add_equity(acct.equity, acct.upl)
            self._last_equity_log = now
        if now - self._last_stats > 5:
            self.stats = self.j.stats()
            self.llm24 = self.j.llm_usage(now - 86400)               # the window slides, so refresh it too
            self._last_stats = now
        if self.risk.killed and acct.positions:
            self.emit("risk", "kill switch active: closing all positions")
            await self.flatten(f"kill switch: {self.risk.kill_reason}")
        self._refresh_status()

    async def _expiry_watch(self, acct: Account) -> None:
        """Dated futures are closed shortly before they expire, never left to settle."""
        limit = self.cfg.risk.futures_close_days_before
        for inst, pos in list(acct.positions.items()):
            spec = self.market.specs.get(inst)
            days = spec.days_left() if spec else None
            if days is None or days >= limit or time.time() - self._expiry_tried.get(inst, 0.0) < 60:
                continue
            self._expiry_tried[inst] = time.time()
            self.emit("risk", f"{self._name(inst)} expires in {max(days, 0) * 24:.1f}h: closing it before settlement")
            async with self.lock:
                try:
                    self._record_fill(await self.broker.close(spec, self.feed.prices.get(inst) or pos.mark,
                                                              "closing before expiry"))
                except (BrokerError, OkxError) as e:
                    self.emit("error", f"closing {self._name(inst)} before expiry failed: {e}")

    # --- decision loop -------------------------------------------------------
    async def _decision_loop(self) -> None:
        self.next_cycle_ts = time.time() + 3
        while True:
            wait = max(self.next_cycle_ts - time.time(), 0.0)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=min(wait, 30.0))
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            forced, self._force = self._force, False
            if not forced and time.time() < self.next_cycle_ts:
                continue
            if self.risk.killed or (self.paused and not forced):
                self.next_cycle_ts = time.time() + 10
                continue
            try:
                await self.cycle()
                self.llm_failures = 0
            except asyncio.CancelledError:
                raise
            except (LlmError, ParseError) as e:
                self.thinking = False
                self.llm_failures += 1
                delay = min(60 * self.llm_failures, 600)
                self.emit("error", f"brain failed ({self.llm_failures}x): {e}. Retrying in {delay}s. "
                                   "Open positions are still protected by their stops.")
                self.next_cycle_ts = time.time() + delay
            except Exception as e:
                self.thinking = False
                self.emit("error", f"cycle crashed: {type(e).__name__}: {e}")
                self.next_cycle_ts = time.time() + 120

    async def _refresh_account(self) -> Account:
        async with self.lock:
            self.account = await self._account()
        return self.account

    async def cycle(self) -> None:
        acct = await self._refresh_account()
        held = set(acct.positions)
        universe, tick = await self.market.pick_universe(acct.equity, held)
        self.universe = universe
        self.feed.watch(set(universe) | held)
        for _ in range(24):                              # give a fresh subscription a moment to deliver ticks
            if self.feed.age() < 30:
                break
            await asyncio.sleep(0.5)
        if self.feed.age() > 90:
            self.emit("risk", f"price feed is stale ({self.feed.transport}); skipping this cycle")
            self.next_cycle_ts = time.time() + 45
            return
        self.snaps = await self.market.snapshot(universe, tick)
        self._snap_ts = time.time()
        self.cycle_no += 1
        n = self.cycle_no
        ctx = Context(now=time.time(), mode=self.mode, cycle_no=n, account=acct, status=self.risk.status(acct),
                      snaps=self.snaps, recent_actions=self.j.recent_actions(10),
                      recent_trades=self.j.recent_trades(8), notes=self.notes, excluded=self.market.excluded,
                      risk=self.cfg.risk)
        self.emit("system", f"cycle #{n}: scanned {len(self.snaps)} markets, thinking...")
        self.thinking = True
        decision = await self.brain.decide(ctx)
        self.thinking = False
        cycle_id = self._book_decision(decision, n)
        for prop in decision.actions:
            await self._handle(prop, cycle_id)
        gap = decision.next_check_min or self.cfg.run.default_cycle_min
        gap = max(self.cfg.run.min_cycle_min, min(self.cfg.run.max_cycle_min, gap))
        self.next_cycle_ts = time.time() + gap * 60
        self.emit("system", f"next check in {gap} min{self._usage_note()}")

    def _usage_note(self) -> str:
        """' | LLM 5,178 in / 404 out, $0.0020 (24h: $0.0412)' for the cycle that just ran, or '' for the rule bot."""
        d = self.last_llm
        if not d or not (d["tin"] or d["tout"]):
            return ""
        return f" | LLM {d['tin']:,} in / {d['tout']:,} out, ${d['cost']:.4f} (24h: ${self.llm24['cost']:.4f})"

    def _book_decision(self, d: Decision, n: int) -> int:
        """Journal the decision and return its database id, which every action it leads to is linked to."""
        self.llm_latency = d.latency
        m = self.meter
        m["tin"] += d.tokens_in
        m["tout"] += d.tokens_out
        m["cost"] += d.cost
        m["calls"] += 1
        self.j.kv_set("meter", m)
        self.last_view = d.view
        if d.notes:
            self.notes = d.notes
            self.j.kv_set("notes", self.notes)
        cycle_id = self.j.add_cycle(self.mode, d.view, d.notes, d.raw, d.tokens_in, d.tokens_out, d.cost, d.latency)
        self.last_llm = dict(tin=d.tokens_in, tout=d.tokens_out, cost=d.cost, latency=d.latency)
        self.llm24 = self.j.llm_usage(time.time() - 86400)
        if d.view:
            self.emit("think", d.view)
        for w in d.warnings:
            self.emit("risk", f"model output: {w}")
        return cycle_id

    # --- execution -----------------------------------------------------------
    def _reject(self, p: Proposal, cycle_id: int | None, why: str) -> None:
        self.emit("risk", f"REJECTED {p.type} {self._name(p.inst)}: {why}")
        self.j.add_action(cycle_id, p.type, p.inst, p.__dict__, "rejected", why)

    async def _handle(self, p: Proposal, cycle_id: int) -> None:
        if p.type == "hold":
            self.emit("think", f"HOLD: {p.reason or 'no edge'}")
            self.j.add_action(cycle_id, "hold", "", p.__dict__, "done", p.reason)
            return
        spec = self.market.specs.get(p.inst)
        price, age = self._price(p.inst)
        if spec and age > PRICE_REFRESH_S:               # the model took a while, or this market is quiet: ask OKX now
            try:
                await self.feed.refresh([p.inst])
            except Exception:
                pass
            price, age = self._price(p.inst)
        if not spec or price <= 0:
            return self._reject(p, cycle_id, "unknown instrument or no live price (use the exact id from the MARKET table)")
        if p.type in ("open", "add") and age > PRICE_MAX_AGE_S:
            return self._reject(p, cycle_id, f"no fresh price (the latest is {age:.0f}s old); not adding risk on a stale price")
        async with self.lock:
            try:
                acct = await self._account()
                pos = acct.positions.get(p.inst)
                extra: dict = {}                    # shown as fields in the Discord fill alert
                if p.type == "open":
                    if spec.kind == "spot" and not p.side:
                        p.side = "long"                      # spot can only be bought
                    v = self.risk.evaluate_open(p, acct, spec, price, set(self.universe))
                    if not v.ok:
                        return self._reject(p, cycle_id, v.why)
                    self._notes(v.notes)
                    v.stop_loss, v.take_profit = self._tick(spec, v.stop_loss), self._tick(spec, v.take_profit)
                    fill = await self.broker.open(spec, p.side, v.contracts, v.leverage, v.stop_loss,
                                                  v.take_profit, price, p.reason)
                    self.thesis[p.inst] = p.reason
                    self.j.kv_set("thesis", self.thesis)
                    self.emit("action", f"OPEN {p.side.upper()} {self._name(p.inst)} "
                                        f"{'spot' if spec.kind == 'spot' else f'{v.leverage:g}x'} | "
                                        f"SL {v.stop_loss:g} TP {v.take_profit if v.take_profit else '-'} | "
                                        f"risk ${v.max_loss:.2f} | conf {p.confidence if p.confidence is not None else '-'} | {p.reason}")
                    extra = dict(sl=v.stop_loss, tp=v.take_profit, leverage=v.leverage,
                                 notional=v.notional, max_loss=v.max_loss)
                elif p.type == "add":
                    v = self.risk.evaluate_add(p, acct, spec, price)
                    if not v.ok:
                        return self._reject(p, cycle_id, v.why)
                    self._notes(v.notes)
                    v.stop_loss, v.take_profit = self._tick(spec, v.stop_loss), self._tick(spec, v.take_profit)
                    fill = await self.broker.add(spec, pos.side, v.contracts, v.stop_loss, v.take_profit, price, p.reason)
                    self.emit("action", f"ADD {self._name(p.inst)} +{v.contracts:g} {'coins' if spec.kind == 'spot' else 'ct'} | {p.reason}")
                    extra = dict(sl=v.stop_loss, tp=v.take_profit, notional=v.notional, leverage=v.leverage)
                elif p.type == "reduce":
                    v = self.risk.evaluate_reduce(p, pos, spec)
                    if not v.ok:
                        return self._reject(p, cycle_id, v.why)
                    self._notes(v.notes)
                    fill = (await self.broker.close(spec, price, p.reason) if pos and v.contracts >= pos.contracts - 1e-12
                            else await self.broker.reduce(spec, v.contracts, price, p.reason))
                    self.emit("action", f"REDUCE {self._name(p.inst)} {p.pct or 50:g}% | {p.reason}")
                    extra = dict(pct=p.pct)
                elif p.type == "close":
                    if not pos:
                        return self._reject(p, cycle_id, "no open position to close")
                    fill = await self.broker.close(spec, price, p.reason)
                    self.emit("action", f"CLOSE {self._name(p.inst)} | {p.reason}")
                elif p.type == "adjust":
                    v = self.risk.evaluate_adjust(p, pos, price)
                    if not v.ok:
                        return self._reject(p, cycle_id, v.why)
                    v.stop_loss, v.take_profit = self._tick(spec, v.stop_loss), self._tick(spec, v.take_profit)
                    await self.broker.set_protection(spec, pos.side, v.stop_loss, v.take_profit)
                    self.emit("action", f"ADJUST {self._name(p.inst)} -> SL {v.stop_loss:g} "
                                        f"TP {v.take_profit if v.take_profit else '-'} | {p.reason}")
                    self.j.add_action(cycle_id, p.type, p.inst, p.__dict__, "done", f"SL {v.stop_loss} TP {v.take_profit}")
                    return
                else:
                    return self._reject(p, cycle_id, "unsupported action")
            except PendingOrder as e:                     # sent, outcome unknown: the monitor books it shortly
                self.emit("risk", f"{p.type.upper()} {self._name(p.inst)}: {e}")
                self.j.add_action(cycle_id, p.type, p.inst, p.__dict__, "pending", str(e))
                if p.type == "open":
                    self.thesis.setdefault(p.inst, p.reason)
                return
            except (BrokerError, OkxError) as e:
                self.emit("error", f"{p.type} {p.inst} failed: {e}")
                self.j.add_action(cycle_id, p.type, p.inst, p.__dict__, "error", str(e))
                return
            self._record_fill(fill, **extra)
            self.j.add_action(cycle_id, p.type, p.inst, p.__dict__, "done", f"{fill.event} @ {fill.price:g}")
            self.account = await self._account()

    @staticmethod
    def _tick(spec, price: float | None) -> float | None:
        return round_step(price, spec.tick_sz) if price else price

    def _notes(self, notes: list[str]) -> None:
        for n in notes:
            self.emit("risk", f"adjusted: {n}")

    def _record_fill(self, f: Fill, **extra) -> None:
        self.j.add_trade(f)
        if f.event == "open" and f.reason and f.inst not in self.thesis:   # e.g. an entry booked by reconciliation
            self.thesis[f.inst] = f.reason
            self.j.kv_set("thesis", self.thesis)
        if f.event in ("open", "add"):
            self.risk.on_trade()
        if f.event == "close":
            self.risk.on_close(f.inst, f.ts)
        spec = self.market.specs.get(f.inst)
        pnl = f"pnl {f.pnl:+.2f}" + (f" (trade {f.trade_pnl:+.2f})" if f.trade_pnl is not None else "")
        self.emit("fill", f"{f.event.upper():<6} {self._name(f.inst):<8} {f.side:<5} {f.contracts:g} @ {f.price:g} | "
                          f"fee {f.fee:.3f} | {pnl}{' | ' + f.reason if f.event in ('close', 'reduce') and f.reason else ''}",
                  event=f.event, pnl=f.pnl, trade_pnl=f.trade_pnl, inst=f.inst, side=f.side, price=f.price,
                  contracts=f.contracts, fee=f.fee, reason=f.reason, est=f.est,
                  spec_kind=(spec.kind if spec else "swap"), **extra)
        if f.event == "close":
            self.thesis.pop(f.inst, None)
