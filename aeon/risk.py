"""Risk engine. Everything here runs in code AFTER the model has decided, so the model can
argue for anything it likes but cannot exceed these limits."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .broker import Account, Position
from .config import RiskCfg
from .journal import Journal
from .market import Spec, size_contracts


@dataclass
class Proposal:
    type: str                          # open | add | reduce | close | adjust | hold
    inst: str = ""
    side: str | None = None            # long | short
    margin_pct: float | None = None    # margin to commit, % of equity
    leverage: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    pct: float | None = None           # reduce: % of the position to close
    reason: str = ""
    confidence: float | None = None


@dataclass
class Verdict:
    ok: bool
    why: str = ""                      # rejection reason when not ok
    notes: list[str] = field(default_factory=list)   # adjustments the engine applied
    contracts: float = 0.0
    leverage: float = 1.0
    notional: float = 0.0
    margin: float = 0.0
    max_loss: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None


def _reject(why: str) -> Verdict:
    return Verdict(ok=False, why=why)


class RiskEngine:
    def __init__(self, cfg: RiskCfg, journal: Journal | None = None):
        self.cfg, self.j = cfg, journal
        self.peak = 0.0
        self.day = ""
        self.day_start = 0.0
        self.trades_today = 0
        self.killed = False
        self.kill_reason = ""
        self.day_blocked = False
        self.last_close: dict[str, float] = {}
        self._load()

    # --- persistence ---------------------------------------------------------
    def _load(self) -> None:
        if not self.j:
            return
        s = self.j.kv_get("risk_state") or {}
        self.peak = s.get("peak", 0.0)
        self.day, self.day_start = s.get("day", ""), s.get("day_start", 0.0)
        self.trades_today = s.get("trades_today", 0)
        self.killed, self.kill_reason = s.get("killed", False), s.get("kill_reason", "")
        self.day_blocked = s.get("day_blocked", False)
        self.last_close = s.get("last_close", {})

    def _save(self) -> None:
        if self.j:
            self.j.kv_set("risk_state", dict(
                peak=self.peak, day=self.day, day_start=self.day_start, trades_today=self.trades_today,
                killed=self.killed, kill_reason=self.kill_reason, day_blocked=self.day_blocked,
                last_close=self.last_close))

    # --- equity watchdog -----------------------------------------------------
    def drawdown_pct(self, equity: float) -> float:
        return max((self.peak - equity) / self.peak * 100.0, 0.0) if self.peak > 0 else 0.0

    def day_pnl_pct(self, equity: float) -> float:
        return (equity - self.day_start) / self.day_start * 100.0 if self.day_start > 0 else 0.0

    def update_equity(self, equity: float, now: float | None = None) -> list[str]:
        """Call on every monitor tick. Returns human-readable events (kill switch, day block)."""
        now = now or time.time()
        events: list[str] = []
        today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if self.peak <= 0:
            self.peak = equity
        if today != self.day:
            self.day, self.day_start, self.trades_today = today, equity, 0
            if self.day_blocked:
                events.append("new UTC day: daily loss block lifted")
            self.day_blocked = False
        self.peak = max(self.peak, equity)
        dd = self.drawdown_pct(equity)
        if not self.killed and dd >= self.cfg.max_drawdown_pct:
            self.killed = True
            self.kill_reason = f"drawdown {dd:.1f}% hit the {self.cfg.max_drawdown_pct:.0f}% limit"
            events.append(f"KILL SWITCH: {self.kill_reason}")
        if not self.day_blocked and self.day_pnl_pct(equity) <= -self.cfg.daily_loss_limit_pct:
            self.day_blocked = True
            events.append(f"daily loss limit reached ({self.day_pnl_pct(equity):.1f}%): no new positions until 00:00 UTC")
        self._save()
        return events

    def trip_kill(self, reason: str) -> None:
        self.killed, self.kill_reason = True, reason
        self._save()

    def resume(self, equity: float) -> None:
        self.killed, self.kill_reason = False, ""
        self.peak = equity            # restart the drawdown clock from here
        self._save()

    def on_trade(self) -> None:
        self.trades_today += 1
        self._save()

    def on_close(self, inst: str, now: float | None = None) -> None:
        self.last_close[inst] = now or time.time()
        self._save()

    def status(self, acct: Account) -> dict:
        c = self.cfg
        return dict(
            killed=self.killed, kill_reason=self.kill_reason, day_blocked=self.day_blocked,
            drawdown=self.drawdown_pct(acct.equity), dd_limit=c.max_drawdown_pct,
            day_pnl=self.day_pnl_pct(acct.equity), day_limit=c.daily_loss_limit_pct,
            exposure=acct.notional / acct.equity if acct.equity else 0.0, exposure_limit=c.max_exposure_x,
            positions=len(acct.positions), positions_limit=c.max_open_positions,
            trades_today=self.trades_today, trades_limit=c.max_trades_per_day,
            peak=self.peak,
        )

    # --- gates ---------------------------------------------------------------
    def gate_new_risk(self, inst: str | None, now: float) -> str | None:
        c = self.cfg
        if self.killed:
            return f"kill switch active ({self.kill_reason})"
        if self.day_blocked:
            return "daily loss limit reached; no new risk until 00:00 UTC"
        if self.trades_today >= c.max_trades_per_day:
            return f"max {c.max_trades_per_day} trades per day reached"
        if inst and now - self.last_close.get(inst, 0.0) < c.reentry_cooldown_min * 60:
            left = c.reentry_cooldown_min - (now - self.last_close[inst]) / 60
            return f"cooldown on {inst}: {left:.0f} more min after the last close"
        return None

    # --- evaluators ----------------------------------------------------------
    def evaluate_open(self, prop: Proposal, acct: Account, spec: Spec, price: float,
                      universe: set[str], now: float | None = None) -> Verdict:
        c, now = self.cfg, now or time.time()
        if prop.inst not in universe:
            return _reject(f"{prop.inst} is not in the current tradable universe")
        if (block := self.gate_new_risk(prop.inst, now)):
            return _reject(block)
        if prop.inst in acct.positions:
            return _reject("already in a position here: use add, adjust, reduce or close")
        if len(acct.positions) >= c.max_open_positions:
            return _reject(f"already at the {c.max_open_positions}-position limit")
        if prop.side not in ("long", "short"):
            return _reject("side must be long or short")
        if spec.kind == "spot" and prop.side != "long":
            return _reject("spot is long-only: to profit from a fall use a perpetual or a dated future")
        if (block := self._expiry_block(spec, now)):
            return _reject(block)
        if c.one_position_per_asset:
            for p in acct.positions.values():
                if p.base == spec.base:
                    return _reject(f"already positioned in {spec.base} via {p.inst}: one position per asset")
        return self._size_new(prop, acct, spec, price, existing=None)

    def _expiry_block(self, spec: Spec, now: float) -> str | None:
        """Dated futures are not opened (or added to) too close to expiry."""
        dl = spec.days_left(now)
        if dl is not None and dl < self.cfg.futures_min_days_to_open:
            return (f"{spec.inst} expires in {dl:.1f} days; dated futures are only opened with at least "
                    f"{self.cfg.futures_min_days_to_open:g} days left")
        return None

    def evaluate_add(self, prop: Proposal, acct: Account, spec: Spec, price: float,
                     now: float | None = None) -> Verdict:
        now = now or time.time()
        pos = acct.positions.get(prop.inst)
        if not pos:
            return _reject("no open position to add to")
        if (block := self.gate_new_risk(None, now)):
            return _reject(block)
        if (block := self._expiry_block(spec, now)):
            return _reject(block)
        if not pos.sl:
            return _reject("position has no stop; adjust the stop first")
        prop = Proposal(**{**prop.__dict__, "side": pos.side,
                           "stop_loss": prop.stop_loss or pos.sl, "take_profit": prop.take_profit or pos.tp})
        v = self._size_new(prop, acct, spec, price, existing=pos)
        if v.ok and self.cfg.stops_only_tighten and not self._tighter_or_equal(pos, v.stop_loss):
            return _reject("adding may not loosen the existing stop")
        return v

    def _size_new(self, prop: Proposal, acct: Account, spec: Spec, price: float,
                  existing: Position | None) -> Verdict:
        c = self.cfg
        long = prop.side == "long"
        sl, tp = prop.stop_loss, prop.take_profit
        if not sl or sl <= 0:
            return _reject("a stop_loss is required on every position")
        if (long and sl >= price) or (not long and sl <= price):
            return _reject(f"stop {sl} is on the wrong side of price {price}")
        d = abs(price - sl) / price * 100.0
        if d < c.min_stop_pct:
            return _reject(f"stop is only {d:.2f}% away; minimum is {c.min_stop_pct}% (it would be hit by noise)")
        if d > c.max_stop_pct:
            return _reject(f"stop is {d:.2f}% away; maximum is {c.max_stop_pct}%")
        if tp and ((long and tp <= price) or (not long and tp >= price)):
            return _reject(f"take_profit {tp} is on the wrong side of price {price}")

        notes: list[str] = []
        eq = acct.equity
        lever = prop.leverage or (existing.lever if existing else 2.0)
        if existing:
            lever = existing.lever
        capped = min(max(lever, 1.0), c.max_leverage, spec.max_lever)     # spot: max_lever is 1
        if capped < lever and prop.leverage and not existing:
            notes.append(f"leverage {lever:g}x -> {capped:g}x" + (" (spot has no leverage)" if spec.kind == "spot" else ""))
        lever = capped

        want_margin_pct = min(prop.margin_pct or 10.0, c.max_margin_pct)
        if (prop.margin_pct or 10.0) > c.max_margin_pct:
            notes.append(f"margin {prop.margin_pct:g}% -> {c.max_margin_pct:g}% of equity")
        want = eq * want_margin_pct / 100.0 * lever

        fee = c.spot_fee_rate if spec.kind == "spot" else c.fee_rate
        loss_per_notional = d / 100.0 + 2.0 * fee
        cur_notional = existing.notional if existing else 0.0
        caps = {
            "risk-per-trade": eq * c.risk_per_trade_pct / 100.0 / loss_per_notional - cur_notional,
            "exposure": c.max_exposure_x * eq - acct.notional,
            "margin-per-position": eq * c.max_margin_pct / 100.0 * lever - cur_notional,
            "available-margin": acct.available / (1.0 / lever + 2.0 * fee) * 0.95,
        }
        binding = min(caps, key=caps.get)
        final = min(want, caps[binding])
        if final <= 0:
            return _reject(f"no room left under the {binding} limit")
        if final < want * 0.999:
            notes.append(f"size clipped by {binding} limit: ${want:,.0f} -> ${final:,.0f} notional")

        contracts = size_contracts(final, price, spec)
        if contracts < spec.min_sz:
            min_notional = spec.min_sz * spec.ct_val * price
            if min_notional <= min(caps.values()) and min_notional <= want * 1.5:
                contracts = spec.min_sz
                notes.append(f"raised to the exchange minimum order (${min_notional:,.2f})")
            else:
                return _reject(f"exchange minimum order is ${min_notional:,.2f}, over your risk limits at a {d:.1f}% stop")
        notional = contracts * spec.ct_val * price
        return Verdict(ok=True, notes=notes, contracts=contracts, leverage=lever, notional=notional,
                       margin=notional / lever, max_loss=(notional + cur_notional) * loss_per_notional,
                       stop_loss=sl, take_profit=tp)

    def _tighter_or_equal(self, pos: Position, new_sl: float | None) -> bool:
        if pos.sl is None or new_sl is None:
            return True
        return new_sl >= pos.sl - 1e-12 if pos.side == "long" else new_sl <= pos.sl + 1e-12

    def evaluate_adjust(self, prop: Proposal, pos: Position | None, price: float) -> Verdict:
        c = self.cfg
        if not pos:
            return _reject("no open position to adjust")
        long = pos.side == "long"
        sl = prop.stop_loss if prop.stop_loss else pos.sl
        tp = prop.take_profit if prop.take_profit else pos.tp
        if prop.stop_loss:
            if (long and sl >= price) or (not long and sl <= price):
                return _reject(f"stop {sl} is on the wrong side of price {price}")
            d = abs(price - sl) / price * 100.0
            if d < c.min_stop_pct:
                return _reject(f"stop is only {d:.2f}% from price; minimum is {c.min_stop_pct}%")
            if d > c.max_stop_pct and pos.sl is None:
                return _reject(f"stop is {d:.2f}% from price; maximum is {c.max_stop_pct}%")
            if c.stops_only_tighten and not self._tighter_or_equal(pos, sl):
                return _reject(f"stops may only move closer ({pos.sl} -> {sl} would loosen it)")
        if prop.take_profit and ((long and tp <= price) or (not long and tp >= price)):
            return _reject(f"take_profit {tp} is on the wrong side of price {price}")
        if sl is None:
            return _reject("a position must always carry a stop")
        return Verdict(ok=True, stop_loss=sl, take_profit=tp)

    def evaluate_reduce(self, prop: Proposal, pos: Position | None, spec: Spec) -> Verdict:
        if not pos:
            return _reject("no open position to reduce")
        pct = min(max(prop.pct or 50.0, 1.0), 100.0)
        if pct >= 99.0:
            return Verdict(ok=True, contracts=pos.contracts, notes=["treated as a full close"])
        contracts = size_contracts(pos.contracts * pct / 100.0 * pos.ct_val * (pos.mark or pos.entry),
                                   pos.mark or pos.entry, spec)
        if contracts < spec.min_sz:
            return _reject("that slice is below the exchange minimum order size")
        if pos.contracts - contracts < spec.min_sz:
            return Verdict(ok=True, contracts=pos.contracts, notes=["remainder would be below minimum size: full close"])
        return Verdict(ok=True, contracts=contracts)
