"""Exchange smoke test for OKX *demo* trading:  python -m aeon --smoke-demo

AEON's order, stop-loss and close logic has been checked against a fake exchange. This runs the same code against
the real OKX demo environment with the smallest possible orders, so any mismatch in a request shape shows up in
minutes, with money that is not real, instead of during the first live trade. Run it once on the machine that will
run AEON, with the daemon stopped.

It walks one perpetual and one spot coin through: buy -> stop (and target) visible on the exchange -> stop moved ->
close -> flat with nothing left behind. It refuses to run unless run.mode is "demo", and it will not start while
another AEON holds the journal (the daemon would otherwise adopt the test position as its own).
"""
from __future__ import annotations

import asyncio

from .config import Config
from .journal import JournalLocked, take_lock
from .market import Market, Spec
from .okx import OkxClient, OkxError, fmt, round_step
from .okx_broker import OkxBroker

PERP, SPOT = "BTC-USDT-SWAP", "BTC-USDT"


class Steps:
    def __init__(self, say):
        self.say, self.good = say, True

    def check(self, ok: bool, text: str, detail: str = "") -> bool:
        self.good &= bool(ok)
        self.say(f"  [{'ok' if ok else '!!'}] {text}" + (f"  ({detail})" if detail and not ok else ""))
        return bool(ok)


async def _eventually(check, tries: int = 6, delay: float = 1.0):
    """The exchange needs a moment to show what it just did: ask a few times before calling it missing."""
    for i in range(tries):
        got = await check()
        if got:
            return got
        if i < tries - 1:
            await asyncio.sleep(delay)
    return None


async def _last_price(okx: OkxClient, spec: Spec) -> float:
    for row in await okx.tickers(spec.inst_type):
        if row.get("instId") == spec.inst:
            return float(row["last"])
    raise OkxError("price", f"no ticker for {spec.inst}")


async def _stop_on_book(broker: OkxBroker, spec: Spec, sl: float | None = None):
    algos = (await broker._algos()).get(spec.inst)
    if not algos or not algos["sl"]:
        return None
    if sl is not None and abs(algos["sl"] - sl) > spec.tick_sz / 2:
        return None
    return algos


async def _flat(broker: OkxBroker, spec: Spec) -> bool:
    await broker._safe_refresh_balances()
    if spec.kind == "spot":
        held = spec.inst in broker._spot
    else:
        held = spec.inst in await broker._fetch_positions()
    return not held and spec.inst not in await broker._algos()


async def _cleanup(broker: OkxBroker, spec: Spec, say) -> None:
    """After a failed leg: leave the demo account as we found it."""
    try:
        open_now = spec.inst in broker._spot if spec.kind == "spot" else spec.inst in await broker._fetch_positions()
        if open_now:
            await broker.close(spec, 0.0, "smoke test cleanup")
        await broker._cancel_algos(spec.inst, only_ours=spec.kind == "spot")
        say(f"  [..] cleaned up {spec.inst}")
    except Exception as e:
        say(f"  [!!] cleanup of {spec.inst} failed ({type(e).__name__}: {e}): check the OKX demo account by hand")


async def _leg(broker: OkxBroker, okx: OkxClient, spec: Spec, say) -> bool:
    steps = Steps(say)
    spot = spec.kind == "spot"
    say(f"{'spot' if spot else 'perpetual'} leg: {spec.inst}")
    try:
        last = await _last_price(okx, spec)
        gap = 0.05 if spot else 0.03
        sl, tp = round_step(last * (1 - gap), spec.tick_sz), round_step(last * (1 + gap), spec.tick_sz)
        size = spec.min_sz * (10 if spot else 1)         # spot: the buy fee comes out of the coin, so keep the sale clear of the minimum
        fill = await broker.open(spec, "long", size, 1.0 if spot else 2.0, sl, tp, last, "smoke test")
        steps.check(True, f"market buy of {fmt(size)} {'coins' if spot else 'contract(s)'} filled at {fill.price:g} (fee {fill.fee:.5f} USDT)")
        algos = await _eventually(lambda: _stop_on_book(broker, spec))
        steps.check(bool(algos and algos["tp"]), "stop-loss and take-profit are on the exchange", f"found {algos}")
        new_sl = round_step(last * (1 - gap / 2), spec.tick_sz)
        await broker.set_protection(spec, "long", new_sl, tp)
        algos = await _eventually(lambda: _stop_on_book(broker, spec, new_sl))
        steps.check(bool(algos) and len(algos["ids"]) == 1, "stop moved to the new level with exactly one protective order left",
                    f"found {algos}")
        fill = await broker.close(spec, last, "smoke test")
        steps.check(True, f"closed at {fill.price:g}, result {fill.pnl:+.5f} USDT{' (an estimate: OKX history lagged)' if fill.est else ''}")
        steps.check(bool(await _eventually(lambda: _flat(broker, spec))), "flat again, no protective order left behind")
    except Exception as e:
        steps.check(False, f"{type(e).__name__}: {e}")
        await _cleanup(broker, spec, say)
    return steps.good


async def smoke(okx: OkxClient, specs: dict[str, Spec], say=print, kinds: set[str] | None = None) -> bool:
    broker = OkxBroker(okx, specs, label="demo", kinds=kinds)
    await broker.init()
    for msg in broker.restrictions:
        say(f"  [..] {msg}")
    good, ran = True, 0
    for inst in (PERP, SPOT):
        spec = specs.get(inst)
        if spec is None or spec.kind not in broker.allowed_kinds:
            say(f"skipping {inst}: not available in this account / config")
            continue
        ran += 1
        good &= await _leg(broker, okx, spec, say)
    if not ran:
        say("  [!!] nothing could be tested: no BTC perpetual or spot pair is available")
    return good and ran > 0


async def run_smoke_demo(cfg: Config, say=print) -> int:
    if cfg.run.mode != "demo":
        raise SystemExit("--smoke-demo places real orders on the exchange, so it only runs in demo mode "
                         f"(run.mode is {cfg.run.mode!r}). Use mode = \"demo\" with OKX demo-trading API keys.")
    key, secret, phrase = cfg.okx_credentials()
    if not (key and secret and phrase):
        raise SystemExit("--smoke-demo needs OKX_API_KEY, OKX_API_SECRET and OKX_PASSPHRASE (OKX demo-trading keys) in .env")
    try:
        lock = take_lock(cfg.run.db_path or f"aeon-{cfg.run.mode}.db")
    except JournalLocked as e:
        raise SystemExit(f"cannot run the smoke test: {e}")
    pub, trade = OkxClient(), OkxClient(key, secret, phrase, demo=True)
    try:
        say("AEON smoke test | OKX DEMO trading | smallest possible orders, then everything is closed again")
        market = Market(pub, cfg.risk, cfg.universe)
        await market.load_specs()
        good = await smoke(trade, market.specs, say, kinds=set(cfg.universe.markets))
        say("SMOKE TEST PASSED" if good else "SMOKE TEST FAILED: fix the items marked !! before relying on the demo run")
        return 0 if good else 1
    except OkxError as e:
        say(f"  [!!] OKX: {e}")
        return 1
    finally:
        await pub.aclose()
        await trade.aclose()
        lock.close()
