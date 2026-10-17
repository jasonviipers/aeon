"""Broker for a real OKX account (demo-trading or live). Same interface as PaperBroker.

Markets
  * perpetuals and dated futures: isolated margin, leverage, a market entry with the stop-loss
    (and optional take-profit) attached, so protection lives on OKX's servers from the first moment;
  * spot: long-only cash trading. A spot buy cannot carry an attached stop (the fee is taken in the coin
    you buy, so the amount to protect is only known after the fill), so the stop order is placed
    straight after the buy, and the monitor below re-arms it if it is ever missing.

How it keeps its books straight
  * every order is written down (and saved) BEFORE it is sent, under a client id AEON chooses. If OKX's
    reply is lost or the fill can't be confirmed, the order is looked up by that id on the next monitor
    pass and whatever really happened is booked: nothing is silently forgotten;
  * spot holdings are AEON's own fills, never "whatever the wallet holds". Coins you already had are never
    counted, protected or sold. When AEON's own stop or target fires, it learns that from the stop order
    itself (OKX reports which order it placed), so the sale is booked even if you hold more of the coin;
  * every fill updates the positions immediately, so the next decision in the same cycle sees it;
  * a trade that executed is always booked, even if re-placing its stop then fails: the failure is
    reported separately, the wanted stop stays on record, and the monitor keeps trying.

Safety design
  * tick() re-checks that every position still has a stop and re-arms it if not; if a stop cannot be
    restored within a minute the position is closed (fail-safe);
  * a kind the account mode cannot trade (e.g. perpetuals in OKX "Spot mode") is switched off with a
    clear message instead of failing on every order.

Run it against OKX *demo trading* first: the request shapes follow OKX's v5 docs, but demo is
where you confirm they behave on your account before real money is involved.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import asdict

from .broker import Account, BrokerError, Fill, PendingOrder, Position, check_levels
from .market import INST_TYPE, Spec
from .okx import OkxClient, OkxError, fmt, px_str

DERIV_TD_MODE = "isolated"
SPOT_TD_MODE = "cash"
NOT_FOUND_CODES = {"51603"}       # "order does not exist"
GIVE_UP_LOOKUP_S = 60.0           # an order OKX still doesn't know after this long never reached it


def _f(x, default: float | None = 0.0) -> float | None:
    try:
        return float(x) if x not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _floor_lot(qty: float, lot: float) -> float:
    return round(math.floor(qty / lot + 1e-9) * lot, 10) if lot > 0 else qty


class _NothingFilled(BrokerError):
    """The exchange canceled the order before any of it filled: a definitive 'nothing happened'."""


class OkxBroker:
    mode = "okx"

    def __init__(self, okx: OkxClient, specs: dict[str, Spec], label: str = "demo", kinds: set[str] | None = None):
        self.okx, self.specs, self.label = okx, specs, label
        self.configured_kinds = set(kinds) if kinds else {"swap", "futures", "spot"}
        self.allowed_kinds = set(self.configured_kinds)
        self.restrictions: list[str] = []
        self.pos_mode = "net_mode"
        self.config_row: dict = {}
        self.last_error = ""
        self.on_change = None                  # called when state worth saving changes; the agent persists it
        self._known: dict[str, Position] = {}
        self._desired: dict[str, tuple[float | None, float | None]] = {}
        self._unprotected_since: dict[str, float] = {}
        self._spot: dict[str, dict] = {}       # spot holdings AEON bought: inst -> qty, entry, fees, realized, opened_ts, since
        self._pending: dict[str, dict] = {}    # clOrdId -> an order sent but not confirmed yet
        self._algo_ids: dict[str, list[str]] = {}   # inst -> AEON's own protective orders
        self._spot_waiting: set[str] = set()
        self._bal: dict[str, float] = {}       # coin -> total balance, refreshed on every tick
        self._notes: list[str] = []
        self._seq = 0

    # --- setup ---------------------------------------------------------------
    async def init(self) -> None:
        cfg = await self.okx.account_config()
        row = cfg[0] if cfg else {}
        self.config_row = row
        self.pos_mode = row.get("posMode", "net_mode")
        if str(row.get("acctLv")) == "1":                    # "Spot mode": spot only
            lost = sorted(self.configured_kinds & {"swap", "futures"})
            self.allowed_kinds = self.configured_kinds - {"swap", "futures"}
            if lost:
                self.restrictions.append(
                    f"OKX account is in Spot mode, so {' and '.join(lost)} trading is switched off and AEON will trade "
                    "spot only. To enable perpetuals and dated futures, switch the OKX account mode to 'Spot and "
                    "futures mode' (or Multi-currency margin) and restart AEON.")
            if not self.allowed_kinds:
                raise BrokerError("OKX account is in Spot mode, which cannot trade the configured markets "
                                  f"({', '.join(sorted(self.configured_kinds))}). Switch the account mode to "
                                  "'Spot and futures mode' (or Multi-currency margin).")

    def drain_notes(self) -> list[str]:
        notes, self._notes = self._notes, []
        return notes

    # --- persistence (so a restart keeps stops, spot holdings, pending orders and the trade ledger) ---
    def dump(self) -> dict:
        return {"desired": {k: list(v) for k, v in self._desired.items()}, "spot": self._spot,
                "known": {k: asdict(p) for k, p in self._known.items()},
                "pending": self._pending, "algo_ids": self._algo_ids}

    def load(self, data: dict) -> None:
        self._desired = {k: (v[0], v[1]) for k, v in (data.get("desired") or {}).items()}
        self._spot = dict(data.get("spot") or {})
        self._known = {k: Position(**v) for k, v in (data.get("known") or {}).items()}
        self._pending = dict(data.get("pending") or {})
        self._algo_ids = {k: list(v) for k, v in (data.get("algo_ids") or {}).items()}

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    # --- small helpers -------------------------------------------------------
    def _cl_id(self) -> str:
        self._seq += 1
        return f"aeon{int(time.time() * 1000)}{self._seq}"[:32]

    def _pos_side(self, side: str) -> str | None:
        return side if self.pos_mode == "long_short_mode" else None

    @staticmethod
    def _fee_usdt(info: dict, spec: Spec) -> float:
        """Fees come back as positive numbers in the coin they were charged in; spot buys pay in the coin bought."""
        return info["fee"] * info["px"] if info["fee_ccy"] == spec.base else info["fee"]

    def _sellable(self, spec: Spec) -> float:
        """What AEON may sell: its own coins, and never more than the wallet actually holds."""
        tracked = self._spot.get(spec.inst, {}).get("qty", 0.0)
        return _floor_lot(min(tracked, self._bal.get(spec.base, 0.0)), spec.lot_sz)

    def _spot_position(self, inst: str, mark: float | None = None) -> Position | None:
        s = self._spot.get(inst)
        if not s:
            return None
        sl, tp = self._desired.get(inst, (None, None))
        return Position(inst=inst, side="long", contracts=s["qty"], ct_val=1.0, entry=s["entry"], lever=1.0,
                        mark=mark or s["entry"], sl=sl, tp=tp, opened_ts=s["opened_ts"], fees=s["fees"],
                        realized=s["realized"], kind="spot")

    def _forget(self, inst: str) -> None:
        for d in (self._spot, self._known, self._desired, self._unprotected_since, self._algo_ids):
            d.pop(inst, None)

    # --- reading state -------------------------------------------------------
    async def _refresh_balances(self) -> dict:
        bal = await self.okx.balance()
        row = bal[0] if bal else {}
        self._bal = {}
        for d in row.get("details", []):
            v = _f(d.get("cashBal"), None)
            self._bal[d.get("ccy", "")] = v if v is not None else _f(d.get("eq"))
        return row

    async def _safe_refresh_balances(self) -> None:
        try:
            await self._refresh_balances()
        except OkxError:
            pass

    def _parse_position(self, r: dict) -> Position | None:
        pos = _f(r.get("pos"))
        if not pos:
            return None
        ps = r.get("posSide", "net")
        side = ps if ps in ("long", "short") else ("long" if pos > 0 else "short")
        inst = r["instId"]
        spec = self.specs.get(inst)
        entry = _f(r.get("avgPx"))
        return Position(
            inst=inst, side=side, contracts=abs(pos), ct_val=spec.ct_val if spec else 1.0, entry=entry,
            lever=_f(r.get("lever"), 1.0) or 1.0, mark=_f(r.get("markPx"), entry) or entry,
            liq=_f(r.get("liqPx"), None) or None, upl_exch=_f(r.get("upl")),
            margin_exch=_f(r.get("imr"), None) or _f(r.get("margin"), None),
            opened_ts=(_f(r.get("cTime")) or 0) / 1000.0 or time.time(),
            kind=spec.kind if spec else "swap", expiry=spec.expiry_ts if spec else 0.0,
        )

    async def _fetch_positions(self) -> dict[str, Position]:
        out: dict[str, Position] = {}
        for kind in ("swap", "futures"):
            if kind not in self.allowed_kinds:
                continue
            for r in await self.okx.positions(INST_TYPE[kind]):
                p = self._parse_position(r)
                if p:
                    out[p.inst] = p
        return out

    async def _algos(self) -> dict[str, dict]:
        """Pending protective orders grouped by instrument: {inst: {sl, tp, ids}}."""
        out: dict[str, dict] = {}
        for ord_type in ("oco", "conditional"):
            for r in await self.okx.algos_pending(ord_type):
                d = out.setdefault(r["instId"], {"sl": None, "tp": None, "ids": []})
                d["ids"].append(r["algoId"])
                d["sl"] = _f(r.get("slTriggerPx"), None) or d["sl"]
                d["tp"] = _f(r.get("tpTriggerPx"), None) or d["tp"]
        return out

    async def account(self, prices: dict[str, float]) -> Account:
        row = await self._refresh_balances()
        equity = _f(row.get("totalEq"))
        avail = None
        for d in row.get("details", []):
            if d.get("ccy") == "USDT":
                avail = _f(d.get("availEq"), None) or _f(d.get("availBal"), None)
        if avail is None:
            avail = _f(row.get("adjEq"), equity)
        positions = dict(self._known)
        for p in positions.values():
            p.mark = prices.get(p.inst, p.mark)
        return Account(equity=equity, available=avail or 0.0,
                       balance=equity - sum(p.upl for p in positions.values()), positions=positions)

    # --- sending orders --------------------------------------------------------
    @staticmethod
    def _fill_info(o: dict | None) -> dict | None:
        """A finished order's fill ({px, sz, fee, fee_ccy}), or None while it is working or if nothing filled."""
        if not o:
            return None
        filled = _f(o.get("accFillSz"))
        if o.get("state") == "filled" or (o.get("state") in ("canceled", "mmp_canceled") and filled > 0):
            return {"px": _f(o.get("avgPx")), "sz": filled, "fee": abs(_f(o.get("fee"))), "fee_ccy": o.get("feeCcy") or ""}
        return None

    async def _await_fill(self, inst: str, ord_id: str) -> dict:
        last = "still working"
        for _ in range(8):
            await asyncio.sleep(0.4)
            try:
                o = await self.okx.get_order(inst, ord_id)
            except OkxError as e:                        # a failed lookup is not a failed order: keep asking
                last = e.msg or str(e)
                continue
            info = self._fill_info(o)
            if info is not None:
                return info
            if o and o.get("state") in ("canceled", "mmp_canceled"):
                raise _NothingFilled(f"{inst}: the exchange canceled the order before anything filled")
        raise BrokerError(f"{inst}: order {ord_id} not confirmed filled yet ({last})")

    async def _submit(self, spec: Spec, body: dict, intent: dict) -> dict:
        """Send one market order and wait for its fill. The intent is recorded and saved BEFORE sending, so a
        lost reply or an unconfirmed fill becomes a PendingOrder that tick() resolves by looking the order up."""
        cl = self._cl_id()
        body["clOrdId"] = cl
        self._pending[cl] = {**intent, "inst": spec.inst, "kind": spec.kind, "sz": float(body["sz"]),
                             "ts": time.time(), "ord_id": None}
        self._changed()
        try:
            rows = await self.okx.place_order(body)
        except OkxError as e:
            if e.code == "net":                          # no answer at all: OKX may or may not have the order
                raise PendingOrder(f"{spec.inst}: no reply from OKX after sending the order; "
                                   "AEON will look it up and book whatever happened") from e
            self._drop_pending(cl)                       # a definitive rejection: nothing happened
            raise
        self._pending[cl]["ord_id"] = rows[0].get("ordId")
        self._changed()
        try:
            info = await self._await_fill(spec.inst, rows[0]["ordId"])
        except _NothingFilled:
            self._drop_pending(cl)
            raise
        except (OkxError, BrokerError) as e:
            raise PendingOrder(f"{spec.inst}: order sent but its fill is not confirmed yet ({e}); "
                               "AEON will look it up and book it") from e
        self._drop_pending(cl)
        return info

    def _drop_pending(self, cl: str) -> None:
        if self._pending.pop(cl, None) is not None:
            self._changed()

    def _attach(self, spec: Spec, sl: float | None, tp: float | None) -> list[dict]:
        a: dict = {}
        if sl:
            a.update(slTriggerPx=px_str(sl, spec.tick_sz), slOrdPx="-1", slTriggerPxType="last")
        if tp:
            a.update(tpTriggerPx=px_str(tp, spec.tick_sz), tpOrdPx="-1", tpTriggerPxType="last")
        return [a] if a else []

    # --- booking a fill (shared by the immediate path and by reconciliation) ----
    async def _book(self, spec: Spec, p: dict, info: dict) -> Fill:
        if not info["px"]:
            info = {**info, "px": p.get("price") or 0.0}
        purpose = p["purpose"]
        if spec.kind == "spot":
            if purpose in ("open", "add"):
                return await self._book_spot_buy(spec, p, info, opening=purpose == "open")
            return await self._book_spot_sale(spec, info, p.get("reason", ""))
        if purpose == "open":
            return self._book_deriv_open(spec, p, info)
        if purpose == "add":
            return await self._book_deriv_add(spec, p, info)
        return await self._book_deriv_reduce(spec, p, info)

    async def _protect(self, spec: Spec, side: str, sl: float | None, tp: float | None) -> None:
        """Re-cover a position after it changed. A failure never hides the trade that just happened: it is
        reported, the wanted stop stays on record, and the monitor keeps trying (and closes after 60s)."""
        self._desired[spec.inst] = (sl, tp)
        try:
            await self.set_protection(spec, side, sl, tp)
        except (OkxError, BrokerError) as e:
            self._unprotected_since.setdefault(spec.inst, time.time())
            self._notes.append(f"{spec.inst}: UNPROTECTED: the stop order could not be placed ({e}); retrying")

    def _book_deriv_open(self, spec: Spec, p: dict, info: dict) -> Fill:
        inst, side, px, sz = spec.inst, p["side"], info["px"], info["sz"]
        fee = self._fee_usdt(info, spec)
        sl, tp = p.get("sl"), p.get("tp")
        self._desired[inst] = (sl, tp)
        # provisional: the next monitor pass replaces it with OKX's own figures
        self._known[inst] = Position(inst=inst, side=side, contracts=sz, ct_val=spec.ct_val, entry=px,
                                     lever=p.get("lever") or 1.0, mark=px, sl=sl, tp=tp, fees=fee,
                                     kind=spec.kind, expiry=spec.expiry_ts)
        self._changed()
        return Fill(time.time(), inst, side, "open", px, sz, fee, -fee, None, p.get("reason", ""))

    async def _book_deriv_add(self, spec: Spec, p: dict, info: dict) -> Fill:
        inst, side, px, sz = spec.inst, p["side"], info["px"], info["sz"]
        fee = self._fee_usdt(info, spec)
        pos = self._known.get(inst)
        if pos:
            total = pos.contracts + sz
            pos.entry = (pos.entry * pos.contracts + px * sz) / total
            pos.contracts, pos.fees = total, pos.fees + fee
            pos.upl_exch = pos.margin_exch = None              # stale until OKX reports again
        self._changed()
        await self._protect(spec, side, p.get("sl"), p.get("tp"))   # re-cover the enlarged position
        return Fill(time.time(), inst, side, "add", px, sz, fee, -fee, None, p.get("reason", ""))

    async def _book_deriv_reduce(self, spec: Spec, p: dict, info: dict) -> Fill:
        inst, side, px, sz = spec.inst, p["side"], info["px"], info["sz"]
        fee = self._fee_usdt(info, spec)
        pos = self._known.get(inst)
        entry = pos.entry if pos else p.get("entry", px)
        gross = (1 if side == "long" else -1) * (px - entry) * sz * spec.ct_val
        if pos:
            pos.contracts = max(pos.contracts - sz, 0.0)
            pos.upl_exch = pos.margin_exch = None
            if pos.contracts < spec.min_sz:
                self._known.pop(inst, None)
        self._changed()
        sl, tp = self._desired.get(inst, (None, None))
        if inst in self._known and (sl or tp):
            await self._protect(spec, side, sl, tp)             # the stop now covers the smaller position
        return Fill(time.time(), inst, side, "reduce", px, sz, fee, gross - fee, None, p.get("reason", ""), est=True)

    async def _book_spot_buy(self, spec: Spec, p: dict, info: dict, opening: bool) -> Fill:
        inst, px, sz = spec.inst, info["px"], info["sz"]
        fee = self._fee_usdt(info, spec)
        got = sz - (info["fee"] if info["fee_ccy"] == spec.base else 0.0)       # the buy fee is taken in the coin
        now = time.time()
        s = self._spot.get(inst)
        if opening or not s:
            self._spot[inst] = {"qty": got, "entry": px, "fees": fee, "realized": 0.0, "opened_ts": now, "since": now}
        else:
            s["entry"] = (s["entry"] * s["qty"] + px * got) / (s["qty"] + got)
            s["qty"] += got
            s["fees"] += fee
            s["since"] = now
        self._known[inst] = self._spot_position(inst, px)
        self._changed()
        await self._safe_refresh_balances()
        await self._protect(spec, "long", p.get("sl"), p.get("tp"))
        return Fill(now, inst, "long", "open" if opening or not s else "add", px, sz, fee, -fee, None, p.get("reason", ""))

    async def _book_spot_sale(self, spec: Spec, info: dict, reason: str) -> Fill:
        inst, px, sz = spec.inst, info["px"], info["sz"]
        fee = self._fee_usdt(info, spec)
        now = time.time()
        s = self._spot.get(inst)
        if not s:                                               # not tracked any more: book what we can
            return Fill(now, inst, "long", "close", px, sz, fee, -fee, None, reason, est=True)
        gross = (px - s["entry"]) * sz
        if s["qty"] - sz < spec.min_sz:                         # everything (or all but dust) is gone
            trade_pnl = s["realized"] + gross - fee - s["fees"]
            self._forget(inst)
            self._changed()
            return Fill(now, inst, "long", "close", px, sz, fee, gross - fee, trade_pnl, reason)
        open_fee_part = s["fees"] * sz / s["qty"]
        s["realized"] += gross - fee - open_fee_part
        s["fees"] -= open_fee_part
        s["qty"] -= sz
        s["since"] = now
        self._known[inst] = self._spot_position(inst, px)
        self._changed()
        await self._safe_refresh_balances()
        sl, tp = self._desired.get(inst, (None, None))
        if sl or tp:
            await self._protect(spec, "long", sl, tp)          # the stop now covers the smaller holding
        return Fill(now, inst, "long", "reduce", px, sz, fee, gross - fee, None, reason)

    # --- orders: open / add / reduce / close ---------------------------------
    async def open(self, spec: Spec, side: str, contracts: float, lever: float, sl: float | None,
                   tp: float | None, price: float, reason: str) -> Fill:
        check_levels(sl, tp)
        inst = spec.inst
        intent = {"purpose": "open", "side": side, "sl": sl, "tp": tp, "lever": lever, "reason": reason, "price": price}
        if spec.kind == "spot":
            if side != "long":
                raise BrokerError("spot cannot be sold short")
            if inst in self._spot:
                raise BrokerError(f"{inst}: already holding it")
            body = {"instId": inst, "tdMode": SPOT_TD_MODE, "side": "buy", "ordType": "market",
                    "sz": fmt(contracts), "tgtCcy": "base_ccy"}                 # size in coins, not USDT
        else:
            ps = self._pos_side(side)
            await self.okx.set_leverage(inst, lever, DERIV_TD_MODE, ps)
            body = {"instId": inst, "tdMode": DERIV_TD_MODE, "side": "buy" if side == "long" else "sell",
                    "ordType": "market", "sz": fmt(contracts)}
            if ps:
                body["posSide"] = ps
            if sl or tp:
                body["attachAlgoOrds"] = self._attach(spec, sl, tp)
        self._desired[inst] = (sl, tp)                 # on record before sending: a lost reply still gets its stop
        try:
            info = await self._submit(spec, body, intent)
        except PendingOrder:
            raise
        except Exception:
            self._desired.pop(inst, None)              # definitively nothing happened
            raise
        return await self._book(spec, intent, info)

    async def add(self, spec: Spec, side: str, contracts: float, sl: float | None, tp: float | None,
                  price: float, reason: str) -> Fill:
        check_levels(sl, tp)
        inst = spec.inst
        intent = {"purpose": "add", "side": side, "sl": sl, "tp": tp, "reason": reason, "price": price}
        if spec.kind == "spot":
            if inst not in self._spot:
                raise BrokerError(f"{inst}: no spot position to add to")
            body = {"instId": inst, "tdMode": SPOT_TD_MODE, "side": "buy", "ordType": "market",
                    "sz": fmt(contracts), "tgtCcy": "base_ccy"}
        else:
            body = {"instId": inst, "tdMode": DERIV_TD_MODE, "side": "buy" if side == "long" else "sell",
                    "ordType": "market", "sz": fmt(contracts)}
            if self._pos_side(side):
                body["posSide"] = side
        info = await self._submit(spec, body, intent)
        return await self._book(spec, intent, info)

    async def reduce(self, spec: Spec, contracts: float, price: float, reason: str) -> Fill:
        if spec.kind == "spot":
            return await self._sell_spot(spec, contracts, price, reason)
        pos = self._known.get(spec.inst) or (await self._fetch_positions()).get(spec.inst)
        if not pos:
            raise BrokerError(f"{spec.inst}: no position")
        body = {"instId": spec.inst, "tdMode": DERIV_TD_MODE, "side": "sell" if pos.side == "long" else "buy",
                "ordType": "market", "sz": fmt(contracts)}
        if self._pos_side(pos.side):
            body["posSide"] = pos.side
        else:
            body["reduceOnly"] = True
        intent = {"purpose": "reduce", "side": pos.side, "entry": pos.entry, "reason": reason, "price": price}
        info = await self._submit(spec, body, intent)
        return await self._book(spec, intent, info)

    async def close(self, spec: Spec, price: float, reason: str) -> Fill:
        if spec.kind == "spot":
            return await self._sell_spot(spec, None, price, reason)
        inst = spec.inst
        pos = (await self._fetch_positions()).get(inst)
        if not pos:
            raise BrokerError(f"{inst}: no position")
        started = time.time()
        body = {"instId": inst, "mgnMode": DERIV_TD_MODE, "autoCxl": True}
        if self._pos_side(pos.side):
            body["posSide"] = pos.side
        await self.okx.close_position(body)
        self._forget(inst)
        self._changed()
        await self._cancel_algos(inst)
        return await self._closing_fill(pos, started, reason, price)

    async def _sell_spot(self, spec: Spec, contracts: float | None, price: float, reason: str) -> Fill:
        inst = spec.inst
        if inst not in self._spot:
            raise BrokerError(f"{inst}: AEON holds no spot position here (it only sells what it bought)")
        await self._safe_refresh_balances()
        have = self._sellable(spec)
        qty = have if contracts is None else min(_floor_lot(contracts, spec.lot_sz), have)
        if qty < spec.min_sz:
            self._forget(inst)
            self._changed()
            raise BrokerError(f"{inst}: balance {have:g} is below the minimum sell size, so the position was dropped from tracking")
        await self._cancel_algos(inst, only_ours=True)       # AEON's stop must not fire on top of this sale
        body = {"instId": inst, "tdMode": SPOT_TD_MODE, "side": "sell", "ordType": "market",
                "sz": fmt(qty), "tgtCcy": "base_ccy"}
        intent = {"purpose": "sell", "side": "long", "reason": reason, "price": price}
        info = await self._submit(spec, body, intent)
        return await self._book(spec, intent, info)

    # --- derivatives: realised result of a close -------------------------------
    async def _cancel_algos(self, inst: str, only_ours: bool = False) -> None:
        """Cancel protective orders on an instrument. For spot only AEON's own (only_ours): a stop you placed on
        coins you hold yourself is never touched. A perpetual/future position is AEON's alone, so all of its go."""
        try:
            ids = (await self._algos()).get(inst, {}).get("ids", [])
            if only_ours:
                ours = set(self._algo_ids.get(inst, []))
                ids = [i for i in ids if i in ours]
            if ids:
                await self.okx.cancel_algos([{"algoId": i, "instId": inst} for i in ids])
            self._algo_ids.pop(inst, None)
        except OkxError as e:
            self._notes.append(f"{inst}: could not clean up protective orders ({e.msg})")

    async def _closing_fill(self, pos: Position, started: float, reason: str, fallback_px: float) -> Fill:
        """Ask OKX what a derivative close really realised; fall back to an estimate if history lags."""
        spec = self.specs.get(pos.inst)
        inst_type = spec.inst_type if spec else "SWAP"
        for _ in range(4):
            await asyncio.sleep(0.8)
            try:
                hist = await self.okx.positions_history(pos.inst, 3, inst_type)
            except OkxError:
                continue
            for h in hist:
                if (_f(h.get("uTime")) or 0) / 1000.0 >= started - 5:
                    kind = {"3": "liquidated", "5": "auto-deleveraged"}.get(str(h.get("type")))
                    realized = _f(h.get("realizedPnl"))
                    px = _f(h.get("closeAvgPx"), fallback_px) or fallback_px
                    return Fill(time.time(), pos.inst, pos.side, "close", px, pos.contracts,
                                abs(_f(h.get("fee"))), realized, realized, kind or reason)
        est = pos.upl
        return Fill(time.time(), pos.inst, pos.side, "close", fallback_px, pos.contracts, 0.0, est, est, reason, est=True)

    # --- protective orders -----------------------------------------------------
    async def set_protection(self, spec: Spec, side: str, sl: float | None, tp: float | None) -> None:
        """Replace whatever stops exist with one stop (+ optional take-profit) covering the whole position."""
        check_levels(sl, tp)
        inst = spec.inst
        await self._cancel_algos(inst, only_ours=spec.kind == "spot")
        if not sl and not tp:
            return
        body = {"instId": inst, "side": "sell" if side == "long" else "buy",
                "ordType": "oco" if (sl and tp) else "conditional", "algoClOrdId": self._cl_id()}
        if spec.kind == "spot":
            qty = self._sellable(spec)
            if qty < spec.min_sz:
                raise BrokerError(f"{inst}: nothing sellable to protect (balance {self._bal.get(spec.base, 0.0):g})")
            body.update(tdMode=SPOT_TD_MODE, sz=fmt(qty))
        else:
            body.update(tdMode=DERIV_TD_MODE, closeFraction="1", reduceOnly=True)
            if self._pos_side(side):
                body["posSide"] = side
        if sl:
            body.update(slTriggerPx=px_str(sl, spec.tick_sz), slOrdPx="-1", slTriggerPxType="last")
        if tp:
            body.update(tpTriggerPx=px_str(tp, spec.tick_sz), tpOrdPx="-1", tpTriggerPxType="last")
        rows = await self.okx.place_algo(body)
        algo_id = rows[0].get("algoId") if rows and isinstance(rows[0], dict) else None
        self._algo_ids[inst] = [algo_id] if algo_id else []
        self._desired[inst] = (sl, tp)
        if inst in self._known:
            self._known[inst].sl, self._known[inst].tp = sl, tp
        self._changed()

    # --- monitor: reconciliation -------------------------------------------------
    async def _reconcile_pending(self) -> list[Fill]:
        """Orders sent but not confirmed: look each one up by its client id and book what really happened."""
        fills: list[Fill] = []
        for cl, p in list(self._pending.items()):
            inst = p["inst"]
            try:
                o = await self.okx.get_order(inst, p.get("ord_id"), cl_id=cl)
            except OkxError as e:
                if e.code in NOT_FOUND_CODES or "not exist" in (e.msg or "").lower():
                    o = None
                else:
                    continue                                   # can't tell right now: try again next pass
            if o is None:
                if time.time() - p["ts"] > GIVE_UP_LOOKUP_S:
                    self._drop_pending(cl)
                    if p["purpose"] == "open" and inst not in self._known and inst not in self._spot:
                        self._desired.pop(inst, None)
                    self._notes.append(f"{inst}: an order AEON sent never reached OKX; nothing was traded")
                continue
            info = self._fill_info(o)
            if info is None:
                if o.get("state") in ("canceled", "mmp_canceled"):
                    self._drop_pending(cl)
                    if p["purpose"] == "open" and inst not in self._known and inst not in self._spot:
                        self._desired.pop(inst, None)
                    self._notes.append(f"{inst}: an unconfirmed order turned out canceled with nothing filled")
                continue                                       # still working: look again next pass
            self._drop_pending(cl)
            spec = self.specs.get(inst)
            if not spec:
                self._notes.append(f"{inst}: an unconfirmed order filled, but its contract spec is unknown")
                continue
            fills.append(await self._book(spec, p, info))
            self._notes.append(f"{inst}: booked an order whose fill was not confirmed at the time "
                               f"({p['purpose']} {info['sz']:g} @ {info['px']:g})")
        return fills

    async def _spot_triggers(self, pending_ids: set[str]) -> list[Fill]:
        """AEON's own spot stops / targets that fired since the last pass. The stop order itself says which order
        it placed, so the sale is booked exactly, even when the account holds more of the same coin."""
        fills: list[Fill] = []
        self._spot_waiting = set()             # coins whose stop left the book but OKX hasn't said why yet
        for inst in list(self._spot):
            spec = self.specs.get(inst)
            if not spec:
                continue
            for algo_id in [a for a in self._algo_ids.get(inst, []) if a not in pending_ids]:
                try:
                    d = await self.okx.algo_details(algo_id) or {}
                except OkxError:
                    self._spot_waiting.add(inst)
                    continue                                   # try again next pass
                state = d.get("state")
                if state in ("effective", "partially_effective"):
                    ord_ids = [o for o in (d.get("ordIdList") or []) if o] or ([d["ordId"]] if d.get("ordId") else [])
                    infos = []
                    for oid in ord_ids:
                        try:
                            info = self._fill_info(await self.okx.get_order(inst, oid))
                        except OkxError:
                            info = None
                        if info:
                            infos.append(info)
                    if len(infos) < len(ord_ids):
                        self._spot_waiting.add(inst)
                        continue                               # the sale is not final yet: look again next pass
                    self._algo_ids[inst] = [a for a in self._algo_ids.get(inst, []) if a != algo_id]
                    reason = {"sl": "stop_loss", "tp": "take_profit"}.get(d.get("actualSide"), "stop/target hit on exchange")
                    for info in infos:
                        fills.append(await self._book_spot_sale(spec, info, reason))
                    if not infos:
                        self._notes.append(f"{inst}: AEON's stop fired but OKX recorded no order for it; checking the balance")
                elif state in ("canceled", "order_failed", "partially_failed"):
                    self._algo_ids[inst] = [a for a in self._algo_ids.get(inst, []) if a != algo_id]
                    self._notes.append(f"{inst}: AEON's stop order is gone ({state}); re-arming it")
                else:
                    self._spot_waiting.add(inst)               # off the book but not finished: OKX is still settling it
                if inst not in self._spot:
                    break
        return fills

    async def _spot_shortfalls(self, prices: dict[str, float], pending_ids: set[str]) -> list[Fill]:
        """AEON's coins that left the wallet without AEON or its own stops selling them: someone sold them
        outside AEON. Booked at the current price and marked as an estimate."""
        fills: list[Fill] = []
        busy = {p["inst"] for p in self._pending.values()}
        for inst, s in list(self._spot.items()):
            spec = self.specs.get(inst)
            if not spec or inst in busy:
                continue
            if any(a not in pending_ids for a in self._algo_ids.get(inst, [])):
                continue                                       # a stop of ours may be firing: _spot_triggers handles it
            bal = self._bal.get(spec.base, 0.0)
            if bal >= s["qty"] - spec.lot_sz:
                continue                                       # all of AEON's coins are still there
            gone = s["qty"] - max(bal, 0.0)
            fill = await self._book_spot_sale(spec, {"px": prices.get(inst) or s["entry"], "sz": gone, "fee": 0.0,
                                                     "fee_ccy": ""}, "sold outside AEON")
            fill.est = True
            fills.append(fill)
            self._notes.append(f"{inst}: {gone:g} of AEON's coins left the account without an AEON order; booked as sold")
        return fills

    # --- monitor -------------------------------------------------------------
    async def tick(self, prices: dict[str, float]) -> list[Fill]:
        fills: list[Fill] = []
        try:
            await self._refresh_balances()
            current = await self._fetch_positions()
            algos = await self._algos()
        except OkxError as e:
            self.last_error = e.msg or str(e)
            return fills
        self.last_error = ""
        pending_ids = {a for d in algos.values() for a in d["ids"]}

        fills += await self._reconcile_pending()
        fills += await self._spot_triggers(pending_ids)
        fills += await self._spot_shortfalls(prices, pending_ids)

        for inst in self._spot:                                # AEON's own spot holdings, as positions
            pos = self._spot_position(inst, prices.get(inst))
            if pos:
                current[inst] = pos

        # derivative positions that vanished: closed by their stop / target / liquidation on the exchange
        for inst, old in list(self._known.items()):
            if old.kind == "spot" or inst in current:
                continue
            fills.append(await self._closing_fill(old, time.time() - 120, "stop/target hit on exchange",
                                                  prices.get(inst, old.mark)))
            for d in (self._desired, self._unprotected_since, self._algo_ids):
                d.pop(inst, None)

        for inst, pos in current.items():
            a = algos.get(inst)
            if pos.kind == "spot":                             # only AEON's own stop counts for its own coins
                protected = bool(a and a["sl"] and any(i in pending_ids for i in self._algo_ids.get(inst, [])))
            else:
                protected = bool(a and a["sl"])
            if protected:
                pos.sl, pos.tp = a["sl"], a["tp"]
                self._unprotected_since.pop(inst, None)
                continue
            want = self._desired.get(inst)
            spec = self.specs.get(inst)
            if not want or not want[0] or not spec:
                continue            # not a position AEON opened: leave it alone
            pos.sl, pos.tp = want
            first = self._unprotected_since.setdefault(inst, time.time())
            if inst in self._spot_waiting and time.time() - first < 60:
                continue            # its stop just left the book: it may have sold the coins, so don't re-arm blind
            try:
                await self.set_protection(spec, pos.side, *want)
                self._notes.append(f"{inst}: stop was missing on the exchange and has been re-armed")
                self._unprotected_since.pop(inst, None)
            except (OkxError, BrokerError) as e:
                self._notes.append(f"{inst}: UNPROTECTED, could not re-arm stop ({e})")
                if time.time() - first > 60:
                    self._notes.append(f"{inst}: still unprotected after 60s: closing as a fail-safe")
                    try:
                        fills.append(await self.close(spec, prices.get(inst, pos.mark), "fail-safe: stop could not be placed"))
                    except (OkxError, BrokerError) as e2:
                        self._notes.append(f"{inst}: fail-safe close FAILED ({e2}); close it by hand")
        closed = {f.inst for f in fills if f.event == "close"}
        self._known = {i: p for i, p in current.items() if i not in closed}
        return fills
