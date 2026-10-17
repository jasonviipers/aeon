"""Market scanner: instrument specs for perpetuals, dated futures and spot; universe selection; snapshots."""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field

from .config import RiskCfg, UniverseCfg
from .indicators import atr, ema, pct_change, rsi
from .okx import OkxClient, OkxError

CATEGORY_NAMES = {"1": "crypto", "3": "stocks", "4": "commodities"}
INST_TYPE = {"swap": "SWAP", "futures": "FUTURES", "spot": "SPOT"}
STABLE_BASES = {"USDC", "USDG", "DAI", "TUSD", "FDUSD", "PYUSD", "USDD", "USDE", "USDP", "BUSD", "EURT", "USDS", "USD1"}
TERM_SHORT = {"this_week": "wk", "next_week": "nwk", "this_month": "mon", "next_month": "nmon",
              "quarter": "qtr", "next_quarter": "nqtr", "this_five_years": "5y"}


@dataclass(frozen=True)
class Spec:
    inst: str
    base: str
    ct_val: float      # base-asset units per contract (1.0 for spot: the order size is the coin amount itself)
    lot_sz: float
    min_sz: float
    tick_sz: float
    max_lever: float   # 1 for spot
    category: str = "crypto"
    kind: str = "swap"             # swap (perpetual) | futures (dated expiry) | spot
    expiry_ts: float = 0.0         # futures only, unix seconds
    alias: str = ""                # futures only: this_month / next_month / quarter / next_quarter / this_five_years

    @property
    def inst_type(self) -> str:
        return INST_TYPE[self.kind]

    @property
    def tag(self) -> str:
        if self.kind == "spot":
            return "spot"
        if self.kind == "swap":
            return "perp"
        return "fut-" + TERM_SHORT.get(self.alias, self.alias or "dated")

    def min_notional(self, price: float) -> float:
        return self.min_sz * self.ct_val * price

    def days_left(self, now: float | None = None) -> float | None:
        if self.kind != "futures" or not self.expiry_ts:
            return None
        return (self.expiry_ts - (now or time.time())) / 86400.0


def size_contracts(notional_usd: float, price: float, spec: Spec) -> float:
    """Largest whole-lot size (contracts, or coins for spot) whose notional fits inside notional_usd."""
    if price <= 0 or notional_usd <= 0:
        return 0.0
    raw = notional_usd / (price * spec.ct_val)
    steps = math.floor(raw / spec.lot_sz + 1e-9)
    return round(steps * spec.lot_sz, 8)


@dataclass
class InstSnap:
    inst: str
    last: float
    chg24h: float = 0.0          # %
    vol_usd: float = 0.0
    funding: float | None = None  # per 8h, as a fraction (0.0001 = 0.01%); perpetuals only
    ret15m: float = 0.0          # % over last 15m candle-open to now
    ret1h: float = 0.0
    ret4h: float = 0.0
    rsi15: float | None = None
    rsi1h: float | None = None
    ema20: float | None = None   # 1h
    ema50: float | None = None   # 1h
    trend: str = "n/a"
    atr1h_pct: float | None = None
    closes_1h: list[float] = field(default_factory=list)
    min_notional: float = 0.0
    max_lever: float = 1.0
    category: str = "crypto"
    kind: str = "swap"
    tag: str = "perp"
    days_left: float | None = None   # dated futures
    basis: float | None = None       # futures premium over the spot index, %
    ann_basis: float | None = None   # the same, annualised, %


def _parse_candles(rows: list[list[str]]) -> tuple[list[float], list[float], list[float]]:
    rows = sorted(rows, key=lambda r: int(r[0]))
    return [float(r[2]) for r in rows], [float(r[3]) for r in rows], [float(r[4]) for r in rows]


class Market:
    def __init__(self, okx: OkxClient, risk: RiskCfg, uni: UniverseCfg):
        self.okx, self.risk, self.uni = okx, risk, uni
        self.specs: dict[str, Spec] = {}
        self.enabled: set[str] = set(uni.markets)   # narrowed at start-up to what the account mode can trade
        self.excluded: list[str] = []     # human-readable reasons for the UI / prompt
        self.last_snapshot_secs = 0.0
        self._sem = asyncio.Semaphore(6)

    # --- instrument specs ------------------------------------------------------
    async def load_specs(self) -> None:
        kinds = [k for k in self.uni.markets if k in INST_TYPE]
        batches = await asyncio.gather(*(self.okx.instruments(INST_TYPE[k]) for k in kinds))
        wanted = set(self.uni.categories)
        specs: dict[str, Spec] = {}
        for kind, rows in zip(kinds, batches):
            for r in rows:
                if r.get("state") != "live":
                    continue
                category = CATEGORY_NAMES.get(str(r.get("instCategory")), "other")
                if category not in wanted:
                    continue
                try:
                    inst = r["instId"]
                    base = inst.split("-")[0]
                    if kind == "spot":
                        if r.get("quoteCcy") != "USDT" or base in STABLE_BASES:
                            continue
                        spec = Spec(inst, base, 1.0, float(r["lotSz"]), float(r["minSz"]), float(r["tickSz"]),
                                    1.0, category, "spot")
                    else:
                        if r.get("ctType") != "linear":
                            continue
                        if kind == "swap" and r.get("settleCcy") != "USDT":
                            continue
                        alias = str(r.get("alias") or "")
                        if kind == "futures":
                            xperp = alias == "this_five_years" or "XPERP" in str(r.get("instFamily"))
                            if xperp and not self.uni.futures_include_xperp:
                                continue
                        spec = Spec(inst, base, float(r["ctVal"]), float(r["lotSz"]), float(r["minSz"]),
                                    float(r["tickSz"]), float(r.get("lever") or 1), category, kind,
                                    float(r.get("expTime") or 0) / 1000.0 if kind == "futures" else 0.0,
                                    alias if kind == "futures" else "")
                    specs[inst] = spec
                except (KeyError, ValueError):
                    continue
        self.specs.clear()          # mutate in place: brokers hold a reference to this dict
        self.specs.update(specs)

    # --- tickers / universe ----------------------------------------------------
    async def tickers(self) -> dict[str, dict]:
        kinds = [k for k in INST_TYPE if k in self.enabled]
        batches = await asyncio.gather(*(self.okx.tickers(INST_TYPE[k]) for k in kinds))
        out: dict[str, dict] = {}
        for kind, rows in zip(kinds, batches):
            for t in rows:
                spec = self.specs.get(t.get("instId"))
                if not spec or spec.kind != kind:
                    continue
                try:
                    last = float(t["last"])
                    open24 = float(t.get("open24h") or last)
                    vol = float(t.get("volCcy24h") or 0)          # spot: already in USDT; derivatives: in coins
                    out[spec.inst] = {"last": last, "chg24h": pct_change(last, open24),
                                      "vol_usd": vol if kind == "spot" else vol * last}
                except (KeyError, ValueError):
                    continue
        return out

    def affordable(self, spec: Spec, price: float, equity: float) -> bool:
        lever = min(self.risk.max_leverage, spec.max_lever)
        return spec.min_notional(price) / lever <= equity * self.risk.max_margin_pct / 100.0

    async def pick_universe(self, equity: float, held: set[str]) -> tuple[list[str], dict[str, dict]]:
        """Scan every enabled OKX market and return the instruments the agent gets to see this cycle."""
        tick = await self.tickers()
        self.excluded = []
        now = time.time()
        chosen: list[str] = []

        def admit(inst: str) -> bool:
            spec, px = self.specs[inst], tick[inst]["last"]
            if self.uni.only_affordable and inst not in held and not self.affordable(spec, px, equity):
                self.excluded.append(f"{spec.base} {spec.tag} (min ${spec.min_notional(px):,.0f})")
                return False
            chosen.append(inst)
            return True

        if self.uni.mode == "list":
            for inst in self.uni.instruments:
                if inst in tick:
                    admit(inst)
        else:
            plan = (("swap", self.uni.top_n, self.uni.min_volume_usd),
                    ("futures", self.uni.top_n_futures, self.uni.futures_min_volume_usd),
                    ("spot", self.uni.top_n_spot, self.uni.min_volume_usd))
            for kind, n, min_vol in plan:
                if kind not in self.enabled or n <= 0:
                    continue
                ranked = sorted((i for i in tick if self.specs[i].kind == kind), key=lambda i: -tick[i]["vol_usd"])
                taken, bases = 0, set()
                for inst in ranked:
                    if taken >= n or tick[inst]["vol_usd"] < min_vol:
                        break
                    spec = self.specs[inst]
                    if kind == "futures":
                        dl = spec.days_left(now)
                        if dl is None or dl < self.risk.futures_min_days_to_open or spec.base in bases:
                            continue            # too close to expiry, or this coin already has a (more liquid) expiry
                    if admit(inst):
                        taken += 1
                        bases.add(spec.base)
        for inst in held:
            if inst not in chosen and inst in tick:
                chosen.append(inst)
        return chosen, tick

    # --- per-instrument snapshot -----------------------------------------------
    async def _funding(self, spec: Spec) -> dict | None:
        if spec.kind == "spot" or (spec.kind == "futures" and "XPERP" not in spec.inst):
            return None
        try:
            return await self.okx.funding_rate(spec.inst)
        except OkxError:
            return None

    async def _one(self, inst: str, base: dict, spec: Spec, index: dict[str, float]) -> InstSnap | None:
        async with self._sem:
            try:
                c15, c1h = await asyncio.gather(self.okx.candles(inst, "15m", 100), self.okx.candles(inst, "1H", 100))
            except OkxError:
                return None
            fr = await self._funding(spec)
        if len(c15) < 20 or len(c1h) < 20:
            return None
        h15, l15, k15 = _parse_candles(c15)
        h1, l1, k1 = _parse_candles(c1h)
        last = base["last"]
        e20, e50 = ema(k1, 20)[-1], ema(k1, 50)[-1]
        if last > e20 > e50:
            trend = "up"
        elif last < e20 < e50:
            trend = "down"
        else:
            trend = "range"
        a = atr(h1, l1, k1, 14)
        funding = None
        if fr and fr.get("fundingRate") not in (None, ""):
            try:
                funding = float(fr["fundingRate"])
            except ValueError:
                pass
        days = spec.days_left()
        basis = ann = None
        idx = index.get(f"{spec.base}-USDT")
        if spec.kind == "futures" and idx:
            basis = (last / idx - 1.0) * 100.0
            if days and 0.5 < days < 400:
                ann = basis * 365.0 / days
        return InstSnap(
            inst=inst, last=last, chg24h=base["chg24h"], vol_usd=base["vol_usd"], funding=funding,
            ret15m=pct_change(last, k15[-2]) if len(k15) > 1 else 0.0,
            ret1h=pct_change(last, k15[-5]) if len(k15) > 4 else 0.0,
            ret4h=pct_change(last, k1[-5]) if len(k1) > 4 else 0.0,
            rsi15=rsi(k15), rsi1h=rsi(k1), ema20=e20, ema50=e50, trend=trend,
            atr1h_pct=(a / last * 100.0) if a else None,
            closes_1h=k1[-24:], min_notional=spec.min_notional(last), max_lever=spec.max_lever,
            category=spec.category, kind=spec.kind, tag=spec.tag, days_left=days, basis=basis, ann_basis=ann,
        )

    async def snapshot(self, insts: list[str], tick: dict[str, dict]) -> dict[str, InstSnap]:
        t0 = time.time()
        index: dict[str, float] = {}
        if any(self.specs[i].kind == "futures" for i in insts if i in self.specs):
            try:
                index = await self.okx.index_tickers("USDT")
            except OkxError:
                index = {}
        results = await asyncio.gather(*(self._one(i, tick[i], self.specs[i], index) for i in insts if i in tick))
        snaps = {s.inst: s for s in results if s}
        self.last_snapshot_secs = time.time() - t0
        return snaps
