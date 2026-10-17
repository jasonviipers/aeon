"""Broker interface types + the paper broker (simulated fills on live prices)."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from .market import Spec

MMR = 0.005  # maintenance margin rate used by the paper liquidation model


class BrokerError(Exception):
    pass


@dataclass
class Position:
    inst: str
    side: str                       # long | short
    contracts: float
    ct_val: float
    entry: float
    lever: float
    mark: float = 0.0
    sl: float | None = None
    tp: float | None = None
    opened_ts: float = field(default_factory=time.time)
    fees: float = 0.0               # opening / adding fees paid so far
    realized: float = 0.0           # net pnl banked by partial reductions
    liq: float | None = None
    upl_exch: float | None = None   # exchange-reported figures (OKX broker)
    margin_exch: float | None = None
    kind: str = "swap"              # swap | futures | spot (spot: contracts = coins held, ct_val 1, lever 1, long only)
    expiry: float = 0.0             # dated futures: unix seconds

    @property
    def direction(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def base(self) -> str:
        return self.inst.split("-")[0]

    @property
    def notional(self) -> float:
        return self.contracts * self.ct_val * (self.mark or self.entry)

    @property
    def margin(self) -> float:
        if self.margin_exch is not None:
            return self.margin_exch
        return self.contracts * self.ct_val * self.entry / self.lever

    @property
    def upl(self) -> float:
        if self.upl_exch is not None:
            return self.upl_exch
        return self.direction * ((self.mark or self.entry) - self.entry) * self.contracts * self.ct_val

    @property
    def upl_pct(self) -> float:
        return self.upl / self.margin * 100.0 if self.margin else 0.0

    @property
    def move_pct(self) -> float:
        return self.direction * ((self.mark or self.entry) / self.entry - 1.0) * 100.0 if self.entry else 0.0


@dataclass
class Fill:
    ts: float
    inst: str
    side: str
    event: str                      # open | add | reduce | close
    price: float
    contracts: float
    fee: float = 0.0
    pnl: float = 0.0                # cash effect of this fill, net of its fee
    trade_pnl: float | None = None  # whole-trade net pnl, set on the closing fill
    reason: str = ""
    est: bool = False               # True when the figure is an estimate, not an exchange record


@dataclass
class Account:
    equity: float
    available: float
    balance: float
    positions: dict[str, Position] = field(default_factory=dict)

    @property
    def upl(self) -> float:
        return sum(p.upl for p in self.positions.values())

    @property
    def notional(self) -> float:
        return sum(p.notional for p in self.positions.values())

    @property
    def margin_used(self) -> float:
        return sum(p.margin for p in self.positions.values())


class PaperBroker:
    """Simulated exchange. Fills at live price +/- slippage, charges taker fees, honours
    stop-loss / take-profit / liquidation on every tick. Funding payments are not modelled."""

    mode = "paper"

    def __init__(self, start_equity: float, fee_rate: float, slippage: float, spot_fee_rate: float | None = None):
        self.balance = float(start_equity)
        self.fee_rate, self.slippage = fee_rate, slippage
        self.spot_fee_rate = fee_rate if spot_fee_rate is None else spot_fee_rate
        self.positions: dict[str, Position] = {}
        self.last_error = ""
        self.allowed_kinds = {"swap", "futures", "spot"}
        self.restrictions: list[str] = []

    async def init(self) -> None:
        return None

    def drain_notes(self) -> list[str]:
        return []

    # --- helpers -------------------------------------------------------------
    def _fee(self, kind: str) -> float:
        return self.spot_fee_rate if kind == "spot" else self.fee_rate

    def _slip(self, price: float, buying: bool) -> float:
        return price * (1 + self.slippage) if buying else price * (1 - self.slippage)

    def _equity(self) -> float:
        return self.balance + sum(p.upl for p in self.positions.values())

    def _available(self) -> float:
        return self._equity() - sum(p.margin for p in self.positions.values())

    @staticmethod
    def _liq(p: Position) -> float:
        return p.entry * (1.0 - p.direction * (1.0 / p.lever - MMR))

    # --- interface -----------------------------------------------------------
    async def account(self, prices: dict[str, float]) -> Account:
        for p in self.positions.values():
            p.mark = prices.get(p.inst, p.mark or p.entry)
        return Account(equity=self._equity(), available=max(self._available(), 0.0),
                       balance=self.balance, positions=dict(self.positions))

    async def open(self, spec: Spec, side: str, contracts: float, lever: float, sl: float | None,
                   tp: float | None, price: float, reason: str) -> Fill:
        if spec.inst in self.positions:
            raise BrokerError(f"{spec.inst}: position already open")
        if spec.kind == "spot":
            if side != "long":
                raise BrokerError("spot cannot be sold short")
            lever = 1.0
        px = self._slip(price, side == "long")
        notional = contracts * spec.ct_val * px
        margin, fee = notional / lever, notional * self._fee(spec.kind)
        if margin + fee > self._available():
            raise BrokerError("insufficient available margin")
        self.balance -= fee
        pos = Position(inst=spec.inst, side=side, contracts=contracts, ct_val=spec.ct_val, entry=px,
                       lever=lever, mark=px, sl=sl, tp=tp, fees=fee, kind=spec.kind, expiry=spec.expiry_ts)
        pos.liq = None if spec.kind == "spot" else self._liq(pos)      # spot cannot be liquidated
        self.positions[spec.inst] = pos
        return Fill(time.time(), spec.inst, side, "open", px, contracts, fee, -fee, None, reason)

    async def add(self, spec: Spec, side: str, contracts: float, sl: float | None, tp: float | None,
                  price: float, reason: str) -> Fill:
        pos = self.positions.get(spec.inst)
        if not pos:
            raise BrokerError(f"{spec.inst}: no position to add to")
        px = self._slip(price, pos.side == "long")
        notional = contracts * spec.ct_val * px
        margin, fee = notional / pos.lever, notional * self._fee(pos.kind)
        if margin + fee > self._available():
            raise BrokerError("insufficient available margin")
        self.balance -= fee
        total = pos.contracts + contracts
        pos.entry = (pos.entry * pos.contracts + px * contracts) / total
        pos.contracts, pos.fees = total, pos.fees + fee
        pos.sl, pos.tp = sl, tp
        pos.liq = None if pos.kind == "spot" else self._liq(pos)
        return Fill(time.time(), spec.inst, pos.side, "add", px, contracts, fee, -fee, None, reason)

    def _reduce(self, inst: str, contracts: float, price: float, reason: str) -> Fill:
        pos = self.positions.get(inst)
        if not pos:
            raise BrokerError(f"{inst}: no position")
        contracts = min(contracts, pos.contracts)
        px = self._slip(price, pos.side == "short")      # selling closes a long, buying closes a short
        gross = pos.direction * (px - pos.entry) * contracts * pos.ct_val
        fee = contracts * pos.ct_val * px * self._fee(pos.kind)
        self.balance += gross - fee
        if contracts >= pos.contracts - 1e-12:
            trade_pnl = pos.realized + gross - fee - pos.fees
            del self.positions[inst]
            return Fill(time.time(), inst, pos.side, "close", px, contracts, fee, gross - fee, trade_pnl, reason)
        open_fee_part = pos.fees * contracts / pos.contracts     # opening fee belonging to the sold slice
        pos.realized += gross - fee - open_fee_part
        pos.fees -= open_fee_part
        pos.contracts -= contracts
        return Fill(time.time(), inst, pos.side, "reduce", px, contracts, fee, gross - fee, None, reason)

    async def reduce(self, spec: Spec, contracts: float, price: float, reason: str) -> Fill:
        return self._reduce(spec.inst, contracts, price, reason)

    async def close(self, spec: Spec, price: float, reason: str) -> Fill:
        pos = self.positions.get(spec.inst)
        if not pos:
            raise BrokerError(f"{spec.inst}: no position")
        return self._reduce(spec.inst, pos.contracts, price, reason)

    async def set_protection(self, spec: Spec, side: str, sl: float | None, tp: float | None) -> None:
        pos = self.positions.get(spec.inst)
        if not pos:
            raise BrokerError(f"{spec.inst}: no position")
        pos.sl, pos.tp = sl, tp

    async def tick(self, prices: dict[str, float]) -> list[Fill]:
        """Apply expiry settlement / liquidation / stop-loss / take-profit triggers at the latest prices."""
        fills: list[Fill] = []
        now = time.time()
        for inst, pos in list(self.positions.items()):
            px = prices.get(inst)
            if not px:
                continue
            pos.mark = px
            long = pos.side == "long"
            reason = None
            if pos.expiry and now >= pos.expiry:
                reason = "expired"            # the agent normally closes well before this
            elif pos.liq and ((long and px <= pos.liq) or (not long and px >= pos.liq)):
                reason = "liquidated"
            elif pos.sl and ((long and px <= pos.sl) or (not long and px >= pos.sl)):
                reason = "stop_loss"
            elif pos.tp and ((long and px >= pos.tp) or (not long and px <= pos.tp)):
                reason = "take_profit"
            if reason:
                fill_px = pos.liq if reason == "liquidated" else px
                fills.append(self._reduce(inst, pos.contracts, fill_px, reason))
        return fills

    # --- persistence (so a restart keeps the paper account) ------------------
    def dump(self) -> dict:
        return {"balance": self.balance, "positions": [asdict(p) for p in self.positions.values()]}

    def load(self, data: dict) -> None:
        self.balance = float(data["balance"])
        self.positions = {p["inst"]: Position(**p) for p in data.get("positions", [])}
