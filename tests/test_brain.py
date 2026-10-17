import time

import pytest

from aeon.brain import (Context, HeuristicBrain, LlmBrain, ParseError, build_system_prompt, extract_json,
                        parse_decision, render_context)
from aeon.broker import Account, Position
from aeon.config import LlmCfg, RiskCfg
from aeon.market import InstSnap


def snap(inst="SOL-USDT-SWAP", last=100.0, trend="up", rsi1h=58.0, ret1h=0.4, ret4h=1.2):
    return InstSnap(inst=inst, last=last, chg24h=2.0, vol_usd=5e8, funding=0.0001, ret15m=0.1, ret1h=ret1h,
                    ret4h=ret4h, rsi15=55.0, rsi1h=rsi1h, ema20=99.0, ema50=97.0, trend=trend, atr1h_pct=0.8,
                    closes_1h=[99.0 + i * 0.05 for i in range(24)], min_notional=1.0, max_lever=50.0)


def ctx(positions=None, snaps=None):
    acct = Account(equity=200.0, available=180.0, balance=200.0, positions=positions or {})
    status = dict(killed=False, kill_reason="", day_blocked=False, drawdown=1.5, dd_limit=25, day_pnl=0.4,
                  day_limit=8, exposure=0.0, exposure_limit=3, positions=0, positions_limit=3,
                  trades_today=1, trades_limit=24, peak=201.0)
    return Context(now=time.time(), mode="paper", cycle_no=3, account=acct, status=status,
                   snaps=snaps if snaps is not None else {"SOL-USDT-SWAP": snap()}, recent_actions=[],
                   recent_trades=[], notes="watching SOL 104 breakout", excluded=[], risk=RiskCfg())


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('<think>hmm {not json}</think>\n{"a": 2}') == {"a": 2}
    assert extract_json('Sure! Here is my decision:\n{"a": 3}\nGood luck') == {"a": 3}
    with pytest.raises(ParseError):
        extract_json("I think we should hold.")
    with pytest.raises(ParseError):
        extract_json("[1, 2, 3]")


def test_parse_decision_normalises_sloppy_output():
    d = parse_decision({
        "market_view": "chop", "notes": "n", "next_check_minutes": "20",
        "actions": [
            {"type": "BUY", "symbol": "sol", "side": "buy", "margin_pct": "15%", "leverage": "3x",
             "sl": "$98.5", "tp": 106, "confidence": 0.7, "reason": "breakout"},
            {"action": "trail", "inst": "ETH-USDT", "stop_loss": 2400},
            {"type": "short", "inst": "BTC", "stop_loss": 82000},
            {"type": "teleport", "inst": "X"},
            "garbage",
        ]})
    t = [a.type for a in d.actions]
    assert t == ["open", "adjust", "open"]
    o = d.actions[0]
    assert (o.inst, o.side, o.margin_pct, o.leverage, o.stop_loss, o.take_profit) == \
           ("SOL-USDT-SWAP", "long", 15.0, 3.0, 98.5, 106.0)
    assert d.actions[1].inst == "ETH-USDT"                 # a full OKX id is kept as given: this one is SPOT
    assert d.actions[2].side == "short" and d.actions[2].inst == "BTC-USDT-SWAP"     # a bare coin means its perpetual
    assert d.next_check_min == 20 and len(d.warnings) == 2


def test_instrument_ids_survive_normalisation():
    d = parse_decision({"actions": [
        {"type": "open", "inst": "btc-usd_um-261030", "side": "long", "stop_loss": 1},   # dated future (underscore!)
        {"type": "close", "inst": "SOL/USDT"},                                            # slash form -> spot id
        {"type": "close", "inst": "doge"}]})
    assert [a.inst for a in d.actions] == ["BTC-USD_UM-261030", "SOL-USDT", "DOGE-USDT-SWAP"]


def test_prompt_shows_all_three_market_kinds():
    sys_prompt = build_system_prompt(RiskCfg())
    for needle in ("perp:", "fut-*:", "spot:", "LONG ONLY", "EXPIRES", "one position per coin"):
        assert needle in sys_prompt, needle
    fut = snap("BTC-USD_UM-261030", 81800.0)
    fut.kind, fut.tag, fut.days_left, fut.basis, fut.ann_basis, fut.funding = "futures", "fut-mon", 13.4, 0.246, 6.7, None
    spot = snap("SOL-USDT", 104.9)
    spot.kind, spot.tag, spot.funding = "spot", "spot", None
    text = render_context(ctx(None, {"BTC-USD_UM-261030": fut, "SOL-USDT": spot, "SOL-USDT-SWAP": snap()}))
    assert "fut-mon" in text and "+0.25" in text and "13.4" in text         # type, basis and days left are shown
    assert text.index("SOL-USDT-SWAP") < text.index("BTC-USD_UM-261030") < text.index("SOL-USDT ")   # perps, futures, spot order
    held = Position("SOL-USDT", "long", 1.5, 1.0, 100.0, 1.0, mark=102.0, sl=96.0, kind="spot")
    pos_text = render_context(ctx({"SOL-USDT": held}))
    assert "SOL-USDT LONG spot | 1.5 coins" in pos_text and "of cost" in pos_text


async def test_heuristic_never_shorts_spot_or_doubles_a_coin():
    s = snap("SOL-USDT", 100.0, trend="down", rsi1h=40.0, ret1h=-0.4, ret4h=-1.5)
    s.kind, s.tag = "spot", "spot"
    d = await HeuristicBrain().decide(ctx(None, {"SOL-USDT": s}))
    assert not any(a.type == "open" for a in d.actions)                  # a falling spot market is not shortable
    perp_held = Position("SOL-USDT-SWAP", "long", 1.0, 1.0, 100.0, 3.0, mark=101.0, sl=97.0)
    up = snap("SOL-USDT", 100.0)
    up.kind, up.tag = "spot", "spot"
    d2 = await HeuristicBrain().decide(ctx({"SOL-USDT-SWAP": perp_held}, {"SOL-USDT": up, "SOL-USDT-SWAP": snap()}))
    assert not any(a.type == "open" and a.inst == "SOL-USDT" for a in d2.actions)   # SOL is already held as a perp


def test_prompts_render():
    sys_prompt = build_system_prompt(RiskCfg(max_leverage=4, risk_per_trade_pct=1.5))
    assert "max leverage 4x" in sys_prompt and "1.5%" in sys_prompt and '"type": "open"' in sys_prompt
    pos = Position("SOL-USDT-SWAP", "long", 2.0, 1.0, 100.0, 3.0, mark=102.0, sl=98.0, tp=108.0)
    text = render_context(ctx({"SOL-USDT-SWAP": pos}))
    for needle in ("ACCOUNT equity 200.00", "OPEN POSITIONS", "SOL-USDT-SWAP LONG 3x", "SL 98.00", "MARKET",
                   "LAST 24 HOURLY CLOSES", "YOUR NOTES FROM LAST CYCLE", "watching SOL 104 breakout"):
        assert needle in text, needle


class StubClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    async def chat(self, messages):
        self.calls += 1
        return self.replies.pop(0), 1000, 200


async def test_llm_brain_repairs_one_bad_reply_and_meters_cost():
    cfg = LlmCfg(input_price_per_m=3.0, output_price_per_m=15.0)
    stub = StubClient(["no json here", '{"market_view": "ok", "actions": [{"type": "hold", "reason": "quiet"}]}'])
    d = await LlmBrain(cfg, RiskCfg(), "k", client=stub).decide(ctx())
    assert stub.calls == 2 and d.actions[0].type == "hold"
    assert (d.tokens_in, d.tokens_out) == (2000, 400)
    assert d.cost == pytest.approx((2000 * 3 + 400 * 15) / 1e6)


async def test_llm_brain_gives_up_after_two_bad_replies():
    stub = StubClient(["nope", "still nope"])
    with pytest.raises(ParseError):
        await LlmBrain(LlmCfg(), RiskCfg(), "k", client=stub).decide(ctx())


async def test_heuristic_brain_enters_with_a_stop_and_trails():
    d = await HeuristicBrain().decide(ctx())
    o = next(a for a in d.actions if a.type == "open")
    assert o.side == "long" and o.stop_loss < 100.0 < o.take_profit
    pos = Position("SOL-USDT-SWAP", "long", 2.0, 1.0, 100.0, 3.0, mark=102.0, sl=98.0)
    d2 = await HeuristicBrain().decide(ctx({"SOL-USDT-SWAP": pos}, {"SOL-USDT-SWAP": snap(last=102.0)}))
    adj = next(a for a in d2.actions if a.type == "adjust")
    assert adj.stop_loss > 98.0
