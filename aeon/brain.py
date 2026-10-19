"""The decision-maker: renders the market for an LLM, calls it, parses its JSON into Proposals.

The model has full freedom over WHAT to do. The risk engine (risk.py) decides what is ALLOWED.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .broker import Account
from .config import LlmCfg, RiskCfg
from .market import InstSnap
from .risk import Proposal


class LlmError(Exception):
    pass


class ParseError(Exception):
    pass


@dataclass
class Context:
    now: float
    mode: str
    cycle_no: int
    account: Account
    status: dict
    snaps: dict[str, InstSnap]
    recent_actions: list[dict]
    recent_trades: list[dict]
    notes: str
    excluded: list[str]
    risk: RiskCfg


@dataclass
class Decision:
    view: str = ""
    actions: list[Proposal] = field(default_factory=list)
    notes: str = ""
    next_check_min: int | None = None
    warnings: list[str] = field(default_factory=list)
    raw: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    latency: float = 0.0


# --------------------------------------------------------------------------- prompt
SYSTEM_PROMPT = """You are AEON, an autonomous trader on OKX (perpetual swaps, dated futures and spot). You run around the clock with a small account and you make every trading decision yourself: which market, long or short, how big, how much leverage, where the stop goes, when to take profit, when to cut a loss, and when to do nothing.

MARKETS YOU MAY TRADE (the `type` column of the MARKET table tells you which is which)
- perp: perpetual swaps margined in USDT. Long or short, with leverage, no expiry. Funding is paid every 8h: positive means longs pay shorts.
- fut-*: dated expiry futures (fut-mon = this month, fut-nmon = next month, fut-qtr = quarter). Long or short, with leverage, but the contract EXPIRES: `days` is the time left, and the system closes any future 1 day before expiry whatever you want. `basis%` is its premium over the spot index and `ann%` that premium annualised; a large positive basis means longs are paying a carry for holding it. No funding.
- spot: the coin itself (e.g. SOL-USDT). LONG ONLY, no leverage, no funding, can never be liquidated: you can only buy it and later sell it. For spot use side "long"; leverage is ignored (always 1x). You can only sell what you bought through AEON.
- Use the exact instrument id shown in the table (SOL-USDT-SWAP, SOL-USDT, BTC-USD_UM-261030).
- You may hold only one position per coin across all three kinds: a SOL spot position blocks a SOL perpetual, and the reverse.

OBJECTIVE
Grow the account while surviving. Survival comes first: a 50% loss needs a 100% gain to recover. Doing nothing is a valid and often correct decision; you are not required to trade every cycle. Each trade costs roughly 0.1% in fees round trip, so churning loses money by itself. Prefer a few well-reasoned trades over many marginal ones.

HOW YOU WORK
- Every cycle you receive your account, open positions, a table of the contracts you may trade, your recent actions and their outcomes, and the notes you left for yourself last time.
- You answer with ONE JSON object (format below) and nothing else.
- You choose when you will look again (next_check_minutes). Check sooner when positions are open or the market is moving; later when flat and quiet.
- Use only the data provided. Do not invent news, prices or indicators. If the data shows no edge, hold.
- Learn from your recent results: if a kind of trade keeps losing, stop making it, and write down why in your notes.

HARD LIMITS (enforced by code after you decide; an action that breaks them is rejected or clipped, and you will see that under RECENT ACTIONS)
{limits}

POSITION RULES
- Every new position MUST carry a stop_loss. It is placed on the exchange the moment you enter.
- A stop can only be moved closer to price, never further away. Put it where your trade idea is proven wrong, not at a distance that merely feels comfortable.
- You set margin_pct (percent of equity committed as margin; for spot, the share of equity you spend) and leverage. The system then clips size so that being stopped out costs no more than the risk-per-trade limit. Tighter stops therefore allow larger positions.
- You may add to a winner (add), take partial profits (reduce), exit early (close), or trail/tighten the stop and move the target (adjust).
- One position per coin; you may hold several different coins.
- Spot trades cost about 0.1% per side (perps and futures about 0.05%), and an unleveraged spot position needs a bigger move to matter. Choose the market that fits the idea: spot for a plain holding, a perpetual for a short or a leveraged view, a dated future when the basis itself is the point.

OUTPUT FORMAT (JSON only, no markdown fences, plain numbers for prices)
{{
  "market_view": "1-3 sentences: what the market is doing and why you act (or don't)",
  "actions": [
    {{"type": "open", "inst": "SOL-USDT-SWAP", "side": "long", "margin_pct": 15, "leverage": 3, "stop_loss": 148.2, "take_profit": 161.0, "confidence": 0.6, "reason": "..."}},
    {{"type": "open", "inst": "ETH-USDT", "side": "long", "margin_pct": 20, "stop_loss": 2380.0, "take_profit": 2600.0, "reason": "a spot buy: no leverage"}},
    {{"type": "add", "inst": "SOL-USDT-SWAP", "margin_pct": 10, "reason": "..."}},
    {{"type": "reduce", "inst": "SOL-USDT-SWAP", "pct": 50, "reason": "..."}},
    {{"type": "close", "inst": "SOL-USDT-SWAP", "reason": "..."}},
    {{"type": "adjust", "inst": "SOL-USDT-SWAP", "stop_loss": 150.0, "take_profit": 165.0, "reason": "..."}},
    {{"type": "hold", "reason": "why doing nothing is right"}}
  ],
  "notes": "your private memory for next cycle, max ~1200 chars: open theses and what invalidates them, lessons, levels you are watching",
  "next_check_minutes": 15
}}
Include only the actions you actually want (an empty list or a single hold is fine)."""


def build_system_prompt(r: RiskCfg) -> str:
    limits = "\n".join([
        f"- max leverage {r.max_leverage:g}x",
        f"- a stopped-out trade may lose at most {r.risk_per_trade_pct:g}% of equity (fees included)",
        f"- margin on one position at most {r.max_margin_pct:g}% of equity; at most {r.max_open_positions} open positions;"
        f" total notional at most {r.max_exposure_x:g}x equity",
        f"- stop distance must be {r.min_stop_pct:g}%-{r.max_stop_pct:g}% from price"
        + ("; stops may only tighten" if r.stops_only_tighten else ""),
        f"- daily loss limit {r.daily_loss_limit_pct:g}% (then no new positions until 00:00 UTC);"
        f" drawdown limit {r.max_drawdown_pct:g}% from peak (then everything is closed and trading HALTS until the owner resumes)",
        f"- {r.reentry_cooldown_min} min cooldown before re-entering a contract you just closed; at most {r.max_trades_per_day} entries per day",
        f"- dated futures are not opened within {r.futures_min_days_to_open:g} days of expiry and are closed {r.futures_close_days_before:g} day(s) before it",
        "- one position per coin across spot, perpetual and dated future" if r.one_position_per_asset else
        "- several positions in the same coin are allowed",
    ])
    return SYSTEM_PROMPT.format(limits=limits)


def fp(x: float | None) -> str:
    if x is None:
        return "-"
    a = abs(x)
    d = 1 if a >= 1000 else 2 if a >= 10 else 3 if a >= 1 else 5 if a >= 0.01 else 7
    return f"{x:.{d}f}"


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")


def _held(sec: float) -> str:
    m = int(sec // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def render_context(c: Context) -> str:
    a, s, r = c.account, c.status, c.risk
    out: list[str] = []
    out.append(f"TIME {datetime.fromtimestamp(c.now, timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC | MODE {c.mode.upper()} | CYCLE #{c.cycle_no}")
    state = "HALTED (kill switch)" if s["killed"] else "NO NEW POSITIONS TODAY (daily loss limit)" if s["day_blocked"] else "OK"
    out.append(
        f"ACCOUNT equity {a.equity:.2f} USDT | free margin {a.available:.2f} | unrealised {a.upl:+.2f} | "
        f"today {s['day_pnl']:+.2f}% | drawdown from peak {s['drawdown']:.1f}% | exposure {s['exposure']:.2f}x | "
        f"entries today {s['trades_today']}/{s['trades_limit']} | status {state}")
    ref = a.equity * r.risk_per_trade_pct / 100.0
    out.append(f"SIZING RULE OF THUMB max notional ~= equity x {r.risk_per_trade_pct:g}% / stop%. "
               f"E.g. a 1.5% stop allows ~${ref / 0.0165:,.0f} notional, a 3% stop ~${ref / 0.031:,.0f}.")
    out.append("")
    out.append("OPEN POSITIONS")
    if not a.positions:
        out.append("  (none)")
    for p in a.positions.values():
        risk_usd = p.notional * abs(p.mark - p.sl) / p.mark if p.sl and p.mark else 0.0
        spot = p.kind == "spot"
        extra = f" | expires in {(p.expiry - c.now) / 86400:.1f}d" if p.expiry else ""
        out.append(
            f"  {p.inst} {p.side.upper()} {'spot' if spot else f'{p.lever:g}x'} | {p.contracts:g} {'coins' if spot else 'ct'} "
            f"(${p.notional:,.1f}) | entry {fp(p.entry)} mark {fp(p.mark)} "
            f"({p.move_pct:+.2f}%) uPnL {p.upl:+.2f} ({p.upl_pct:+.0f}% of {'cost' if spot else 'margin'}) | SL {fp(p.sl)} TP {fp(p.tp)} "
            f"| risk at stop ${risk_usd:.2f} | liq {fp(p.liq)} | held {_held(c.now - p.opened_ts)}{extra}")
    out.append("")
    out.append("MARKET (what you may trade; each kind sorted by 24h volume)")
    out.append("  inst                 type     cat      last        24h%   15m%    1h%    4h%  RSI15 RSI1h trend  ATR1h%  fund%/8h   days  basis%   ann%  vol$M  minUSD")
    kind_order = {"swap": 0, "futures": 1, "spot": 2}
    for sn in sorted(c.snaps.values(), key=lambda x: (kind_order.get(x.kind, 9), -x.vol_usd)):
        fund = f"{sn.funding * 100:9.4f}" if sn.funding is not None else f"{'-':>9}"
        days = f"{sn.days_left:6.1f}" if sn.days_left is not None else f"{'-':>6}"
        basis = f"{sn.basis:+7.2f}" if sn.basis is not None else f"{'-':>7}"
        ann = f"{sn.ann_basis:+6.1f}" if sn.ann_basis is not None else f"{'-':>6}"
        out.append(
            f"  {sn.inst:<20} {sn.tag:<8} {sn.category[:3]:<4} {fp(sn.last):>10} {sn.chg24h:+7.2f} {sn.ret15m:+6.2f} {sn.ret1h:+6.2f} {sn.ret4h:+6.2f} "
            f"{(sn.rsi15 or 0):5.0f} {(sn.rsi1h or 0):5.0f} {sn.trend:<6} {(sn.atr1h_pct or 0):6.2f} "
            f"{fund} {days} {basis} {ann} {sn.vol_usd / 1e6:6.0f} {sn.min_notional:7.1f}")
    out.append("  (type: perp = perpetual, fut-* = dated future, spot = long-only coin. trend: up = price>EMA20>EMA50 on 1h; down = the reverse; range = neither."
               " days = time to expiry; basis% = future vs spot index, ann% annualised. minUSD = smallest order the exchange accepts)")
    if c.excluded:
        out.append(f"  not offered, exchange minimum order too large for the account: {', '.join(c.excluded[:12])}")
    out.append("")
    out.append("LAST 24 HOURLY CLOSES (oldest -> newest)")
    for sn in sorted(c.snaps.values(), key=lambda x: (kind_order.get(x.kind, 9), -x.vol_usd)):
        out.append(f"  {sn.inst:<20} " + " ".join(fp(x) for x in sn.closes_1h))
    out.append("")
    out.append("RECENT ACTIONS (oldest -> newest)")
    if not c.recent_actions:
        out.append("  (none yet)")
    for x in c.recent_actions:
        out.append(f"  {_hm(x['ts'])} {x['type']} {x['inst']} -> {x['status']}: {x['detail']}")
    out.append("RECENT CLOSED/REDUCED TRADES")
    done = [t for t in c.recent_trades if t["event"] in ("close", "reduce")]
    if not done:
        out.append("  (none yet)")
    for t in done:
        out.append(f"  {_hm(t['ts'])} {t['event']} {t['inst']} {t['side']} @ {fp(t['price'])} pnl {t['pnl']:+.2f}"
                   + (f" (whole trade {t['trade_pnl']:+.2f})" if t['trade_pnl'] is not None else "") + f" [{t['reason']}]")
    out.append("")
    out.append("YOUR NOTES FROM LAST CYCLE")
    out.append(c.notes.strip() or "  (empty: this is your first cycle)")
    out.append("")
    out.append("Decide now. Reply with the JSON object only.")
    return "\n".join(out)


# --------------------------------------------------------------------------- parsing
def extract_json(text: str) -> dict:
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
    try:
        obj = json.loads(t, parse_constant=_no_constant)
    except ValueError:
        s, e = t.find("{"), t.rfind("}")
        if s == -1 or e <= s:
            raise ParseError("no JSON object in the reply") from None
        try:
            obj = json.loads(t[s:e + 1], parse_constant=_no_constant)
        except ValueError as err:
            raise ParseError(f"invalid JSON: {err}") from None
    if not isinstance(obj, dict):
        raise ParseError("reply JSON is not an object")
    return obj


def _no_constant(name: str) -> float:
    """Preserve non-finite constants so action validation cannot mistake them for missing fields."""
    return float(name)


def _num(x) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        try:
            v = float(x)
        except OverflowError:
            return None
    elif isinstance(x, str):
        if not re.fullmatch(r"\s*\$?[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:%|[xX])?\s*", x):
            return None
        try:
            v = float(x.strip().removeprefix("$").rstrip("%xX"))
        except ValueError:
            return None
    else:
        return None
    return v if math.isfinite(v) else None


_TYPE_ALIASES = {"buy": "open", "enter": "open", "long": "open", "short": "open", "increase": "add",
                 "scale_in": "add", "decrease": "reduce", "trim": "reduce", "partial_close": "reduce",
                 "take_profit": "reduce", "exit": "close", "flat": "close", "modify": "adjust",
                 "update": "adjust", "trail": "adjust", "move_stop": "adjust", "wait": "hold", "none": "hold"}


def _norm_inst(x) -> str:
    """Full OKX ids are kept exactly as given (SOL-USDT is spot, SOL-USDT-SWAP a perpetual, BTC-USD_UM-261030 a
    dated future); only a bare coin ("SOL") is expanded, to its perpetual."""
    s = str(x or "").strip().upper().replace("/", "-")
    if not s:
        return ""
    return s if "-" in s else f"{s}-USDT-SWAP"


def parse_decision(obj: dict) -> Decision:
    d = Decision(view=str(obj.get("market_view") or obj.get("view") or "")[:600],
                 notes=str(obj.get("notes") or "")[:2000])
    n = _num(obj.get("next_check_minutes"))
    d.next_check_min = int(n) if n else None
    raw_actions = obj.get("actions") or []
    if isinstance(raw_actions, dict):
        raw_actions = [raw_actions]
    for a in raw_actions:
        if not isinstance(a, dict):
            d.warnings.append(f"ignored a malformed action: {str(a)[:60]}")
            continue
        t = str(a.get("type") or a.get("action") or "").strip().lower().replace(" ", "_")
        side_hint = t if t in ("long", "short") else None
        t = _TYPE_ALIASES.get(t, t)
        if t not in ("open", "add", "reduce", "close", "adjust", "hold"):
            d.warnings.append(f"ignored unknown action type {t!r}")
            continue
        side = str(a.get("side") or side_hint or "").strip().lower()
        side = {"buy": "long", "sell": "short"}.get(side, side) or None
        numeric_fields = ("margin_pct", "leverage", "stop_loss", "sl", "take_profit", "tp", "pct", "confidence")
        invalid = [name for name in numeric_fields if a.get(name) is not None and _num(a[name]) is None]
        if invalid:
            d.warnings.append(f"ignored action with invalid numeric fields: {', '.join(invalid)}")
            continue
        d.actions.append(Proposal(
            type=t, inst=_norm_inst(a.get("inst") or a.get("instrument") or a.get("symbol")), side=side,
            margin_pct=_num(a.get("margin_pct")), leverage=_num(a.get("leverage")),
            stop_loss=_num(a.get("stop_loss") if a.get("stop_loss") is not None else a.get("sl")),
            take_profit=_num(a.get("take_profit") if a.get("take_profit") is not None else a.get("tp")),
            pct=_num(a.get("pct")), reason=str(a.get("reason") or "")[:300], confidence=_num(a.get("confidence"))))
    return d


# --------------------------------------------------------------------------- LLM client
class LlmClient:
    def __init__(self, cfg: LlmCfg, api_key: str):
        self.cfg, self.key = cfg, api_key
        self._http = httpx.AsyncClient(timeout=cfg.timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def chat(self, messages: list[dict]) -> tuple[str, int, int]:
        if not self.key:
            raise LlmError(f"no API key: set {self.cfg.api_key_env} in .env")
        body = {"model": self.cfg.model, "messages": messages, "temperature": self.cfg.temperature,
                "max_tokens": self.cfg.max_tokens}
        if self.cfg.json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(self.cfg.extra_body)
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                   "HTTP-Referer": self.cfg.referer, "X-Title": "AEON"}
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        for attempt in range(3):
            try:
                resp = await self._http.post(url, json=body, headers=headers)
            except httpx.TransportError as e:
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise LlmError(f"network error: {type(e).__name__}: {e}") from e
            if resp.status_code in (408, 429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(3 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise LlmError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                data = resp.json()
                choice = data["choices"][0]
                content = choice["message"].get("content")
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise LlmError(f"unexpected response shape: {resp.text[:200]}") from e
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            if not content or not str(content).strip():
                if choice.get("finish_reason") == "length":
                    raise LlmError("the model hit max_tokens before it finished (reasoning models spend tokens "
                                   "thinking first): raise llm.max_tokens or switch thinking off in llm.extra_body")
                if attempt < 2:                      # providers occasionally return an empty body; just ask again
                    await asyncio.sleep(2)
                    continue
                raise LlmError("the model returned an empty reply three times in a row")
            u = data.get("usage") or {}
            return str(content), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        raise LlmError("gave up after retries")


class LlmBrain:
    name = "llm"

    def __init__(self, cfg: LlmCfg, risk: RiskCfg, api_key: str, client: LlmClient | None = None):
        self.cfg, self.risk = cfg, risk
        self.client = client or LlmClient(cfg, api_key)
        self.label = cfg.model

    async def decide(self, ctx: Context) -> Decision:
        messages = [{"role": "system", "content": build_system_prompt(self.risk)},
                    {"role": "user", "content": render_context(ctx)}]
        t0 = time.time()
        text, tin, tout = await self.client.chat(messages)
        try:
            obj = extract_json(text)
        except ParseError:
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "That was not a valid JSON object. Reply again with ONLY the JSON object."}]
            text, a, b = await self.client.chat(messages)
            tin, tout = tin + a, tout + b
            obj = extract_json(text)         # a second failure propagates as ParseError
        d = parse_decision(obj)
        d.raw, d.tokens_in, d.tokens_out, d.latency = text, tin, tout, time.time() - t0
        d.cost = (tin * self.cfg.input_price_per_m + tout * self.cfg.output_price_per_m) / 1e6
        return d


# --------------------------------------------------------------------------- baseline brain
class HeuristicBrain:
    """A plain trend-following rulebook. Costs nothing and needs no API key; it exists so the
    whole pipeline (scan, risk, execution, UI) can be exercised and so you have a baseline
    that the LLM agent should be able to beat."""

    name = "heuristic"
    label = "rules"

    async def decide(self, ctx: Context) -> Decision:
        d = Decision(next_check_min=5, notes=ctx.notes)
        held = ctx.account.positions
        for pos in held.values():
            s = ctx.snaps.get(pos.inst)
            if not s:
                continue
            atr = (s.atr1h_pct or 1.0) / 100.0 * s.last
            if (pos.side == "long" and s.trend == "down") or (pos.side == "short" and s.trend == "up"):
                d.actions.append(Proposal("close", pos.inst, reason="1h trend flipped against the position"))
                continue
            trail = s.last - 1.2 * atr if pos.side == "long" else s.last + 1.2 * atr
            better = pos.sl is None or (trail > pos.sl if pos.side == "long" else trail < pos.sl)
            if pos.move_pct > 0.8 and better:
                d.actions.append(Proposal("adjust", pos.inst, stop_loss=round(trail, 8), reason="trailing the stop 1.2 ATR behind price"))
        if len(held) < 2:
            best, best_score = None, 0.0
            held_coins = {p.base for p in held.values()}                   # one position per coin
            for s in ctx.snaps.values():
                if s.inst in held or s.inst.split("-")[0] in held_coins or not s.atr1h_pct or not s.rsi1h:
                    continue
                if s.trend == "up" and 52 < s.rsi1h < 68 and s.ret1h > 0.15:
                    score, side = s.ret4h, "long"
                elif s.trend == "down" and 32 < s.rsi1h < 48 and s.ret1h < -0.15 and s.kind != "spot":
                    score, side = -s.ret4h, "short"                           # spot cannot be sold short
                else:
                    continue
                if score > best_score:
                    best, best_score, best_side = s, score, side
            if best:
                atr = best.atr1h_pct / 100.0 * best.last
                sign = 1 if best_side == "long" else -1
                d.actions.append(Proposal(
                    "open", best.inst, side=best_side, margin_pct=15, leverage=3,
                    stop_loss=best.last - sign * 1.8 * atr, take_profit=best.last + sign * 3.0 * atr,
                    confidence=0.5, reason=f"{best.trend}trend, 1h RSI {best.rsi1h:.0f}, 4h move {best.ret4h:+.2f}%"))
        d.view = (f"Rule baseline: {len(ctx.snaps)} markets scanned, "
                  f"{sum(1 for a in d.actions if a.type == 'open')} entry signal(s), {len(held)} open position(s).")
        if not d.actions:
            d.actions.append(Proposal("hold", reason="no rule fired"))
        return d
