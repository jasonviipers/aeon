"""Broker for a real OKX account (demo-trading or live). Same interface as PaperBroker.

Markets
  * perpetuals and dated futures: isolated margin, leverage, a market entry with the stop-loss
    (and optional take-profit) attached, so protection lives on OKX's servers from the first moment;
  * spot: long-only cash trading. A spot buy cannot carry an attached stop (the fee is taken in the coin
    you buy, so the amount to protect is only known after the fill), so the stop order is placed
    straight after the buy, and the monitor below re-arms it if it is ever missing.

Safety design
  * tick() re-checks that every position still has a stop and re-arms it if not; if a stop cannot be
    restored within a minute the position is closed (fail-safe);
  * AEON only manages what it opened: spot holdings are tracked from its own fills, so coins you already
    held are never sold, and state is saved between runs (see dump/load) so a restart loses nothing;
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

from .broker import Account, BrokerError, Fill, Position
from .market import INST_TYPE, Spec
from .okx import OkxClient, OkxError, fmt, px_str

DERIV_TD_MODE = "isolated"
SPOT_TD_MODE = "cash"


def _f(x, default: float | None = 0.0) -> float | None:
    try:
        return float(x) if x not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _floor_lot(qty: float, lot: float) -> float:
    return round(math.floor(qty / lot + 1e-9) * lot, 10) if lot > 0 else qty


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
        self._known: dict[str, Position] = {}
        self._desired: dict[str, tuple[float | None, float | None]] = {}
        self._unprotected_since: dict[str, float] = {}
        self._spot: dict[str, dict] = {}       # spot holdings AEON bought: inst -> qty, entry, fees, realized, opened_ts, since
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

    # --- persistence (so a restart keeps stops, spot holdings and the trade ledger) -------------
    def dump(self) -> dict:
        return {"desired": {k: list(v) for k, v in self._desired.items()}, "spot": self._spot,
                "known": {k: asdict(p) for k, p in self._known.items()}}

    def load(self, data: dict) -> None:
        self._desired = {k: (v[0], v[1]) for k, v in (data.get("desired") or {}).items()}
        self._spot = dict(data.get("spot") or {})
        self._known = {k: Position(**v) for k, v in (data.get("known") or {}).items()}

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
        tracked = self._spot.get(spec.inst, {}).get("qty", 0.0)
        return _floor_lot(min(tracked, self._bal.get(spec.base, 0.0)), spec.lot_sz)

    # --- reading state -------------------------------------------------------
    async def _refresh_balances(self) -> dict:
        bal = await self.okx.balance()
        row = bal[0] if bal else {}
        self._bal = {}
        for d in row.get("details", []):
            v = _f(d.get("cashBal"), None)
            self._bal[d.get("ccy", "")] = v if v is not None else _f(d.get("eq"))
        return row

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

    # --- orders: shared ------------------------------------------------------
    async def _await_fill(self, inst: str, ord_id: str) -> dict:
        """Wait for a market order to fill: {px, sz, fee (positive), fee_ccy}."""
        for _ in range(8):
            await asyncio.sleep(0.4)
            o = await self.okx.get_order(inst, ord_id)
            if o and o.get("state") == "filled":
                return {"px": _f(o.get("avgPx")), "sz": _f(o.get("accFillSz")),
                        "fee": abs(_f(o.get("fee"))), "fee_ccy": o.get("feeCcy") or ""}
            if o and o.get("state") in ("canceled", "mmp_canceled"):
                raise BrokerError(f"{inst}: order was canceled by the exchange")
        raise BrokerError(f"{inst}: order {ord_id} not confirmed filled yet; check the account before retrying")

    def _attach(self, spec: Spec, sl: float | None, tp: float | None) -> list[dict]:
        a: dict = {}
        if sl:
            a.update(slTriggerPx=px_str(sl, spec.tick_sz), slOrdPx="-1", slTriggerPxType="last")
        if tp:
            a.update(tpTriggerPx=px_str(tp, spec.tick_sz), tpOrdPx="-1", tpTriggerPxType="last")
        return [a] if a else []

    # --- orders: open / add / reduce / close ---------------------------------
    async def open(self, spec: Spec, side: str, contracts: float, lever: float, sl: float | None,
                   tp: float | None, price: float, reason: str) -> Fill:
        if spec.kind == "spot":
            if side != "long":
                raise BrokerError("spot cannot be sold short")
            return await self._open_spot(spec, contracts, sl, tp, price, reason)
        inst, ps = spec.inst, self._pos_side(side)
        await self.okx.set_leverage(inst, lever, DERIV_TD_MODE, ps)
        body = {"instId": inst, "tdMode": DERIV_TD_MODE, "side": "buy" if side == "long" else "sell",
                "ordType": "market", "sz": fmt(contracts), "clOrdId": self._cl_id()}
        if ps:
            body["posSide"] = ps
        if sl or tp:
            body["attachAlgoOrds"] = self._attach(spec, sl, tp)
        rows = await self.okx.place_order(body)
        info = await self._await_fill(inst, rows[0]["ordId"])
        self._desired[inst] = (sl, tp)
        fee = self._fee_usdt(info, spec)
        return Fill(time.time(), inst, side, "open", info["px"] or price, info["sz"] or contracts, fee, -fee, None, reason)

    async def add(self, spec: Spec, side: str, contracts: float, sl: float | None, tp: float | None,
                  price: float, reason: str) -> Fill:
        if spec.kind == "spot":
            return await self._add_spot(spec, contracts, sl, tp, price, reason)
        inst, ps = spec.inst, self._pos_side(side)
        body = {"instId": inst, "tdMode": DERIV_TD_MODE, "side": "buy" if side == "long" else "sell",
                "ordType": "market", "sz": fmt(contracts), "clOrdId": self._cl_id()}
        if ps:
            body["posSide"] = ps
        rows = await self.okx.place_order(body)
        info = await self._await_fill(inst, rows[0]["ordId"])
        await self.set_protection(spec, side, sl, tp)       # re-cover the enlarged position
        fee = self._fee_usdt(info, spec)
        return Fill(time.time(), inst, side, "add", info["px"] or price, info["sz"] or contracts, fee, -fee, None, reason)

    async def reduce(self, spec: Spec, contracts: float, price: float, reason: str) -> Fill:
        if spec.kind == "spot":
            return await self._sell_spot(spec, contracts, price, reason)
        pos = self._known.get(spec.inst) or (await self._fetch_positions()).get(spec.inst)
        if not pos:
            raise BrokerError(f"{spec.inst}: no position")
        ps = self._pos_side(pos.side)
        body = {"instId": spec.inst, "tdMode": DERIV_TD_MODE, "side": "sell" if pos.side == "long" else "buy",
                "ordType": "market", "sz": fmt(contracts), "clOrdId": self._cl_id()}
        if ps:
            body["posSide"] = ps
        else:
            body["reduceOnly"] = True
        rows = await self.okx.place_order(body)
        info = await self._await_fill(spec.inst, rows[0]["ordId"])
        want = self._desired.get(spec.inst, (pos.sl, pos.tp))
        await self.set_protection(spec, pos.side, *want)    # stop now covers the smaller position
        px, sz, fee = info["px"], info["sz"], self._fee_usdt(info, spec)
        gross = pos.direction * (px - pos.entry) * sz * pos.ct_val
        return Fill(time.time(), spec.inst, pos.side, "reduce", px, sz, fee, gross - fee, None, reason, est=True)

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
        self._known.pop(inst, None)
        self._desired.pop(inst, None)
        self._unprotected_since.pop(inst, None)
        await self._cancel_algos(inst)
        return await self._closing_fill(pos, started, reason, price)

    # --- spot ------------------------------------------------------------------
    async def _open_spot(self, spec: Spec, qty: float, sl: float | None, tp: float | None,
                         price: float, reason: str) -> Fill:
        inst = spec.inst
        if inst in self._spot:
            raise BrokerError(f"{inst}: already holding it")
        body = {"instId": inst, "tdMode": SPOT_TD_MODE, "side": "buy", "ordType": "market",
                "sz": fmt(qty), "tgtCcy": "base_ccy", "clOrdId": self._cl_id()}     # size in coins, not USDT
        rows = await self.okx.place_order(body)
        info = await self._await_fill(inst, rows[0]["ordId"])
        px, sz = info["px"] or price, info["sz"] or qty
        fee = self._fee_usdt({**info, "px": px}, spec)
        held = sz - (info["fee"] if info["fee_ccy"] == spec.base else 0.0)         # the buy fee is taken in the coin
        now = time.time()
        self._spot[inst] = {"qty": held, "entry": px, "fees": fee, "realized": 0.0, "opened_ts": now, "since": now}
        self._desired[inst] = (sl, tp)
        await self._safe_refresh_balances()
        try:
            await self.set_protection(spec, "long", sl, tp)
        except (OkxError, BrokerError) as e:
            self._notes.append(f"{inst}: bought, but the stop order could not be placed yet ({e}); retrying")
        return Fill(now, inst, "long", "open", px, sz, fee, -fee, None, reason)

    async def _add_spot(self, spec: Spec, qty: float, sl: float | None, tp: float | None,
                        price: float, reason: str) -> Fill:
        inst = spec.inst
        s = self._spot.get(inst)
        if not s:
            raise BrokerError(f"{inst}: no spot position to add to")
        body = {"instId": inst, "tdMode": SPOT_TD_MODE, "side": "buy", "ordType": "market",
                "sz": fmt(qty), "tgtCcy": "base_ccy", "clOrdId": self._cl_id()}
        rows = await self.okx.place_order(body)
        info = await self._await_fill(inst, rows[0]["ordId"])
        px, sz = info["px"] or price, info["sz"] or qty
        fee = self._fee_usdt({**info, "px": px}, spec)
        got = sz - (info["fee"] if info["fee_ccy"] == spec.base else 0.0)
        s["entry"] = (s["entry"] * s["qty"] + px * got) / (s["qty"] + got)
        s["qty"] += got
        s["fees"] += fee
        s["since"] = time.time()
        await self._safe_refresh_balances()
        await self.set_protection(spec, "long", sl, tp)
        return Fill(time.time(), inst, "long", "add", px, sz, fee, -fee, None, reason)

    async def _sell_spot(self, spec: Spec, contracts: float | None, price: float, reason: str) -> Fill:
        inst = spec.inst
        s = self._spot.get(inst)
        if not s:
            raise BrokerError(f"{inst}: AEON holds no spot position here (it only sells what it bought)")
        await self._safe_refresh_balances()
        have = self._sellable(spec)
        qty = have if contracts is None else min(_floor_lot(contracts, spec.lot_sz), have)
        if qty < spec.min_sz:
            for d in (self._spot, self._known, self._desired, self._unprotected_since):
                d.pop(inst, None)
            raise BrokerError(f"{inst}: balance {have:g} is below the minimum sell size, so the position was dropped from tracking")
        await self._cancel_algos(inst)                       # the stop must not fire on top of this sale
        body = {"instId": inst, "tdMode": SPOT_TD_MODE, "side": "sell", "ordType": "market",
                "sz": fmt(qty), "tgtCcy": "base_ccy", "clOrdId": self._cl_id()}
        rows = await self.okx.place_order(body)
        info = await self._await_fill(inst, rows[0]["ordId"])
        px, sz = info["px"] or price, info["sz"] or qty
        fee = self._fee_usdt({**info, "px": px}, spec)
        gross = (px - s["entry"]) * sz
        now = time.time()
        if s["qty"] - sz < spec.min_sz:                      # everything (or all but dust) is gone
            trade_pnl = s["realized"] + gross - fee - s["fees"]
            for d in (self._spot, self._known, self._desired, self._unprotected_since):
                d.pop(inst, None)
            return Fill(now, inst, "long", "close", px, sz, fee, gross - fee, trade_pnl, reason)
        open_fee_part = s["fees"] * sz / s["qty"]
        s["realized"] += gross - fee - open_fee_part
        s["fees"] -= open_fee_part
        s["qty"] -= sz
        s["since"] = now
        await self._safe_refresh_balances()
        sl, tp = self._desired.get(inst, (None, None))
        if sl or tp:
            await self.set_protection(spec, "long", sl, tp)  # the stop now covers the smaller holding
        return Fill(now, inst, "long", "reduce", px, sz, fee, gross - fee, None, reason)

    async def _safe_refresh_balances(self) -> None:
        try:
            await self._refresh_balances()
        except OkxError:
            pass

    async def _spot_closing_fill(self, pos: Position, reason: str, fallback_px: float) -> Fill:
        """A spot holding disappeared: its stop or target fired (or it was sold by hand). Rebuild the sale
        from OKX's execution records; fall back to an estimate if they are not available yet."""
        s = self._spot.pop(pos.inst, None) or {"entry": pos.entry, "fees": pos.fees, "realized": pos.realized,
                                               "qty": pos.contracts, "since": time.time() - 300}
        sold = proceeds = fee = 0.0
        try:
            for f in await self.okx.fills("SPOT", pos.inst, 30):
                if f.get("side") == "sell" and (_f(f.get("ts")) or 0) / 1000.0 >= s.get("since", 0) - 5:
                    sz, px = _f(f.get("fillSz")), _f(f.get("fillPx"))
                    sold += sz
                    proceeds += sz * px
                    fee += abs(_f(f.get("fee"))) * (px if f.get("feeCcy") == pos.base else 1.0)
        except OkxError:
            pass
        now = time.time()
        if sold > 0:
            px = proceeds / sold
            gross = (px - s["entry"]) * sold
            return Fill(now, pos.inst, "long", "close", px, sold, fee, gross - fee,
                        s["realized"] + gross - fee - s["fees"], reason)
        gross = (fallback_px - s["entry"]) * s["qty"]
        return Fill(now, pos.inst, "long", "close", fallback_px, s["qty"], 0.0, gross, s["realized"] + gross - s["fees"],
                    reason, est=True)

    # --- derivatives: realised result of a close -------------------------------
    async def _cancel_algos(self, inst: str) -> None:
        try:
            ids = (await self._algos()).get(inst, {}).get("ids", [])
            if ids:
                await self.okx.cancel_algos([{"algoId": i, "instId": inst} for i in ids])
        except OkxError as e:
            self._notes.append(f"{inst}: could not clean up protective orders ({e.msg})")

    async def _closing_fill(self, pos: Position, started: float, reason: str, fallback_px: float) -> Fill:
        """Ask OKX what the close really realised; fall back to an estimate if history lags."""
        if pos.kind == "spot":
            return await self._spot_closing_fill(pos, reason, fallback_px)
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
        inst = spec.inst
        await self._cancel_algos(inst)
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
        await self.okx.place_algo(body)
        self._desired[inst] = (sl, tp)
        if inst in self._known:
            self._known[inst].sl, self._known[inst].tp = sl, tp

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

        # spot holdings AEON bought, as positions
        for inst, s in self._spot.items():
            spec = self.specs.get(inst)
            if not spec:
                continue
            qty = min(s["qty"], self._bal.get(spec.base, 0.0))
            if qty >= spec.min_sz:
                current[inst] = Position(
                    inst=inst, side="long", contracts=qty, ct_val=1.0, entry=s["entry"], lever=1.0,
                    mark=prices.get(inst, s["entry"]), opened_ts=s["opened_ts"], fees=s["fees"],
                    realized=s["realized"], kind="spot")

        # positions that vanished: closed by their stop / target / liquidation on the exchange
        for inst, old in list(self._known.items()):
            if inst not in current:
                fills.append(await self._closing_fill(old, time.time() - 120, "stop/target hit on exchange",
                                                      prices.get(inst, old.mark)))
                for d in (self._desired, self._unprotected_since, self._spot):
                    d.pop(inst, None)
        for inst in [i for i in self._spot if i not in current]:      # tracked, but no coins left and never seen
            self._spot.pop(inst, None)
            self._notes.append(f"{inst}: tracked spot holding has no balance any more; dropped")

        for inst, pos in current.items():
            a = algos.get(inst)
            if a and a["sl"]:
                pos.sl, pos.tp = a["sl"], a["tp"]
                self._unprotected_since.pop(inst, None)
                continue
            want = self._desired.get(inst)
            spec = self.specs.get(inst)
            if not want or not want[0] or not spec:
                continue            # not a position AEON opened: leave it alone
            pos.sl, pos.tp = want
            first = self._unprotected_since.setdefault(inst, time.time())
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
        closed = {f.inst for f in fills}
        self._known = {i: p for i, p in current.items() if i not in closed}
        return fills
